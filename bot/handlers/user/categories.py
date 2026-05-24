from __future__ import annotations

from aiogram import Router, F
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.keyboards import categories_kb
from database.models import Category, UserCategory, CategoryType, User

router = Router(name="categories")


async def _show_categories(callback: CallbackQuery, session: AsyncSession, cat_type: CategoryType) -> None:
    cats_result = await session.execute(
        select(Category).where(Category.is_active == True, Category.type == cat_type)
    )
    categories = cats_result.scalars().all()

    uc_result = await session.execute(
        select(UserCategory).where(UserCategory.user_id == callback.from_user.id)
    )
    user_cats = uc_result.scalars().all()

    label = "запросов" if cat_type == CategoryType.request else "предложений"
    text = (
        f"📂 <b>Категории {label}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Нажмите на категорию чтобы включить или выключить её."
    )
    await callback.message.edit_text(
        text,
        reply_markup=categories_kb(cat_type, categories, user_cats),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "cats_request")
async def cb_cats_request(callback: CallbackQuery, session: AsyncSession) -> None:
    await _show_categories(callback, session, CategoryType.request)


@router.callback_query(F.data == "cats_offer")
async def cb_cats_offer(callback: CallbackQuery, session: AsyncSession) -> None:
    await _show_categories(callback, session, CategoryType.offer)


@router.callback_query(F.data.startswith("toggle_cat:"))
async def cb_toggle_cat(callback: CallbackQuery, session: AsyncSession) -> None:
    cat_id = int(callback.data.split(":")[1])

    result = await session.execute(
        select(UserCategory).where(
            UserCategory.user_id == callback.from_user.id,
            UserCategory.category_id == cat_id,
        )
    )
    uc = result.scalar_one_or_none()
    if not uc:
        uc = UserCategory(user_id=callback.from_user.id, category_id=cat_id, enabled=True)
        session.add(uc)
    else:
        uc.enabled = not uc.enabled

    await session.commit()

    cat_result = await session.execute(select(Category).where(Category.id == cat_id))
    cat = cat_result.scalar_one()
    cat_type = cat.type

    await _show_categories(callback, session, cat_type)
