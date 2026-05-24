from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import admin_main_kb
from database.models import User, Subscription, ParsedMessage

router = Router(name="admin_dashboard")


async def _stats_text(session: AsyncSession) -> str:
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    two_days_ago = datetime.utcnow() - timedelta(days=2)

    total_users = (await session.execute(select(func.count()).select_from(User))).scalar()
    today_users = (
        await session.execute(
            select(func.count()).select_from(User).where(User.created_at >= today)
        )
    ).scalar()
    active_today = (
        await session.execute(
            select(func.count()).select_from(User).where(User.last_active_at >= today)
        )
    ).scalar()
    subscribed = (
        await session.execute(
            select(func.count()).select_from(Subscription).where(
                Subscription.expires_at > datetime.utcnow()
            )
        )
    ).scalar()
    total_msgs = (
        await session.execute(select(func.count()).select_from(ParsedMessage))
    ).scalar()
    today_msgs = (
        await session.execute(
            select(func.count()).select_from(ParsedMessage).where(
                ParsedMessage.parsed_at >= today
            )
        )
    ).scalar()

    return (
        "📊 <b>Панель администратора</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👥  <b>Пользователи</b>\n"
        f"  ├ Новых сегодня: <b>{today_users}</b>\n"
        f"  ├ Всего: <b>{total_users}</b>\n"
        f"  └ Активных сегодня: <b>{active_today}</b>\n\n"
        "💳  <b>Подписки</b>\n"
        f"  └ С активной подпиской: <b>{subscribed}</b>\n\n"
        "📨  <b>Запросы</b>\n"
        f"  ├ За всё время: <b>{total_msgs}</b>\n"
        f"  └ Сегодня: <b>{today_msgs}</b>"
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message, session: AsyncSession) -> None:
    text = await _stats_text(session)
    bot_info = await message.bot.get_me()
    await message.answer(text, reply_markup=admin_main_kb(bot_info.username), parse_mode="HTML")


@router.callback_query(F.data == "adm:main")
async def cb_admin_main(callback: CallbackQuery, session: AsyncSession) -> None:
    text = await _stats_text(session)
    bot_info = await callback.bot.get_me()
    await callback.message.edit_text(text, reply_markup=admin_main_kb(bot_info.username), parse_mode="HTML")
    await callback.answer()
