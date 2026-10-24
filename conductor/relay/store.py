"""Persistence: SQLite, one row per case, plus a small key/value table.

Relay restarts without losing the desk. That matters more than it sounds: the
agent's API key is returned exactly once at registration, so a conductor that
forgot its credentials on restart would have to re-register under a new
identity and orphan every room it belongs to.

SQLite is driven through `asyncio.to_thread` rather than an async driver. The
write volume of a work desk is a handful of rows per case, and one fewer
dependency is worth more here than a connection pool.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from .cases import Case, Priority, State, Transition

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id         TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    body       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cases_state ON cases(state, updated_at);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        # WAL keeps the API's reads from blocking the conductor's writes.
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    async def init(self) -> None:
        def run() -> None:
            with self._connect() as conn:
                conn.executescript(_SCHEMA)

        await asyncio.to_thread(run)

    # -- Credentials and counters --------------------------------------------

    async def get_meta(self, key: str) -> str | None:
        def run() -> str | None:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = ?", (key,)
                ).fetchone()
                return row["value"] if row else None

        return await asyncio.to_thread(run)

    async def set_meta(self, key: str, value: str) -> None:
        def run() -> None:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

        await asyncio.to_thread(run)

    async def next_case_number(self) -> int:
        """Hand out the next case number, under the write lock.

        Case ids are the handle people type and quote at each other, so they
        have to be short, ordered and never reused. The lock is what stops two
        requests arriving in the same second from getting the same one.
        """
        async with self._lock:

            def run() -> int:
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT value FROM meta WHERE key = 'case_seq'"
                    ).fetchone()
                    nxt = (int(row["value"]) if row else 0) + 1
                    conn.execute(
                        "INSERT INTO meta(key, value) VALUES('case_seq', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (str(nxt),),
                    )
                    return nxt

            return await asyncio.to_thread(run)

    # -- Cases ----------------------------------------------------------------

    async def save(self, case: Case) -> None:
        payload = json.dumps(_to_row(case))

        def run() -> None:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO cases(id, state, updated_at, body) "
                    "VALUES(?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "state = excluded.state, updated_at = excluded.updated_at, "
                    "body = excluded.body",
                    (case.id, str(case.state), case.updated_at, payload),
                )

        async with self._lock:
            await asyncio.to_thread(run)

    async def load_all(self) -> list[Case]:
        def run() -> list[dict[str, Any]]:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT body FROM cases ORDER BY updated_at ASC"
                ).fetchall()
                return [json.loads(r["body"]) for r in rows]

        return [_from_row(r) for r in await asyncio.to_thread(run)]


def _to_row(case: Case) -> dict[str, Any]:
    return {
        "id": case.id,
        "title": case.title,
        "state": str(case.state),
        "priority": str(case.priority),
        "requester": case.requester,
        "owner": case.owner,
        "desk_room_id": case.desk_room_id,
        "desk_thread_id": case.desk_thread_id,
        "case_room_id": case.case_room_id,
        "opened_at": case.opened_at,
        "updated_at": case.updated_at,
        "acked_at": case.acked_at,
        "closed_at": case.closed_at,
        "paused_ms": case.paused_ms,
        "outcome": case.outcome,
        "log": [t.as_dict() for t in case.log],
    }


def _from_row(row: dict[str, Any]) -> Case:
    return Case(
        id=row["id"],
        title=row["title"],
        state=State(row["state"]),
        priority=Priority(row.get("priority") or "p2"),
        requester=row.get("requester", ""),
        owner=row.get("owner", ""),
        desk_room_id=row.get("desk_room_id", ""),
        desk_thread_id=row.get("desk_thread_id", ""),
        case_room_id=row.get("case_room_id", ""),
        opened_at=row["opened_at"],
        updated_at=row["updated_at"],
        acked_at=row.get("acked_at", 0),
        closed_at=row.get("closed_at", 0),
        paused_ms=row.get("paused_ms", 0),
        outcome=row.get("outcome", ""),
        log=[
            Transition(
                at=t["at"], frm=t["from"], to=t["to"],
                actor=t.get("actor", ""), note=t.get("note", ""),
            )
            for t in row.get("log", [])
        ],
    )
