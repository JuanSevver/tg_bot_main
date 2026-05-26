from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import accounts_list_kb, account_detail_kb, cancel_kb
from bot.states import AccountSG
from database.models import ParserAccount
from parser.manager import parser_manager, cancel_pending_signin

router = Router(name="admin_accounts")
logger = logging.getLogger(__name__)


async def _update_account_session(
    session: AsyncSession, acc_id: int, session_string: str, phone: str | None
) -> None:
    """Перевыдача сессии без удаления записи: сохраняет messages_parsed,
    CategoryAccount-привязки и историю в _group_owner."""
    acc = await session.get(ParserAccount, acc_id)
    if not acc:
        raise ValueError(f"Account {acc_id} not found")
    acc.session_string = session_string
    if phone:
        acc.phone = phone
    acc.is_valid = True
    await session.commit()
    await parser_manager.reload_clients()


@router.callback_query(F.data == "adm:accounts")
async def cb_accounts(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    result = await session.execute(select(ParserAccount).order_by(ParserAccount.added_at.desc()))
    accounts = result.scalars().all()
    await state.set_state(AccountSG.list)
    await callback.message.edit_text(
        f"🤖 <b>Аккаунты парсера</b> ({len(accounts)})",
        reply_markup=accounts_list_kb(list(accounts)),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "adm:acc:add")
async def cb_acc_add(callback: CallbackQuery, state: FSMContext) -> None:
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📱 По номеру телефона", callback_data="adm:acc:by_phone", style="primary"))
    builder.row(InlineKeyboardButton(text="🔑 По строке сессии", callback_data="adm:acc:by_session", style="primary"))
    builder.row(InlineKeyboardButton(text="◀ Назад", callback_data="adm:accounts", style="primary"))
    await callback.message.edit_text(
        "Выберите способ добавления аккаунта:",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:acc:by_phone")
async def cb_acc_by_phone(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AccountSG.add_phone)
    await callback.message.edit_text(
        "Введите номер телефона в формате <code>+79001234567</code>:",
        reply_markup=cancel_kb("adm:acc:add"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AccountSG.add_phone)
async def process_phone(message: Message, state: FSMContext, session: AsyncSession) -> None:
    phone = message.text.strip()
    await state.update_data(phone=phone)
    try:
        code = await parser_manager.request_code(phone)
        await state.update_data(phone_code_hash=code)
        await state.set_state(AccountSG.add_code)
        await message.answer(
            f"📨 Код отправлен на <code>{phone}</code>. Введите код:",
            reply_markup=cancel_kb("adm:accounts"),
            parse_mode="HTML",
        )
    except Exception as e:
        # При ошибке снимаем FSM, иначе следующее сообщение пойдёт как код.
        await state.clear()
        logger.exception("request_code failed")
        await message.answer(
            f"❌ Ошибка отправки кода: {type(e).__name__}",
            reply_markup=cancel_kb("adm:accounts"),
        )


@router.message(AccountSG.add_code)
async def process_code(message: Message, state: FSMContext, session: AsyncSession) -> None:
    code = message.text.strip()
    data = await state.get_data()
    phone = data["phone"]
    phone_code_hash = data.get("phone_code_hash")
    reissue_acc_id = data.get("reissue_acc_id")
    try:
        session_string = await parser_manager.sign_in(phone, code, phone_code_hash)
        if reissue_acc_id:
            await _update_account_session(session, reissue_acc_id, session_string, phone)
            await message.answer(
                "✅ Сессия перевыдана. История и привязки сохранены.",
                reply_markup=cancel_kb("adm:accounts", "◀ К аккаунтам"),
            )
        else:
            await _save_account(session, phone, session_string)
            await message.answer(
                "✅ Аккаунт успешно добавлен!",
                reply_markup=cancel_kb("adm:accounts", "◀ К аккаунтам"),
            )
        await state.clear()
    except Exception as e:
        err = str(e).lower()
        if "two" in err or "password" in err or "2fa" in err:
            await state.set_state(AccountSG.add_2fa)
            await message.answer("🔒 Требуется пароль 2FA. Введите пароль:", reply_markup=cancel_kb("adm:accounts"))
        else:
            # _pending уже cleanup-нут внутри sign_in. FSM сбрасываем.
            await state.clear()
            logger.exception("sign_in failed")
            await message.answer(
                f"❌ Ошибка авторизации: {type(e).__name__}",
                reply_markup=cancel_kb("adm:accounts"),
            )


@router.message(AccountSG.add_2fa)
async def process_2fa(message: Message, state: FSMContext, session: AsyncSession) -> None:
    password = message.text.strip()
    data = await state.get_data()
    phone = data["phone"]
    reissue_acc_id = data.get("reissue_acc_id")
    try:
        session_string = await parser_manager.sign_in_2fa(phone, password)
        if reissue_acc_id:
            await _update_account_session(session, reissue_acc_id, session_string, phone)
            await message.answer(
                "✅ Сессия перевыдана (2FA).",
                reply_markup=cancel_kb("adm:accounts", "◀ К аккаунтам"),
            )
        else:
            await _save_account(session, phone, session_string)
            await message.answer(
                "✅ Аккаунт добавлен с 2FA!",
                reply_markup=cancel_kb("adm:accounts", "◀ К аккаунтам"),
            )
        await state.clear()
    except Exception as e:
        await state.clear()
        logger.exception("sign_in_2fa failed")
        await message.answer(
            f"❌ Ошибка 2FA: {type(e).__name__}",
            reply_markup=cancel_kb("adm:accounts"),
        )


@router.callback_query(F.data == "adm:acc:by_session")
async def cb_acc_by_session(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AccountSG.add_session_string)
    await callback.message.edit_text(
        "Вставьте строку сессии Telethon (StringSession):",
        reply_markup=cancel_kb("adm:acc:add"),
    )
    await callback.answer()


@router.message(AccountSG.add_session_string)
async def process_session_string(message: Message, state: FSMContext, session: AsyncSession) -> None:
    ss = message.text.strip()
    await _save_account(session, None, ss)
    await message.answer("✅ Аккаунт добавлен по строке сессии!", reply_markup=cancel_kb("adm:accounts", "◀ К аккаунтам"))
    await state.clear()


async def _save_account(session: AsyncSession, phone: str | None, session_string: str) -> None:
    acc = ParserAccount(phone=phone, session_string=session_string)
    session.add(acc)
    await session.commit()
    await parser_manager.reload_clients()


@router.callback_query(F.data.startswith("adm:acc:detail:"))
async def cb_acc_detail(callback: CallbackQuery, session: AsyncSession) -> None:
    acc_id = int(callback.data.split(":")[-1])
    result = await session.execute(select(ParserAccount).where(ParserAccount.id == acc_id))
    acc = result.scalar_one_or_none()
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    status = "🟢 активен" if acc.is_active and acc.is_valid else "🔴 неактивен/невалиден"
    label = acc.phone or f"ID {acc.id}"
    joined = "ВКЛ ✅" if acc.parse_joined_groups else "ВЫКЛ ❌"

    text = (
        f"🤖 <b>Аккаунт {label}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Статус: {status}\n"
        f"📨 Спарсено сообщений: <b>{acc.messages_parsed}</b>\n\n"
        f"📂 Парсинг собственных групп: <b>{joined}</b>\n\n"
        "<i>Если включено — аккаунт будет также сканировать все группы, "
        "в которых он состоит, а не только явно добавленные через «Группы».</i>"
    )
    await callback.message.edit_text(
        text,
        reply_markup=account_detail_kb(acc_id, acc.parse_joined_groups),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:acc:toggle_joined:"))
async def cb_acc_toggle_joined(callback: CallbackQuery, session: AsyncSession) -> None:
    acc_id = int(callback.data.split(":")[-1])
    result = await session.execute(select(ParserAccount).where(ParserAccount.id == acc_id))
    acc = result.scalar_one_or_none()
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    acc.parse_joined_groups = not acc.parse_joined_groups
    await session.commit()
    await parser_manager.reload_clients()

    status = "включён ✅" if acc.parse_joined_groups else "отключён ❌"
    await callback.answer(f"Парсинг собственных групп {status}", show_alert=True)

    label = acc.phone or f"ID {acc.id}"
    joined = "ВКЛ ✅" if acc.parse_joined_groups else "ВЫКЛ ❌"
    text = (
        f"🤖 <b>Аккаунт {label}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Статус: {'🟢 активен' if acc.is_active and acc.is_valid else '🔴 неактивен'}\n"
        f"📨 Спарсено сообщений: <b>{acc.messages_parsed}</b>\n\n"
        f"📂 Парсинг собственных групп: <b>{joined}</b>\n\n"
        "<i>Если включено — аккаунт будет также сканировать все группы, "
        "в которых он состоит, а не только явно добавленные через «Группы».</i>"
    )
    await callback.message.edit_text(
        text,
        reply_markup=account_detail_kb(acc_id, acc.parse_joined_groups),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("adm:acc:reissue:"))
async def cb_acc_reissue(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Перевыдача сессии: запускаем тот же phone-flow, но в конце UPDATE
    существующей записи вместо INSERT. messages_parsed, CategoryAccount и
    история ParsedMessage сохраняются."""
    acc_id = int(callback.data.split(":")[-1])
    acc = await session.get(ParserAccount, acc_id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await state.update_data(reissue_acc_id=acc_id)
    await state.set_state(AccountSG.add_phone)
    await callback.message.edit_text(
        f"🔄 <b>Перевыдача сессии для acc_{acc_id}</b>\n\n"
        f"Введите номер телефона (можно тот же — <code>{acc.phone or '—'}</code>):",
        reply_markup=cancel_kb("adm:accounts"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:acc:delete:"))
async def cb_acc_delete(callback: CallbackQuery, session: AsyncSession) -> None:
    acc_id = int(callback.data.split(":")[-1])
    result = await session.execute(select(ParserAccount).where(ParserAccount.id == acc_id))
    acc = result.scalar_one_or_none()
    if acc:
        await session.delete(acc)
        await session.commit()
        await parser_manager.reload_clients()
        await callback.answer("✅ Аккаунт удалён.", show_alert=True)
    else:
        await callback.answer("Аккаунт не найден.", show_alert=True)
    result2 = await session.execute(select(ParserAccount).order_by(ParserAccount.added_at.desc()))
    accounts = result2.scalars().all()
    await callback.message.edit_reply_markup(reply_markup=accounts_list_kb(list(accounts)))
