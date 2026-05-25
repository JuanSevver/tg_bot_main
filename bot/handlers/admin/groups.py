from __future__ import annotations

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.keyboards import groups_list_kb, cancel_kb, group_detail_kb, group_categories_kb
from bot.states import GroupSG
from database.models import TelegramGroup, Category, GroupCategory

router = Router(name="admin_groups")


# ── Список групп ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:groups")
async def cb_groups(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    result = await session.execute(select(TelegramGroup).order_by(TelegramGroup.added_at.desc()))
    groups = result.scalars().all()
    await state.set_state(GroupSG.list)
    await callback.message.edit_text(
        f"🔗 <b>Группы/каналы</b> ({len(groups)})\n\nНажмите на группу для управления:",
        reply_markup=groups_list_kb(list(groups)),
        parse_mode="HTML",
    )
    await callback.answer()


# ── Детали группы ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("adm:grp:detail:"))
async def cb_group_detail(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[-1])
    group = await session.get(TelegramGroup, group_id)
    if not group:
        await callback.answer("Группа не найдена", show_alert=True)
        return

    # Кол-во назначенных категорий
    res = await session.execute(
        select(GroupCategory).where(GroupCategory.group_id == group_id)
    )
    assigned_count = len(res.scalars().all())

    type_label = "📢 Канал" if group.is_channel else "👥 Группа"
    status = "✅ Активна" if group.is_active else "❌ Выключена"
    title = group.title or group.link
    cats_note = (
        f"Категории: {assigned_count} назначено" if assigned_count
        else "Категории: <i>все (не ограничено)</i>"
    )

    await callback.message.edit_text(
        f"{type_label} <b>{title}</b>\n"
        f"🔗 <code>{group.link}</code>\n"
        f"Статус: {status}\n"
        f"{cats_note}",
        reply_markup=group_detail_kb(group, assigned_count),
        parse_mode="HTML",
    )
    await callback.answer()


# ── Вкл/выкл группы ──────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("adm:grp:toggle:"))
async def cb_group_toggle(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[-1])
    group = await session.get(TelegramGroup, group_id)
    if not group:
        await callback.answer("Группа не найдена", show_alert=True)
        return

    group.is_active = not group.is_active
    await session.commit()

    res = await session.execute(
        select(GroupCategory).where(GroupCategory.group_id == group_id)
    )
    assigned_count = len(res.scalars().all())

    type_label = "📢 Канал" if group.is_channel else "👥 Группа"
    status = "✅ Активна" if group.is_active else "❌ Выключена"
    title = group.title or group.link
    cats_note = (
        f"Категории: {assigned_count} назначено" if assigned_count
        else "Категории: <i>все (не ограничено)</i>"
    )

    await callback.message.edit_text(
        f"{type_label} <b>{title}</b>\n"
        f"🔗 <code>{group.link}</code>\n"
        f"Статус: {status}\n"
        f"{cats_note}",
        reply_markup=group_detail_kb(group, assigned_count),
        parse_mode="HTML",
    )
    await callback.answer("Статус обновлён")


# ── Управление категориями группы ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("adm:grp:cats:"))
async def cb_group_cats(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[-1])
    group = await session.get(TelegramGroup, group_id)
    if not group:
        await callback.answer("Группа не найдена", show_alert=True)
        return

    cats_result = await session.execute(
        select(Category).where(Category.is_active == True).order_by(Category.name)
    )
    categories = cats_result.scalars().all()

    gc_result = await session.execute(
        select(GroupCategory).where(GroupCategory.group_id == group_id)
    )
    assigned_ids = {gc.category_id for gc in gc_result.scalars().all()}

    title = group.title or group.link
    note = (
        "\n\n<i>ℹ️ Если ни одна не выбрана — группа парсится по всем категориям.</i>"
    )
    await callback.message.edit_text(
        f"📂 <b>Категории для «{title}»</b>{note}",
        reply_markup=group_categories_kb(group_id, list(categories), assigned_ids),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:grp:cat_toggle:"))
async def cb_group_cat_toggle(callback: CallbackQuery, session: AsyncSession) -> None:
    # adm:grp:cat_toggle:{group_id}:{cat_id}
    parts = callback.data.split(":")
    group_id = int(parts[-2])
    cat_id = int(parts[-1])

    existing = await session.execute(
        select(GroupCategory).where(
            GroupCategory.group_id == group_id,
            GroupCategory.category_id == cat_id,
        )
    )
    gc = existing.scalar_one_or_none()
    if gc:
        await session.delete(gc)
    else:
        session.add(GroupCategory(group_id=group_id, category_id=cat_id))
    await session.commit()

    # Обновляем клавиатуру
    cats_result = await session.execute(
        select(Category).where(Category.is_active == True).order_by(Category.name)
    )
    categories = cats_result.scalars().all()

    gc_result = await session.execute(
        select(GroupCategory).where(GroupCategory.group_id == group_id)
    )
    assigned_ids = {gc.category_id for gc in gc_result.scalars().all()}

    group = await session.get(TelegramGroup, group_id)
    title = (group.title or group.link) if group else str(group_id)
    note = "\n\n<i>ℹ️ Если ни одна не выбрана — группа парсится по всем категориям.</i>"

    await callback.message.edit_text(
        f"📂 <b>Категории для «{title}»</b>{note}",
        reply_markup=group_categories_kb(group_id, list(categories), assigned_ids),
        parse_mode="HTML",
    )
    await callback.answer()


# ── Удаление группы ───────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("adm:grp:delete:"))
async def cb_group_delete(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[-1])
    group = await session.get(TelegramGroup, group_id)
    if group:
        await session.delete(group)
        await session.commit()

    result = await session.execute(select(TelegramGroup).order_by(TelegramGroup.added_at.desc()))
    groups = result.scalars().all()
    await callback.message.edit_text(
        f"🔗 <b>Группы/каналы</b> ({len(groups)})\n\nНажмите на группу для управления:",
        reply_markup=groups_list_kb(list(groups)),
        parse_mode="HTML",
    )
    await callback.answer("Группа удалена")


# ── Добавление группы ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:grp:add")
async def cb_group_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GroupSG.add_link)
    await callback.message.edit_text(
        "Введите ссылку на группу/канал\n(например: <code>https://t.me/example</code> или <code>@example</code>):",
        reply_markup=cancel_kb("adm:groups"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(GroupSG.add_link)
async def process_group_link(message: Message, state: FSMContext, session: AsyncSession) -> None:
    link = message.text.strip()

    result = await session.execute(select(TelegramGroup).where(TelegramGroup.link == link))
    if result.scalar_one_or_none():
        await message.answer("⚠️ Эта группа уже добавлена.", reply_markup=cancel_kb("adm:groups", "◀ К списку групп"))
        return

    # Пробуем определить название/тип/chat_id через парсер.
    # Перебираем ВСЕ аккаунты — приватную группу видит только тот, кто в ней.
    title: str | None = None
    is_channel = False
    chat_id: int | None = None
    try:
        from parser.manager import parser_manager
        from telethon.tl.types import Channel
        from telethon.utils import get_peer_id
        for client in parser_manager._clients:
            try:
                entity = await client.get_entity(link)
            except Exception:
                continue
            title = getattr(entity, "title", None) or title
            is_channel = isinstance(entity, Channel) and entity.broadcast
            try:
                chat_id = get_peer_id(entity)
            except Exception:
                chat_id = None
            break
    except Exception:
        pass  # Нет аккаунтов — добавим без метаданных, резолв будет в _process_group

    group = TelegramGroup(link=link, title=title, is_channel=is_channel, chat_id=chat_id)
    session.add(group)
    await session.commit()

    # Сразу обновляем realtime-фильтр, чтобы новая группа ловилась мгновенно,
    # а не только после следующего цикла полла.
    try:
        from parser.manager import parser_manager
        await parser_manager._refresh_explicit_filter()
    except Exception:
        pass

    type_label = "📢 Канал" if is_channel else "👥 Группа"
    display = title or link
    result2 = await session.execute(select(TelegramGroup).order_by(TelegramGroup.added_at.desc()))
    groups = result2.scalars().all()
    await message.answer(
        f"✅ {type_label} <b>{display}</b> добавлен(а).\n\n"
        f"🔗 <b>Группы/каналы</b> ({len(groups)})",
        reply_markup=groups_list_kb(list(groups)),
        parse_mode="HTML",
    )
    await state.set_state(GroupSG.list)
