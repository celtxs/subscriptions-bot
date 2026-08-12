import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.db.connection import connect
from app.db.schema import SCHEMA_VERSION


class SchemaMigrationTests(unittest.TestCase):
    def _create_legacy(self, path: Path, *, duplicate_reminders: bool = False) -> None:
        legacy = sqlite3.connect(path)
        legacy.executescript(
            """
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at_utc TEXT NOT NULL);
            INSERT INTO schema_migrations(version, applied_at_utc) VALUES (1, '2026-01-01T00:00:00+00:00');
            CREATE TABLE subscriptions (
                id TEXT PRIMARY KEY,
                owner_telegram_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL,
                end_at_utc TEXT NOT NULL,
                recurrence_value INTEGER,
                recurrence_unit TEXT,
                term_version INTEGER NOT NULL DEFAULT 1,
                record_version INTEGER NOT NULL DEFAULT 1,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );
            CREATE TABLE reminder_deliveries (
                id TEXT PRIMARY KEY,
                subscription_id TEXT NOT NULL,
                term_version INTEGER NOT NULL,
                kind TEXT NOT NULL,
                scheduled_at_utc TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );
            INSERT INTO subscriptions(id, owner_telegram_id, name, category, status, end_at_utc, recurrence_unit, created_at_utc, updated_at_utc)
            VALUES ('legacy-1', 42, 'Legacy VPS', 'SERVER', 'active', '2026-01-20T12:00:00+00:00', 'NONE', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
            """
        )
        reminder = (
            "r1", "legacy-1", 1, "BEFORE_24H",
            "2026-01-19T12:00:00+00:00", "pending",
            "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
        )
        legacy.execute(
            "INSERT INTO reminder_deliveries(id,subscription_id,term_version,kind,scheduled_at_utc,status,created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,?,?,?)",
            reminder,
        )
        if duplicate_reminders:
            legacy.execute(
                "INSERT INTO reminder_deliveries(id,subscription_id,term_version,kind,scheduled_at_utc,status,created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,?,?,?)",
                ("r2", *reminder[1:]),
            )
        legacy.commit()
        legacy.close()

    def test_legacy_schema_is_upgraded_without_losing_existing_subscription(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.db"
            self._create_legacy(path)

            con = connect(path)
            columns = {row[1] for row in con.execute("PRAGMA table_info(subscriptions)")}
            self.assertTrue({"cost_minor", "service_url", "note", "secret_ciphertext", "secret_nonce", "encryption_key_version", "replaced_from_id", "deactivation_reason", "deactivated_at_utc"}.issubset(columns))
            row = con.execute("SELECT name, owner_telegram_id, status, term_version, record_version FROM subscriptions WHERE id='legacy-1'").fetchone()
            self.assertEqual(tuple(row), ("Legacy VPS", 42, "active", 1, 1))
            subscription_fk = con.execute("PRAGMA foreign_key_list(subscriptions)").fetchall()
            reminder_fk = con.execute("PRAGMA foreign_key_list(reminder_deliveries)").fetchall()
            self.assertTrue(any(tuple(fk)[2:5] == ("subscriptions", "replaced_from_id", "id") for fk in subscription_fk))
            self.assertTrue(any(tuple(fk)[2:5] == ("subscriptions", "subscription_id", "id") for fk in reminder_fk))
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO reminder_deliveries(id,subscription_id,term_version,kind,scheduled_at_utc,status,created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,?,?,?)",
                    ("orphan", "missing", 1, "BEFORE_24H", "2026-01-02T00:00:00+00:00", "pending", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO subscriptions(id,owner_telegram_id,name,category,status,end_at_utc,recurrence_unit,created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("invalid", 42, "Invalid", "BROKEN", "active", "2026-01-20T12:00:00+00:00", "NONE", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
                )
            self.assertEqual(con.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], SCHEMA_VERSION)
            for table in ("settings", "scheduler_lease", "dialog_sessions", "audit_events"):
                self.assertIsNotNone(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())
            con.close()

            con = connect(path)
            self.assertEqual(con.execute("SELECT count(*) FROM reminder_deliveries").fetchone()[0], 1)
            con.close()

    def test_duplicate_legacy_reminders_fail_closed(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "duplicate.db"
            self._create_legacy(path, duplicate_reminders=True)
            with self.assertRaisesRegex(RuntimeError, "duplicate reminder deliveries"):
                connect(path)
            verify = sqlite3.connect(path)
            self.assertEqual(verify.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 1)
            verify.close()

    def test_newer_schema_version_fails_closed(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "future.db"
            legacy = sqlite3.connect(path)
            legacy.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at_utc TEXT NOT NULL)")
            legacy.execute("INSERT INTO schema_migrations(version, applied_at_utc) VALUES (?, ?)", (SCHEMA_VERSION + 1, "2026-01-01T00:00:00+00:00"))
            legacy.commit()
            legacy.close()
            with self.assertRaisesRegex(RuntimeError, "newer than application"):
                connect(path)


if __name__ == "__main__":
    unittest.main()
