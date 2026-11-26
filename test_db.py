"""Unit tests for db.py and message formatting."""

import tempfile
import unittest
from pathlib import Path

from telegram import User

from bot import format_sender_header
from db import BotDatabase


class TestBotDatabase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test.db"
        self.db = BotDatabase(self.db_path, database_url="")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_record_start_and_users(self) -> None:
        c1 = self.db.record_start(555, 555, "alice", "Alice", start_param="ref_6261812670")
        c2 = self.db.record_start(555, 555, "alice_new", "Alice B", start_param="ref_6261812670")
        self.assertEqual(c1, 1)
        self.assertEqual(c2, 2)
        users = self.db.get_recent_users()
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["username"], "alice_new")
        self.assertEqual(users[0]["start_count"], 2)

    def test_record_and_lookup_sender(self) -> None:
        self.db.record_message(
            owner_msg_id=101,
            sender_id=555,
            sender_username="alice",
            sender_name="Alice",
            chat_id=555,
            content_preview="hello",
        )
        self.assertEqual(self.db.get_sender_for_owner_message(101), 555)
        self.assertIsNone(self.db.get_sender_for_owner_message(999))

    def test_block_and_unblock(self) -> None:
        self.assertFalse(self.db.is_blocked(42))
        self.db.block_user(42)
        self.assertTrue(self.db.is_blocked(42))
        self.assertTrue(self.db.unblock_user(42))
        self.assertFalse(self.db.is_blocked(42))
        self.assertFalse(self.db.unblock_user(42))

    def test_stats_and_export(self) -> None:
        self.db.record_start(10, 10, "u1", "User 1", "ref_1")
        self.db.record_message(1, 10, "u1", "User 1", chat_id=10, content_preview="hi")
        self.db.record_message(2, 10, "u1", "User 1", chat_id=10, content_preview="yo")
        self.db.record_message(3, 20, None, "User 2", chat_id=20, content_preview="hey")
        self.db.block_user(99)
        self.assertEqual(self.db.get_stats(), (2, 1, 3, 2, 1))
        exported = self.db.export_all()
        self.assertEqual(len(exported["users"]), 2)
        self.assertEqual(len(exported["start_logs"]), 1)
        self.assertEqual(len(exported["messages"]), 3)
        self.assertEqual(len(exported["blocked_users"]), 1)

    def test_format_sender_header_escapes_html(self) -> None:
        user = User(id=777, first_name="<Evil>", last_name="&Co", is_bot=False, username="evil_user")
        header = format_sender_header(user, chat_id=888)
        self.assertIn("&lt;Evil&gt; &amp;Co", header)
        self.assertIn("@evil_user", header)
        self.assertIn("777", header)
        self.assertIn("888", header)


if __name__ == "__main__":
    unittest.main()
