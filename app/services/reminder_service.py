from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.repositories.subscriptions import SubscriptionRepository


class ReminderService:
    def __init__(self, repo: SubscriptionRepository, bot, owner_id: int, instance_id: str, max_attempts: int = 3, retry_max_seconds: int = 3600) -> None:
        if max_attempts < 1 or retry_max_seconds < 1:
            raise ValueError("invalid reminder retry policy")
        self.repo, self.bot, self.owner_id, self.instance_id = repo, bot, owner_id, instance_id
        self.max_attempts, self.retry_max_seconds = max_attempts, retry_max_seconds
        self._admitted = False

    def tick_at(self, now: datetime) -> bool:
        self._admitted = self.repo.acquire_scheduler_lease(self.instance_id, now, 90)
        if not self._admitted:
            return False
        self.repo.recover_expired_claims(now)
        self.repo.expire_and_cancel(now)
        self.repo.coalesce_due(now)
        self.repo.set_setting("last_scheduler_tick_utc", now.isoformat(), now)
        return True

    async def process_due(self, now: datetime, *, live_clock: bool = False) -> None:
        if not self._admitted or self.bot is None:
            return
        def current_now() -> datetime:
            return datetime.now(timezone.utc) if live_clock else now

        while self.repo.scheduler_lease_owned(self.instance_id, current_now()):
            claim_now = current_now()
            if not self.repo.renew_scheduler_lease(self.instance_id, claim_now, 90):
                return
            reminder = self.repo.claim_one_due(claim_now, 120, self.instance_id)
            if reminder is None:
                return
            if not self.repo.scheduler_lease_owned(self.instance_id, current_now()):
                return
            subscription = self.repo.con.execute("SELECT * FROM subscriptions WHERE id=? AND status='active' AND term_version=?", (reminder["subscription_id"], reminder["term_version"])).fetchone()
            if not subscription:
                self.repo.mark_unknown(reminder["id"], "STATE_CHANGED_AFTER_CLAIM", now)
                continue
            try:
                end_at = datetime.fromisoformat(subscription["end_at_utc"]).astimezone(ZoneInfo("Europe/Moscow"))
                text = f"Напоминание {reminder['kind']}: {subscription['name']} ({subscription['category']})\nОкончание: {end_at:%d.%m.%Y %H:%M} MSK"
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Продлить", callback_data=f"sub:renew:{subscription['id']}:{subscription['term_version']}"), InlineKeyboardButton(text="Открыть", callback_data=f"sub:view:{subscription['id']}")]])
                message = await asyncio.wait_for(
                    self.bot.send_message(self.owner_id, text, reply_markup=keyboard),
                    timeout=60,
                )
                final_now = current_now()
                if self.repo.scheduler_lease_owned(self.instance_id, final_now):
                    self.repo.mark_sent(reminder["id"], message.message_id, final_now)
            except TelegramRetryAfter as exc:
                final_now = current_now()
                if not self.repo.scheduler_lease_owned(self.instance_id, final_now):
                    return
                if reminder["attempt_count"] >= self.max_attempts:
                    self.repo.mark_failed(reminder["id"], "RATE_LIMIT_ATTEMPTS_EXHAUSTED", final_now)
                else:
                    delay = min(max(1, int(exc.retry_after)), self.retry_max_seconds)
                    self.repo.mark_retryable(reminder["id"], "RATE_LIMIT", final_now + timedelta(seconds=delay), final_now)
            except (TelegramServerError, TelegramNetworkError):
                final_now = current_now()
                if not self.repo.scheduler_lease_owned(self.instance_id, final_now):
                    return
                if reminder["attempt_count"] >= self.max_attempts:
                    self.repo.mark_failed(reminder["id"], "TEMPORARY_ATTEMPTS_EXHAUSTED", final_now)
                else:
                    delay = min(30 * (2 ** (reminder["attempt_count"] - 1)), self.retry_max_seconds)
                    self.repo.mark_retryable(reminder["id"], "TEMPORARY_TELEGRAM", final_now + timedelta(seconds=delay), final_now)
            except (TelegramBadRequest, TelegramForbiddenError):
                final_now = current_now()
                if self.repo.scheduler_lease_owned(self.instance_id, final_now):
                    self.repo.mark_failed(reminder["id"], "PERMANENT_TELEGRAM", final_now)
            except Exception:
                final_now = current_now()
                if self.repo.scheduler_lease_owned(self.instance_id, final_now):
                    self.repo.mark_unknown(reminder["id"], "AMBIGUOUS_SEND_RESULT", final_now)

    async def tick(self) -> None:
        now = datetime.now(timezone.utc)
        if not self.tick_at(now):
            return
        await self.process_due(now, live_clock=True)
