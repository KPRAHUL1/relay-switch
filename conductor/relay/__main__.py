"""Start Relay: register if needed, open the stream, serve the board.

Everything runs in one event loop. The stream, the clock and the HTTP server
are three tasks that share the conductor, and the process stays up as long as
the stream task does -- which is forever, because the client reopens rather
than returning.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from pathlib import Path

from . import switch
from .api import build_app, serve
from .conductor import Conductor, load_case_template
from .config import load
from .store import Store

log = logging.getLogger("relay")

REPO_ROOT = Path(__file__).resolve().parents[2]
CASE_TEMPLATE = REPO_ROOT / "templates" / "relay-case.room.yaml"
BOARD_DIR = REPO_ROOT / "board"

BANNER = """
  ┌─────────────────────────────────────────────────────────┐
  │  R E L A Y   ·   a conductor for your work desk         │
  │  speaks the Switch agent protocol natively              │
  └─────────────────────────────────────────────────────────┘
"""


async def identity(settings, store: Store) -> switch.Identity:
    """Load Relay's credentials, or register once and keep them.

    The API key is returned exactly once at registration. A conductor that
    forgot it on restart would have to register again under a new identity and
    orphan every room the old one belonged to -- so this is the one piece of
    state Relay cannot afford to lose.
    """
    agent_id = await store.get_meta("agent_id")
    api_key = await store.get_meta("api_key")
    if agent_id and api_key and not settings.register_overwrite:
        log.info("using the credentials on file for agent %s", agent_id)
        return switch.Identity(agent_id=agent_id, api_key=api_key)

    if not settings.registration_token:
        sys.exit(
            "No credentials on file and RELAY_REGISTRATION_TOKEN is unset.\n"
            "Copy AGENT_REGISTRATION_TOKEN from your Switch server's .env "
            "into .env here, then start Relay again."
        )

    log.info("registering %r with Switch", settings.agent_name)
    found = await switch.register_always_on(
        settings.switch_base_url,
        settings.registration_token,
        name=settings.agent_name,
        description=(
            "Runs the work desk: files cases, opens a room per case, holds the "
            "SLA clocks and pages whoever is on duty. Ask it for `help`."
        ),
        display_name="Relay",
        overwrite=settings.register_overwrite,
    )
    await store.set_meta("agent_id", found.agent_id)
    await store.set_meta("api_key", found.api_key)
    log.info("registered as %s", found.agent_id)
    return found


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
        datefmt="%H:%M:%S",
    )
    print(BANNER, flush=True)

    settings = load()
    store = Store(settings.db_path)
    await store.init()

    if not await switch.probe(settings.switch_base_url):
        sys.exit(
            f"No healthy Switch at {settings.switch_base_url}.\n"
            "GET /health is the probe for a bridge base URL: a 401 means the "
            "right host and the wrong path, and the agent bridge is usually "
            "not the Gateway URL. Check RELAY_SWITCH_BASE_URL."
        )

    found = await identity(settings, store)
    client = switch.SwitchClient(
        settings.switch_base_url,
        found,
        # Cover every room Relay belongs to, and see everything said in them.
        # `filter=addressed` would be tempting and wrong: it never delivers
        # `command` events, and a desk wants the room's whole context.
        scope="all",
        event_filter="all",
        # One connection id, reused across reopens, so a dropped socket is a
        # resume. It is persisted for the same reason the API key is.
        connection_id=await store.get_meta("connection_id"),
    )
    await store.set_meta("connection_id", client.connection_id)

    if not CASE_TEMPLATE.is_file():
        sys.exit(f"Missing the case room template at {CASE_TEMPLATE}")

    conductor = Conductor(
        settings, client, store, load_case_template(CASE_TEMPLATE)
    )
    await conductor.start()

    app = build_app(conductor, BOARD_DIR)
    async with serve(app, settings.api_host, settings.api_port):
        log.info("board on http://localhost:%d/", settings.api_port)
        clock = asyncio.create_task(conductor.tick_forever())
        try:
            await client.run(conductor.on_event, conductor.on_control)
        finally:
            clock.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await clock
            await client.aclose()


def run() -> None:
    """Synchronous entry point, for `python -m relay` and the console script."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


if __name__ == "__main__":
    run()
