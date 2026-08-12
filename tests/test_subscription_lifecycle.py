import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.db.connection import connect
from app.domain.calendar import Recurrence
from app.repositories.subscriptions import SubscriptionRepository, StaleRecordError
from app.services.reminder_service import ReminderService

NOW = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)


class SubscriptionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.con = connect(Path(self.temp.name) / "subscriptions.db")
        self.repo = SubscriptionRepository(self.con)
        self.subscription_id = self.repo.create(
            owner_id=42,
            name="VPS",
            category="SERVER",
            end_at=NOW + timedelta(days=3),
            recurrence=Recurrence(1, "MONTHS"),
            secret_payload=None,
            now=NOW,
        )

    def tearDown(self) -> None:
        self.con.close()
        self.temp.cleanup()

    def test_create_is_atomic_and_creates_unique_reminders(self) -> None:
        rows = self.con.execute(
            "SELECT kind, status FROM reminder_deliveries WHERE subscription_id=? ORDER BY kind",
            (self.subscription_id,),
        ).fetchall()
        self.assertEqual([(row["kind"], row["status"]) for row in rows], [("BEFORE_24H", "pending"), ("BEFORE_48H", "pending")])
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO reminder_deliveries(id, subscription_id, term_version, kind, scheduled_at_utc, status, attempt_count, created_at_utc, updated_at_utc) VALUES ('duplicate', ?, 1, 'BEFORE_24H', ?, 'pending', 0, ?, ?)",
                (self.subscription_id, NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
            )

    def test_renew_cancels_old_term_and_stale_callback_cannot_apply_twice(self) -> None:
        record = self.repo.get(self.subscription_id, 42)
        new_end = self.repo.renew(self.subscription_id, 42, record["term_version"], Recurrence(1, "MONTHS"), NOW)
        self.assertEqual(new_end, datetime(2026, 2, 13, 12, tzinfo=timezone.utc))
        old = self.con.execute("SELECT status FROM reminder_deliveries WHERE subscription_id=? AND term_version=1", (self.subscription_id,)).fetchall()
        self.assertEqual({row["status"] for row in old}, {"cancelled"})
        self.assertEqual(self.con.execute("SELECT count(*) FROM reminder_deliveries WHERE subscription_id=? AND term_version=2", (self.subscription_id,)).fetchone()[0], 2)
        with self.assertRaises(StaleRecordError):
            self.repo.renew(self.subscription_id, 42, 1, Recurrence(1, "MONTHS"), NOW)

    def test_replace_is_atomic_and_second_callback_is_stale(self) -> None:
        record = self.repo.get(self.subscription_id, 42)
        new_id = self.repo.replace(
            self.subscription_id, 42, record["record_version"], name="New VPS", category="SERVER", end_at=NOW + timedelta(days=5), recurrence=None, secret_payload=None, now=NOW
        )
        old = self.repo.get(self.subscription_id, 42)
        new = self.repo.get(new_id, 42)
        self.assertEqual(old["status"], "inactive")
        self.assertEqual(old["deactivation_reason"], "replaced")
        self.assertEqual(new["replaced_from_id"], self.subscription_id)
        with self.assertRaises(StaleRecordError):
            self.repo.replace(self.subscription_id, 42, record["record_version"], name="x", category="OTHER", end_at=NOW + timedelta(days=6), recurrence=None, secret_payload=None, now=NOW)

    def test_deactivate_and_delete_are_idempotent(self) -> None:
        record = self.repo.get(self.subscription_id, 42)
        self.assertTrue(self.repo.deactivate(self.subscription_id, 42, record["record_version"], "manual", NOW))
        self.assertFalse(self.repo.deactivate(self.subscription_id, 42, record["record_version"] + 1, "manual", NOW))
        self.assertEqual({row[0] for row in self.con.execute("SELECT status FROM reminder_deliveries WHERE subscription_id=?", (self.subscription_id,))}, {"cancelled"})
        self.assertTrue(self.repo.delete(self.subscription_id, 42, NOW))
        self.assertFalse(self.repo.delete(self.subscription_id, 42, NOW))

    def test_expiry_precedes_due_processing(self) -> None:
        expired = self.repo.create(owner_id=42, name="Old", category="AI", end_at=NOW - timedelta(minutes=1), recurrence=None, secret_payload=None, now=NOW - timedelta(days=3))
        service = ReminderService(self.repo, bot=None, owner_id=42, instance_id="test")
        service.tick_at(NOW)
        self.assertEqual(self.repo.get(expired, 42)["status"], "inactive")
        self.assertEqual({row[0] for row in self.con.execute("SELECT status FROM reminder_deliveries WHERE subscription_id=?", (expired,))}, {"cancelled"})

    def test_due_claim_is_exclusive_and_expired_claim_is_unknown(self) -> None:
        service = ReminderService(self.repo, bot=None, owner_id=42, instance_id="test")
        due = NOW
        self.con.execute("UPDATE reminder_deliveries SET scheduled_at_utc=? WHERE subscription_id=? AND kind='BEFORE_48H'", (due.isoformat(), self.subscription_id))
        first = self.repo.claim_one_due(due, 120)
        second = self.repo.claim_one_due(due, 120)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.repo.recover_expired_claims(NOW + timedelta(seconds=121))
        self.assertEqual(self.con.execute("SELECT status FROM reminder_deliveries WHERE id=?", (first["id"],)).fetchone()[0], "unknown")

    def test_scheduler_lease_allows_only_one_holder(self) -> None:
        self.assertTrue(self.repo.acquire_scheduler_lease("first", NOW, 90))
        self.assertFalse(self.repo.acquire_scheduler_lease("second", NOW, 90))
        self.assertTrue(self.repo.acquire_scheduler_lease("second", NOW + timedelta(seconds=91), 90))

    def test_create_and_replace_roll_back_all_state_on_reminder_failure(self) -> None:
        with patch("app.repositories.subscriptions._reminders", side_effect=RuntimeError("reminder failure")):
            with self.assertRaises(RuntimeError):
                self.repo.create(
                    owner_id=42,
                    name="Atomic create",
                    category="AI",
                    end_at=NOW + timedelta(days=5),
                    recurrence=None,
                    secret_payload=None,
                    now=NOW,
                )
        self.assertIsNone(self.con.execute("SELECT 1 FROM subscriptions WHERE name='Atomic create'").fetchone())

        original = self.repo.get(self.subscription_id, 42)
        with patch("app.repositories.subscriptions._reminders", side_effect=RuntimeError("reminder failure")):
            with self.assertRaises(RuntimeError):
                self.repo.replace(
                    self.subscription_id,
                    42,
                    original["record_version"],
                    name="Atomic replacement",
                    category="SERVER",
                    end_at=NOW + timedelta(days=5),
                    recurrence=None,
                    secret_payload=None,
                    now=NOW,
                )
        current = self.repo.get(self.subscription_id, 42)
        self.assertEqual(current["status"], "active")
        self.assertEqual(current["record_version"], original["record_version"])
        self.assertIsNone(self.con.execute("SELECT 1 FROM subscriptions WHERE name='Atomic replacement'").fetchone())

    def test_replace_rolls_back_when_outer_audit_fails_after_nested_create(self) -> None:
        original = self.repo.get(self.subscription_id, 42)
        calls = 0

        def fail_outer_audit(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("outer audit failure")

        with patch.object(self.repo, "_audit", side_effect=fail_outer_audit):
            with self.assertRaises(RuntimeError):
                self.repo.replace(
                    self.subscription_id,
                    42,
                    original["record_version"],
                    name="Audit replacement",
                    category="SERVER",
                    end_at=NOW + timedelta(days=5),
                    recurrence=None,
                    secret_payload=None,
                    now=NOW,
                )
        current = self.repo.get(self.subscription_id, 42)
        self.assertEqual(current["status"], "active")
        self.assertIsNone(self.con.execute("SELECT 1 FROM subscriptions WHERE name='Audit replacement'").fetchone())

    def test_delete_of_replaced_source_is_rejected_without_integrity_error(self) -> None:
        original = self.repo.get(self.subscription_id, 42)
        replacement_id = self.repo.replace(
            self.subscription_id,
            42,
            original["record_version"],
            name="Replacement",
            category="SERVER",
            end_at=NOW + timedelta(days=5),
            recurrence=None,
            secret_payload=None,
            now=NOW,
        )

        self.assertTrue(replacement_id)
        self.assertFalse(self.repo.delete(self.subscription_id, 42, NOW))
        self.assertIsNotNone(self.repo.get(self.subscription_id, 42))


if __name__ == "__main__":
    unittest.main()
