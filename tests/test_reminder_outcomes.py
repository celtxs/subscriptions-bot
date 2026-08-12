import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.db.connection import connect
from app.repositories.subscriptions import SubscriptionRepository
from app.services.reminder_service import ReminderService

NOW = datetime(2026, 2, 1, 12, tzinfo=timezone.utc)

class Message: message_id = 77
class OkBot:
    async def send_message(self, *args, **kwargs): return Message()
class FailBot:
    async def send_message(self, *args, **kwargs): raise RuntimeError('ambiguous')

class RateLimitBot:
    def __init__(self, retry_after=5): self.retry_after = retry_after
    async def send_message(self, *args, **kwargs):
        from aiogram.exceptions import TelegramRetryAfter
        raise TelegramRetryAfter(method=None, message="retry", retry_after=self.retry_after)

class BadRequestBot:
    async def send_message(self, *args, **kwargs):
        from aiogram.exceptions import TelegramBadRequest
        raise TelegramBadRequest(method=None, message="bad request")

class LeaseStealingBot:
    def __init__(self, repo): self.repo = repo
    async def send_message(self, *args, **kwargs):
        self.repo.acquire_scheduler_lease('second', NOW + timedelta(seconds=91), 90)
        return Message()

class ReminderOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.con=connect(Path(self.tmp.name)/'x.db'); self.repo=SubscriptionRepository(self.con)
        self.sub=self.repo.create(owner_id=1,name='X',category='AI',end_at=NOW+timedelta(days=3),recurrence=None,secret_payload=None,now=NOW)
        self.con.execute("UPDATE reminder_deliveries SET scheduled_at_utc=? WHERE subscription_id=? AND kind='BEFORE_48H'",(NOW.isoformat(),self.sub))
    def tearDown(self): self.con.close(); self.tmp.cleanup()
    def state(self): return self.con.execute("SELECT status FROM reminder_deliveries WHERE subscription_id=? AND kind='BEFORE_48H'",(self.sub,)).fetchone()[0]
    def test_success_is_sent_once(self):
        service=ReminderService(self.repo,OkBot(),1,'a'); service.tick_at(NOW); asyncio.run(service.process_due(NOW)); self.assertEqual(self.state(),'sent')
        service=ReminderService(self.repo,OkBot(),1,'b'); service.tick_at(NOW+timedelta(minutes=1)); asyncio.run(service.process_due(NOW+timedelta(minutes=1))); self.assertEqual(self.state(),'sent')
    def test_ambiguous_result_is_unknown_without_retry(self):
        service=ReminderService(self.repo,FailBot(),1,'a'); service.tick_at(NOW); asyncio.run(service.process_due(NOW)); self.assertEqual(self.state(),'unknown')
    def test_second_lease_holder_does_not_send(self):
        self.assertTrue(self.repo.acquire_scheduler_lease('first',NOW,90)); service=ReminderService(self.repo,OkBot(),1,'second'); service.tick_at(NOW); asyncio.run(service.process_due(NOW)); self.assertEqual(self.state(),'pending')

    def test_retryable_delivery_becomes_failed_after_bounded_attempts(self):
        service=ReminderService(self.repo,RateLimitBot(),1,'a',max_attempts=2)
        service.tick_at(NOW)
        asyncio.run(service.process_due(NOW))
        self.assertEqual(self.state(),'retryable')
        asyncio.run(service.process_due(NOW+timedelta(minutes=1)))
        self.assertEqual(self.state(),'failed')

    def test_non_holder_does_not_recover_an_expired_claim(self):
        self.assertTrue(self.repo.acquire_scheduler_lease('first', NOW, 300))
        first = self.repo.claim_one_due(NOW, 120)
        self.assertIsNotNone(first)
        service = ReminderService(self.repo, OkBot(), 1, 'second')
        self.assertFalse(service.tick_at(NOW + timedelta(seconds=121)))
        self.assertEqual(self.con.execute("SELECT status FROM reminder_deliveries WHERE id=?", (first['id'],)).fetchone()[0], 'claimed')

    def test_deterministic_telegram_rejection_is_failed_not_ambiguous(self):
        service = ReminderService(self.repo, BadRequestBot(), 1, 'a')
        service.tick_at(NOW)
        asyncio.run(service.process_due(NOW))
        self.assertEqual(self.state(), 'failed')

    def test_service_stops_claiming_after_scheduler_lease_is_lost(self):
        service = ReminderService(self.repo, OkBot(), 1, 'first')
        self.assertTrue(service.tick_at(NOW))
        self.assertTrue(self.repo.acquire_scheduler_lease('second', NOW + timedelta(seconds=91), 90))
        asyncio.run(service.process_due(NOW + timedelta(seconds=91)))
        self.assertEqual(self.state(), 'pending')

    def test_send_result_is_not_finalized_after_scheduler_lease_loss(self):
        service = ReminderService(self.repo, LeaseStealingBot(self.repo), 1, 'first')
        self.assertTrue(service.tick_at(NOW))
        asyncio.run(service.process_due(NOW))
        self.assertEqual(self.state(), 'claimed')

if __name__=='__main__': unittest.main()
