"""A read-only HTTP face on the conductor, and the board it serves.

The board is a window, not a control panel. Every way of changing a case goes
through the room, because that is where the audit trail is and where the other
participants are watching. A button here that moved a case would be a decision
taken somewhere nobody else can see.

So: one snapshot endpoint, one event stream, and the static board.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .conductor import Conductor

log = logging.getLogger("relay.api")


def build_app(conductor: Conductor, board_dir: Path) -> FastAPI:
    app = FastAPI(title="Relay", docs_url=None, redoc_url=None)

    @app.get("/api/health")
    async def health() -> JSONResponse:
        state = conductor.client.state
        return JSONResponse(
            {
                "ok": state is not None,
                "agent": conductor.settings.agent_name,
                "desk_room_id": conductor.desk_room_id,
                "rooms": len(state.rooms) if state else 0,
                "cursor": state.cursor if state else 0,
            }
        )

    @app.get("/api/snapshot")
    async def snapshot() -> JSONResponse:
        return JSONResponse(conductor.snapshot())

    @app.get("/api/stream")
    async def stream() -> StreamingResponse:
        """Push a fresh snapshot whenever the desk changes.

        Relay is on the receiving end of exactly this pattern from Switch, so
        the board gets it too: the server says when something happened, rather
        than the client asking every second whether it has.
        """

        async def frames():
            queue = conductor.subscribe()
            try:
                yield _sse(conductor.snapshot())
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        # The same keepalive comment Switch writes on an idle
                        # stream, for the same reason: proxies drop quiet
                        # connections.
                        yield ": keepalive\n\n"
                        continue
                    yield _sse(payload)
            finally:
                conductor.unsubscribe(queue)

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # Mounted last, so the API routes above win on the paths they own.
    if board_dir.is_dir():
        app.mount("/", StaticFiles(directory=board_dir, html=True), name="board")
    else:
        log.warning("no board directory at %s -- serving the API only", board_dir)

    return app


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


@contextlib.asynccontextmanager
async def serve(app: FastAPI, host: str, port: int):
    """Run uvicorn alongside the conductor in one event loop."""
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        yield server
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, timeout=5)
