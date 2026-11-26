"""Database persistence supporting PostgreSQL (DATABASE_URL) and SQLite fallback."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

DEFAULT_DB_PATH = Path(__file__).parent / "inbox_bot.db"


class BotDatabase:
    """Manages users, /start logs, message reply routing, and blocked users."""

    def __init__(
        self,
        db_path: Path | str = DEFAULT_DB_PATH,
        database_url: str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        env_url = os.getenv("DATABASE_URL", "").strip() if database_url is None else database_url.strip()
        self.database_url = env_url or None
        self.is_postgres = bool(self.database_url)
        self._init_schema()

    def _ph(self, count: int = 1) -> str:
        token = "%s" if self.is_postgres else "?"
        return ", ".join([token] * count)

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        if self.is_postgres:
            assert self.database_url is not None
            conn = psycopg.connect(self.database_url, row_factory=dict_row)
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()
        else:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _init_schema(self) -> None:
        id_col = "SERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        int_type = "BIGINT" if self.is_postgres else "INTEGER"

        with self._connect() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS users (
                    user_id {int_type} PRIMARY KEY,
                    chat_id {int_type} NOT NULL,
                    username TEXT,
                    full_name TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    start_count INTEGER NOT NULL DEFAULT 0,
                    last_start_param TEXT
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS start_logs (
                    id {id_col},
                    user_id {int_type} NOT NULL,
                    chat_id {int_type} NOT NULL,
                    username TEXT,
                    full_name TEXT NOT NULL,
                    start_param TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS messages (
                    id {id_col},
                    owner_msg_id {int_type} NOT NULL,
                    sender_id {int_type} NOT NULL,
                    chat_id {int_type},
                    sender_username TEXT,
                    sender_name TEXT NOT NULL,
                    message_type TEXT NOT NULL DEFAULT 'text',
                    content_preview TEXT,
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
                f"""
                CREATE TABLE IF NOT EXISTS blocked_users (
                    user_id {int_type} PRIMARY KEY,
                    blocked_at TEXT NOT NULL
                )
                """
            )

            # Lightweight migration for existing SQLite DBs created before chat_id/message_type existed
            if not self.is_postgres:
                cols = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(messages)").fetchall()
                }
                if "chat_id" not in cols:
                    conn.execute("ALTER TABLE messages ADD COLUMN chat_id INTEGER")
                if "message_type" not in cols:
                    conn.execute(
                        "ALTER TABLE messages ADD COLUMN message_type TEXT NOT NULL DEFAULT 'text'"
                    )
                if "content_preview" not in cols:
                    conn.execute("ALTER TABLE messages ADD COLUMN content_preview TEXT")

    def _upsert_user(
        self,
        conn: Any,
        user_id: int,
        chat_id: int,
        username: str | None,
        full_name: str,
        now: str,
        increment_start: bool = False,
        start_param: str | None = None,
    ) -> int:
        p = "%s" if self.is_postgres else "?"
        start_inc = 1 if increment_start else 0

        conn.execute(
            f"""
            INSERT INTO users (
                user_id, chat_id, username, full_name,
                first_seen_at, last_seen_at, start_count, last_start_param
            )
            VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                username = excluded.username,
                full_name = excluded.full_name,
                last_seen_at = excluded.last_seen_at,
                start_count = users.start_count + {p},
                last_start_param = COALESCE(excluded.last_start_param, users.last_start_param)
            """,
            (
                user_id,
                chat_id,
                username,
                full_name,
                now,
                now,
                start_inc,
                start_param,
                start_inc,
            ),
        )
        row = conn.execute(
            f"SELECT start_count FROM users WHERE user_id = {p}",
            (user_id,),
        ).fetchone()
        return int(row["start_count"]) if row else start_inc

    def record_start(
        self,
        user_id: int,
        chat_id: int,
        username: str | None,
        full_name: str,
        start_param: str | None = None,
    ) -> int:
        """Record a /start event and upsert the user. Returns the user's total start_count."""
        now = datetime.now(timezone.utc).isoformat()
        p = "%s" if self.is_postgres else "?"
        with self._connect() as conn:
            start_count = self._upsert_user(
                conn,
                user_id=user_id,
                chat_id=chat_id,
                username=username,
                full_name=full_name,
                now=now,
                increment_start=True,
                start_param=start_param,
            )
            conn.execute(
                f"""
                INSERT INTO start_logs (user_id, chat_id, username, full_name, start_param, created_at)
                VALUES ({p}, {p}, {p}, {p}, {p}, {p})
                """,
                (user_id, chat_id, username, full_name, start_param, now),
            )
            return start_count

    def record_message(
        self,
        owner_msg_id: int,
        sender_id: int,
        sender_username: str | None,
        sender_name: str,
        chat_id: int | None = None,
        message_type: str = "text",
        content_preview: str | None = None,
    ) -> None:
        """Store a forwarded message mapping and update the sender's user record."""
        now = datetime.now(timezone.utc).isoformat()
        p = "%s" if self.is_postgres else "?"
        resolved_chat_id = chat_id if chat_id is not None else sender_id
        with self._connect() as conn:
            self._upsert_user(
                conn,
                user_id=sender_id,
                chat_id=resolved_chat_id,
                username=sender_username,
                full_name=sender_name,
                now=now,
                increment_start=False,
            )
            conn.execute(
                f"""
                INSERT INTO messages (
                    owner_msg_id, sender_id, chat_id, sender_username,
                    sender_name, message_type, content_preview, created_at
                )
                VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
                """,
                (
                    owner_msg_id,
                    sender_id,
                    resolved_chat_id,
                    sender_username,
                    sender_name,
                    message_type,
                    content_preview,
                    now,
                ),
            )

    def get_sender_for_owner_message(self, owner_msg_id: int) -> int | None:
        """Return the original sender's Telegram user ID for a given owner message ID."""
        p = "%s" if self.is_postgres else "?"
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT sender_id FROM messages WHERE owner_msg_id = {p} ORDER BY id DESC LIMIT 1",
                (owner_msg_id,),
            ).fetchone()
            return int(row["sender_id"]) if row else None

    def is_blocked(self, user_id: int) -> bool:
        """Check whether a Telegram user ID is blocked."""
        p = "%s" if self.is_postgres else "?"
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT 1 FROM blocked_users WHERE user_id = {p}",
                (user_id,),
            ).fetchone()
            return row is not None

    def block_user(self, user_id: int) -> None:
        """Block a user ID from sending messages."""
        now = datetime.now(timezone.utc).isoformat()
        p = "%s" if self.is_postgres else "?"
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO blocked_users (user_id, blocked_at)
                VALUES ({p}, {p})
                ON CONFLICT(user_id) DO NOTHING
                """,
                (user_id, now),
            )

    def unblock_user(self, user_id: int) -> bool:
        """Unblock a user ID. Returns True if the user was previously blocked."""
        p = "%s" if self.is_postgres else "?"
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM blocked_users WHERE user_id = {p}",
                (user_id,),
            )
            return cursor.rowcount > 0

    def get_stats(self) -> tuple[int, int, int, int, int]:
        """Return (total_users, total_starts, total_messages, unique_senders, blocked_count)."""
        with self._connect() as conn:
            usr_row = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()
            str_row = conn.execute("SELECT COUNT(*) AS cnt FROM start_logs").fetchone()
            msg_row = conn.execute(
                "SELECT COUNT(*) AS total, COUNT(DISTINCT sender_id) AS unique_senders FROM messages"
            ).fetchone()
            blk_row = conn.execute("SELECT COUNT(*) AS blocked FROM blocked_users").fetchone()
            return (
                int(usr_row["cnt"]),
                int(str_row["cnt"]),
                int(msg_row["total"]),
                int(msg_row["unique_senders"]),
                int(blk_row["blocked"]),
            )

    def get_recent_users(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent users ordered by last_seen_at descending."""
        p = "%s" if self.is_postgres else "?"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT user_id, chat_id, username, full_name, first_seen_at,
                       last_seen_at, start_count, last_start_param
                FROM users
                ORDER BY last_seen_at DESC
                LIMIT {p}
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def export_all(self) -> dict[str, list[dict[str, Any]]]:
        """Export all database tables as JSON-serializable dictionaries."""
        with self._connect() as conn:
            return {
                "users": [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY last_seen_at DESC").fetchall()],
                "start_logs": [dict(r) for r in conn.execute("SELECT * FROM start_logs ORDER BY id DESC").fetchall()],
                "messages": [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY id DESC").fetchall()],
                "blocked_users": [dict(r) for r in conn.execute("SELECT * FROM blocked_users ORDER BY blocked_at DESC").fetchall()],
            }
