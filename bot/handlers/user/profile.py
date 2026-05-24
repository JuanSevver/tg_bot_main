from __future__ import annotations

from aiogram import Router, F
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.keyboards import profile_kb
from database.models import User

router = Router(name="profile")

PLAN_LABELS = {
    "trial": "Пробный",
    "1m":    "1 месяц",
    "3m":    "3 месяца",
    "1y":    "1 год",
    "manual": "Ручная выдача",
}


@router.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery, session: AsyncSession) -> None:
    result = await session.execute(
        select(User)
        .where(User.id == callback.from_user.id)
        .options(selectinload(User.subscription))
    )
    user = result.scalar_one_or_none()
    if not user:
        await callback.answer()
        return

    sub = user.subscription
    if sub and sub.is_active:
        plan_label = PLAN_LABELS.get(sub.plan, sub.plan)
        sub_block = (
            f"  ├ Статус: <b>Активна ✅</b>\n"
            f"  ├ Тариф: <b>{plan_label}</b>\n"
            f"  └ Осталось: <b>{sub.days_left} дн.</b>"
        )
    else:
        sub_block = "  └ <i>Нет активной подписки</i>"

    receiving = "Включена 🟢" if user.receiving_enabled else "Выключена 🔴"

    text = (
        "👤 <b>Профиль</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔  ID: <code>{user.id}</code>\n"
        f"📨  Получено заявок: <b>{user.messages_received}</b>\n"
        f"📡  Лента запросов: <b>{receiving}</b>\n\n"
        "💳  <b>Подписка:</b>\n"
        f"{sub_block}"
    )
    await callback.message.edit_text(text, reply_markup=profile_kb(), parse_mode="HTML")
    await callback.answer()
