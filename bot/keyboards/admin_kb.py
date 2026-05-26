from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database.models import (
    User, TelegramGroup, ParserAccount, Proxy, Category, CategoryType,
)


def cancel_kb(callback_data: str, label: str = "◀ Отмена") -> InlineKeyboardMarkup:
    """Универсальная клавиатура с одной кнопкой отмены."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=label, callback_data=callback_data, style="primary"))
    return builder.as_markup()


def admin_main_kb(bot_username: str = "") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if bot_username:
        builder.row(InlineKeyboardButton(
            text="👥 Найти пользователя",
            switch_inline_query_current_chat="",
        ))
    builder.row(InlineKeyboardButton(text="📢 Рассылка", callback_data="adm:broadcast", style="primary"))
    builder.row(InlineKeyboardButton(text="🔗 Группы", callback_data="adm:groups", style="primary"))
    builder.row(InlineKeyboardButton(text="🤖 Аккаунты", callback_data="adm:accounts", style="primary"))
    builder.row(InlineKeyboardButton(text="🛡 Прокси", callback_data="adm:proxies", style="primary"))
    builder.row(InlineKeyboardButton(text="📂 Категории", callback_data="adm:categories", style="primary"))
    return builder.as_markup()


def users_list_kb(users: list[User], page: int = 0, page_size: int = 10) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    start = page * page_size
    for u in users[start:start + page_size]:
        label = f"@{u.username}" if u.username else u.full_name
        builder.row(InlineKeyboardButton(text=label, callback_data=f"adm:user:{u.id}", style="primary"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"adm:users:page:{page - 1}", style="primary"))
    if start + page_size < len(users):
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"adm:users:page:{page + 1}", style="primary"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔍 Поиск", callback_data="adm:users:search", style="primary"))
    builder.row(InlineKeyboardButton(text="◀ Назад", callback_data="adm:main", style="primary"))
    return builder.as_markup()


def user_detail_kb(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🎁 Выдать подписку", callback_data=f"adm:grant:{user_id}", style="success"))
    builder.row(InlineKeyboardButton(text="🚫 Отозвать подписку", callback_data=f"adm:revoke:{user_id}", style="danger"))
    builder.row(InlineKeyboardButton(text="✉ Написать сообщение", callback_data=f"adm:msg:{user_id}", style="primary"))
    builder.row(InlineKeyboardButton(text="◀ Назад", callback_data="adm:users", style="primary"))
    return builder.as_markup()


def broadcast_target_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="👥 Всем", callback_data="bcast:target:all", style="primary"))
    builder.row(InlineKeyboardButton(text="✅ Активным", callback_data="bcast:target:active", style="success"))
    builder.row(InlineKeyboardButton(text="💤 Неактивным", callback_data="bcast:target:inactive", style="danger"))
    builder.row(InlineKeyboardButton(text="💳 С подпиской", callback_data="bcast:target:subscribed", style="primary"))
    builder.row(InlineKeyboardButton(text="◀ Назад", callback_data="adm:main", style="primary"))
    return builder.as_markup()


def broadcast_content_type_kb(selected: set[str]) -> InlineKeyboardMarkup:
    types = [("text", "📝 Текст"), ("photo", "🖼 Картинка"), ("video", "🎥 Видео"), ("button", "🔘 Кнопка")]
    builder = InlineKeyboardBuilder()
    for key, label in types:
        style = "success" if key in selected else "primary"
        builder.row(InlineKeyboardButton(
            text=label,
            callback_data=f"bcast:type:{key}",
            style=style,
        ))
    if selected:
        builder.row(InlineKeyboardButton(text="➡ Продолжить", callback_data="bcast:type:done", style="success"))
    builder.row(InlineKeyboardButton(text="◀ Назад", callback_data="adm:broadcast", style="primary"))
    return builder.as_markup()


def groups_list_kb(groups: list[TelegramGroup]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for g in groups:
        style = "success" if g.is_active else "danger"
        status = "ВКЛ" if g.is_active else "ВЫКЛ"
        title = g.title or g.link
        builder.row(
            InlineKeyboardButton(
                text=f"{status}  {title[:35]}",
                callback_data=f"adm:grp:detail:{g.id}",
                style=style,
            )
        )
    builder.row(InlineKeyboardButton(text="➕ Добавить группу", callback_data="adm:grp:add", style="primary"))
    if groups:
        builder.row(InlineKeyboardButton(
            text="👥 Подписать все аккаунты на все группы",
            callback_data="adm:grp:joinall",
            style="primary",
        ))
        builder.row(InlineKeyboardButton(
            text="🗺 Матрица «аккаунт × группа»",
            callback_data="adm:grp:matrix",
            style="primary",
        ))
    builder.row(InlineKeyboardButton(text="◀ Назад", callback_data="adm:main", style="primary"))
    return builder.as_markup()


def group_detail_kb(group: TelegramGroup, assigned_count: int) -> InlineKeyboardMarkup:
    """Страница деталей одной группы."""
    builder = InlineKeyboardBuilder()
    active_label = "✅ Активна — выключить" if group.is_active else "❌ Выключена — включить"
    active_style = "success" if group.is_active else "danger"
    builder.row(InlineKeyboardButton(
        text=active_label,
        callback_data=f"adm:grp:toggle:{group.id}",
        style=active_style,
    ))
    cats_label = f"📂 Категории ({assigned_count if assigned_count else 'все'})"
    builder.row(InlineKeyboardButton(
        text=cats_label,
        callback_data=f"adm:grp:cats:{group.id}",
        style="primary",
    ))
    builder.row(InlineKeyboardButton(
        text="🗑 Удалить группу",
        callback_data=f"adm:grp:delete:{group.id}",
        style="danger",
    ))
    builder.row(InlineKeyboardButton(text="◀ Назад к списку", callback_data="adm:groups", style="primary"))
    return builder.as_markup()


def group_categories_kb(
    group_id: int,
    categories: list[Category],
    assigned_ids: set[int],
) -> InlineKeyboardMarkup:
    """Список категорий с тоглами для конкретной группы."""
    builder = InlineKeyboardBuilder()
    for cat in categories:
        is_on = cat.id in assigned_ids
        icon = "✅" if is_on else "☐"
        style = "success" if is_on else "primary"
        builder.row(InlineKeyboardButton(
            text=f"{icon} {cat.name}",
            callback_data=f"adm:grp:cat_toggle:{group_id}:{cat.id}",
            style=style,
        ))
    builder.row(InlineKeyboardButton(
        text="◀ Назад к группе",
        callback_data=f"adm:grp:detail:{group_id}",
        style="primary",
    ))
    return builder.as_markup()


def accounts_list_kb(accounts: list[ParserAccount]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for acc in accounts:
        style = "success" if acc.is_active and acc.is_valid else "danger"
        label = acc.phone or f"ID {acc.id}"
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=f"adm:acc:detail:{acc.id}",
                style=style,
            )
        )
    builder.row(InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="adm:acc:add", style="primary"))
    builder.row(InlineKeyboardButton(text="◀ Назад", callback_data="adm:main", style="primary"))
    return builder.as_markup()


def account_detail_kb(acc_id: int, parse_joined: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    joined_label = "✅ Парсить свои группы: ВКЛ" if parse_joined else "❌ Парсить свои группы: ВЫКЛ"
    joined_style = "success" if parse_joined else "danger"
    builder.row(InlineKeyboardButton(
        text=joined_label,
        callback_data=f"adm:acc:toggle_joined:{acc_id}",
        style=joined_style,
    ))
    builder.row(InlineKeyboardButton(
        text="🔄 Перевыдать сессию",
        callback_data=f"adm:acc:reissue:{acc_id}",
        style="primary",
    ))
    builder.row(InlineKeyboardButton(
        text="🗑 Удалить аккаунт",
        callback_data=f"adm:acc:delete:{acc_id}",
        style="danger",
    ))
    builder.row(InlineKeyboardButton(text="◀ Назад", callback_data="adm:accounts", style="primary"))
    return builder.as_markup()


def proxies_list_kb(proxies: list[Proxy]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for p in proxies:
        style = "success" if p.is_working else ("danger" if p.is_working is False else "primary")
        builder.row(
            InlineKeyboardButton(
                text=f"{p.host}:{p.port}",
                callback_data=f"adm:proxy:check:{p.id}",
                style=style,
            ),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm:proxy:delete:{p.id}", style="danger"),
        )
    builder.row(InlineKeyboardButton(text="➕ Добавить прокси", callback_data="adm:proxy:add", style="primary"))
    builder.row(InlineKeyboardButton(text="◀ Назад", callback_data="adm:main", style="primary"))
    return builder.as_markup()


def categories_list_kb(categories: list[Category], cat_type: CategoryType | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    shown = [c for c in categories if cat_type is None or c.type == cat_type]
    for cat in shown:
        style = "success" if cat.is_active else "danger"
        status = "ВКЛ" if cat.is_active else "ВЫКЛ"
        builder.row(
            InlineKeyboardButton(
                text=f"{status}  {cat.name}",
                callback_data=f"adm:cat:detail:{cat.id}",
                style=style,
            )
        )
    builder.row(InlineKeyboardButton(text="➕ Запрос", callback_data="adm:cat:create:request", style="success"))
    builder.row(InlineKeyboardButton(text="➕ Предложение", callback_data="adm:cat:create:offer", style="success"))
    builder.row(InlineKeyboardButton(text="◀ Назад", callback_data="adm:main", style="primary"))
    return builder.as_markup()


def category_detail_kb(cat_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Ключевое слово", callback_data=f"adm:cat:addkw:{cat_id}", style="success"))
    builder.row(InlineKeyboardButton(text="➖ Удалить ключевое слово", callback_data=f"adm:cat:delkw:{cat_id}", style="danger"))
    builder.row(InlineKeyboardButton(text="🚫 Добавить минус-слово", callback_data=f"adm:cat:addsw:{cat_id}", style="primary"))
    builder.row(InlineKeyboardButton(text="✂️ Удалить минус-слово", callback_data=f"adm:cat:delsw:{cat_id}", style="danger"))
    builder.row(InlineKeyboardButton(text="🤖 Аккаунты-парсеры", callback_data=f"adm:cat:accounts:{cat_id}", style="primary"))
    builder.row(InlineKeyboardButton(text="🔄 Вкл/Выкл", callback_data=f"adm:cat:toggle:{cat_id}", style="primary"))
    builder.row(InlineKeyboardButton(text="🗑 Удалить категорию", callback_data=f"adm:cat:delete:{cat_id}", style="danger"))
    builder.row(InlineKeyboardButton(text="◀ Назад", callback_data="adm:categories", style="primary"))
    return builder.as_markup()


def category_accounts_kb(cat_id: int, accounts: list[ParserAccount], assigned_ids: set[int]) -> InlineKeyboardMarkup:
    """Список аккаунтов с тоглом — какие парсят эту категорию."""
    builder = InlineKeyboardBuilder()
    if not accounts:
        builder.row(InlineKeyboardButton(text="⚠️ Нет аккаунтов", callback_data="noop", style="primary"))
    for acc in accounts:
        label = acc.phone or f"ID {acc.id}"
        is_on = acc.id in assigned_ids
        icon = "✅" if is_on else "☐"
        style = "success" if is_on else "primary"
        builder.row(InlineKeyboardButton(
            text=f"{icon} {label}",
            callback_data=f"adm:cat:acc_toggle:{cat_id}:{acc.id}",
            style=style,
        ))
    note = (
        "\n<i>Если ни один не выбран — парсят все аккаунты.</i>"
        if not assigned_ids else ""
    )
    builder.row(InlineKeyboardButton(text="◀ Назад", callback_data=f"adm:cat:detail:{cat_id}", style="primary"))
    return builder.as_markup()
