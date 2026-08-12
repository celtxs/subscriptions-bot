from __future__ import annotations
import asyncio, logging
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.bot.routers import build_router
from app.config import Settings
from app.db.connection import connect
from app.logging import configure
from app.services.reminder_service import ReminderService
from app.repositories.subscriptions import SubscriptionRepository
from app.repositories.sessions import DialogSessionRepository
from app.bot.session_restore import restore_dialog_session
import uuid
async def main():
    settings=Settings.from_env(); configure(settings.log_level); log=logging.getLogger(__name__)
    con=connect(settings.db_path); settings.encryption_key(); log.info("database_ready",extra={"event":"database_ready"})
    bot=Bot(settings.bot_token); dispatcher=Dispatcher(); dispatcher.include_router(build_router(settings,con))
    await restore_dialog_session(dispatcher, bot, settings.owner_telegram_id, DialogSessionRepository(con), datetime.now(timezone.utc))
    scheduler=AsyncIOScheduler(timezone="UTC"); service=ReminderService(SubscriptionRepository(con), bot, settings.owner_telegram_id, str(uuid.uuid4()))
    scheduler.add_job(service.tick,"interval",seconds=settings.scheduler_interval_seconds,max_instances=1,coalesce=True)
    scheduler.start(); log.info("scheduler_started",extra={"event":"scheduler_started"})
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False); await bot.session.close(); con.close()
if __name__ == "__main__": asyncio.run(main())
