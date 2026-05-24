"""Tests for keyboard generation functions."""
from __future__ import annotations

import pytest

from database.models import Category, CategoryType, UserCategory, Proxy, TelegramGroup, User
from bot.keyboards.user_kb import (
    main_menu_kb, categories_kb, message_action_kb, subscription_kb,
)
from bot.keyboards.admin_kb import (
    admin_main_kb, proxies_list_kb, users_list_kb, broadcast_target_kb,
    broadcast_content_type_kb, groups_list_kb, categories_list_kb,
    category_detail_kb, user_detail_kb,
)


def _get_all_buttons(kb):
    """Flatten inline keyboard rows into a list of buttons."""
    return [btn for row in kb.inline_keyboard for btn in row]


def _get_callback_datas(kb) -> list[str]:
    return [btn.callback_data for btn in _get_all_buttons(kb) if btn.callback_data]


def _get_button_texts(kb) -> list[str]:
    return [btn.text for btn in _get_all_buttons(kb)]


class TestMainMenuKeyboard:
    def test_has_profile_button(self):
        kb = main_menu_kb(receiving_enabled=False)
        assert "👤 Профиль" in _get_button_texts(kb)

    def test_receiving_off_shows_red(self):
        kb = main_menu_kb(receiving_enabled=False)
        texts = _get_button_texts(kb)
        assert any("ВЫКЛ" in t for t in texts)
        assert not any("ВКЛ" in t for t in texts)

    def test_receiving_on_shows_green(self):
        kb = main_menu_kb(receiving_enabled=True)
        texts = _get_button_texts(kb)
        assert any("ВКЛ" in t for t in texts)

    def test_has_support_url_button(self):
        kb = main_menu_kb(receiving_enabled=False)
        buttons = _get_all_buttons(kb)
        url_buttons = [b for b in buttons if b.url]
        assert len(url_buttons) >= 1

    def test_has_categories_buttons(self):
        kb = main_menu_kb(receiving_enabled=False)
        cbs = _get_callback_datas(kb)
        assert "cats_request" in cbs
        assert "cats_offer" in cbs

    def test_has_buy_subscription_button(self):
        kb = main_menu_kb(receiving_enabled=False)
        assert "buy_subscription" in _get_callback_datas(kb)

    def test_has_instruction_button(self):
        kb = main_menu_kb(receiving_enabled=False)
        assert "instruction" in _get_callback_datas(kb)


class TestCategoriesKeyboard:
    def _make_cats(self) -> list[Category]:
        return [
            Category(id=1, name="Дизайн", type=CategoryType.request, is_active=True),
            Category(id=2, name="Разработка", type=CategoryType.request, is_active=True),
            Category(id=3, name="SEO", type=CategoryType.offer, is_active=True),
        ]

    def _make_ucs(self, enabled_ids: list[int]) -> list[UserCategory]:
        return [UserCategory(category_id=cid, enabled=True) for cid in enabled_ids]

    def test_shows_only_matching_type(self):
        cats = self._make_cats()
        ucs = self._make_ucs([1, 2])
        kb = categories_kb(CategoryType.request, cats, ucs)
        texts = _get_button_texts(kb)
        assert "Дизайн" in " ".join(texts)
        assert "Разработка" in " ".join(texts)
        assert "SEO" not in " ".join(texts)

    def test_enabled_category_shows_green_style(self):
        cats = self._make_cats()
        ucs = self._make_ucs([1])
        kb = categories_kb(CategoryType.request, cats, ucs)
        buttons = _get_all_buttons(kb)
        design_btn = next(b for b in buttons if "Дизайн" in b.text)
        assert design_btn.style == "success"

    def test_disabled_category_shows_red_style(self):
        cats = self._make_cats()
        ucs = self._make_ucs([])
        kb = categories_kb(CategoryType.request, cats, ucs)
        buttons = _get_all_buttons(kb)
        design_btn = next(b for b in buttons if "Дизайн" in b.text)
        assert design_btn.style == "danger"

    def test_toggle_callback_format(self):
        cats = self._make_cats()
        kb = categories_kb(CategoryType.request, cats, [])
        cbs = _get_callback_datas(kb)
        assert "toggle_cat:1" in cbs
        assert "toggle_cat:2" in cbs

    def test_inactive_category_not_shown(self):
        cats = [Category(id=1, name="Дизайн", type=CategoryType.request, is_active=False)]
        kb = categories_kb(CategoryType.request, cats, [])
        texts = _get_button_texts(kb)
        assert not any("Дизайн" in t for t in texts)

    def test_has_back_button(self):
        kb = categories_kb(CategoryType.request, [], [])
        assert "main_menu" in _get_callback_datas(kb)


class TestMessageActionKeyboard:
    def test_with_username_generates_url(self):
        kb = message_action_kb("testuser", None)
        buttons = _get_all_buttons(kb)
        assert any("https://t.me/testuser" in (b.url or "") for b in buttons)

    def test_with_explicit_link(self):
        kb = message_action_kb(None, "tg://user?id=123")
        buttons = _get_all_buttons(kb)
        assert any("tg://user?id=123" in (b.url or "") for b in buttons)

    def test_no_author_no_button(self):
        kb = message_action_kb(None, None)
        assert len(_get_all_buttons(kb)) == 0

    def test_explicit_link_takes_priority_over_username(self):
        kb = message_action_kb("user", "https://t.me/customlink")
        buttons = _get_all_buttons(kb)
        assert any("https://t.me/customlink" in (b.url or "") for b in buttons)


class TestSubscriptionKeyboard:
    def test_has_all_plans(self):
        kb = subscription_kb()
        cbs = _get_callback_datas(kb)
        assert "plan_trial" in cbs
        assert "plan_1m" in cbs
        assert "plan_3m" in cbs
        assert "plan_1y" in cbs

    def test_has_back_button(self):
        kb = subscription_kb()
        assert "main_menu" in _get_callback_datas(kb)


class TestAdminKeyboards:
    def test_admin_main_has_all_sections(self):
        kb = admin_main_kb(bot_username="testbot")
        cbs = _get_callback_datas(kb)
        # Users section moved to inline — no adm:users callback anymore
        assert "adm:broadcast" in cbs
        assert "adm:groups" in cbs
        assert "adm:accounts" in cbs
        assert "adm:proxies" in cbs
        assert "adm:categories" in cbs

    def test_admin_main_has_inline_users_button(self):
        kb = admin_main_kb(bot_username="testbot")
        texts = _get_button_texts(kb)
        assert any("Найти пользователя" in t for t in texts)

    def test_proxies_list_has_delete_button(self):
        proxy = Proxy(id=1, host="1.2.3.4", port=1080, type="socks5")
        kb = proxies_list_kb([proxy])
        cbs = _get_callback_datas(kb)
        assert "adm:proxy:delete:1" in cbs

    def test_proxies_list_has_check_button(self):
        proxy = Proxy(id=1, host="1.2.3.4", port=1080, type="socks5")
        kb = proxies_list_kb([proxy])
        cbs = _get_callback_datas(kb)
        assert "adm:proxy:check:1" in cbs

    def test_proxies_list_add_button(self):
        kb = proxies_list_kb([])
        assert "adm:proxy:add" in _get_callback_datas(kb)

    def test_broadcast_target_all_options(self):
        kb = broadcast_target_kb()
        cbs = _get_callback_datas(kb)
        assert "bcast:target:all" in cbs
        assert "bcast:target:active" in cbs
        assert "bcast:target:inactive" in cbs
        assert "bcast:target:subscribed" in cbs

    def test_broadcast_content_type_no_done_when_empty(self):
        kb = broadcast_content_type_kb(set())
        cbs = _get_callback_datas(kb)
        assert "bcast:type:done" not in cbs

    def test_broadcast_content_type_done_when_selected(self):
        kb = broadcast_content_type_kb({"text"})
        cbs = _get_callback_datas(kb)
        assert "bcast:type:done" in cbs

    def test_broadcast_content_type_selected_has_success_style(self):
        kb = broadcast_content_type_kb({"text"})
        buttons = _get_all_buttons(kb)
        text_btn = next(b for b in buttons if "Текст" in b.text)
        assert text_btn.style == "success"

    def test_users_list_pagination_next(self):
        users = [User(id=i, full_name=f"User {i}") for i in range(1, 16)]
        kb = users_list_kb(users, page=0, page_size=10)
        cbs = _get_callback_datas(kb)
        assert "adm:users:page:1" in cbs

    def test_users_list_pagination_prev(self):
        users = [User(id=i, full_name=f"User {i}") for i in range(1, 16)]
        kb = users_list_kb(users, page=1, page_size=10)
        cbs = _get_callback_datas(kb)
        assert "adm:users:page:0" in cbs

    def test_users_list_no_prev_on_first_page(self):
        users = [User(id=i, full_name=f"User {i}") for i in range(1, 5)]
        kb = users_list_kb(users, page=0)
        cbs = _get_callback_datas(kb)
        assert "adm:users:page:-1" not in cbs

    def test_user_detail_has_grant_revoke_message(self):
        kb = user_detail_kb(42)
        cbs = _get_callback_datas(kb)
        assert "adm:grant:42" in cbs
        assert "adm:revoke:42" in cbs
        assert "adm:msg:42" in cbs

    def test_category_detail_has_crud_buttons(self):
        kb = category_detail_kb(5)
        cbs = _get_callback_datas(kb)
        assert "adm:cat:addkw:5" in cbs
        assert "adm:cat:delkw:5" in cbs
        assert "adm:cat:toggle:5" in cbs
        assert "adm:cat:delete:5" in cbs

    def test_categories_list_has_create_buttons(self):
        kb = categories_list_kb([])
        cbs = _get_callback_datas(kb)
        assert "adm:cat:create:request" in cbs
        assert "adm:cat:create:offer" in cbs

    def test_groups_list_detail_callback(self):
        from datetime import datetime
        g = TelegramGroup(id=7, link="https://t.me/test", title="Test", is_active=True, added_at=datetime.utcnow())
        kb = groups_list_kb([g])
        # Clicking a group now opens detail page, not direct toggle
        assert "adm:grp:detail:7" in _get_callback_datas(kb)

    def test_groups_list_add_button(self):
        kb = groups_list_kb([])
        assert "adm:grp:add" in _get_callback_datas(kb)
