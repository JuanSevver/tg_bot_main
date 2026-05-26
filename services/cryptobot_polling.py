"""
CryptoBot polling-based payment handler.
Polls GET /getInvoices?status=paid every 30 seconds.
No domain/HTTPS/webhook required.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import aiohttp
from aiogram import Bot
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from config import load_config
from database.db import async_session
from database.models import User, Subscription, ProcessedInvoice

logger = logging.getLogger(__name__)
_config = load_config()
_BASE = "https://pay.crypt.bot/api"

POLL_INTERVAL = 30  # seconds


class CryptoBotPoller:
    def __init__(self) -> None:
        self._bot: Bot | None = None
        # In-memory cache поверх БД — чтобы не делать SELECT на каждый инвойс
        # внутри одного poll-цикла. Источник истины всё равно ProcessedInvoice.
        self._processed_cache: set[int] = set()
        self._task: asyncio.Task | None = None

    def set_bot(self, bot: Bot) -> None:
        self._bot = bot

    async def start(self) -> None:
        if not _config.cryptobot_token:
            logger.warning("CRYPTOBOT_TOKEN not set — CryptoBot polling disabled.")
            return
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("CryptoBot polling started (interval=%ds).", POLL_INTERVAL)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._check_invoices()
            except Exception as exc:
                logger.exception("CryptoBot polling error: %s", exc)
            await asyncio.sleep(POLL_INTERVAL)

    async def _check_invoices(self) -> None:
        headers = {"Crypto-Pay-API-Token": _config.cryptobot_token}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_BASE}/getInvoices",
                params={"status": "paid", "count": 100},
                headers=headers,
            ) as resp:
                data = await resp.json()

        if not data.get("ok"):
            logger.warning("CryptoBot getInvoices error: %s", data)
            return

        items = data.get("result", {}).get("items", [])
        if not items:
            return

        # Подтягиваем уже обработанные инвойсы из БД одним запросом —
        # после рестарта _processed_cache пустой, иначе начислили бы по второму
        # разу. Persistence лечит финансовую дыру при крашах/рестартах.
        invoice_ids = [inv.get("invoice_id") for inv in items if inv.get("invoice_id")]
        async with async_session() as db:
            existing = await db.execute(
                select(ProcessedInvoice.invoice_id).where(
                    ProcessedInvoice.invoice_id.in_(invoice_ids)
                )
            )
            existing_set = set(existing.scalars().all())
            self._processed_cache.update(existing_set)

            # SEED-РЕЖИМ при первом запуске после миграции на ProcessedInvoice.
            # Если БД пустая (никаких инвойсов раньше не отмечалось), то «новизна»
            # каждого из этих 100 paid-инвойсов — иллюзия: они оплачивались
            # ДО деплоя и подписки по ним уже выданы старым кодом.
            # Помечаем их processed без начисления — это однократно при upgrade'e.
            # На последующих циклах existing_set всегда будет содержать недавние ID
            # и мы пойдём в обычную ветку.
            total_in_db = await db.execute(select(func.count(ProcessedInvoice.invoice_id)))
            existing_total = total_in_db.scalar_one()
            if existing_total == 0:
                logger.warning(
                    "ProcessedInvoice is empty — seed mode: marking %d historical "
                    "invoices as already-processed without grant. "
                    "This is expected ONLY on first run after upgrade.",
                    len(invoice_ids),
                )
                for inv_id in invoice_ids:
                    db.add(ProcessedInvoice(invoice_id=inv_id))
                await db.commit()
                self._processed_cache.update(invoice_ids)
                return

        for invoice in items:
            invoice_id = invoice.get("invoice_id")
            if not invoice_id or invoice_id in self._processed_cache:
                continue
            # Помечаем сразу — даже если начисление упадёт ниже, повторная
            # попытка пойдёт по тому же пути и снова попадёт сюда; идемпотентность
            # обеспечивает PK на invoice_id (см. _handle_paid_invoice).
            self._processed_cache.add(invoice_id)
            await self._handle_paid_invoice(invoice)

    async def _handle_paid_invoice(self, invoice: dict) -> None:
        user_id_str = invoice.get("payload", "")
        try:
            user_id = int(user_id_str)
        except (ValueError, TypeError):
            return

        amount = float(invoice.get("amount", 0))
        plans = _config.SUBSCRIPTION_PLANS
        matched_plan = None
        for plan_id, plan in plans.items():
            if plan_id != "trial" and round(amount, 2) == plan["price"]:
                matched_plan = (plan_id, plan)
                break

        if not matched_plan:
            logger.warning("No plan matched for invoice %s, amount=%s", invoice.get("invoice_id"), amount)
            return

        plan_id, plan = matched_plan

        invoice_id = invoice.get("invoice_id")
        async with async_session() as db:
            # Двойная проверка идемпотентности: между чтением списка и попыткой
            # начисления мог пройти параллельный обработчик (или предыдущий
            # crashed mid-commit). PK на invoice_id всё равно поймает повтор.
            already = await db.get(ProcessedInvoice, invoice_id) if invoice_id else None
            if already:
                return

            result = await db.execute(
                select(User).where(User.id == user_id).options(selectinload(User.subscription))
            )
            user = result.scalar_one_or_none()
            if not user:
                logger.warning("User %s not found for invoice %s", user_id, invoice_id)
                # Всё равно отметим инвойс обработанным — иначе будем щёлкать на каждом цикле.
                if invoice_id:
                    db.add(ProcessedInvoice(invoice_id=invoice_id))
                    await db.commit()
                return

            now = datetime.utcnow()
            days = plan["days"]
            if user.subscription and user.subscription.is_active:
                expires = user.subscription.expires_at + timedelta(days=days)
            else:
                expires = now + timedelta(days=days)

            if user.subscription:
                user.subscription.plan = plan_id
                user.subscription.expires_at = expires
                user.subscription.purchases_count += 1
            else:
                sub = Subscription(
                    user_id=user.id,
                    plan=plan_id,
                    expires_at=expires,
                    purchases_count=1,
                )
                db.add(sub)

            if invoice_id:
                db.add(ProcessedInvoice(invoice_id=invoice_id))

            await db.commit()
            logger.info("Subscription %s granted to user %s via CryptoBot polling.", plan_id, user_id)

        if self._bot:
            plan_label = plan.get("label", plan_id)
            try:
                await self._bot.send_message(
                    user_id,
                    f"✅ <b>Оплата получена!</b>\n\n"
                    f"💳 Подписка <b>{plan_label}</b> активирована на {days} дней.\n"
                    "Используйте кнопку «Получать запросы» в главном меню.",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("Cannot notify user %s: %s", user_id, e)


cryptobot_poller = CryptoBotPoller()
