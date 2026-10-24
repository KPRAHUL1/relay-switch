"""The protocol client, against a mock agent bridge.

`switch.py` is the riskiest file in the project: it is the only one whose
correctness depends on a wire contract rather than on logic you can read off
the page, and the failure mode is a conductor that looks healthy and silently
stops receiving events.

So this stands up a server that behaves like the agent bridge -- SSE down,
HTTP up, the same frames in the same order -- and checks the things that are
easy to get wrong:

* `connection_state` is read rather than assumed
* the heartbeat starts only once a stream is attached, and carries the cursor
* domain events are delivered with their sequence, control frames are not
* `evicted` reopens, and the reopen resumes with `Last-Event-ID`
* operations carry the connection id header
* keepalive comments are not mistaken for frames

No pytest-asyncio: each test drives its own loop with `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sys
from pathlib import Path

import pytest
# Imported at module level, not inside the factory: `from __future__ import
# annotations` turns the route signatures into strings, and FastAPI resolves
# them against module globals.
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relay import switch  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class MockBridge:
    """Just enough of the agent bridge to hold a client to its contract."""

    def __init__(self) -> None:
        self.beats: list[dict] = []
        self.ops_called: list[tuple[str, dict, str | None]] = []
        self.opens: list[dict] = []
        self.messages: list[dict] = []
        #: Frames the next stream will write, as (event name, data) pairs.
        self.script: list[tuple[str, dict]] = []
        #: Streams opened so far; the first is evicted to test the reopen.
        self.stream_count = 0
        self.evict_first = False
        #: The server's current view of the covered rooms. The heartbeat
        #: response is authoritative about this, so it has to stay consistent
        #: with whatever `subscription_changed` last announced.
        self.rooms: list[str] = ["room-a"]
        #: This stream's incarnation of the connection id. The client must echo
        #: it on every heartbeat or the tick cannot be fenced.
        self.generation = 918_273

    def app(self) -> FastAPI:
        api = FastAPI()

        @api.get("/health")
        async def health():
            return {"ok": True}

        @api.post("/agents")
        async def register(request: Request):
            body = await request.json()
            assert request.headers["authorization"] == "Bearer reg-token"
            # A client written from scratch must declare a full profile.
            assert body["integration_profile"]["connection_model"] == "always_on"
            return JSONResponse({"id": "agent-1", "api_key": "key-1"})

        @api.get("/agents/{agent_id}/ops")
        async def ops(agent_id: str):
            return {"operations": {"list_rooms": {"description": "", "input_schema": {}}}}

        @api.post("/agents/{agent_id}/ops/{name}")
        async def call_op(agent_id: str, name: str, request: Request):
            body = await request.json()
            self.ops_called.append(
                (name, body, request.headers.get("x-switch-connection-id"))
            )
            return {"result": {"ok": True, "op": name}}

        @api.post("/agents/{agent_id}/message")
        async def message(agent_id: str, request: Request):
            self.messages.append(await request.json())
            return {"event_id": "evt-1"}

        @api.post("/agents/{agent_id}/connection/beat")
        async def beat(agent_id: str, request: Request):
            try:
                body = await request.json()
            except Exception:
                # The client went away mid-beat during teardown.
                return JSONResponse({"ok": True, "rooms": self.rooms, "cursor": 0})
            self.beats.append(body)
            # The real server fences every tick from a client that declares
            # protocol 6: an unfenced beat is refused, the connection is never
            # renewed, and it is swept a few seconds later. Reproduced here
            # because a client that drops the generation looks completely
            # healthy right up to the point where it stops receiving anything.
            if body.get("generation") != self.generation:
                return JSONResponse(
                    {
                        "code": "unfenced",
                        "message": "this tick carried no usable incarnation",
                    },
                    status_code=409,
                )
            return JSONResponse(
                {"ok": True, "rooms": self.rooms, "cursor": body.get("cursor", 0)}
            )

        @api.get("/agents/{agent_id}/events")
        async def events(agent_id: str, request: Request):
            self.stream_count += 1
            mine = self.stream_count
            self.opens.append(
                {
                    "query": dict(request.query_params),
                    "last_event_id": request.headers.get("last-event-id"),
                }
            )

            async def frames():
                yield _frame(
                    "connection_state",
                    {
                        "type": "connection_state",
                        "connection_id": request.query_params.get("connection_id"),
                        "agent_id": agent_id,
                        "scope": request.query_params.get("scope"),
                        "filter": request.query_params.get("filter"),
                        "rooms": ["room-a"],
                        "cursor": 100,
                        "heartbeat_interval_seconds": 0.05,
                        "protocol": 6,
                        "generation": self.generation,
                    },
                )
                # A keepalive comment, which is not a frame.
                yield ": keepalive\n\n"

                for name, data in self.script:
                    if name == "subscription_changed":
                        self.rooms = list(data["rooms"])
                    yield _frame(name, data, seq=data.get("sequence"))
                    await asyncio.sleep(0.01)

                if self.evict_first and mine == 1:
                    yield _frame("evicted", {"type": "evicted", "reason": "takeover"})
                    return

                # Hold the stream open, as a real bridge does.
                while True:
                    await asyncio.sleep(0.05)
                    yield ": keepalive\n\n"

            return StreamingResponse(frames(), media_type="text/event-stream")

        return api


def _frame(name: str, data: dict, seq: int | None = None) -> str:
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {name}\ndata: {json.dumps(data)}\n\n"


@contextlib.asynccontextmanager
async def running(bridge: MockBridge, port: int):
    import uvicorn

    config = uvicorn.Config(bridge.app(), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.02)
    try:
        yield
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=5)


async def collect(client, seen, controls, seconds: float):
    """Run the client for a bounded time, then stop it."""

    async def on_event(event):
        seen.append(event)

    async def on_control(frame):
        controls.append(frame)

    task = asyncio.create_task(client.run(on_event, on_control))
    await asyncio.sleep(seconds)
    client._closed = True  # noqa: SLF001 - the test owns this client
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# -- Tests --------------------------------------------------------------------


def test_registration_and_the_first_frame():
    async def scenario():
        bridge = MockBridge()
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        async with running(bridge, port):
            assert await switch.probe(base) is True

            identity = await switch.register_always_on(
                base, "reg-token", "relay", "a desk conductor"
            )
            assert identity.agent_id == "agent-1"
            assert identity.api_key == "key-1"

            client = switch.SwitchClient(base, identity)
            seen, controls = [], []
            await collect(client, seen, controls, 0.4)
            await client.aclose()

            # connection_state is read, not assumed: the interval and the room
            # set come from the server.
            assert client.state is not None
            assert client.state.rooms == ["room-a"]
            assert client.state.heartbeat_interval_seconds == 0.05
            assert client.state.cursor == 100
            # It is a control frame, so it carries no sequence and is not an event.
            assert controls[0].type == "connection_state"
            assert seen == []

        return bridge

    bridge = asyncio.run(scenario())
    # The heartbeat starts only once a stream is attached, and carries the cursor.
    assert bridge.beats, "no heartbeat was sent"
    assert bridge.beats[0]["cursor"] == 100
    assert bridge.beats[0]["connection_id"]


def test_the_heartbeat_is_fenced_with_the_stream_generation():
    """Regression: a beat without the generation is refused by a real server.

    From protocol 6 the server stamps each stream with an incarnation of the
    connection id and refuses any tick that does not carry it -- a displaced
    client still holds the id and the API key, so the id alone proves nothing.
    A client that omits it looks perfectly healthy: the stream opens, events
    arrive, and then the connection is swept a few seconds later for a lapsed
    heartbeat, forever.
    """

    async def scenario():
        bridge = MockBridge()
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        async with running(bridge, port):
            identity = await switch.register_always_on(base, "reg-token", "relay", "d")
            client = switch.SwitchClient(base, identity)
            seen, controls = [], []
            await collect(client, seen, controls, 0.5)
            generation = client.state.generation if client.state else None
            await client.aclose()
        return bridge, generation

    bridge, generation = asyncio.run(scenario())

    # It is read off connection_state, not invented.
    assert generation == bridge.generation
    assert bridge.beats, "no heartbeat was sent"
    assert all(b.get("generation") == bridge.generation for b in bridge.beats), (
        "a heartbeat went out unfenced; a real server would refuse it and "
        "sweep the connection"
    )
    # Refused beats mean reopen, so an unfenced client reconnects forever.
    # Fenced, one stream is enough.
    assert bridge.stream_count == 1, "the connection was not renewed cleanly"


def test_domain_events_are_delivered_and_the_cursor_advances():
    async def scenario():
        bridge = MockBridge()
        bridge.script = [
            (
                "message",
                {
                    "type": "message",
                    "room_id": "room-a",
                    "sequence": 101,
                    "payload": {
                        "addressed": True,
                        "sender_name": "sam",
                        "body": "@relay open the export job is dropping rows",
                        "message_id": "m-1",
                    },
                },
            ),
            (
                "subscription_changed",
                {"type": "subscription_changed", "rooms": ["room-a", "room-b"]},
            ),
        ]
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        async with running(bridge, port):
            identity = await switch.register_always_on(base, "reg-token", "relay", "d")
            client = switch.SwitchClient(base, identity)
            seen, controls = [], []
            await collect(client, seen, controls, 0.5)
            cursor = client._cursor  # noqa: SLF001
            rooms = client.state.rooms if client.state else []
            await client.aclose()
        return bridge, seen, controls, cursor, rooms

    bridge, seen, controls, cursor, rooms = asyncio.run(scenario())

    assert len(seen) == 1
    event = seen[0]
    assert event.type == "message"
    assert event.room_id == "room-a"
    assert event.sequence == 101
    assert event.addressed is True
    assert event.sender_name == "sam"
    assert event.body.startswith("@relay open")
    assert event.message_id == "m-1"

    # The cursor follows the sequence, and the beat reports it.
    assert cursor == 101
    assert any(b["cursor"] == 101 for b in bridge.beats)

    # A control frame reaches on_control, never on_event, and updates the
    # covered room set.
    kinds = [c.type for c in controls]
    assert "subscription_changed" in kinds
    assert rooms == ["room-a", "room-b"]


def test_eviction_reopens_and_resumes_from_the_cursor():
    """A connection outlives its socket. The id is reused and the reopen
    carries Last-Event-ID, which is what makes a drop a resume."""

    async def scenario():
        bridge = MockBridge()
        bridge.evict_first = True
        bridge.script = [
            (
                "message",
                {
                    "type": "message",
                    "room_id": "room-a",
                    "sequence": 205,
                    "payload": {"addressed": False, "body": "hello", "sender_name": "sam"},
                },
            )
        ]
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        async with running(bridge, port):
            identity = await switch.register_always_on(base, "reg-token", "relay", "d")
            client = switch.SwitchClient(base, identity)
            seen, controls = [], []
            await collect(client, seen, controls, 0.8)
            await client.aclose()
        return bridge

    bridge = asyncio.run(scenario())

    assert bridge.stream_count >= 2, "the client did not reopen after being evicted"
    first, second = bridge.opens[0], bridge.opens[1]
    # One connection id across both opens.
    assert first["query"]["connection_id"] == second["query"]["connection_id"]
    # The reopen resumes rather than starting again from the head.
    assert second["last_event_id"] == "205"


def test_the_open_url_declares_what_the_client_wants():
    async def scenario():
        bridge = MockBridge()
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        async with running(bridge, port):
            identity = await switch.register_always_on(base, "reg-token", "relay", "d")
            client = switch.SwitchClient(base, identity, scope="all", event_filter="all")
            seen, controls = [], []
            await collect(client, seen, controls, 0.3)
            await client.aclose()
        return bridge.opens[0]["query"]

    query = asyncio.run(scenario())
    assert query["scope"] == "all"
    # `addressed` would never deliver command events; a desk wants everything.
    assert query["filter"] == "all"
    assert query["start_from"] == "head"
    # Protocol negotiation is by overlap, and declaring nothing records the
    # client as unknown -- so it is declared.
    assert query["protocol"] == str(switch.PROTOCOL_REVISION)
    assert query["client"] == switch.CLIENT_NAME


def test_operations_carry_the_connection_id_and_drop_unset_arguments():
    async def scenario():
        bridge = MockBridge()
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        async with running(bridge, port):
            identity = await switch.register_always_on(base, "reg-token", "relay", "d")
            client = switch.SwitchClient(base, identity)
            ops = await client.load_operations()
            assert "list_rooms" in ops

            result = await client.op("list_rooms", include_archived=False, missing=None)
            assert result == {"ok": True, "op": "list_rooms"}

            # op_in_room claims the room first, then calls.
            await client.op_in_room("room-a", "post_message", body="hi")
            # The claim is cached: a second call in the same room does not re-claim.
            await client.op_in_room("room-a", "post_message", body="again")

            await client.say("room-b", "a connectionless post")
            await client.aclose()
        return bridge

    bridge = asyncio.run(scenario())

    names = [name for name, _, _ in bridge.ops_called]
    assert names == [
        "list_rooms",
        "connect_to_room",
        "post_message",
        "post_message",
    ], names

    # Every call is bound to the connection.
    assert all(conn for _, _, conn in bridge.ops_called)
    # Unset arguments are dropped: they are validated by signature inspection,
    # so an unexpected null is a 400.
    assert bridge.ops_called[0][1] == {"include_archived": False}
    # The connectionless path names its room outright.
    assert bridge.messages == [{"room_id": "room-b", "content": "a connectionless post"}]


@pytest.mark.parametrize(
    "raw, expected",
    [
        (": keepalive\n\n", []),
        ('event: message\ndata: {"type":"message"}\n\n', [("message", None)]),
        ('id: 7\nevent: message\ndata: {"type":"message"}\n\n', [("message", 7)]),
        ('data: {"type":"gap"}\n\n', [("gap", None)]),  # name falls back to type
        ('event: message\ndata: not json\n\n', []),  # undecodable is dropped, not fatal
    ],
)
def test_sse_parsing(raw, expected):
    async def scenario():
        async def lines():
            for line in raw.split("\n"):
                yield line

        return [(name, seq) async for name, _data, seq in switch._parse_sse(lines())]

    assert asyncio.run(scenario()) == expected
