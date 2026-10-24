"""The case: what Relay tracks, and the only ways it may move.

A case is one unit of work that entered the desk. Everything the board shows
and everything the conductor nudges about is derived from a case and its
transition log -- there is no second source of truth.

The state machine is deliberately small and explicit. A workflow that people
have to trust is one they can predict, and the cost of an extra state is that
every nudge, every clock and every board column has to know about it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class State(StrEnum):
    TRIAGING = "triaging"
    NEEDS_INFO = "needs_info"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    AWAITING_REVIEW = "awaiting_review"
    DONE = "done"
    REJECTED = "rejected"


#: States a case can no longer move out of. Clocks stop here.
TERMINAL = frozenset({State.DONE, State.REJECTED})

#: States where the clock is not the owner's problem: the desk is waiting on
#: the person who asked, so holding it against the SLA would be dishonest.
PAUSED = frozenset({State.NEEDS_INFO})

#: The whole of the workflow. A transition absent from this table cannot
#: happen, whoever asks for it.
ALLOWED: dict[State, frozenset[State]] = {
    State.TRIAGING: frozenset({State.NEEDS_INFO, State.ACCEPTED, State.REJECTED}),
    State.NEEDS_INFO: frozenset({State.TRIAGING, State.ACCEPTED, State.REJECTED}),
    State.ACCEPTED: frozenset({State.IN_PROGRESS, State.REJECTED}),
    State.IN_PROGRESS: frozenset({State.AWAITING_REVIEW, State.NEEDS_INFO, State.REJECTED}),
    # Review is a gate, not a rubber stamp: it can send work back.
    State.AWAITING_REVIEW: frozenset({State.DONE, State.IN_PROGRESS, State.REJECTED}),
    State.DONE: frozenset(),
    State.REJECTED: frozenset(),
}

#: Board columns, in the order work flows through them.
PIPELINE: tuple[State, ...] = (
    State.TRIAGING,
    State.NEEDS_INFO,
    State.ACCEPTED,
    State.IN_PROGRESS,
    State.AWAITING_REVIEW,
    State.DONE,
)


class Priority(StrEnum):
    P1 = "p1"
    P2 = "p2"
    P3 = "p3"


@dataclass(frozen=True)
class Sla:
    """Two promises per priority, in minutes.

    `ack` is how long a case may sit in triage before someone has said
    something about it. `resolve` is how long the whole thing may take. They
    are tracked separately because a desk that answers fast and finishes slowly
    fails differently from one that does the reverse.
    """

    ack_minutes: int
    resolve_minutes: int


DEFAULT_SLA: dict[Priority, Sla] = {
    Priority.P1: Sla(ack_minutes=5, resolve_minutes=60),
    Priority.P2: Sla(ack_minutes=30, resolve_minutes=480),
    Priority.P3: Sla(ack_minutes=240, resolve_minutes=2880),
}


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Transition:
    """One move, and who moved it. The audit trail is a list of these."""

    at: int
    frm: str
    to: str
    actor: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"at": self.at, "from": self.frm, "to": self.to,
                "actor": self.actor, "note": self.note}


@dataclass
class Case:
    id: str
    title: str
    state: State = State.TRIAGING
    priority: Priority = Priority.P2
    requester: str = ""
    owner: str = ""
    desk_room_id: str = ""
    desk_thread_id: str = ""
    case_room_id: str = ""
    opened_at: int = field(default_factory=now_ms)
    updated_at: int = field(default_factory=now_ms)
    acked_at: int = 0
    closed_at: int = 0
    #: Milliseconds spent in a PAUSED state, excluded from every clock.
    paused_ms: int = 0
    outcome: str = ""
    log: list[Transition] = field(default_factory=list)

    # -- Clocks ---------------------------------------------------------------

    @property
    def sla(self) -> Sla:
        return DEFAULT_SLA[self.priority]

    def elapsed_ms(self, at: int | None = None) -> int:
        """Age with paused time removed, frozen once the case closes.

        A closed case has a fixed age. Letting it keep counting would make
        yesterday's breaches grow forever and the board useless.
        """
        end = self.closed_at if self.state in TERMINAL and self.closed_at else (at or now_ms())
        live_pause = 0
        if self.state in PAUSED:
            live_pause = max(0, (at or now_ms()) - self.updated_at)
        return max(0, end - self.opened_at - self.paused_ms - live_pause)

    def ack_overdue_by_ms(self, at: int | None = None) -> int:
        """How far past the acknowledgement promise, or 0.

        Only triage owes an ack. Once anyone has moved the case on, the
        question is no longer whether it was seen.
        """
        if self.acked_at or self.state is not State.TRIAGING:
            return 0
        return max(0, self.elapsed_ms(at) - self.sla.ack_minutes * 60_000)

    def resolve_overdue_by_ms(self, at: int | None = None) -> int:
        if self.state in TERMINAL:
            return 0
        return max(0, self.elapsed_ms(at) - self.sla.resolve_minutes * 60_000)

    def health(self, at: int | None = None) -> str:
        """`breached`, `at_risk` or `ok` -- what the board colours by.

        At-risk is 80% of the resolve budget: late enough to act on, early
        enough that acting still helps.
        """
        if self.state in TERMINAL:
            return "ok"
        if self.ack_overdue_by_ms(at) or self.resolve_overdue_by_ms(at):
            return "breached"
        budget = self.sla.resolve_minutes * 60_000
        if budget and self.elapsed_ms(at) >= budget * 0.8:
            return "at_risk"
        return "ok"

    def stalled_for_ms(self, at: int | None = None) -> int:
        """Time since the last transition, for cases that owe one.

        A paused or closed case is not stalled: nothing is expected of the
        desk, so silence is the correct state.
        """
        if self.state in TERMINAL or self.state in PAUSED:
            return 0
        return max(0, (at or now_ms()) - self.updated_at)

    # -- Movement -------------------------------------------------------------

    def can_move_to(self, target: State) -> bool:
        return target in ALLOWED[self.state]

    def move(self, target: State, actor: str, note: str = "") -> Transition:
        """Apply a transition, or raise if the workflow forbids it.

        Raising rather than clamping is the point: an unexpected move is a bug
        in whoever asked, and swallowing it would leave the board saying
        something that never happened.
        """
        if not self.can_move_to(target):
            raise ValueError(
                f"{self.id} cannot go from {self.state} to {target}; "
                f"allowed: {', '.join(sorted(ALLOWED[self.state])) or 'nothing'}"
            )
        at = now_ms()
        # Leaving a paused state banks the time spent there, so it never
        # counts against any clock.
        if self.state in PAUSED:
            self.paused_ms += max(0, at - self.updated_at)

        transition = Transition(at=at, frm=str(self.state), to=str(target),
                                actor=actor, note=note)
        self.state = target
        self.updated_at = at
        # Any move out of triage is the acknowledgement.
        if not self.acked_at:
            self.acked_at = at
        if target in TERMINAL:
            self.closed_at = at
        self.log.append(transition)
        return transition

    def as_dict(self, at: int | None = None) -> dict[str, Any]:
        at = at or now_ms()
        return {
            "id": self.id,
            "title": self.title,
            "state": str(self.state),
            "priority": str(self.priority),
            "requester": self.requester,
            "owner": self.owner,
            "desk_room_id": self.desk_room_id,
            "desk_thread_id": self.desk_thread_id,
            "case_room_id": self.case_room_id,
            "opened_at": self.opened_at,
            "updated_at": self.updated_at,
            "closed_at": self.closed_at,
            "outcome": self.outcome,
            "elapsed_ms": self.elapsed_ms(at),
            "stalled_for_ms": self.stalled_for_ms(at),
            "health": self.health(at),
            "ack_overdue_by_ms": self.ack_overdue_by_ms(at),
            "resolve_overdue_by_ms": self.resolve_overdue_by_ms(at),
            "sla": {"ack_minutes": self.sla.ack_minutes,
                    "resolve_minutes": self.sla.resolve_minutes},
            "log": [t.as_dict() for t in self.log],
        }
