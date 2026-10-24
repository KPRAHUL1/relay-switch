"""Tests for the two pieces that must not be wrong.

The state machine and the command grammar are where a desk quietly loses
work: a transition that should have been refused, a clock that keeps running
while the desk is waiting on someone else, a command parsed as a different
one. None of that needs Switch running to check, so it is checked here.

Run with `python -m pytest conductor/tests -q` from the repo root.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relay import commands  # noqa: E402
from relay.cases import Case, Priority, State  # noqa: E402


def case(**kw) -> Case:
    return Case(id="RLY-1", title="checkout is failing", **kw)


# -- The state machine --------------------------------------------------------


def test_the_happy_path_runs_end_to_end():
    c = case()
    assert c.state is State.TRIAGING
    for target in (
        State.ACCEPTED,
        State.IN_PROGRESS,
        State.AWAITING_REVIEW,
        State.DONE,
    ):
        c.move(target, actor="sam")
    assert c.state is State.DONE
    assert [t.to for t in c.log] == ["accepted", "in_progress", "awaiting_review", "done"]


def test_review_can_send_work_back():
    c = case()
    c.move(State.ACCEPTED, "sam")
    c.move(State.IN_PROGRESS, "worker")
    c.move(State.AWAITING_REVIEW, "worker")
    c.move(State.IN_PROGRESS, "priya", note="missing the regression test")
    assert c.state is State.IN_PROGRESS
    assert c.log[-1].note == "missing the regression test"


def test_a_closed_case_cannot_be_moved():
    c = case()
    c.move(State.REJECTED, "sam", note="duplicate")
    with pytest.raises(ValueError, match="cannot go from"):
        c.move(State.IN_PROGRESS, "sam")


def test_skipping_the_pipeline_is_refused():
    """Triage cannot jump straight to review. An accidental `@relay review`
    on a brand-new case must fail loudly rather than skip the work."""
    c = case()
    with pytest.raises(ValueError):
        c.move(State.AWAITING_REVIEW, "sam")


def test_the_first_move_is_the_acknowledgement():
    c = case()
    assert c.acked_at == 0
    c.move(State.ACCEPTED, "sam")
    assert c.acked_at > 0
    first_ack = c.acked_at
    c.move(State.IN_PROGRESS, "sam")
    assert c.acked_at == first_ack


# -- The clocks ---------------------------------------------------------------


def test_waiting_on_the_requester_does_not_count_against_the_sla():
    """The whole point of needs_info. A desk judged on time it spent waiting
    for an answer would learn to stop asking questions."""
    opened = int(time.time() * 1000) - 60 * 60_000  # an hour ago
    c = case(opened_at=opened, updated_at=opened, priority=Priority.P1)

    c.move(State.NEEDS_INFO, "intake", note="which environment?")
    # Simulate 30 minutes of the requester being slow.
    c.updated_at -= 30 * 60_000
    c.move(State.ACCEPTED, "intake")

    assert c.paused_ms >= 30 * 60_000 - 1000
    # An hour old, half of it spent waiting: about 30 minutes chargeable.
    assert 28 * 60_000 < c.elapsed_ms() < 32 * 60_000


def test_a_p1_in_triage_breaches_its_ack_promise():
    opened = int(time.time() * 1000) - 10 * 60_000
    c = case(opened_at=opened, updated_at=opened, priority=Priority.P1)
    assert c.sla.ack_minutes == 5
    assert c.ack_overdue_by_ms() > 0
    assert c.health() == "breached"


def test_only_triage_owes_an_ack():
    opened = int(time.time() * 1000) - 10 * 60_000
    c = case(opened_at=opened, updated_at=opened, priority=Priority.P1)
    c.move(State.ACCEPTED, "sam")
    assert c.ack_overdue_by_ms() == 0


def test_a_closed_case_stops_ageing():
    opened = int(time.time() * 1000) - 5 * 60_000
    c = case(opened_at=opened, updated_at=opened)
    c.move(State.ACCEPTED, "sam")
    c.move(State.IN_PROGRESS, "sam")
    c.move(State.AWAITING_REVIEW, "sam")
    c.move(State.DONE, "priya", note="rolled back")
    frozen = c.elapsed_ms()
    time.sleep(0.05)
    assert c.elapsed_ms() == frozen
    assert c.health() == "ok"
    assert c.resolve_overdue_by_ms() == 0


def test_a_paused_case_is_not_stalled():
    """Silence while the desk waits on a person is the correct state, so it
    must not trigger a stall nudge."""
    c = case()
    c.move(State.NEEDS_INFO, "intake", note="which environment?")
    c.updated_at -= 60 * 60_000
    assert c.stalled_for_ms() == 0


def test_an_untouched_open_case_is_stalled():
    c = case()
    c.move(State.ACCEPTED, "sam")
    c.updated_at -= 45 * 60_000
    assert c.stalled_for_ms() >= 44 * 60_000


# -- The grammar --------------------------------------------------------------


@pytest.mark.parametrize(
    "body, verb, case_id",
    [
        ("@relay accept RLY-12", "accept", "RLY-12"),
        ("@relay done RLY-3 rolled back to v2.13.2", "done", "RLY-3"),
        ("@relay  review   RLY-7  ", "review", "RLY-7"),
        ("close RLY-9 shipped", "done", "RLY-9"),  # alias
        ("@relay status", "status", ""),
        ("@relay status RLY-4", "status", "RLY-4"),
    ],
)
def test_commands_parse(body, verb, case_id):
    cmd = commands.parse(body)
    assert cmd is not None
    assert cmd.verb == verb
    assert cmd.case_id == case_id


def test_ordinary_conversation_is_not_a_command():
    """Relay sits in a room where people talk to each other. Acting on an
    ambiguous sentence is worse than waiting to be asked plainly."""
    for body in [
        "@relay what do you think about the checkout thing",
        "thanks!",
        "",
        "@relay",
        "I'll review this later",
    ]:
        assert commands.parse(body) is None


def test_the_outcome_survives_the_case_id_being_cut_out():
    cmd = commands.parse("@relay done RLY-3 rolled back to v2.13.2")
    assert cmd is not None
    assert cmd.text == "rolled back to v2.13.2"


def test_priority_is_read_and_removed_from_the_text():
    cmd = commands.parse("@relay accept RLY-5 p1 checkout is down")
    assert cmd is not None
    assert cmd.priority is Priority.P1
    assert "p1" not in cmd.text
    assert cmd.text == "checkout is down"


def test_open_keeps_everything_as_the_title():
    cmd = commands.parse("@relay open the export job has been failing since 09:40")
    assert cmd is not None
    assert cmd.verb == "open"
    assert cmd.text == "the export job has been failing since 09:40"


def test_a_move_without_a_case_id_is_caught_rather_than_guessed():
    cmd = commands.parse("@relay done it's fixed")
    assert cmd is not None
    assert cmd.verb == "__needs_id"


def test_several_mentions_are_stripped():
    """Platforms prepend their own mentions when a message is addressed to
    more than one participant."""
    cmd = commands.parse("@relay @relay-worker start RLY-2")
    assert cmd is not None
    assert cmd.verb == "start"
    assert cmd.case_id == "RLY-2"


def test_every_move_verb_maps_to_a_real_state():
    for verb, state in commands.MOVES.items():
        assert isinstance(state, State), verb
