"""Triage records in their own SQLite file (not the bot's main database).

One row per alert that reached the worker. Phase 4 looks rows up by the
Telegram message IDs to resume a triage session from a reply.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_triage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    alert_ts TEXT,
    job TEXT,
    severity TEXT,
    summary TEXT,
    fingerprint TEXT,
    chat_id INTEGER,
    alert_message_id INTEGER,
    status TEXT NOT NULL,
    verdict TEXT,
    triage_message_id INTEGER,
    session_id TEXT,
    cost_usd REAL NOT NULL DEFAULT 0,
    error TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_triage_fp ON alert_triage (fingerprint, received_at);
CREATE INDEX IF NOT EXISTS idx_triage_msg ON alert_triage (alert_message_id);
CREATE INDEX IF NOT EXISTS idx_triage_reply ON alert_triage (triage_message_id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class TriageStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def add(self, event: Dict[str, Any], status: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO alert_triage (received_at, alert_ts, job, severity, summary,"
                " fingerprint, chat_id, alert_message_id, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(),
                    event.get("ts"),
                    event.get("job"),
                    event.get("severity"),
                    event.get("summary"),
                    event.get("fingerprint"),
                    event.get("telegram_chat_id"),
                    event.get("telegram_message_id"),
                    status,
                ),
            )
            await db.commit()
            return int(cur.lastrowid or 0)

    async def update(self, row_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"UPDATE alert_triage SET {cols} WHERE id = ?",  # noqa: S608 — keys are ours
                (*fields.values(), row_id),
            )
            await db.commit()

    async def cost_today(self) -> float:
        day = datetime.now(UTC).date().isoformat()
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM alert_triage WHERE received_at >= ?",
                (day,),
            )
            row = await cur.fetchone()
            return float(row[0]) if row else 0.0

    async def recent_triage(
        self, fingerprint: str, hours: float
    ) -> Optional[Dict[str, Any]]:
        """The latest completed triage of this fingerprint within ``hours``."""
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(
            timespec="seconds"
        )
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM alert_triage WHERE fingerprint = ? AND status = 'done'"
                " AND received_at >= ? ORDER BY id DESC LIMIT 1",
                (fingerprint, since),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get(self, row_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM alert_triage WHERE id = ?", (row_id,))
            row = await cur.fetchone()
            return dict(row) if row else None
