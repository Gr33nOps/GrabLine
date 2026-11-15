"""Links the PC user queued "for my phone" (PROTOCOL.md section 13).

``remote_outbox`` rows are addressed to one paired device or, with a NULL
``device_id``, to any phone. They stay until the phone acknowledges them.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.database import Database

MAX_ITEMS = 100
MAX_PENDING = 1000  # per audience; the oldest is dropped beyond this

_SCHEMA = """
CREATE TABLE IF NOT EXISTS remote_outbox (
    item_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  TEXT,
    url        TEXT NOT NULL,
    title      TEXT,
    quality    TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_remote_outbox_device ON remote_outbox(device_id, item_id);
"""


@dataclass(frozen=True)
class OutboxItem:
    item_id: int
    url: str
    title: str | None
    quality: str | None
    created_at: int

    def as_wire(self) -> dict[str, object]:
        return {
            "itemId": self.item_id,
            "url": self.url,
            "title": self.title,
            "quality": self.quality,
            "createdAt": self.created_at,
        }


class Outbox:
    def __init__(self, db: Database, *, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._clock = clock
        with db._lock, db._conn:
            db._conn.executescript(_SCHEMA)

    def add(
        self,
        device_id: str | None,
        url: str,
        title: str | None = None,
        quality: str | None = None,
    ) -> int:
        with self._db._lock, self._db._conn:
            conn = self._db._conn
            cur = conn.execute(
                "INSERT INTO remote_outbox (device_id, url, title, quality, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (device_id, url, title, quality, int(self._clock())),
            )
            conn.execute(
                "DELETE FROM remote_outbox WHERE device_id IS ? AND item_id NOT IN ("
                "SELECT item_id FROM remote_outbox WHERE device_id IS ? "
                "ORDER BY item_id DESC LIMIT ?)",
                (device_id, device_id, MAX_PENDING),
            )
            return int(cur.lastrowid or 0)

    def items_for(self, device_id: str, limit: int = MAX_ITEMS) -> list[OutboxItem]:
        """Oldest first: items for this device and for any phone."""
        with self._db._lock:
            rows = self._db._conn.execute(
                "SELECT item_id, url, title, quality, created_at FROM remote_outbox "
                "WHERE device_id = ? OR device_id IS NULL ORDER BY item_id LIMIT ?",
                (device_id, limit),
            ).fetchall()
        return [OutboxItem(int(r[0]), str(r[1]), r[2], r[3], int(r[4])) for r in rows]

    def count_for(self, device_id: str) -> int:
        with self._db._lock:
            row = self._db._conn.execute(
                "SELECT COUNT(*) FROM remote_outbox WHERE device_id = ? OR device_id IS NULL",
                (device_id,),
            ).fetchone()
        return int(row[0])

    def ack(self, device_id: str, item_ids: Iterable[int]) -> int:
        """Delete the acknowledged items this device may see; unknown or
        already removed ids are skipped. Returns how many were removed."""
        removed = 0
        with self._db._lock, self._db._conn:
            for item_id in item_ids:
                cur = self._db._conn.execute(
                    "DELETE FROM remote_outbox WHERE item_id = ? "
                    "AND (device_id = ? OR device_id IS NULL)",
                    (item_id, device_id),
                )
                removed += cur.rowcount
        return removed

    def forget_device(self, device_id: str) -> None:
        with self._db._lock, self._db._conn:
            self._db._conn.execute("DELETE FROM remote_outbox WHERE device_id = ?", (device_id,))
