import base64
import contextlib
import io
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.db.connection import connect
from app.repositories.subscriptions import SubscriptionRepository


class HealthcheckTests(unittest.TestCase):
    def _run(self, db_path: Path, key_path: Path):
        env = {
            "BOT_TOKEN": "offline-test-token",
            "OWNER_TELEGRAM_ID": "42",
            "DB_PATH": str(db_path),
            "ENCRYPTION_KEY_PATH": str(key_path),
            "TIMEZONE": "Europe/Moscow",
            "SCHEDULER_INTERVAL_SECONDS": "60",
            "LOG_LEVEL": "INFO",
        }
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(output):
            import app.healthcheck as healthcheck
            healthcheck.main()
        return output.getvalue()

    def _fixtures(self):
        temp = TemporaryDirectory()
        root = Path(temp.name)
        db_path = root / "subscriptions.db"
        key_path = root / "encryption.key"
        key_path.write_text(base64.urlsafe_b64encode(b"k" * 32).decode("ascii"), encoding="ascii")
        return temp, db_path, key_path

    def test_missing_heartbeat_is_not_healthy(self):
        temp, db_path, key_path = self._fixtures()
        self.addCleanup(temp.cleanup)
        connect(db_path).close()
        with self.assertRaises(SystemExit) as raised:
            self._run(db_path, key_path)
        self.assertEqual(raised.exception.code, 1)

    def test_stale_heartbeat_is_not_healthy(self):
        temp, db_path, key_path = self._fixtures()
        self.addCleanup(temp.cleanup)
        con = connect(db_path)
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        SubscriptionRepository(con).set_setting("last_scheduler_tick_utc", old.isoformat(), old)
        con.close()
        with self.assertRaises(SystemExit) as raised:
            self._run(db_path, key_path)
        self.assertEqual(raised.exception.code, 1)

    def test_fresh_heartbeat_is_healthy(self):
        temp, db_path, key_path = self._fixtures()
        self.addCleanup(temp.cleanup)
        con = connect(db_path)
        current = datetime.now(timezone.utc)
        repo = SubscriptionRepository(con)
        repo.acquire_scheduler_lease("test", current, 90)
        repo.set_setting("last_scheduler_tick_utc", current.isoformat(), current)
        con.close()
        self.assertEqual(self._run(db_path, key_path), "healthy\n")

    def test_missing_scheduler_lease_is_not_healthy(self):
        temp, db_path, key_path = self._fixtures()
        self.addCleanup(temp.cleanup)
        con = connect(db_path)
        current = datetime.now(timezone.utc)
        SubscriptionRepository(con).set_setting("last_scheduler_tick_utc", current.isoformat(), current)
        con.close()
        with self.assertRaises(SystemExit) as raised:
            self._run(db_path, key_path)
        self.assertEqual(raised.exception.code, 1)

    def test_future_heartbeat_is_not_healthy(self):
        temp, db_path, key_path = self._fixtures()
        self.addCleanup(temp.cleanup)
        con = connect(db_path)
        current = datetime.now(timezone.utc)
        repo = SubscriptionRepository(con)
        repo.acquire_scheduler_lease("test", current, 90)
        repo.set_setting("last_scheduler_tick_utc", (current + timedelta(hours=1)).isoformat(), current)
        con.close()
        with self.assertRaises(SystemExit) as raised:
            self._run(db_path, key_path)
        self.assertEqual(raised.exception.code, 1)

    def test_naive_heartbeat_is_not_healthy(self):
        temp, db_path, key_path = self._fixtures()
        self.addCleanup(temp.cleanup)
        con = connect(db_path)
        current = datetime.now(timezone.utc)
        repo = SubscriptionRepository(con)
        repo.acquire_scheduler_lease("test", current, 90)
        repo.set_setting("last_scheduler_tick_utc", current.replace(tzinfo=None).isoformat(), current)
        con.close()
        with self.assertRaises(SystemExit) as raised:
            self._run(db_path, key_path)
        self.assertEqual(raised.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
