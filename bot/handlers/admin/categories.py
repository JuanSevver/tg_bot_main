from __future__ import annotations

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import categories_list_kb, category_detail_kb, category_accounts_kb, cancel_kb
from bot.states import CategorySG
from database.models import Category, CategoryType, UserCategory, User, CategoryAccount, ParserAccount

router = Router(name="admin_categories")


def _cat_detail_text(cat: Category) -> str:
    kws = cat.get_keywords()
    kw_text = "\n".join(f"  • {k}" for k in kws) if kws else "  <i>нет</i>"
    sws = cat.get_stop_words()
    sw_text = "\n".join(f"  • {w}" for w in sws) if sws else "  <i>нет</i>"
    status = "🟢 активна" if cat.is_active else "🔴 отключена"
    return (
        f"📂 <b>{cat.name}</b> [{cat.type.value}] — {status}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔑 <b>Ключевые фразы</b> <i>(любая одна)</i>:\n{kw_text}\n\n"
        f"🚫 <b>Минус-слова</b> <i>(блокируют пост)</i>:\n{sw_text}"
    )


async def _show_cat_detail(
    callback: CallbackQuery, state: FSMContext, cat: Category
) -> None:
    await state.update_data(current_cat_id=cat.id)
    await state.set_state(CategorySG.detail)
    await callback.message.edit_text(
        _cat_detail_text(cat),
        reply_markup=category_detail_kb(cat.id),
        parse_mode="HTML",
    )
    await callback.answer()


async def _reply_and_show_detail(
    message: Message, state: FSMContext, session: AsyncSession, text: str
) -> None:
    """Отправляет подтверждение и сразу показывает деталь категории."""
    data = await state.get_data()
    cat_id = data["current_cat_id"]
    result = await session.execute(select(Category).where(Category.id == cat_id))
    cat = result.scalar_one()
    await message.answer(
        text,
        reply_markup=category_detail_kb(cat_id),
        parse_mode="HTML",
    )
    # Отдельным сообщением — актуальная карточка категории
    await message.answer(
        _cat_detail_text(cat),
        reply_markup=category_detail_kb(cat_id),
        parse_mode="HTML",
    )
    await state.set_state(CategorySG.detail)


@router.callback_query(F.data == "adm:categories")
async def cb_categories(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    result = await session.execute(select(Category).order_by(Category.id))
    cats = result.scalars().all()
    await state.set_state(CategorySG.list)
    await callback.message.edit_text(
        f"📂 <b>Категории</b> ({len(cats)})",
        reply_markup=categories_list_kb(list(cats)),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:cat:create:"))
async def cb_cat_create(callback: CallbackQuery, state: FSMContext) -> None:
    cat_type = callback.data.split(":")[-1]
    await state.update_data(new_cat_type=cat_type)
    await state.set_state(CategorySG.create_name)
    await callback.message.edit_text(
        "Введите название новой категории:",
        reply_markup=cancel_kb("adm:categories"),
    )
    await callback.answer()


@router.message(CategorySG.create_name)
async def process_cat_name(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    cat_type = CategoryType(data["new_cat_type"])
    cat = Category(name=message.text.strip(), type=cat_type)
    session.add(cat)
    await session.flush()

    users_result = await session.execute(select(User))
    users = users_result.scalars().all()
    for user in users:
        uc = UserCategory(user_id=user.id, category_id=cat.id, enabled=True)
        session.add(uc)

    await session.commit()

    result = await session.execute(select(Category).order_by(Category.id))
    cats = result.scalars().all()
    await message.answer(
        f"✅ Категория «{cat.name}» создана.\n\n"
        f"📂 <b>Категории</b> ({len(cats)})",
        reply_markup=categories_list_kb(list(cats)),
        parse_mode="HTML",
    )
    await state.set_state(CategorySG.list)


@router.callback_query(F.data.startswith("adm:cat:detail:"))
async def cb_cat_detail(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    cat_id = int(callback.data.split(":")[-1])
    result = await session.execute(select(Category).where(Category.id == cat_id))
    cat = result.scalar_one_or_none()
    if not cat:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    await _show_cat_detail(callback, state, cat)


@router.callback_query(F.data.startswith("adm:cat:toggle:"))
async def cb_cat_toggle(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    cat_id = int(callback.data.split(":")[-1])
    result = await session.execute(select(Category).where(Category.id == cat_id))
    cat = result.scalar_one_or_none()
    if not cat:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    cat.is_active = not cat.is_active
    await session.commit()
    await _show_cat_detail(callback, state, cat)


@router.callback_query(F.data.startswith("adm:cat:addkw:"))
async def cb_cat_addkw(callback: CallbackQuery, state: FSMContext) -> None:
    cat_id = int(callback.data.split(":")[-1])
    await state.update_data(current_cat_id=cat_id)
    await state.set_state(CategorySG.add_keyword)
    await callback.message.edit_text(
        "Введите ключевые <b>фразы</b> — каждая с новой строки.\n\n"
        "⚠️ <b>Важно:</b>\n"
        "• Одна строка = одна фраза\n"
        "• Слова внутри строки ищутся <b>все вместе</b> (AND)\n"
        "• Разные строки — это синонимы (OR)\n\n"
        "<i>Пример:\n"
        "<code>ищу смм специалиста</code> ← сработает только если все три слова рядом\n"
        "<code>нужен smm менеджер</code> ← или эта фраза\n\n"
        "❌ Не добавляйте каждое слово отдельной строкой!</i>",
        reply_markup=cancel_kb(f"adm:cat:detail:{cat_id}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(CategorySG.add_keyword)
async def process_add_kw(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    cat_id = data["current_cat_id"]
    result = await session.execute(select(Category).where(Category.id == cat_id))
    cat = result.scalar_one()
    new_kws = [k.strip().lower() for k in message.text.splitlines() if k.strip()]
    existing = cat.get_keywords()
    merged = list(dict.fromkeys(existing + new_kws))
    cat.set_keywords(merged)
    await session.commit()
    await _reply_and_show_detail(
        message, state, session,
        f"✅ Добавлено фраз: <b>{len(new_kws)}</b>. Всего: <b>{len(merged)}</b>.",
    )


@router.callback_query(F.data.startswith("adm:cat:delkw:"))
async def cb_cat_delkw(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    cat_id = int(callback.data.split(":")[-1])
    result = await session.execute(select(Category).where(Category.id == cat_id))
    cat = result.scalar_one()
    kws = cat.get_keywords()
    if not kws:
        await callback.answer("Ключевых фраз нет.", show_alert=True)
        return
    await state.update_data(current_cat_id=cat_id)
    await state.set_state(CategorySG.delete_keyword)
    await callback.message.edit_text(
        "Введите фразу для удаления:\n\n" + "\n".join(f"• {k}" for k in kws),
        reply_markup=cancel_kb(f"adm:cat:detail:{cat_id}"),
    )
    await callback.answer()


@router.message(CategorySG.delete_keyword)
async def process_del_kw(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    cat_id = data["current_cat_id"]
    result = await session.execute(select(Category).where(Category.id == cat_id))
    cat = result.scalar_one()
    to_delete = message.text.strip().lower()
    kws = [k for k in cat.get_keywords() if k != to_delete]
    cat.set_keywords(kws)
    await session.commit()
    await _reply_and_show_detail(
        message, state, session,
        f"✅ Фраза «{to_delete}» удалена.",
    )


@router.callback_query(F.data.startswith("adm:cat:addsw:"))
async def cb_cat_addsw(callback: CallbackQuery, state: FSMContext) -> None:
    cat_id = int(callback.data.split(":")[-1])
    await state.update_data(current_cat_id=cat_id)
    await state.set_state(CategorySG.add_stop_word)
    await callback.message.edit_text(
        "Введите минус-слова — каждое с новой строки или через запятую:\n\n"
        "<i>Пример:\n"
        "предлагаю\n"
        "продам\n"
        "услуги</i>\n\n"
        "Посты с этими словами <b>не будут</b> приходить.",
        reply_markup=cancel_kb(f"adm:cat:detail:{cat_id}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(CategorySG.add_stop_word)
async def process_add_sw(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    cat_id = data["current_cat_id"]
    result = await session.execute(select(Category).where(Category.id == cat_id))
    cat = result.scalar_one()
    raw = message.text.replace(",", "\n")
    new_sws = [w.strip().lower() for w in raw.splitlines() if w.strip()]
    existing = cat.get_stop_words()
    merged = list(dict.fromkeys(existing + new_sws))
    cat.set_stop_words(merged)
    await session.commit()
    await _reply_and_show_detail(
        message, state, session,
        f"✅ Добавлено минус-слов: <b>{len(new_sws)}</b>. Всего: <b>{len(merged)}</b>.",
    )


@router.callback_query(F.data.startswith("adm:cat:delsw:"))
async def cb_cat_delsw(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    cat_id = int(callback.data.split(":")[-1])
    result = await session.execute(select(Category).where(Category.id == cat_id))
    cat = result.scalar_one()
    sws = cat.get_stop_words()
    if not sws:
        await callback.answer("Минус-слов нет.", show_alert=True)
        return
    await state.update_data(current_cat_id=cat_id)
    await state.set_state(CategorySG.delete_stop_word)
    await callback.message.edit_text(
        "Введите минус-слово для удаления:\n\n" + "\n".join(f"• {w}" for w in sws),
        reply_markup=cancel_kb(f"adm:cat:detail:{cat_id}"),
    )
    await callback.answer()


@router.message(CategorySG.delete_stop_word)
async def process_del_sw(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    cat_id = data["current_cat_id"]
    result = await session.execute(select(Category).where(Category.id == cat_id))
    cat = result.scalar_one()
    to_delete = message.text.strip().lower()
    sws = [w for w in cat.get_stop_words() if w != to_delete]
    cat.set_stop_words(sws)
    await session.commit()
    await _reply_and_show_detail(
        message, state, session,
        f"✅ Минус-слово «{to_delete}» удалено.",
    )


@router.callback_query(F.data.startswith("adm:cat:delete:"))
async def cb_cat_delete(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    cat_id = int(callback.data.split(":")[-1])
    result = await session.execute(select(Category).where(Category.id == cat_id))
    cat = result.scalar_one_or_none()
    if cat:
        await session.delete(cat)
        await session.commit()
        await callback.answer("✅ Категория удалена.", show_alert=True)
    result2 = await session.execute(select(Category).order_by(Category.id))
    cats = result2.scalars().all()
    await callback.message.edit_text(
        f"📂 <b>Категории</b> ({len(cats)})",
        reply_markup=categories_list_kb(list(cats)),
        parse_mode="HTML",
    )
    await state.set_state(CategorySG.list)


async def _show_cat_accounts(callback: CallbackQuery, session: AsyncSession, cat_id: int) -> None:
    """Показывает страницу привязки аккаунтов к категории."""
    cat_r = await session.execute(select(Category).where(Category.id == cat_id))
    cat = cat_r.scalar_one_or_none()
    if not cat:
        await callback.answer("Категория не найдена.", show_alert=True)
        return

    accs_r = await session.execute(select(ParserAccount).order_by(ParserAccount.id))
    accounts = accs_r.scalars().all()

    assigned_r = await session.execute(
        select(CategoryAccount).where(CategoryAccount.category_id == cat_id)
    )
    assigned_ids = {ca.account_id for ca in assigned_r.scalars().all()}

    note = "  <i>Ни один не выбран — парсят все аккаунты.</i>" if not assigned_ids else ""
    text = (
        f"🤖 <b>Аккаунты для «{cat.name}»</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Выберите какие аккаунты будут собирать сообщения для этой категории.\n"
        f"{note}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=category_accounts_kb(cat_id, list(accounts), assigned_ids),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:cat:accounts:"))
async def cb_cat_accounts(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    cat_id = int(callback.data.split(":")[-1])
    await _show_cat_accounts(callback, session, cat_id)


@router.callback_query(F.data.startswith("adm:cat:acc_toggle:"))
async def cb_cat_acc_toggle(callback: CallbackQuery, session: AsyncSession) -> None:
    # adm:cat:acc_toggle:{cat_id}:{acc_id}
    parts = callback.data.split(":")
    cat_id, acc_id = int(parts[-2]), int(parts[-1])

    existing = await session.execute(
        select(CategoryAccount).where(
            CategoryAccount.category_id == cat_id,
            CategoryAccount.account_id == acc_id,
        )
    )
    ca = existing.scalar_one_or_none()

    if ca:
        await session.delete(ca)
        await session.commit()
        await callback.answer("Аккаунт откреплён от категории.", show_alert=False)
    else:
        session.add(CategoryAccount(category_id=cat_id, account_id=acc_id))
        await session.commit()
        await callback.answer("Аккаунт привязан к категории.", show_alert=False)

    await _show_cat_accounts(callback, session, cat_id)
