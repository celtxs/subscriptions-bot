import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from aiogram import Bot, Dispatcher

from app.bot.auth import allowed
from app.bot.routers import AddFlow, EditFlow
from app.bot.session_restore import restore_dialog_session
from app.db.connection import connect
from app.repositories.sessions import DialogSessionRepository


class AuthSessionTests(unittest.TestCase):
    def test_numeric_owner_and_private_chat_required(self):
        event = SimpleNamespace(
            from_user=SimpleNamespace(id=7),
            chat=SimpleNamespace(type="private"),
        )
        self.assertTrue(allowed(event, 7))
        self.assertFalse(allowed(event, 8))
        event.chat.type = "group"
        self.assertFalse(allowed(event, 7))

    def test_session_persists_only_non_secret_payload_and_expires(self):
        with TemporaryDirectory() as tmp:
            repo = DialogSessionRepository(connect(Path(tmp) / "x.db"))
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            repo.save(7, "add", "date", {"name": "X", "category": "AI"}, now, 60)
            row = repo.load(7, now + timedelta(seconds=30))
            self.assertEqual(row["current_step"], "date")
            self.assertNotIn("password", row["non_secret_payload"])
            self.assertIsNone(repo.load(7, now + timedelta(seconds=61)))

    def test_secret_keys_are_rejected_from_persistent_dialog_state(self):
        with TemporaryDirectory() as tmp:
            repo = DialogSessionRepository(connect(Path(tmp) / "x.db"))
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            with self.assertRaises(ValueError):
                repo.save(7, "add", "password", {"password": "secret"}, now, 60)

    def test_unknown_dialog_fields_are_rejected(self):
        with TemporaryDirectory() as tmp:
            repo = DialogSessionRepository(connect(Path(tmp) / "x.db"))
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            with self.assertRaises(ValueError):
                repo.save(7, "add", "name", {"unexpected": "secret"}, now, 60)

    def test_non_secret_dialog_state_is_restored_after_restart(self):
        async def scenario():
            with TemporaryDirectory() as tmp:
                repo = DialogSessionRepository(connect(Path(tmp) / "x.db"))
                now = datetime(2026, 1, 1, tzinfo=timezone.utc)
                repo.save(
                    7,
                    "add",
                    "currency",
                    {"name": "X", "category": "AI", "date": "2026-01-10"},
                    now,
                    60,
                )
                dispatcher = Dispatcher()
                bot = Bot("123456:ABCDEF")
                self.assertTrue(
                    await restore_dialog_session(
                        dispatcher, bot, 7, repo, now + timedelta(seconds=30)
                    )
                )
                context = dispatcher.fsm.get_context(bot=bot, chat_id=7, user_id=7)
                self.assertEqual(await context.get_state(), AddFlow.currency.state)
                self.assertEqual(
                    await context.get_data(),
                    {"name": "X", "category": "AI", "date": "2026-01-10"},
                )

                repo.clear(7)
                repo.save(
                    7,
                    "edit",
                    "value",
                    {"subscription_id": "sid", "record_version": 3, "field": "password"},
                    now,
                    60,
                )
                dispatcher = Dispatcher()
                self.assertTrue(
                    await restore_dialog_session(
                        dispatcher, bot, 7, repo, now + timedelta(seconds=30)
                    )
                )
                context = dispatcher.fsm.get_context(bot=bot, chat_id=7, user_id=7)
                self.assertEqual(await context.get_state(), EditFlow.value.state)
                self.assertEqual(
                    await context.get_data(),
                    {"edit_id": "sid", "edit_version": 3, "edit_field": "password"},
                )
                repo.clear(7)
                repo.con.execute(
                    "INSERT INTO dialog_sessions(owner_telegram_id,flow_name,current_step,non_secret_payload,created_at,updated_at,expires_at) VALUES (?,?,?,?,?,?,?)",
                    (7, "add", "name", "{broken", now.isoformat(), now.isoformat(), (now + timedelta(seconds=60)).isoformat()),
                )
                dispatcher = Dispatcher()
                self.assertFalse(
                    await restore_dialog_session(
                        dispatcher, bot, 7, repo, now + timedelta(seconds=30)
                    )
                )
                self.assertIsNone(repo.load(7, now + timedelta(seconds=30)))
                await bot.session.close()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
