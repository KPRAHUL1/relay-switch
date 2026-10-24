"""The grammar people and agents use to drive the desk.

Relay takes its instructions as messages in the room, addressed to it like any
other teammate. That is a deliberate choice over a side channel: the room is
already the audit log, so a workflow driven from it explains itself. Anyone
scrolling back sees who moved what and when, without a second system to go and
look in.

It also means a person and an agent drive Relay the same way. `@relay done
RLY-7 rolled back to v2.13.2` reads the same whether a human typed it or
Claude Code posted it, and neither needs a tool the other lacks.

The grammar is terse on purpose. A command people have to look up is a command
they route around.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .cases import Priority, State

CASE_ID = re.compile(r"\b([A-Z]{2,5}-\d+)\b")
_LEADING_MENTIONS = re.compile(r"^(?:\s*@[\w.\-]+\s*)+")
_PRIORITY = re.compile(r"\b(p[123])\b", re.IGNORECASE)


@dataclass(frozen=True)
class Command:
    verb: str
    case_id: str = ""
    text: str = ""
    priority: Priority | None = None


#: Verb -> the state it moves a case into. Verbs absent from this map do
#: something other than move a case.
MOVES: dict[str, State] = {
    "accept": State.ACCEPTED,
    "info": State.NEEDS_INFO,
    "start": State.IN_PROGRESS,
    "review": State.AWAITING_REVIEW,
    "changes": State.IN_PROGRESS,
    "done": State.DONE,
    "reject": State.REJECTED,
}

#: Verbs that act on the desk rather than on one case.
DESK_VERBS = frozenset({"open", "status", "board", "help"})

#: Verbs that edit a case without moving it.
EDIT_VERBS = frozenset({"claim", "note", "priority"})

ALIASES = {
    "ack": "accept",
    "needs-info": "info",
    "need-info": "info",
    "assign": "claim",
    "mine": "claim",
    "wip": "start",
    "close": "done",
    "resolved": "done",
    "decline": "reject",
    "pipeline": "status",
    "?": "help",
}

HELP = """**Relay** — the desk runs on these. Anyone can use them, people and agents alike.

`@relay open <what you need>` — file a request. Relay answers with its case id.
`@relay accept <id> [p1|p2|p3]` — take it on. Relay opens a case room and links it here.
`@relay info <id> <question>` — hand it back to the requester. The clock pauses.
`@relay claim <id>` — put your name on it.
`@relay start <id>` — work has begun.
`@relay note <id> <text>` — add to the trail without moving the case.
`@relay review <id>` — hand to whoever holds the duty role.
`@relay changes <id> <why>` — send it back to the worker.
`@relay done <id> <outcome>` — close it. The outcome goes back to the requester.
`@relay reject <id> <why>` — close it without doing it.
`@relay priority <id> p1` — re-rank it.
`@relay status [id]` — the pipeline, or one case.
`@relay board` — the live board's address."""


def parse(body: str) -> Command | None:
    """Read one message as a command, or return None if it is just talk.

    Returns None rather than guessing. Relay sits in a room where people are
    also talking to each other, and an agent that acts on an ambiguous
    sentence is worse than one that waits to be asked plainly.
    """
    text = _LEADING_MENTIONS.sub("", (body or "").strip())
    if not text:
        return None

    head, _, rest = text.partition(" ")
    verb = ALIASES.get(head.lower().strip(":,"), head.lower().strip(":,"))
    rest = rest.strip()

    if verb not in MOVES and verb not in DESK_VERBS and verb not in EDIT_VERBS:
        return None

    if verb == "open":
        # Everything after the verb is the request itself.
        return Command(verb="open", text=rest) if rest else None

    if verb in ("help", "board"):
        return Command(verb=verb)

    found = CASE_ID.search(rest)
    case_id = found.group(1) if found else ""

    if verb == "status":
        return Command(verb="status", case_id=case_id)

    if not case_id:
        # Every other verb acts on a specific case. Without an id there is
        # nothing to act on, and picking one for the sender would be a guess.
        return Command(verb="__needs_id", text=verb)

    remainder = (rest[: found.start()] + rest[found.end() :]).strip() if found else ""
    priority = None
    pri_match = _PRIORITY.search(remainder)
    if pri_match:
        priority = Priority(pri_match.group(1).lower())
        remainder = (
            remainder[: pri_match.start()] + remainder[pri_match.end() :]
        ).strip()

    return Command(verb=verb, case_id=case_id, text=remainder.strip(" -:—"),
                   priority=priority)
