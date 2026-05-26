from __future__ import annotations

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
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
    resolving_acc_id: int | None = None  # запомним, какой акк смог отрезолвить
    is_public_link = False
    try:
        from parser.manager import parser_manager
        from telethon.tl.types import Channel
        from telethon.utils import get_peer_id
        for client, acc_id in parser_manager._client_pairs:
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
            resolving_acc_id = acc_id
            break
        # Признак «публичной» ссылки — нет +/joinchat: с такими join() ничего
        # не делает, и аккаунт без подписки не получит NewMessage в realtime.
        lower = link.lower()
        is_public_link = not (
            "/+" in lower or "joinchat/" in lower or lower.lstrip("@").startswith("+")
        )
    except Exception:
        pass  # Нет аккаунтов — добавим без метаданных, резолв будет в _process_group

    group = TelegramGroup(link=link, title=title, is_channel=is_channel, chat_id=chat_id)
    session.add(group)
    await session.commit()
    # Прокидываем владельца сразу — иначе на первом полле round-robin
    # ткнётся в случайный аккаунт и для приватной группы получит «не вижу».
    if resolving_acc_id is not None and group.id:
        try:
            from parser.manager import parser_manager
            parser_manager._group_owner[group.id] = resolving_acc_id
        except Exception:
            pass

    # Автоматически подписываем аккаунты на новую группу — но ТОЛЬКО если это
    # приватная группа (по инвайт-ссылке). Для публичных вступление пропускается
    # сознательно: анонимность парсера + экономия лимита каналов (~500/акк).
    # Публичные ловятся коротким поллом (POLL_INTERVAL_SECONDS, ~60с), что
    # обычно достаточно для бизнес-задач.
    join_summary = ""
    try:
        from parser.manager import parser_manager
        join_result = await parser_manager.join_group(link)
        total = len(join_result)
        joined = sum(1 for v in join_result.values() if v in ("joined", "already"))
        skipped_public = sum(1 for v in join_result.values() if v.startswith("skipped"))
        if skipped_public == total and total > 0:
            # Публичная группа — вступление пропущено намеренно.
            join_summary = (
                "\nℹ️ Публичная группа — аккаунты не вступают (анонимность). "
                "Новые сообщения подбираются <b>поллом ~1 раз в минуту</b>."
            )
        else:
            join_summary = f"\n👥 Аккаунтов подписано: {joined}/{total}"
            failures = [f"acc_{a}: {v}" for a, v in join_result.items()
                        if v not in ("joined", "already") and not v.startswith("skipped")]
            if failures:
                join_summary += "\n⚠️ " + "; ".join(failures[:3])
    except Exception as e:
        join_summary = f"\n⚠️ Авто-подписка не удалась: {type(e).__name__}"

    type_label = "📢 Канал" if is_channel else "👥 Группа"
    display = title or link
    result2 = await session.execute(select(TelegramGroup).order_by(TelegramGroup.added_at.desc()))
    groups = result2.scalars().all()
    await message.answer(
        f"✅ {type_label} <b>{display}</b> добавлен(а).{join_summary}\n\n"
        f"🔗 <b>Группы/каналы</b> ({len(groups)})",
        reply_markup=groups_list_kb(list(groups)),
        parse_mode="HTML",
    )
    await state.set_state(GroupSG.list)


# ── Массовое вступление всех аккаунтов во все активные группы ─────────────────

@router.callback_query(F.data == "adm:grp:joinall")
async def cb_groups_join_all(callback: CallbackQuery, session: AsyncSession) -> None:
    """Прогоняет join_group для каждой активной группы.

    Полезно после: (а) добавления новых аккаунтов, (б) если группы добавляли
    в БД руками, (в) после перевыдачи сессии. Идемпотентно — повторные вызовы
    дают "already" для уже вступлённых, ничего не ломается.
    """
    result = await session.execute(
        select(TelegramGroup).where(TelegramGroup.is_active == True)
    )
    groups = result.scalars().all()
    if not groups:
        await callback.answer("Нет активных групп.", show_alert=True)
        return

    await callback.message.edit_text(
        f"⏳ Подписываю аккаунты на {len(groups)} групп... Это может занять минуту.",
        parse_mode="HTML",
    )
    await callback.answer()

    from parser.manager import parser_manager
    stats = {"joined": 0, "already": 0, "skipped": 0, "failed": 0}
    failures: list[str] = []

    for grp in groups:
        try:
            res = await parser_manager.join_group(grp.link)
        except Exception as e:
            failures.append(f"{grp.link}: {type(e).__name__}")
            stats["failed"] += 1
            continue
        for status in res.values():
            if status == "joined":
                stats["joined"] += 1
            elif status == "already":
                stats["already"] += 1
            elif status.startswith("skipped"):
                stats["skipped"] += 1
            else:
                stats["failed"] += 1
                if len(failures) < 8:
                    failures.append(f"{grp.link[:40]}: {status[:40]}")

    summary = (
        f"✅ Готово.\n\n"
        f"Новых вступлений: <b>{stats['joined']}</b>\n"
        f"Уже состояли: <b>{stats['already']}</b>\n"
        f"Пропущено (публичные, не вступаем): <b>{stats['skipped']}</b>\n"
        f"Не удалось: <b>{stats['failed']}</b>\n"
        f"<i>Публичные группы парсятся анонимно поллом ~1 раз в минуту "
        f"без вступления.</i>"
    )
    if failures:
        summary += "\n\n⚠️ Проблемы:\n" + "\n".join(f"• {f}" for f in failures)

    result2 = await session.execute(
        select(TelegramGroup).order_by(TelegramGroup.added_at.desc())
    )
    await callback.message.answer(
        summary + f"\n\n🔗 <b>Группы/каналы</b> ({len(groups)})",
        reply_markup=groups_list_kb(list(result2.scalars().all())),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "adm:grp:matrix")
async def cb_groups_matrix(callback: CallbackQuery, session: AsyncSession) -> None:
    """Показывает матрицу «какой аккаунт владеет какой группой».

    Источник правды — parser_manager._group_owner: туда попадает acc_id того
    аккаунта, который первым смог отрезолвить entity группы. Это сильный
    индикатор подписки (особенно для приватных групп). Без этого экрана
    админ не понимает, кто реально парсит каждую группу.
    """
    from parser.manager import parser_manager
    from database.models import ParserAccount

    grp_res = await session.execute(
        select(TelegramGroup).where(TelegramGroup.is_active == True).order_by(TelegramGroup.added_at)
    )
    groups = grp_res.scalars().all()
    acc_res = await session.execute(select(ParserAccount))
    accs = {a.id: a for a in acc_res.scalars().all()}

    if not groups:
        await callback.answer("Нет активных групп.", show_alert=True)
        return

    lines = ["🗺 <b>Матрица аккаунт × группа</b>", ""]
    for g in groups:
        owner_id = parser_manager._group_owner.get(g.id)
        if owner_id:
            label = accs.get(owner_id).phone if accs.get(owner_id) and accs[owner_id].phone else f"acc_{owner_id}"
            owner_str = f"✅ {label}"
        else:
            owner_str = "⚠️ не определён (ждёт первого полла)"
        title = (g.title or g.link)[:35]
        lines.append(f"• <code>{title}</code> → {owner_str}")

    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n…"
    await callback.message.edit_text(
        text,
        reply_markup=groups_list_kb(list(groups)),
        parse_mode="HTML",
    )
    await callback.answer()
