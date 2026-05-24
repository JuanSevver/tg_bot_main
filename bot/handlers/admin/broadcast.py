from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import broadcast_target_kb, broadcast_content_type_kb, cancel_kb
from bot.states import BroadcastSG
from database.models import User, Subscription, BroadcastHistory

router = Router(name="admin_broadcast")


@router.callback_query(F.data == "adm:broadcast")
async def cb_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastSG.choose_target)
    await callback.message.edit_text("📢 <b>Рассылка</b>\n\nКому отправить?", reply_markup=broadcast_target_kb(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("bcast:target:"))
async def cb_bcast_target(callback: CallbackQuery, state: FSMContext) -> None:
    target = callback.data.split(":")[-1]
    await state.update_data(target=target, content_types=set())
    await state.set_state(BroadcastSG.choose_content_types)
    await callback.message.edit_text(
        "📝 Выберите тип контента (можно несколько):",
        reply_markup=broadcast_content_type_kb(set()),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("bcast:type:"))
async def cb_bcast_type(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":")[-1]
    data = await state.get_data()
    selected: set = data.get("content_types", set())

    if action == "done":
        if not selected:
            await callback.answer("Выберите хотя бы один тип!", show_alert=True)
            return
        await state.update_data(content_types=selected, bcast_text=None, bcast_media=None, bcast_button=None)
        await state.set_state(BroadcastSG.enter_text)
        hint = "📝 Введите текст сообщения:" if "text" in selected else "✅ Введите подпись к медиа (или /skip):"
        await callback.message.edit_text(hint, reply_markup=cancel_kb("adm:broadcast"))
        await callback.answer()
        return

    if action in selected:
        selected.discard(action)
    else:
        selected.add(action)

    await state.update_data(content_types=selected)
    await callback.message.edit_reply_markup(reply_markup=broadcast_content_type_kb(selected))
    await callback.answer()


@router.message(BroadcastSG.enter_text)
async def bcast_enter_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    text = None if message.text == "/skip" else message.text
    await state.update_data(bcast_text=text)

    if "photo" in data["content_types"] or "video" in data["content_types"]:
        await state.set_state(BroadcastSG.enter_media)
        await message.answer("📎 Отправьте фото или видео:", reply_markup=cancel_kb("adm:broadcast"))
    elif "button" in data["content_types"]:
        await state.set_state(BroadcastSG.enter_button)
        await message.answer("🔘 Введите текст кнопки и ссылку через | (например: Сайт|https://...):", reply_markup=cancel_kb("adm:broadcast"))
    else:
        await _confirm_broadcast(message, state)


@router.message(BroadcastSG.enter_media)
async def bcast_enter_media(message: Message, state: FSMContext) -> None:
    if message.photo:
        file_id = message.photo[-1].file_id
        media_type = "photo"
    elif message.video:
        file_id = message.video.file_id
        media_type = "video"
    else:
        await message.answer("❌ Отправьте фото или видео.", reply_markup=cancel_kb("adm:broadcast"))
        return

    await state.update_data(bcast_media=file_id, bcast_media_type=media_type)
    data = await state.get_data()
    if "button" in data["content_types"]:
        await state.set_state(BroadcastSG.enter_button)
        await message.answer("🔘 Введите текст кнопки и ссылку через | (например: Сайт|https://...):", reply_markup=cancel_kb("adm:broadcast"))
    else:
        await _confirm_broadcast(message, state)


@router.message(BroadcastSG.enter_button)
async def bcast_enter_button(message: Message, state: FSMContext) -> None:
    await state.update_data(bcast_button=message.text)
    await _confirm_broadcast(message, state)


async def _confirm_broadcast(message: Message, state: FSMContext) -> None:
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    data = await state.get_data()
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🚀 Запустить рассылку", callback_data="bcast:run"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="adm:main"))
    await state.set_state(BroadcastSG.confirm)
    target_labels = {"all": "всем", "active": "активным", "inactive": "неактивным", "subscribed": "с подпиской"}
    await message.answer(
        f"📢 <b>Рассылка готова</b>\n"
        f"Кому: <b>{target_labels.get(data['target'], data['target'])}</b>\n"
        f"Типы: {', '.join(data['content_types'])}",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "bcast:run", BroadcastSG.confirm)
async def cb_run_broadcast(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    data = await state.get_data()
    target = data["target"]
    now = datetime.utcnow()
    two_days_ago = now - timedelta(days=2)

    q = select(User)
    if target == "active":
        q = q.where(User.last_active_at >= now.replace(hour=0, minute=0, second=0))
    elif target == "inactive":
        q = q.where(User.last_active_at < two_days_ago)
    elif target == "subscribed":
        from sqlalchemy.orm import selectinload
        q = q.join(User.subscription).where(Subscription.expires_at > now)

    users = (await session.execute(q)).scalars().all()
    total = len(users)

    history = BroadcastHistory(target=target, message_text=data.get("bcast_text"), total=total)
    session.add(history)
    await session.commit()

    await callback.message.edit_text(f"⏳ Запускаю рассылку на {total} пользователей...")
    await callback.answer()

    sent = failed = 0
    button_kb = None
    if data.get("bcast_button"):
        parts = data["bcast_button"].split("|", 1)
        if len(parts) == 2:
            from aiogram.utils.keyboard import InlineKeyboardBuilder
            from aiogram.types import InlineKeyboardButton
            b = InlineKeyboardBuilder()
            b.row(InlineKeyboardButton(text=parts[0].strip(), url=parts[1].strip()))
            button_kb = b.as_markup()

    for user in users:
        try:
            text = data.get("bcast_text") or ""
            media = data.get("bcast_media")
            media_type = data.get("bcast_media_type")
            if media:
                if media_type == "photo":
                    await bot.send_photo(user.id, media, caption=text, reply_markup=button_kb)
                else:
                    await bot.send_video(user.id, media, caption=text, reply_markup=button_kb)
            else:
                await bot.send_message(user.id, text, reply_markup=button_kb)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    history.finished_at = datetime.utcnow()
    history.sent = sent
    history.failed = failed
    await session.commit()

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    done_kb = InlineKeyboardBuilder()
    done_kb.row(InlineKeyboardButton(text="◀ В панель администратора", callback_data="adm:main", style="primary"))
    await bot.send_message(
        callback.from_user.id,
        f"✅ <b>Рассылка завершена</b>\n\n"
        f"Всего: {total}\nОтправлено: {sent}\nНе отправлено: {failed}",
        reply_markup=done_kb.as_markup(),
        parse_mode="HTML",
    )
    await state.clear()
