from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.keyboards import users_list_kb, user_detail_kb, cancel_kb
from bot.states import UserManageSG
from database.models import User, Subscription

router = Router(name="admin_users")


@router.callback_query(F.data == "adm:users")
async def cb_users_list(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    result = await session.execute(
        select(User).order_by(User.created_at.desc()).options(selectinload(User.subscription))
    )
    users = result.scalars().all()
    await state.set_state(UserManageSG.list)
    await state.update_data(users=[u.id for u in users])
    await callback.message.edit_text(
        f"👥 <b>Пользователи</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Всего в боте: <b>{len(users)}</b>",
        reply_markup=users_list_kb(list(users)),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:users:page:"))
async def cb_users_page(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    page = int(callback.data.split(":")[-1])
    result = await session.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()
    await callback.message.edit_text(
        f"👥 <b>Пользователи</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Всего в боте: <b>{len(users)}</b>",
        reply_markup=users_list_kb(list(users), page=page),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "adm:users:search")
async def cb_users_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserManageSG.search)
    await callback.message.edit_text(
        "🔍 Введите username или ID пользователя:",
        reply_markup=cancel_kb("adm:users"),
    )
    await callback.answer()


@router.message(UserManageSG.search)
async def process_user_search(message: Message, state: FSMContext, session: AsyncSession) -> None:
    query = message.text.strip().lstrip("@")
    try:
        uid = int(query)
        cond = User.id == uid
    except ValueError:
        cond = User.username.ilike(f"%{query}%")

    result = await session.execute(select(User).where(cond).options(selectinload(User.subscription)))
    users = result.scalars().all()
    if not users:
        await message.answer("❌ Пользователь не найден.", reply_markup=cancel_kb("adm:users"))
        return
    await message.answer(
        f"🔍 Найдено: <b>{len(users)}</b>",
        reply_markup=users_list_kb(list(users)),
        parse_mode="HTML",
    )
    await state.set_state(UserManageSG.list)


@router.callback_query(F.data.startswith("adm:user:"))
async def cb_user_detail(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    user_id = int(callback.data.split(":")[-1])
    result = await session.execute(
        select(User).where(User.id == user_id).options(selectinload(User.subscription))
    )
    user = result.scalar_one_or_none()
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    sub = user.subscription
    days_using = (datetime.utcnow() - user.created_at).days
    sub_status = "есть ✅" if sub and sub.is_active else "нет ❌"
    purchases = sub.purchases_count if sub else 0
    tg_link = f"tg://user?id={user.id}"
    username_text = f"@{user.username}" if user.username else f"<a href='{tg_link}'>Написать</a>"

    text = (
        f"👤 <b>Пользователь</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔  ID: <code>{user.id}</code>\n"
        f"💬  Логин: {username_text}\n"
        f"📅  Дней в боте: <b>{days_using}</b>\n"
        f"🕐  Последняя активность: <b>{user.last_active_at.strftime('%d.%m.%Y %H:%M') if user.last_active_at else '—'}</b>\n\n"
        f"💳  Подписка: {sub_status}\n"
        f"💰  Платных подписок: <b>{purchases}</b>"
    )
    await state.set_state(UserManageSG.detail)
    await state.update_data(target_user_id=user_id)
    await callback.message.edit_text(text, reply_markup=user_detail_kb(user_id), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm:grant:"))
async def cb_grant_sub(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    user_id = int(callback.data.split(":")[-1])
    await state.update_data(target_user_id=user_id)
    await state.set_state(UserManageSG.grant_subscription)
    # Отправляем новым сообщением — callback может прийти из inline-режима,
    # где callback.message is None (только inline_message_id)
    await callback.bot.send_message(
        callback.from_user.id,
        "🎁 <b>Выдать подписку</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Введите количество дней:\n"
        "<i>Например: 30, 90, 365</i>",
        reply_markup=cancel_kb(f"adm:user:{user_id}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(UserManageSG.grant_subscription)
async def process_grant_days(message: Message, state: FSMContext, session: AsyncSession) -> None:
    try:
        days = int(message.text.strip())
        if days <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите положительное число дней.", reply_markup=cancel_kb("adm:users"))
        return

    data = await state.get_data()
    user_id = data["target_user_id"]

    result = await session.execute(
        select(User).where(User.id == user_id).options(selectinload(User.subscription))
    )
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("Пользователь не найден.", reply_markup=cancel_kb("adm:users"))
        return

    now = datetime.utcnow()
    if user.subscription and user.subscription.is_active:
        expires = user.subscription.expires_at + timedelta(days=days)
    else:
        expires = now + timedelta(days=days)

    # plan="manual", purchases_count не трогаем: admin grant — это
    # компенсация/тест/подарок, в статистике «Платных подписок» он не учитывается
    # сознательно (см. inline-карточку: счётчик отражает только реальные оплаты
    # через CryptoBot). Если позже юзер реально оплатит — purchases_count += 1 встанет корректно.
    if user.subscription:
        user.subscription.expires_at = expires
        user.subscription.plan = "manual"
    else:
        sub = Subscription(user_id=user.id, plan="manual", expires_at=expires, purchases_count=0)
        session.add(sub)

    await session.commit()
    await message.answer(
        f"✅ Подписка на {days} дней выдана пользователю <code>{user_id}</code>.",
        reply_markup=cancel_kb(f"adm:user:{user_id}", "◀ К пользователю"),
        parse_mode="HTML",
    )
    await state.set_state(UserManageSG.detail)


@router.callback_query(F.data.startswith("adm:revoke:"))
async def cb_revoke_sub(callback: CallbackQuery, session: AsyncSession) -> None:
    user_id = int(callback.data.split(":")[-1])
    result = await session.execute(
        select(User).where(User.id == user_id).options(selectinload(User.subscription))
    )
    user = result.scalar_one_or_none()
    if user and user.subscription:
        user.subscription.expires_at = datetime.utcnow()
        await session.commit()
        await callback.answer("✅ Подписка отозвана.", show_alert=True)
    else:
        await callback.answer("Подписка не найдена.", show_alert=True)


@router.callback_query(F.data.startswith("adm:msg:"))
async def cb_send_msg_to_user(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = int(callback.data.split(":")[-1])
    await state.update_data(target_user_id=user_id)
    await state.set_state(UserManageSG.send_message)
    # Отправляем новым сообщением — callback может прийти из inline-режима,
    # где callback.message is None (только inline_message_id)
    await callback.bot.send_message(
        callback.from_user.id,
        "✉ Введите текст сообщения для отправки пользователю:",
        reply_markup=cancel_kb(f"adm:user:{user_id}"),
    )
    await callback.answer()


@router.message(UserManageSG.send_message)
async def process_send_msg(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user_id = data["target_user_id"]
    try:
        # parse_mode="HTML" — единообразно со всеми остальными send_message
        # в боте; иначе HTML/Markdown в сообщении админа уйдёт как plain text.
        await message.bot.send_message(user_id, message.text or "", parse_mode="HTML")
        await message.answer(
            "✅ Сообщение отправлено.",
            reply_markup=cancel_kb(f"adm:user:{user_id}", "◀ К пользователю"),
        )
    except Exception as e:
        # Не показываем raw stack — может содержать пути/имя БД.
        import logging
        logging.getLogger(__name__).exception("admin->user send failed")
        await message.answer(
            f"❌ Ошибка отправки: {type(e).__name__}",
            reply_markup=cancel_kb(f"adm:user:{user_id}", "◀ К пользователю"),
        )
    await state.set_state(UserManageSG.detail)
