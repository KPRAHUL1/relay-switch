"""Serve the board against a seeded desk, with no Switch server involved.

This exists for two reasons. It is how the board gets worked on without
standing up Switch and a chat app first, and it is how someone cloning this
repo sees what Relay produces before deciding whether to set the rest up.

It is not a simulation of Switch. Nothing here speaks the agent protocol -- it
seeds the same case store the real conductor writes to, then serves the same
read-only API over it. Every number on the board comes from the same code path
as in production.

    python conductor/demo.py          then open http://localhost:8787/
"""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from relay.api import build_app, serve  # noqa: E402
from relay.cases import Case, Priority, State, now_ms  # noqa: E402
from relay.conductor import Conductor  # noqa: E402
from relay.config import Settings  # noqa: E402
from relay.store import Store  # noqa: E402
from relay.switch import ConnectionState  # noqa: E402

MINUTE = 60_000

#: Each entry is (title, priority, path through the state machine, requester,
#: owner, how long ago it opened, in minutes). The paths are real: every one is
#: walked through `Case.move`, so anything the state machine forbids would fail
#: here rather than render.
SEED = [
    ("checkout 500s since the 09:40 deploy", Priority.P1,
     [State.ACCEPTED, State.IN_PROGRESS, State.AWAITING_REVIEW],
     "sam", "relay-worker", 74),
    ("export job silently dropping rows for EU tenants", Priority.P1,
     [State.ACCEPTED, State.IN_PROGRESS], "priya", "relay-worker", 38),
    ("add a staging seed for the invoices table", Priority.P3,
     [], "alex", "", 310),
    ("SSO login loops on Safari 18", Priority.P2,
     [State.NEEDS_INFO], "dana", "", 96),
    ("rotate the stale Grafana read-only token", Priority.P2,
     [State.ACCEPTED], "sam", "priya", 22),
    ("webhook retries hammering the partner sandbox", Priority.P2,
     [State.ACCEPTED, State.IN_PROGRESS], "omar", "relay-worker", 140),
    ("bump pillow past the CVE", Priority.P2,
     [State.ACCEPTED, State.IN_PROGRESS, State.AWAITING_REVIEW, State.DONE],
     "alex", "relay-worker", 420),
    ("docs: the onboarding page still names the old CLI", Priority.P3,
     [State.ACCEPTED, State.IN_PROGRESS, State.AWAITING_REVIEW, State.DONE],
     "dana", "relay-worker", 640),
    ("duplicate of RLY-2", Priority.P3, [State.REJECTED], "omar", "", 300),
]

NOTES = {
    State.ACCEPTED: ["triaged, taking it", "reproduced, worth doing", "picking this up"],
    State.IN_PROGRESS: ["bisected to the cart-service bump", "reproduced locally",
                        "narrowed to the retry wrapper"],
    State.AWAITING_REVIEW: ["rollback to v2.13.2 is ready, needs a sign-off",
                            "patch plus a regression test", "fixed, verified on staging"],
    State.DONE: ["rolled back to v2.13.2", "merged and deployed",
                 "shipped behind the existing flag"],
    State.NEEDS_INFO: ["which Safari build, and is it reproducible in a private window?"],
    State.REJECTED: ["duplicate"],
}


class StubClient:
    """Enough of a SwitchClient for the read-only API. Nothing more."""

    def __init__(self) -> None:
        self.state = ConnectionState(
            connection_id="demo", agent_id="demo",
            rooms=["desk"] + [f"case-{i}" for i in range(5)], cursor=4813,
        )
        self.operations: dict[str, dict] = {}

    async def load_operations(self) -> dict[str, dict]:
        return {}

    async def say(self, *_a, **_k) -> None:
        pass

    async def op(self, *_a, **_k):
        return None

    async def op_in_room(self, *_a, **_k):
        return None


def seed(conductor: Conductor) -> None:
    random.seed(7)
    for index, (title, priority, path, requester, owner, age_min) in enumerate(SEED, 1):
        case = Case(
            id=f"RLY-{index}",
            title=title,
            priority=priority,
            requester=requester,
            owner=owner,
            desk_room_id="desk",
            opened_at=now_ms() - age_min * MINUTE,
        )
        case.updated_at = case.opened_at

        # Walk the real state machine, spacing the moves across the case's age
        # so the clocks and the trail are internally consistent.
        step = (age_min * MINUTE) // (len(path) + 1)
        for position, target in enumerate(path, 1):
            case.move(
                target,
                actor=owner or requester or "sam",
                note=random.choice(NOTES.get(target, [""])),
            )
            case.updated_at = case.opened_at + step * position
            if target in (State.DONE, State.REJECTED):
                case.closed_at = case.updated_at
                case.outcome = case.log[-1].note
            case.log[-1].at = case.updated_at

        conductor.cases[case.id] = case

    # One case left genuinely untouched and overdue, so the breach styling and
    # the stall nudge both have something to show.
    conductor.cases["RLY-3"].updated_at = conductor.cases["RLY-3"].opened_at


async def main() -> None:
    settings = Settings(
        state_dir=Path("./.relay-demo"), desk_room_name="relay-desk", api_port=8787
    )
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.db_path)
    await store.init()

    conductor = Conductor(settings, StubClient(), store, case_template="")
    conductor.desk_room_id = "demo-desk"
    seed(conductor)

    app = build_app(conductor, Path(__file__).resolve().parents[1] / "board")
    print(f"\n  Relay board (demo data) -> http://localhost:{settings.api_port}/\n")
    async with serve(app, "127.0.0.1", settings.api_port):
        # Keep the clocks moving so the board is live, without paging anyone.
        while True:
            await asyncio.sleep(5)
            await conductor._broadcast()  # noqa: SLF001 - the demo is the one caller


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
