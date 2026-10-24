"""The conductor: the deterministic half of the desk.

Relay splits the work in two, and the split is the whole idea.

**Judgement belongs to the agents.** Reading a half-formed request, deciding
whether it is a bug or a question, reproducing it, writing the fix -- that is
what Claude Code and Codex are for, and Switch Console already runs them.

**Bookkeeping belongs to code.** Which cases are open, who owns each one, how
long it has been sitting there, who needs paging because a promise is about to
break. An LLM asked to do this will do it well most of the time, and a desk
that loses a case one time in twenty is a desk nobody trusts. So the conductor
has no model behind it: it is a state machine, a clock and a set of Switch
operations.

What makes it a *teammate* rather than a cron job is that it holds its own
`always_on` connection to the agent bridge and is addressed in the room like
anyone else. The room is the interface, and the room is the audit log.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from . import commands
from .cases import (
    PAUSED,
    PIPELINE,
    TERMINAL,
    Case,
    State,
    Transition,
    now_ms,
)
from .config import Settings
from .store import Store
from .switch import Control, Event, SwitchClient

log = logging.getLogger("relay.conductor")

CASE_PREFIX = "RLY"


class Conductor:
    def __init__(
        self,
        settings: Settings,
        client: SwitchClient,
        store: Store,
        case_template: str,
    ) -> None:
        self.settings = settings
        self.client = client
        self.store = store
        self.case_template = case_template

        self.cases: dict[str, Case] = {}
        self.desk_room_id: str = ""
        self.desk_room_name: str = settings.desk_room_name
        self.started_at = now_ms()

        #: Cases already nudged for a given reason, so a breach pages once and
        #: not every tick. Cleared when the case moves.
        self._nudged: set[tuple[str, str]] = set()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._duty_defined = False

    # -- Startup --------------------------------------------------------------

    async def start(self) -> None:
        """Everything that can be done before a connection exists.

        Which is less than it looks. The operations registry is a plain GET and
        needs no connection, but every *call* resolves its caller from the
        connection id header -- so an operation invoked before the stream is
        open is answered with a 409, not a result. Room work therefore waits
        for `connection_state`; see `_on_connected`.
        """
        self.cases = {c.id: c for c in await self.store.load_all()}
        log.info("loaded %d case(s)", len(self.cases))

        await self.client.load_operations()
        log.info("%d operation(s) on this server", len(self.client.operations))

    async def _on_connected(self) -> None:
        """Startup work that needs a live connection, run once per stream.

        Re-running `_resolve_desk` on every reconnect is deliberate: Relay may
        have been added to the desk room while it was away, and the room set
        arrives with the new connection rather than being remembered.
        """
        try:
            await self._resolve_desk()
            if self.desk_room_id and not self._duty_defined:
                await self._ensure_duty_role()
                self._duty_defined = True
        except Exception:  # noqa: BLE001 - never kill the stream over startup
            log.exception("startup work failed; the stream stays up")

    async def _resolve_desk(self) -> None:
        """Find the desk room by name, so no id has to be configured.

        A room id is a UUID nobody can type and that changes the moment the
        room is recreated. The name is what people already use.
        """
        rooms = await self.client.op("list_rooms") or []
        wanted = self.settings.desk_room_name.lower()
        for room in rooms:
            if str(room.get("name", "")).lower() == wanted:
                self.desk_room_id = str(room.get("id") or room.get("room_id") or "")
                self.desk_room_name = str(room.get("name"))
                log.info("desk room %r -> %s", self.desk_room_name, self.desk_room_id)
                return
        log.warning(
            "no room named %r among %d room(s) Relay belongs to; "
            "create it from templates/relay-desk.template.yaml and restart",
            self.settings.desk_room_name,
            len(rooms),
        )

    async def _ensure_duty_role(self) -> None:
        """Define the duty role in the desk, idempotently.

        Escalating to a role rather than a name is what makes the workflow
        outlive the rota. Whoever assumes `duty` this week is who gets paged,
        and Relay never has to be told who that is.
        """
        try:
            existing = await self.client.op_in_room(self.desk_room_id, "list_roles") or []
            if any(str(r.get("name")) == self.settings.duty_role for r in existing):
                return
            await self.client.op_in_room(
                self.desk_room_id,
                "define_role",
                name=self.settings.duty_role,
                instructions=(
                    "You are on duty at the Relay desk. You are the review gate: "
                    "cases reach you at `awaiting_review`, and nothing closes "
                    "without you. Reply `@relay done <id> <outcome>` to close one, "
                    "or `@relay changes <id> <why>` to send it back. Relay pages "
                    "this role when a case breaches its SLA or goes quiet."
                ),
                exclusive=True,
            )
            log.info("defined the %r role in the desk", self.settings.duty_role)
        except Exception as exc:  # noqa: BLE001 - best effort, never fatal
            log.warning("could not define the duty role: %s", exc)

    # -- Event handling -------------------------------------------------------

    async def on_control(self, frame: Control) -> None:
        if frame.type == "connection_state":
            # The connection now exists, so operations will resolve a caller.
            await self._on_connected()
        elif frame.type == "gap":
            # A gap means events were missed. The case store is the record, not
            # the stream, so there is nothing to replay -- but say so in the
            # room rather than letting it pass silently.
            log.warning("gap: re-reading the desk before acting further")
            if self.desk_room_id:
                await self._safe_say(
                    self.desk_room_id,
                    "I lost part of the event stream and may have missed "
                    "messages. `@relay status` for where every case stands.",
                )
        elif frame.type == "subscription_changed" and not self.desk_room_id:
            # Relay was added to a room while connected -- it may be the desk.
            await self._on_connected()

    async def on_event(self, event: Event) -> None:
        if event.type != "message":
            return
        # Never act on Switch's own voice, or on an echo of Relay's posts: both
        # are how a conductor talks itself into a loop.
        if event.is_from_platform:
            return
        if event.sender_name.lower() == self.settings.agent_name.lower():
            return
        if not event.addressed:
            return

        command = commands.parse(event.body)
        if command is None:
            return

        try:
            await self._dispatch(command, event)
        except ValueError as exc:
            # A refused transition is information, not a crash. Say it where it
            # was asked for.
            await self._safe_say(event.room_id, f"Can't do that: {exc}",
                                 event.thread_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("dispatching %s failed", command.verb)
            await self._safe_say(
                event.room_id, f"That failed on my side: {exc}", event.thread_id
            )

    async def _dispatch(self, cmd: commands.Command, event: Event) -> None:
        if cmd.verb == "help":
            await self._safe_say(event.room_id, commands.HELP, event.thread_id)
            return
        if cmd.verb == "board":
            await self._safe_say(
                event.room_id,
                f"The live board: http://localhost:{self.settings.api_port}/",
                event.thread_id,
            )
            return
        if cmd.verb == "__needs_id":
            await self._safe_say(
                event.room_id,
                f"`{cmd.text}` needs a case id, like `{CASE_PREFIX}-12`. "
                "`@relay status` lists what is open.",
                event.thread_id,
            )
            return
        if cmd.verb == "open":
            await self._open(cmd, event)
            return
        if cmd.verb == "status":
            await self._status(cmd, event)
            return

        case = self.cases.get(cmd.case_id)
        if case is None:
            await self._safe_say(
                event.room_id, f"I have no case {cmd.case_id}.", event.thread_id
            )
            return

        if cmd.verb in commands.EDIT_VERBS:
            await self._edit(cmd, case, event)
            return
        await self._move(cmd, case, event)

    # -- Verbs ----------------------------------------------------------------

    async def _open(self, cmd: commands.Command, event: Event) -> None:
        number = await self.store.next_case_number()
        case = Case(
            id=f"{CASE_PREFIX}-{number}",
            title=cmd.text[:200],
            requester=event.sender_name,
            desk_room_id=event.room_id,
            # Thread under the message that opened it, so every later update
            # lands next to the original request instead of at the bottom of
            # the channel.
            desk_thread_id=event.thread_id or event.message_id or "",
        )
        if cmd.priority:
            case.priority = cmd.priority
        self.cases[case.id] = case
        await self._persist(case)

        sla = case.sla
        await self._safe_say(
            event.room_id,
            f"**{case.id}** — {case.title}\n"
            f"`{case.state}` · {case.priority.upper()} · "
            f"acknowledge within {sla.ack_minutes}m, resolve within "
            f"{sla.resolve_minutes}m · opened by {case.requester}\n"
            f"`@relay accept {case.id}` to take it on, "
            f"`@relay info {case.id} <question>` if something is missing.",
            case.desk_thread_id or event.thread_id,
        )

    async def _edit(self, cmd: commands.Command, case: Case, event: Event) -> None:
        if cmd.verb == "claim":
            case.owner = cmd.text or event.sender_name
            case.log.append(
                _note(case, event.sender_name, f"owner set to {case.owner}")
            )
            await self._persist(case)
            await self._safe_say(
                event.room_id, f"**{case.id}** is {case.owner}'s.",
                case.desk_thread_id or event.thread_id,
            )
        elif cmd.verb == "priority":
            if not cmd.priority:
                await self._safe_say(
                    event.room_id, "Give me `p1`, `p2` or `p3`.", event.thread_id
                )
                return
            was = case.priority
            case.priority = cmd.priority
            case.log.append(
                _note(case, event.sender_name, f"priority {was} -> {case.priority}")
            )
            # A re-rank changes the promise, so old breach pages no longer apply.
            self._clear_nudges(case.id)
            await self._persist(case)
            await self._safe_say(
                event.room_id,
                f"**{case.id}** is now {case.priority.upper()} — resolve within "
                f"{case.sla.resolve_minutes}m.",
                case.desk_thread_id or event.thread_id,
            )
        elif cmd.verb == "note":
            if not cmd.text:
                return
            case.log.append(_note(case, event.sender_name, cmd.text))
            case.updated_at = now_ms()
            self._clear_nudges(case.id)
            await self._persist(case)

    async def _move(self, cmd: commands.Command, case: Case, event: Event) -> None:
        target = commands.MOVES[cmd.verb]
        if cmd.priority:
            case.priority = cmd.priority

        was = case.state
        case.move(target, actor=event.sender_name, note=cmd.text)
        if cmd.verb == "done":
            case.outcome = cmd.text
        # Whoever starts work owns it, unless someone already claimed it.
        if cmd.verb == "start" and not case.owner:
            case.owner = event.sender_name
        self._clear_nudges(case.id)
        await self._persist(case)

        log.info("%s %s -> %s by %s", case.id, was, case.state, event.sender_name)

        # Each move has one thing it should cause in Switch beyond the record.
        if target is State.ACCEPTED and not case.case_room_id:
            await self._open_case_room(case, event)
        elif target is State.AWAITING_REVIEW:
            await self._request_review(case, event)
        elif target in TERMINAL:
            await self._close_out(case, event)
        elif target is State.NEEDS_INFO:
            await self._safe_say(
                case.desk_room_id or event.room_id,
                f"**{case.id}** needs something from you"
                + (f", {case.requester}" if case.requester else "")
                + f": {cmd.text or 'see above'}\n"
                f"_The clock is paused until you reply, then "
                f"`@relay accept {case.id}`._",
                case.desk_thread_id or event.thread_id,
            )
        else:
            await self._safe_say(
                event.room_id,
                f"**{case.id}** → `{case.state}`"
                + (f" — {cmd.text}" if cmd.text else ""),
                case.desk_thread_id if event.room_id == case.desk_room_id else event.thread_id,
            )

    # -- Things a move causes -------------------------------------------------

    async def _open_case_room(self, case: Case, event: Event) -> None:
        """Give the case a room of its own, linked back to the desk.

        This is the part that would not survive Switch being taken away. The
        case room is where the worker agent and the reviewer do the work with
        the request already in front of them, and the link is what lets anyone
        in the desk follow it without being told where to look.
        """
        try:
            result = await self.client.op(
                "create_room_from_yaml",
                yaml=self.case_template,
                inputs={
                    "case_id": case.id,
                    "title": case.title or case.id,
                    "priority": str(case.priority),
                    "requester": case.requester or "unknown",
                    "owner": case.owner or "unassigned",
                    "resolve_minutes": str(case.sla.resolve_minutes),
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not create the room for %s: %s", case.id, exc)
            await self._safe_say(
                case.desk_room_id or event.room_id,
                f"**{case.id}** is accepted, but I could not open its room: "
                f"{exc}\nWork it here and I will keep the record.",
                case.desk_thread_id,
            )
            return

        case.case_room_id = str(result.get("room_id") or "")
        await self._persist(case)

        # The people go in after the room exists. Done inside the template they
        # would be a provisioning dependency: one name that does not resolve to
        # an account on the chat app and there is no room at all. Here the cost
        # of a bad name is a warning.
        wanted = [n for n in {case.requester, case.owner} if n]
        if case.case_room_id and wanted:
            try:
                added = await self.client.op(
                    "add_users_to_room",
                    room_id=case.case_room_id,
                    user_names=wanted,
                )
                missing = (added or {}).get("unresolved") or []
                if missing:
                    log.info(
                        "%s: no chat account for %s; they are not in the case room",
                        case.id,
                        ", ".join(missing),
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("could not add %s to %s: %s", wanted, case.id, exc)

        if case.case_room_id and case.desk_room_id:
            try:
                await self.client.op(
                    "link_rooms",
                    source_room_id=case.desk_room_id,
                    target_room_id=case.case_room_id,
                    label=f"case {case.id}",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("could not link %s to the desk: %s", case.id, exc)

        await self._safe_say(
            case.desk_room_id or event.room_id,
            f"**{case.id}** accepted · {case.priority.upper()} · "
            f"resolve within {case.sla.resolve_minutes}m\n"
            f"Room **{result.get('room_name', case.id)}** is open and linked here. "
            f"I will page @{self.settings.duty_role} if it goes quiet.",
            case.desk_thread_id,
        )

    async def _request_review(self, case: Case, event: Event) -> None:
        """Hand the case to whoever holds the duty role.

        `send_targeted_message` with a role fans out to every live holder, so
        this reaches the person on duty without Relay knowing their name.
        """
        room = case.case_room_id or case.desk_room_id or event.room_id
        body = (
            f"{case.id} is ready for review — {case.title}\n"
            f"Opened {_ago(case.opened_at)} by {case.requester or 'someone'}, "
            f"worked by {case.owner or 'unassigned'}.\n"
            f"`@relay done {case.id} <outcome>` to close it, "
            f"`@relay changes {case.id} <why>` to send it back."
        )
        try:
            await self.client.op_in_room(
                room,
                "send_targeted_message",
                body=body,
                target_roles=[self.settings.duty_role],
            )
        except Exception as exc:  # noqa: BLE001
            # No live holder, or no such role: fall back to the room at large
            # rather than leaving the case waiting on nobody.
            log.info("role handoff fell back to a broadcast: %s", exc)
            await self._safe_say(room, body)

    async def _close_out(self, case: Case, event: Event) -> None:
        """Write the trail into the case room, then answer the requester.

        The document is the reason the desk is worth running: the next person
        to hit this finds what was decided, in the room that decided it,
        instead of reconstructing it from scrollback.
        """
        if case.case_room_id:
            try:
                await self.client.op_in_room(
                    case.case_room_id,
                    "create_room_document",
                    name=f"{case.id} record",
                    description=f"What happened on {case.id}: {case.title}"[:200],
                    instructions=(
                        "The closed record for this case. Read it before "
                        "reopening the question or filing anything similar."
                    ),
                    content=_record(case),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("could not write the record for %s: %s", case.id, exc)

        verdict = "resolved" if case.state is State.DONE else "closed without action"
        await self._safe_say(
            case.desk_room_id or event.room_id,
            f"**{case.id}** {verdict} by {case.log[-1].actor if case.log else 'someone'}"
            + (f" — {case.outcome}" if case.outcome else "")
            + f"\n_{_duration(case.elapsed_ms())} from open to close"
            + (", within SLA._" if not case.resolve_overdue_by_ms() else
               f", {_duration(case.resolve_overdue_by_ms())} over._"),
            case.desk_thread_id,
        )

    async def _status(self, cmd: commands.Command, event: Event) -> None:
        if cmd.case_id:
            case = self.cases.get(cmd.case_id)
            if case is None:
                await self._safe_say(
                    event.room_id, f"I have no case {cmd.case_id}.", event.thread_id
                )
                return
            await self._safe_say(event.room_id, _one_line_detail(case), event.thread_id)
            return

        open_cases = [c for c in self.cases.values() if c.state not in TERMINAL]
        if not open_cases:
            await self._safe_say(
                event.room_id,
                "Nothing open. `@relay open <what you need>` starts a case.",
                event.thread_id,
            )
            return

        lines = [f"**The desk** — {len(open_cases)} open"]
        for state in PIPELINE:
            if state in TERMINAL:
                continue
            group = [c for c in open_cases if c.state is state]
            if not group:
                continue
            lines.append(f"\n`{state}` ({len(group)})")
            for case in sorted(group, key=lambda c: (c.priority, -c.elapsed_ms())):
                flag = {"breached": " ⚠ over", "at_risk": " · close to SLA"}.get(
                    case.health(), ""
                )
                lines.append(
                    f"  **{case.id}** {case.title[:60]} · {case.priority.upper()} · "
                    f"{_duration(case.elapsed_ms())}"
                    + (f" · {case.owner}" if case.owner else "")
                    + flag
                )
        await self._safe_say(event.room_id, "\n".join(lines), event.thread_id)

    # -- The clock ------------------------------------------------------------

    async def tick_forever(self) -> None:
        """Watch the clocks. This is the half no human remembers to do.

        Everything here is a promise the desk made when it accepted a case:
        answer within this long, finish within that long, and do not go quiet
        in the middle. Each one pages once.
        """
        while True:
            await asyncio.sleep(self.settings.tick_seconds)
            try:
                await self._tick()
            except Exception:  # noqa: BLE001
                log.exception("tick failed")

    async def _tick(self) -> None:
        at = now_ms()
        stall_ms = self.settings.stall_minutes * 60_000

        for case in list(self.cases.values()):
            if case.state in TERMINAL or case.state in PAUSED:
                continue

            if case.ack_overdue_by_ms(at) and self._mark(case.id, "ack"):
                await self._page(
                    case,
                    f"**{case.id}** has been in triage {_duration(case.elapsed_ms(at))} "
                    f"and nobody has picked it up. {case.priority.upper()} promises an "
                    f"answer within {case.sla.ack_minutes}m.",
                )
                continue

            if case.resolve_overdue_by_ms(at) and self._mark(case.id, "resolve"):
                await self._page(
                    case,
                    f"**{case.id}** is past its resolve time by "
                    f"{_duration(case.resolve_overdue_by_ms(at))} "
                    f"(`{case.state}`, {case.owner or 'unassigned'}).",
                )
                continue

            if case.stalled_for_ms(at) >= stall_ms and self._mark(case.id, "stall"):
                await self._page(
                    case,
                    f"**{case.id}** has not moved in "
                    f"{_duration(case.stalled_for_ms(at))} "
                    f"(`{case.state}`, {case.owner or 'unassigned'}). "
                    f"`@relay note {case.id} <where it stands>` if it is still live.",
                )

        await self._broadcast()

    async def _page(self, case: Case, body: str) -> None:
        """Tell the duty role, in the room the case actually lives in."""
        room = case.case_room_id or case.desk_room_id or self.desk_room_id
        if not room:
            return
        try:
            await self.client.op_in_room(
                room,
                "send_targeted_message",
                body=body,
                target_roles=[self.settings.duty_role],
            )
        except Exception:  # noqa: BLE001
            await self._safe_say(room, body)

    def _mark(self, case_id: str, reason: str) -> bool:
        key = (case_id, reason)
        if key in self._nudged:
            return False
        self._nudged.add(key)
        return True

    def _clear_nudges(self, case_id: str) -> None:
        """A case that moved earns a fresh set of pages."""
        self._nudged = {k for k in self._nudged if k[0] != case_id}

    # -- Plumbing -------------------------------------------------------------

    async def _persist(self, case: Case) -> None:
        await self.store.save(case)
        await self._broadcast()

    async def _safe_say(
        self, room_id: str, body: str, thread_id: str | None = None
    ) -> None:
        if not room_id:
            return
        try:
            await self.client.say(room_id, body, thread_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not post to %s: %s", room_id, exc)

    def snapshot(self) -> dict[str, Any]:
        at = now_ms()
        cases = [c.as_dict(at) for c in self.cases.values()]
        open_cases = [c for c in cases if c["state"] not in {"done", "rejected"}]
        closed = [c for c in cases if c["state"] == "done"]
        durations = sorted(c["elapsed_ms"] for c in closed)
        return {
            "now": at,
            "desk": {
                "room_name": self.desk_room_name,
                "room_id": self.desk_room_id,
                "connected": self.client.state is not None,
                "rooms": len(self.client.state.rooms) if self.client.state else 0,
                "agent_name": self.settings.agent_name,
                "duty_role": self.settings.duty_role,
                "started_at": self.started_at,
            },
            "totals": {
                "open": len(open_cases),
                "breached": sum(1 for c in open_cases if c["health"] == "breached"),
                "at_risk": sum(1 for c in open_cases if c["health"] == "at_risk"),
                "closed": len(closed),
                "median_close_ms": durations[len(durations) // 2] if durations else 0,
            },
            "pipeline": [
                {
                    "state": str(state),
                    "cases": sorted(
                        (c for c in cases if c["state"] == str(state)),
                        key=lambda c: (c["priority"], -c["elapsed_ms"]),
                    ),
                }
                for state in PIPELINE
            ],
            "cases": cases,
        }

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=4)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    async def _broadcast(self) -> None:
        if not self._subscribers:
            return
        snapshot = self.snapshot()
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(snapshot)
            except asyncio.QueueFull:
                # A board that cannot keep up gets the next snapshot instead of
                # a backlog of stale ones.
                pass


# -- Formatting ---------------------------------------------------------------


def _note(case: Case, actor: str, text: str) -> Transition:
    """A log entry that records something without moving the case."""
    return Transition(at=now_ms(), frm=str(case.state), to=str(case.state),
                      actor=actor, note=text)


def _duration(ms: int) -> str:
    seconds = ms // 1000
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f}h".replace(".0h", "h")
    return f"{hours / 24:.1f}d".replace(".0d", "d")


def _ago(at: int) -> str:
    return f"{_duration(now_ms() - at)} ago"


def _one_line_detail(case: Case) -> str:
    lines = [
        f"**{case.id}** — {case.title}",
        f"`{case.state}` · {case.priority.upper()} · "
        f"{_duration(case.elapsed_ms())} open · "
        f"{case.owner or 'unassigned'} · opened by {case.requester or 'unknown'}",
    ]
    if case.health() == "breached":
        lines.append("⚠ Past its SLA.")
    elif case.health() == "at_risk":
        lines.append("Close to its SLA.")
    for t in case.log[-6:]:
        arrow = f"{t.frm} → {t.to}" if t.frm != t.to else "note"
        lines.append(f"  `{arrow}` {t.actor}{': ' + t.note if t.note else ''}")
    return "\n".join(lines)


def _record(case: Case) -> str:
    """The closed record, written into the case room as a document."""
    out = [
        f"# {case.id} — {case.title}",
        "",
        f"- **Outcome**: {case.outcome or '(none recorded)'}",
        f"- **Final state**: {case.state}",
        f"- **Priority**: {case.priority.upper()} "
        f"(resolve within {case.sla.resolve_minutes}m)",
        f"- **Requested by**: {case.requester or 'unknown'}",
        f"- **Worked by**: {case.owner or 'unassigned'}",
        f"- **Open for**: {_duration(case.elapsed_ms())}",
        f"- **Within SLA**: {'no' if case.resolve_overdue_by_ms() else 'yes'}",
        "",
        "## Trail",
        "",
    ]
    for t in case.log:
        arrow = f"{t.frm} → {t.to}" if t.frm != t.to else "note"
        out.append(f"- `{arrow}` by {t.actor}{': ' + t.note if t.note else ''}")
    out += [
        "",
        "---",
        "",
        "Recorded by Relay, the conductor on this desk. Every line above is a "
        "transition it was asked for in this room.",
    ]
    return "\n".join(out)


def load_case_template(path: Path) -> str:
    return path.read_text(encoding="utf-8")
