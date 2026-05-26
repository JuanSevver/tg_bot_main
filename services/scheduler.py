"""
APScheduler-based jobs:

1. Retention для parsed_messages: ежедневный DELETE старше N дней.
   Без него таблица растёт без ограничений → SQLite тормозит на дедуп-запросах.
2. Subscription expiry notifications: предупреждение пользователю за 24ч,
   за 1ч и факт истечения подписки. Раньше юзер просто переставал получать
   сообщения и не понимал почему — теряется конверсия в продление.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, delete
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from database.db import async_session
from database.models import ParsedMessage, Subscription, User

logger = logging.getLogger(__name__)

PARSED_MESSAGES_RETENTION_DAYS = 30


class JobScheduler:
    def __init__(self) -> None:
        self._bot: Bot | None = None
        self._scheduler: AsyncIOScheduler | None = None
        # Чтобы не слать одно и то же предупреждение каждый час: храним
        # для каждого user_id timestamp последней нотификации каждого типа.
        # In-memory: при рестарте худшее что произойдёт — одна дополнительная
        # нотификация, что приемлемо.
        self._notified_24h: set[int] = set()
        self._notified_1h: set[int] = set()
        self._notified_expired: set[int] = set()

    def set_bot(self, bot: Bot) -> None:
        self._bot = bot

    def start(self) -> None:
        if self._scheduler is not None:
            return
        sched = AsyncIOScheduler()
        # Раз в день в 03:00 UTC чистим parsed_messages.
        sched.add_job(self._cleanup_parsed_messages, "cron", hour=3, minute=0)
        # Каждые 15 минут — проверяем подписки.
        sched.add_job(self._check_subscriptions, "interval", minutes=15)
        sched.start()
        self._scheduler = sched
        logger.info("Scheduler started: retention + subscription expiry checks.")

    def stop(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    async def _cleanup_parsed_messages(self) -> None:
        cutoff = datetime.utcnow() - timedelta(days=PARSED_MESSAGES_RETENTION_DAYS)
        async with async_session() as db:
            result = await db.execute(
                delete(ParsedMessage).where(ParsedMessage.parsed_at < cutoff)
            )
            await db.commit()
            try:
                rows = result.rowcount or 0
            except Exception:
                rows = 0
            logger.info("Retention: removed %d parsed_messages older than %sd.",
                        rows, PARSED_MESSAGES_RETENTION_DAYS)

    async def _check_subscriptions(self) -> None:
        if not self._bot:
            return
        now = datetime.utcnow()
        async with async_session() as db:
            # Берём всех у кого подписка либо в ближайшие 24ч истекает,
            # либо истекла недавно (последние 24ч) и юзера ещё не уведомили.
            window_high = now + timedelta(hours=25)
            window_low = now - timedelta(hours=24)
            result = await db.execute(
                select(Subscription, User).join(User, User.id == Subscription.user_id).where(
                    Subscription.expires_at >= window_low,
                    Subscription.expires_at <= window_high,
                )
            )
            rows = result.all()

        for sub, user in rows:
            if user.is_blocked:
                continue
            await self._maybe_notify(sub, user, now)

    async def _maybe_notify(self, sub: Subscription, user: User, now: datetime) -> None:
        until_expiry = sub.expires_at - now
        if timedelta(hours=23) < until_expiry <= timedelta(hours=25):
            await self._send(user.id, self._notified_24h,
                             "⏰ <b>Подписка истекает через 24 часа</b>\n\n"
                             "Чтобы лента не отключилась — продлите в разделе «Купить подписку».")
        elif timedelta(0) < until_expiry <= timedelta(hours=1, minutes=15):
            await self._send(user.id, self._notified_1h,
                             "⏰ <b>Подписка истекает в ближайший час</b>\n\n"
                             "Продлите её, чтобы лента не отключилась.")
        elif until_expiry <= timedelta(0) and until_expiry > timedelta(hours=-24):
            await self._send(user.id, self._notified_expired,
                             "🔕 <b>Подписка истекла</b>\n\n"
                             "Лента запросов выключена. Возобновите подписку в разделе «Купить подписку».")

    async def _send(self, user_id: int, tracker: set[int], text: str) -> None:
        if user_id in tracker:
            return
        tracker.add(user_id)
        try:
            await self._bot.send_message(user_id, text, parse_mode="HTML")
        except TelegramForbiddenError:
            pass
        except TelegramRetryAfter as e:
            tracker.discard(user_id)  # пусть в следующий цикл попробует снова
            logger.warning("RetryAfter %s in subscription notice", e.retry_after)
        except Exception as e:
            logger.debug("Notify %s failed: %s", user_id, e)


scheduler = JobScheduler()
