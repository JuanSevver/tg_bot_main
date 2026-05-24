"""Inline mode handler for admin user search."""
from __future__ import annotations

from datetime import datetime

from aiogram import Router
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, or_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from config import load_config
from database.models import User

router = Router(name="admin_inline")
_config = load_config()


def _user_action_kb(user_id: int) -> InlineKeyboardMarkup:
    """Action buttons sent along with the inline user card."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🎁 Выдать подписку", callback_data=f"adm:grant:{user_id}"),
        InlineKeyboardButton(text="🚫 Отозвать", callback_data=f"adm:revoke:{user_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="✉ Написать", callback_data=f"adm:msg:{user_id}"),
    )
    return builder.as_markup()


def _user_card(user: User) -> str:
    username_str = f"@{user.username}" if user.username else "—"
    sub = user.subscription
    if sub and sub.is_active:
        sub_status = f"✅ активна до {sub.expires_at.strftime('%d.%m.%Y')}"
        purchases = sub.purchases_count
    else:
        sub_status = "❌ нет"
        purchases = sub.purchases_count if sub else 0

    days_in_bot = (datetime.utcnow() - user.created_at).days
    last_active = user.last_active_at.strftime('%d.%m.%Y %H:%M') if user.last_active_at else "—"

    return (
        f"👤 <b>{user.full_name}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"💬 Username: {username_str}\n"
        f"📅 В боте: <b>{days_in_bot} дн.</b>\n"
        f"🕐 Последняя активность: <b>{last_active}</b>\n\n"
        f"💳 Подписка: <b>{sub_status}</b>\n"
        f"💰 Платных подписок: <b>{purchases}</b>\n"
        f"📨 Получено заявок: <b>{user.messages_received}</b>"
    )


@router.inline_query()
async def inline_users(query: InlineQuery, session: AsyncSession) -> None:
    # Only admins can use inline search
    if query.from_user.id not in _config.admin_ids:
        await query.answer([], cache_time=1)
        return

    search = query.query.strip().lstrip("@")

    if not search:
        # Empty query — show latest 50 users as a list
        result = await session.execute(
            select(User)
            .options(selectinload(User.subscription))
            .order_by(User.created_at.desc())
            .limit(50)
        )
    else:
        # Search by username or Telegram ID
        try:
            uid = int(search)
            cond = or_(User.id == uid, User.username.ilike(f"%{search}%"))
        except ValueError:
            cond = User.username.ilike(f"%{search}%")
        result = await session.execute(
            select(User)
            .where(cond)
            .options(selectinload(User.subscription))
            .limit(20)
        )

    users = result.scalars().all()

    if not users:
        await query.answer(
            [],
            cache_time=5,
            is_personal=True,
            switch_pm_text="❌ Пользователь не найден",
            switch_pm_parameter="start",
        )
        return

    items = []
    for user in users:
        sub = user.subscription
        sub_status = "✅" if (sub and sub.is_active) else "❌"
        username_str = f"@{user.username}" if user.username else "—"

        items.append(
            InlineQueryResultArticle(
                id=str(user.id),
                title=f"{user.full_name}  {sub_status}",
                description=f"{username_str}  ·  ID: {user.id}  ·  📨 {user.messages_received}",
                input_message_content=InputTextMessageContent(
                    message_text=_user_card(user),
                    parse_mode="HTML",
                ),
                reply_markup=_user_action_kb(user.id),
            )
        )

    await query.answer(items, cache_time=5, is_personal=True)
