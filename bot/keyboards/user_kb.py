from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database.models import Category, UserCategory, CategoryType
from config import load_config

_config = load_config()


def main_menu_kb(receiving_enabled: bool) -> InlineKeyboardMarkup:
    receive_text = "Получать запросы: ВКЛ" if receiving_enabled else "Получать запросы: ВЫКЛ"
    receive_style = "success" if receiving_enabled else "danger"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="👤 Профиль", callback_data="profile", style="primary"))
    builder.row(InlineKeyboardButton(text=receive_text, callback_data="toggle_receive", style=receive_style))
    builder.row(InlineKeyboardButton(text="📋 Инструкция", callback_data="instruction", style="primary"))
    builder.row(InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{_config.support_username}"))
    builder.row(InlineKeyboardButton(text="💳 Купить подписку", callback_data="buy_subscription", style="primary"))
    builder.row(InlineKeyboardButton(text="📂 Категории запросов", callback_data="cats_request", style="primary"))
    builder.row(InlineKeyboardButton(text="📂 Категории предложений", callback_data="cats_offer", style="primary"))
    return builder.as_markup()


def profile_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀ Назад", callback_data="main_menu", style="primary"))
    return builder.as_markup()


def instruction_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀ Назад", callback_data="main_menu", style="primary"))
    return builder.as_markup()


def subscription_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🆓 Пробный (3 дня)", callback_data="plan_trial", style="success"))
    builder.row(InlineKeyboardButton(text="📅 1 месяц — 20 USDT", callback_data="plan_1m", style="primary"))
    builder.row(InlineKeyboardButton(text="📅 3 месяца — 45 USDT", callback_data="plan_3m", style="primary"))
    builder.row(InlineKeyboardButton(text="📅 1 год — 200 USDT", callback_data="plan_1y", style="primary"))
    builder.row(InlineKeyboardButton(text="◀ Назад", callback_data="main_menu", style="primary"))
    return builder.as_markup()


def payment_kb(plan: str, invoice_url: str | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if invoice_url:
        builder.row(InlineKeyboardButton(text="💎 Оплатить Crypto USDT", url=invoice_url, style="success"))
    builder.row(
        InlineKeyboardButton(
            text="🆘 Купить через поддержку",
            url=f"https://t.me/{_config.support_username}",
        )
    )
    builder.row(InlineKeyboardButton(text="◀ Назад", callback_data="buy_subscription", style="primary"))
    return builder.as_markup()


def categories_kb(
    cat_type: CategoryType,
    categories: list[Category],
    user_cats: list[UserCategory],
) -> InlineKeyboardMarkup:
    enabled_ids = {uc.category_id for uc in user_cats if uc.enabled}
    builder = InlineKeyboardBuilder()
    for cat in categories:
        if cat.type == cat_type and cat.is_active:
            is_on = cat.id in enabled_ids
            style = "success" if is_on else "danger"
            label = f"{'ВКЛ' if is_on else 'ВЫКЛ'}  {cat.name}"
            builder.row(
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"toggle_cat:{cat.id}",
                    style=style,
                )
            )
    builder.row(InlineKeyboardButton(text="◀ Назад", callback_data="main_menu", style="primary"))
    return builder.as_markup()


def message_action_kb(author_username: str | None, author_link: str | None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    link = author_link or (f"https://t.me/{author_username}" if author_username else None)
    if link:
        builder.row(InlineKeyboardButton(text="✉ Написать пользователю", url=link, style="primary"))
    return builder.as_markup()
