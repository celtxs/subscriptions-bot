import asyncio
import base64
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.bot import routers
from app.config import Settings
from app.db.connection import connect
from app.domain.crypto import EncryptionService
from app.repositories.subscriptions import SubscriptionRepository


NOW = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)


class FakeState:
    def __init__(self):
        self.data = {}
        self.current = None
        self.cleared = False

    async def update_data(self, **values):
        self.data.update(values)

    async def get_data(self):
        return dict(self.data)

    async def set_state(self, state):
        self.current = state

    async def clear(self):
        self.data.clear()
        self.current = None
        self.cleared = True


class RouterFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.con = connect(Path(self.temp.name) / "subscriptions.db")
        self.repo = SubscriptionRepository(self.con)
        self.key = b"k" * 32
        key_path = Path(self.temp.name) / "key"
        key_path.write_text(base64.urlsafe_b64encode(self.key).decode("ascii"), encoding="ascii")
        self.settings = Settings(
            bot_token="not-used",
            owner_telegram_id=42,
            db_path=Path(self.temp.name) / "subscriptions.db",
            encryption_key_path=key_path,
            timezone=timezone.utc,
            scheduler_interval_seconds=60,
            log_level="INFO",
        )
        self.subscription_id = self.repo.create(
            owner_id=42,
            name="VPS",
            category="SERVER",
            end_at=NOW + timedelta(days=3),
            recurrence=None,
            secret_payload=None,
            now=NOW,
        )

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def _callback(self, name):
        router = routers.build_router(self.settings, self.con)
        return next(handler.callback for handler in router.callback_query.handlers if handler.callback.__name__ == name)

    def _message(self, name):
        router = routers.build_router(self.settings, self.con)
        return next(handler.callback for handler in router.message.handlers if handler.callback.__name__ == name)

    def _query(self, data):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(
                chat=SimpleNamespace(type="private"),
                edit_text=AsyncMock(),
                answer=AsyncMock(),
            ),
            answer=AsyncMock(),
        )

    def test_card_offers_edit_replace_and_secret_actions_without_plaintext(self):
        row = self.repo.get(self.subscription_id, 42)
        text, keyboard = routers.card(row)
        callbacks = [button.callback_data for line in keyboard.inline_keyboard for button in line]
        self.assertIn(f"sub:edit:{self.subscription_id}:{row['record_version']}", callbacks)
        self.assertIn(f"sub:replace:{self.subscription_id}:{row['record_version']}", callbacks)
        self.assertIn(f"sub:secret:request:{self.subscription_id}:{row['record_version']}", callbacks)
        self.assertNotIn("password", text)

    def test_edit_value_updates_only_selected_field_and_clears_state(self):
        async def scenario():
            state = FakeState()
            row = self.repo.get(self.subscription_id, 42)
            state.data = {"edit_id": self.subscription_id, "edit_version": row["record_version"], "edit_field": "name"}
            message = SimpleNamespace(text="New VPS", answer=AsyncMock(), from_user=SimpleNamespace(id=42), chat=SimpleNamespace(type="private"))
            with patch.object(routers, "allowed", return_value=True):
                await self._message("edit_value")(message, state)
            self.assertEqual(self.repo.get(self.subscription_id, 42)["name"], "New VPS")
            self.assertTrue(state.cleared)

        asyncio.run(scenario())

    def test_replace_starts_a_new_non_secret_dialog(self):
        async def scenario():
            state = FakeState()
            row = self.repo.get(self.subscription_id, 42)
            query = self._query(f"sub:replace:{self.subscription_id}:{row['record_version']}")
            with patch.object(routers, "allowed", return_value=True):
                await self._callback("replace_start")(query, state)
            self.assertEqual(state.current, routers.ReplaceFlow.name)
            self.assertEqual(state.data, {"replace_id": self.subscription_id, "replace_version": row["record_version"]})
            self.assertIn("не копируются", query.message.edit_text.await_args.args[0])

        asyncio.run(scenario())

    def test_secret_needs_confirmation_and_reveals_only_the_selected_field(self):
        async def scenario():
            row = self.repo.get(self.subscription_id, 42)
            cipher, nonce = EncryptionService(self.key).encrypt(
                {"login": "owner", "password": "p@ss", "api_key": "api-value"},
                f"subscription:{self.subscription_id}:v1".encode(),
            )
            self.repo.update(self.subscription_id, 42, row["record_version"], {"secret_ciphertext": cipher, "secret_nonce": nonce, "encryption_key_version": 1}, NOW)
            row = self.repo.get(self.subscription_id, 42)
            request = self._query(f"sub:secret:request:{self.subscription_id}:{row['record_version']}")
            confirm = self._query(f"sub:secret:confirm:{self.subscription_id}:{row['record_version']}")
            reveal = self._query(f"sub:secret:show:{self.subscription_id}:{row['record_version']}:p")

            def discard(coro):
                coro.close()
                return None

            with patch.object(routers, "allowed", return_value=True), patch.object(routers.asyncio, "create_task", side_effect=discard):
                await self._callback("secret_request")(request)
                self.assertNotIn("p@ss", request.message.edit_text.await_args.args[0])
                await self._callback("secret_confirm")(confirm)
                self.assertNotIn("p@ss", confirm.message.edit_text.await_args.args[0])
                await self._callback("secret_show")(reveal)

            shown = reveal.message.answer.await_args.args[0]
            self.assertIn("p@ss", shown)
            self.assertNotIn("owner", shown)
            self.assertNotIn("api-value", shown)

        asyncio.run(scenario())

    def test_add_flow_encrypts_secrets_only_at_final_confirmation(self):
        async def scenario():
            state = FakeState()
            state.data = {
                "name": "AI service",
                "category": "AI",
                "end_at": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
            }

            def message(text):
                return SimpleNamespace(
                    text=text,
                    answer=AsyncMock(),
                    from_user=SimpleNamespace(id=42),
                    chat=SimpleNamespace(type="private"),
                )

            with patch.object(routers, "allowed", return_value=True):
                await self._message("add_recurrence")(message("1 MONTHS"), state)
                await self._message("add_cost")(message("NONE"), state)
                await self._message("add_currency")(message("NONE"), state)
                await self._message("add_url")(message("NONE"), state)
                await self._message("add_note")(message("NONE"), state)
                await self._message("add_login")(message("owner"), state)
                await self._message("add_password")(message("p@ss"), state)
                await self._message("add_api_key")(message("api-value"), state)
                await self._callback("add_confirm")(self._query("add:confirm"), state)

            row = self.con.execute("SELECT * FROM subscriptions WHERE name=?", ("AI service",)).fetchone()
            self.assertIsNotNone(row["secret_ciphertext"])
            payload = EncryptionService(self.key).decrypt(
                row["secret_ciphertext"], row["secret_nonce"], f"subscription:{row['id']}:v1".encode()
            )
            self.assertEqual(payload, {"login": "owner", "password": "p@ss", "api_key": "api-value"})
            self.assertIsNone(self.con.execute("SELECT * FROM dialog_sessions WHERE owner_telegram_id=42").fetchone())
            self.assertTrue(state.cleared)

        asyncio.run(scenario())

    def test_edit_secret_reencrypts_complete_payload_without_persisting_plaintext(self):
        async def scenario():
            cipher, nonce = EncryptionService(self.key).encrypt(
                {"login": "owner", "password": "old", "api_key": "api-value"},
                f"subscription:{self.subscription_id}:v1".encode(),
            )
            row = self.repo.get(self.subscription_id, 42)
            self.repo.update(
                self.subscription_id,
                42,
                row["record_version"],
                {"secret_ciphertext": cipher, "secret_nonce": nonce, "encryption_key_version": 1},
                NOW,
            )
            state = FakeState()
            row = self.repo.get(self.subscription_id, 42)
            with patch.object(routers, "allowed", return_value=True):
                await self._callback("edit_field")(
                    self._query(f"sub:edit-field:{self.subscription_id}:{row['record_version']}:password"), state
                )
                message = SimpleNamespace(
                    text="new-password",
                    answer=AsyncMock(),
                    from_user=SimpleNamespace(id=42),
                    chat=SimpleNamespace(type="private"),
                )
                await self._message("edit_value")(message, state)

            row = self.repo.get(self.subscription_id, 42)
            payload = EncryptionService(self.key).decrypt(
                row["secret_ciphertext"], row["secret_nonce"], f"subscription:{self.subscription_id}:v1".encode()
            )
            self.assertEqual(payload, {"login": "owner", "password": "new-password", "api_key": "api-value"})
            self.assertIsNone(self.con.execute("SELECT * FROM dialog_sessions WHERE owner_telegram_id=42").fetchone())

        asyncio.run(scenario())

    def test_edit_secret_can_create_payload_when_record_has_no_secrets(self):
        async def scenario():
            row = self.repo.get(self.subscription_id, 42)
            state = FakeState()
            with patch.object(routers, "allowed", return_value=True):
                await self._callback("edit_field")(
                    self._query(f"sub:edit-field:{self.subscription_id}:{row['record_version']}:login"), state
                )
                message = SimpleNamespace(
                    text="new-login",
                    answer=AsyncMock(),
                    delete=AsyncMock(),
                    from_user=SimpleNamespace(id=42),
                    chat=SimpleNamespace(type="private"),
                )
                await self._message("edit_value")(message, state)

            row = self.repo.get(self.subscription_id, 42)
            payload = EncryptionService(self.key).decrypt(
                row["secret_ciphertext"], row["secret_nonce"], f"subscription:{self.subscription_id}:v1".encode()
            )
            self.assertEqual(payload, {"login": "new-login", "password": None, "api_key": None})
            message.delete.assert_awaited_once_with()

        asyncio.run(scenario())

    def test_invalid_add_secret_is_deleted_before_validation_error(self):
        async def scenario():
            state = FakeState()
            message = SimpleNamespace(
                text="x" * 4097,
                answer=AsyncMock(),
                delete=AsyncMock(),
                from_user=SimpleNamespace(id=42),
                chat=SimpleNamespace(type="private"),
            )
            with patch.object(routers, "allowed", return_value=True):
                await self._message("add_password")(message, state)
            message.delete.assert_awaited_once_with()
            self.assertIn("слишком длинный", message.answer.await_args.args[0])

        asyncio.run(scenario())

    def test_invalid_edit_secret_is_deleted_before_validation_error(self):
        async def scenario():
            row = self.repo.get(self.subscription_id, 42)
            state = FakeState()
            state.data = {"edit_id": self.subscription_id, "edit_version": row["record_version"], "edit_field": "password"}
            message = SimpleNamespace(
                text="x" * 4097,
                answer=AsyncMock(),
                delete=AsyncMock(),
                from_user=SimpleNamespace(id=42),
                chat=SimpleNamespace(type="private"),
            )
            with patch.object(routers, "allowed", return_value=True):
                await self._message("edit_value")(message, state)
            message.delete.assert_awaited_once_with()
            self.assertIn("Не сохранено", message.answer.await_args.args[0])

        asyncio.run(scenario())

    def test_edit_end_rejects_stale_record_version(self):
        async def scenario():
            row = self.repo.get(self.subscription_id, 42)
            old_end = row["end_at_utc"]
            old_version = row["record_version"]
            self.repo.update(self.subscription_id, 42, old_version, {"name": "Changed elsewhere"}, NOW)
            future = (datetime.now(routers.MOSCOW) + timedelta(days=30)).replace(second=0, microsecond=0)
            state = FakeState()
            state.data = {"edit_id": self.subscription_id, "edit_version": old_version, "edit_field": "end"}
            message = SimpleNamespace(
                text=f"{future:%Y-%m-%d %H:%M}",
                answer=AsyncMock(),
                from_user=SimpleNamespace(id=42),
                chat=SimpleNamespace(type="private"),
            )
            with patch.object(routers, "allowed", return_value=True):
                await self._message("edit_value")(message, state)
            self.assertEqual(self.repo.get(self.subscription_id, 42)["end_at_utc"], old_end)
            self.assertIn("Не сохранено", message.answer.await_args.args[0])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
