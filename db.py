"""SQLite persistence for message reply routing, stats, and blocked users."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = Path(__file__).parent / "inbox_bot.db"


class BotDatabase:
    """Manages SQLite storage for the Telegram inbox bot."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_msg_id INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    sender_username TEXT,
                    sender_name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_owner_msg_id
                ON messages (owner_msg_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS blocked_users (
                    user_id INTEGER PRIMARY KEY,
                    blocked_at TEXT NOT NULL
                )
                """
            )

    def record_message(
        self,
        owner_msg_id: int,
        sender_id: int,
        sender_username: str | None,
        sender_name: str,
    ) -> None:
        """Store a forwarded message mapping so owner replies can be routed back."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages (owner_msg_id, sender_id, sender_username, sender_name, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (owner_msg_id, sender_id, sender_username, sender_name, now),
            )

    def get_sender_for_owner_message(self, owner_msg_id: int) -> int | None:
        """Return the original sender's Telegram user ID for a given owner message ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT sender_id FROM messages WHERE owner_msg_id = ? ORDER BY id DESC LIMIT 1",
                (owner_msg_id,),
            ).fetchone()
            return int(row["sender_id"]) if row else None

    def is_blocked(self, user_id: int) -> bool:
        """Check whether a Telegram user ID is blocked."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM blocked_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return row is not None

    def block_user(self, user_id: int) -> None:
        """Block a user ID from sending messages."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO blocked_users (user_id, blocked_at)
                VALUES (?, ?)
                """,
                (user_id, now),
            )

    def unblock_user(self, user_id: int) -> bool:
        """Unblock a user ID. Returns True if the user was previously blocked."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM blocked_users WHERE user_id = ?",
                (user_id,),
            )
            return cursor.rowcount > 0

    def get_stats(self) -> tuple[int, int, int]:
        """Return (total_messages, unique_senders, blocked_count)."""
        with self._connect() as conn:
            msg_row = conn.execute(
                "SELECT COUNT(*) AS total, COUNT(DISTINCT sender_id) AS unique_senders FROM messages"
            ).fetchone()
            blk_row = conn.execute("SELECT COUNT(*) AS blocked FROM blocked_users").fetchone()
            return (
                int(msg_row["total"]),
                int(msg_row["unique_senders"]),
                int(blk_row["blocked"]),
            )
