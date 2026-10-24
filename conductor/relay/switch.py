"""A Switch agent-protocol client: SSE down, HTTP up.

This speaks the wire contract at
https://docs.switchagents.ai/switch-rooms/internals/agent-protocol directly,
with nothing in between. Switch Console speaks it on behalf of the Claude Code
and Codex sessions it starts; Relay's conductor speaks it for itself, which is
what lets it be an `always_on` teammate rather than a session someone has to
keep alive.

Three things the protocol insists on, and the reasons they are coded the way
they are:

* The *client* owns the connection id. A connection outlives its socket, so the
  id is generated once and replayed on every reopen. That is what makes a
  dropped stream a resume rather than a new connection.
* The heartbeat is the only authority on liveness, and it is refused when no
  stream is attached. So the beat task is owned by the stream: started after
  `connection_state` arrives, cancelled when the socket goes.
* A 404 or 409 on the beat means *reopen*, not retry. Retrying keeps a dead
  connection dead.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

log = logging.getLogger("relay.switch")

PROTOCOL_REVISION = 6
CLIENT_NAME = "relay-conductor"

# Frames that carry no SSE id, are never buffered and are never filtered.
CONTROL_FRAMES = frozenset(
    {"connection_state", "gap", "evicted", "subscription_changed", "room_released"}
)


class ReopenStream(Exception):
    """Tear this socket down and reopen it.

    Carries no backoff. The connection is still alive server-side for its TTL,
    so reopening immediately is correct, not a retry storm.
    """


class SwitchError(RuntimeError):
    def __init__(self, status: int, body: str, where: str) -> None:
        super().__init__(f"{where} failed: HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


@dataclass(frozen=True)
class Event:
    """A domain event: `message`, `command`, `room_join` or a task event.

    `sequence` appears both on the SSE `id:` line and on the data object, and
    they are the same number. The event names no session -- which room it
    belongs to is `room_id`, and routing is the client's business.
    """

    type: str
    room_id: str
    payload: dict[str, Any]
    sequence: int
    bridge_id: str | None = None
    channel_type: str | None = None

    @property
    def body(self) -> str:
        return str(self.payload.get("body") or "")

    @property
    def addressed(self) -> bool:
        return bool(self.payload.get("addressed"))

    @property
    def sender(self) -> str:
        return str(self.payload.get("sender") or "")

    @property
    def sender_name(self) -> str:
        return str(self.payload.get("sender_name") or self.payload.get("sender") or "")

    @property
    def thread_id(self) -> str | None:
        value = self.payload.get("thread_id")
        return str(value) if value else None

    @property
    def message_id(self) -> str | None:
        value = self.payload.get("message_id")
        return str(value) if value else None

    @property
    def is_from_platform(self) -> bool:
        """True when the Switch app posted this, not a participant.

        `sender_kind == "platform"` marks output Switch speaks in its own
        voice. Acting on it is how a conductor talks itself into a loop.
        """
        return self.payload.get("sender_kind") == "platform"


@dataclass(frozen=True)
class Control:
    type: str
    data: dict[str, Any]


@dataclass
class ConnectionState:
    """The first frame on every stream. Read it; do not assume it.

    The heartbeat interval and the room set are the server's answer, not an
    echo of what was asked for.
    """

    connection_id: str
    agent_id: str
    rooms: list[str] = field(default_factory=list)
    cursor: int = 0
    heartbeat_interval_seconds: float = 2.0
    protocol: Any = None
    #: Which incarnation of the connection id this stream is -- the fence.
    #:
    #: A connection id alone says nothing about *which* client is on it. A
    #: client that has already been displaced still holds the id and the API
    #: key, and is otherwise indistinguishable from the one that replaced it.
    #: So from protocol 6 the server stamps each stream with a generation and
    #: refuses any heartbeat, room claim or placement that does not carry it.
    #:
    #: Never derive or remember it: take it from `connection_state` every time,
    #: because a reopen is a new incarnation of the same id.
    generation: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Identity:
    agent_id: str
    api_key: str


# -- Registration -------------------------------------------------------------


async def register_always_on(
    base_url: str,
    registration_token: str,
    name: str,
    description: str,
    *,
    display_name: str | None = None,
    overwrite: bool = False,
) -> Identity:
    """Register a custom always-on agent and return its credentials.

    `register-known` is for the providers Switch ships a profile for (Claude
    Code, Codex, OpenCode). A client written from scratch uses the lower-level
    `POST /agents`, which takes a full integration profile, so it can declare
    `always_on`: reachable at all, independent of any room.

    The API key comes back **once**. The caller persists it or loses it.

    The registration token authenticates this call and nothing else -- it will
    not open a stream, and an API key will not register an agent. The token
    identifies a Gateway user, and that user becomes the agent's owner, which
    is where the agent's permissions come from.
    """
    payload = {
        "name": name,
        "description": description,
        "display_name": display_name or name,
        "connector_type": CLIENT_NAME,
        "integration_profile": {
            "connection_model": "always_on",
            "message_exchange": True,
            "pre_invocation_mediation": [],
            "post_invocation_mediation": [],
            "event_reporting": [],
        },
        "tools": [],
        "models": [],
        "metadata": {"client": CLIENT_NAME, "project": "relay"},
        "overwrite": overwrite,
    }
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=30) as http:
        res = await http.post(
            "/agents",
            json=payload,
            headers={"Authorization": f"Bearer {registration_token}"},
        )
    if res.status_code == 409:
        raise SwitchError(
            409,
            f"an agent named {name!r} already exists; "
            "set RELAY_REGISTER_OVERWRITE=true to replace it",
            "register",
        )
    if res.status_code >= 400:
        raise SwitchError(res.status_code, res.text, "register")
    body = res.json()
    return Identity(agent_id=body["id"], api_key=body["api_key"])


async def probe(base_url: str) -> bool:
    """`GET /health` is public and is the correct probe for a bridge base URL.

    A healthy response means the right base URL. A 401 means the right host
    and the wrong path -- operators reach the Gateway, agents reach the bridge,
    and a client configured with one cannot use the other.
    """
    try:
        async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=10) as http:
            res = await http.get("/health")
        return res.status_code < 400
    except httpx.HTTPError:
        return False


# -- The client ---------------------------------------------------------------


class SwitchClient:
    """One agent, one connection, one event stream.

    The connection id is generated once per client and reused across reopens,
    which is what makes the connection survive a dropped socket. Two processes
    sharing one id would evict each other on every reconnect, so one client is
    one process's view of one agent.
    """

    def __init__(
        self,
        base_url: str,
        identity: Identity,
        *,
        scope: str = "all",
        event_filter: str = "all",
        connection_id: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.identity = identity
        self.scope = scope
        self.event_filter = event_filter
        self.connection_id = connection_id or str(uuid.uuid4())
        self.state: ConnectionState | None = None

        self._cursor = 0
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(30.0, read=None),
            headers={"Authorization": f"Bearer {identity.api_key}"},
        )
        self._ops: dict[str, dict[str, Any]] = {}
        self._claimed_room: str | None = None
        self._claim_lock = asyncio.Lock()
        self._closed = False

    @property
    def _agent(self) -> str:
        return f"/agents/{self.identity.agent_id}"

    @property
    def operations(self) -> dict[str, dict[str, Any]]:
        return self._ops

    async def aclose(self) -> None:
        self._closed = True
        await self._http.aclose()

    # -- Operations -----------------------------------------------------------

    async def load_operations(self) -> dict[str, dict[str, Any]]:
        """Read the operations registry rather than hard-coding a list.

        The registry the server's decorators write into is the single
        definition of the operation surface, so what it returns at startup is
        the truth about what this server can do.
        """
        res = await self._http.get(f"{self._agent}/ops")
        if res.status_code >= 400:
            raise SwitchError(res.status_code, res.text, "GET /ops")
        self._ops = res.json().get("operations", {})
        return self._ops

    async def op(self, name: str, /, **args: Any) -> Any:
        """Call an operation.

        The arguments object goes in the body unwrapped, and the connection id
        header is what the server resolves the caller's current room from.

        Arguments are validated by signature inspection, not against the
        published schema, so a payload that satisfies the schema can still be
        refused -- which is why unset values are dropped rather than sent as
        null.
        """
        body = {k: v for k, v in args.items() if v is not None}
        res = await self._http.post(
            f"{self._agent}/ops/{name}",
            json=body,
            headers={"X-Switch-Connection-Id": self.connection_id},
        )
        if res.status_code == 409:
            self._claimed_room = None
            raise ReopenStream(f"op {name}: connection unknown or dead")
        if res.status_code >= 400:
            raise SwitchError(res.status_code, res.text, f"op {name}")
        return res.json().get("result")

    async def op_in_room(self, room_id: str, name: str, /, **args: Any) -> Any:
        """Call an operation that resolves its room from the connection.

        `create_room_document`, `define_role` and `send_targeted_message` take
        no room argument. An `all`-scope connection covers every room the
        agent belongs to but claims none, so the room has to be claimed first.

        `connect_to_room` always takes over. That is safe here because this
        process holds the agent's only connection, so the claim it takes is its
        own. The lock keeps two concurrent callers from interleaving a claim
        with each other's operation.
        """
        async with self._claim_lock:
            if self._claimed_room != room_id:
                await self.op(
                    "connect_to_room",
                    room_id=room_id,
                    include_general_instructions=False,
                )
                self._claimed_room = room_id
            return await self.op(name, **args)

    async def say(self, room_id: str, body: str, thread_id: str | None = None) -> None:
        """Post into any room, with no connection and no claim.

        `POST /agents/{id}/message` is the connectionless path: it names the
        room outright, so a conductor writing into many rooms need not claim
        each one first. Addressing still works the ordinary way -- an `@name`
        in the body reaches that participant, exactly as it would from a person
        typing it.
        """
        payload: dict[str, Any] = {"room_id": room_id, "content": body}
        if thread_id:
            payload["thread_id"] = thread_id
        res = await self._http.post(f"{self._agent}/message", json=payload)
        if res.status_code >= 400:
            raise SwitchError(res.status_code, res.text, "POST /message")

    # -- The stream -----------------------------------------------------------

    def _events_url(self, rooms: list[str] | None) -> str:
        params = [
            f"connection_id={self.connection_id}",
            f"scope={self.scope}",
            f"filter={self.event_filter}",
            "start_from=head" if not self._cursor else f"start_from={self._cursor}",
            f"protocol={PROTOCOL_REVISION}",
            f"protocol_accepts={PROTOCOL_REVISION}",
            f"client={CLIENT_NAME}",
            "client_version=1.0.0",
        ]
        # Rooms are declared on the open URL, never after. Catch-up runs
        # immediately on open, so a room subscribed a moment later arrives too
        # late: its buffered events are skipped as not-covered and the cursor
        # moves past them, losing exactly what a resume exists to recover.
        if rooms:
            params.append("rooms=" + ",".join(rooms))
        return f"{self._agent}/events?" + "&".join(params)

    async def run(
        self,
        on_event: Callable[[Event], Awaitable[None]],
        on_control: Callable[[Control], Awaitable[None]] | None = None,
        *,
        rooms: list[str] | None = None,
    ) -> None:
        """Hold the stream open, reopening as needed, until closed.

        Protocol errors reopen at once; transport errors back off. They are
        different failures and deserve different responses.
        """
        backoff = 1.0
        while not self._closed:
            try:
                await self._one_stream(on_event, on_control, rooms)
                backoff = 1.0
            except ReopenStream as exc:
                log.info("reopening stream: %s", exc)
                backoff = 1.0
            except (httpx.HTTPError, SwitchError) as exc:
                log.warning("stream error, retrying in %.0fs: %s", backoff, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _one_stream(
        self,
        on_event: Callable[[Event], Awaitable[None]],
        on_control: Callable[[Control], Awaitable[None]] | None,
        rooms: list[str] | None,
    ) -> None:
        headers = {"Accept": "text/event-stream"}
        # Last-Event-ID beats start_from when both are present, and is the
        # right way to say "resume from here" on a reopen.
        if self._cursor:
            headers["Last-Event-ID"] = str(self._cursor)

        beat: asyncio.Task[None] | None = None
        # A gap is not a wake. Hold it and attach it to the next event
        # surfaced, rather than interrupting on it.
        pending_gap: dict[str, Any] | None = None

        try:
            async with self._http.stream(
                "GET", self._events_url(rooms), headers=headers
            ) as res:
                if res.status_code >= 400:
                    await res.aread()
                    raise SwitchError(res.status_code, res.text, "GET /events")

                async for name, data, seq in _parse_sse(res.aiter_lines()):
                    if name == "connection_state":
                        self.state = ConnectionState(
                            connection_id=data.get("connection_id", self.connection_id),
                            agent_id=data.get("agent_id", self.identity.agent_id),
                            rooms=list(data.get("rooms") or []),
                            cursor=int(data.get("cursor") or 0),
                            heartbeat_interval_seconds=float(
                                data.get("heartbeat_interval_seconds") or 2.0
                            ),
                            protocol=data.get("protocol"),
                            generation=data.get("generation"),
                            raw=data,
                        )
                        self._cursor = max(self._cursor, self.state.cursor)
                        self._claimed_room = None
                        log.info(
                            "connected: %d room(s), cursor %d, beat every %.1fs",
                            len(self.state.rooms),
                            self._cursor,
                            self.state.heartbeat_interval_seconds,
                        )
                        # The beat is refused unless a stream is attached, so
                        # it starts here and not a moment earlier.
                        beat = asyncio.create_task(
                            self._heartbeat(
                                self.state.heartbeat_interval_seconds,
                                self.state.generation,
                            )
                        )
                        if on_control:
                            await on_control(Control(name, data))

                    elif name == "evicted":
                        raise ReopenStream(f"evicted: {data.get('reason')}")

                    elif name == "gap":
                        pending_gap = data
                        log.warning("gap from sequence %s", data.get("from_sequence"))

                    elif name in CONTROL_FRAMES:
                        if name == "room_released" and (
                            data.get("room_id") == self._claimed_room
                        ):
                            self._claimed_room = None
                        if name == "subscription_changed" and self.state is not None:
                            self.state.rooms = list(
                                data.get("rooms") or self.state.rooms
                            )
                        if on_control:
                            await on_control(Control(name, data))

                    else:
                        if seq is not None:
                            self._cursor = max(self._cursor, seq)
                        if pending_gap is not None and on_control:
                            await on_control(Control("gap", pending_gap))
                        pending_gap = None
                        await on_event(
                            Event(
                                type=name,
                                room_id=str(data.get("room_id") or ""),
                                payload=dict(data.get("payload") or {}),
                                sequence=int(data.get("sequence") or seq or 0),
                                bridge_id=data.get("bridge_id"),
                                channel_type=data.get("channel_type"),
                            )
                        )
        finally:
            if beat is not None:
                beat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await beat

        raise ReopenStream("stream closed by the server")

    async def _heartbeat(self, interval: float, generation: int | None) -> None:
        """Beat on the server's interval. 404 and 409 both mean reopen.

        When the beat lapses the connection is swept: the room claim is
        released, any role lease goes, and a still-open socket is torn down
        with an `evicted` frame. Failing to beat is not a degraded state, it is
        the end of the connection.

        The generation is this stream's incarnation, and it fences the tick. A
        client that declares protocol 6 and then beats without one is refused
        outright -- naming nothing is only honest from a client that never had
        a generation to name. It is passed in rather than read off `self.state`
        so that a beat belonging to a superseded stream cannot pick up the
        generation of the stream that replaced it.
        """
        while True:
            await asyncio.sleep(interval)
            payload: dict[str, Any] = {
                "connection_id": self.connection_id,
                "cursor": self._cursor,
            }
            if generation is not None:
                payload["generation"] = generation
            try:
                res = await self._http.post(
                    f"{self._agent}/connection/beat", json=payload
                )
            except httpx.HTTPError as exc:
                # One lost beat is survivable: the TTL is three intervals.
                log.debug("beat transport error: %s", exc)
                continue
            if res.status_code in (404, 409):
                # Retrying here would keep a dead connection dead.
                log.info("beat refused (%d) -- connection is gone", res.status_code)
                raise ReopenStream(f"beat returned {res.status_code}")
            if res.status_code >= 400:
                log.warning("beat failed: %d %s", res.status_code, res.text[:200])
                continue
            body = res.json()
            # The server clamps the cursor to the buffer head and ignores a
            # lower value: cursors only move forward.
            self._cursor = max(self._cursor, int(body.get("cursor") or 0))
            if self.state is not None:
                self.state.rooms = list(body.get("rooms") or self.state.rooms)


async def _parse_sse(
    lines: AsyncIterator[str],
) -> AsyncIterator[tuple[str, dict[str, Any], int | None]]:
    """Yield (event name, data object, sequence) per SSE frame.

    A line beginning with `:` is a keepalive comment, not a frame -- it is
    written when nothing has happened for 15 seconds, purely so an
    intermediate proxy does not drop an idle stream. There is no server-sent
    heartbeat event; the heartbeat runs client to server only.
    """
    name: str | None = None
    seq: int | None = None
    data: list[str] = []

    async for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if data:
                text = "\n".join(data)
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    log.warning("undecodable SSE data: %s", text[:200])
                else:
                    if isinstance(parsed, dict):
                        yield (
                            name or str(parsed.get("type") or "message"),
                            parsed,
                            seq,
                        )
            name, seq, data = None, None, []
            continue
        if line.startswith(":"):
            continue
        key, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if key == "event":
            name = value
        elif key == "data":
            data.append(value)
        elif key == "id":
            with contextlib.suppress(ValueError):
                seq = int(value)
