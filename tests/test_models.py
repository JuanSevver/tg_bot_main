"""Tests for database model methods and properties."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from database.models import Category, CategoryType, Subscription, User


class TestCategoryKeywords:
    """Keywords are stored as newline-separated phrases (one phrase per line)."""

    def test_get_keywords_parses_newline_separated(self, make_category):
        cat = make_category(keywords="логотип\nдизайн\nбаннер")
        assert cat.get_keywords() == ["логотип", "дизайн", "баннер"]

    def test_get_keywords_strips_spaces(self, make_category):
        cat = make_category(keywords="  лого  \n  дизайн  \n  ux  ")
        assert cat.get_keywords() == ["лого", "дизайн", "ux"]

    def test_get_keywords_empty_string(self, make_category):
        cat = make_category(keywords="")
        assert cat.get_keywords() == []

    def test_get_keywords_skips_blank_lines(self, make_category):
        cat = make_category(keywords="лого\n\nдизайн\n")
        assert cat.get_keywords() == ["лого", "дизайн"]

    def test_get_keywords_lowercased(self, make_category):
        cat = make_category(keywords="Логотип\nДИЗАЙН")
        assert cat.get_keywords() == ["логотип", "дизайн"]

    def test_set_keywords_roundtrip(self, make_category):
        cat = make_category(keywords="")
        kws = ["дизайн", "логотип", "макет"]
        cat.set_keywords(kws)
        assert cat.get_keywords() == kws

    def test_set_keywords_empty_list(self, make_category):
        cat = make_category(keywords="дизайн")
        cat.set_keywords([])
        assert cat.get_keywords() == []

    def test_keyword_dedup_on_add(self, make_category):
        """Simulate add-keyword handler: merge keeps insertion order, no dups."""
        cat = make_category(keywords="дизайн\nлоготип")
        existing = cat.get_keywords()
        new_kws = ["логотип", "баннер"]
        merged = list(dict.fromkeys(existing + new_kws))
        cat.set_keywords(merged)
        assert cat.get_keywords() == ["дизайн", "логотип", "баннер"]

    def test_phrase_with_spaces_stored_as_one_keyword(self, make_category):
        """Multi-word phrase on one line = one keyword (AND match required)."""
        cat = make_category(keywords="ищу дизайнера\nнужен логотип")
        kws = cat.get_keywords()
        assert len(kws) == 2
        assert "ищу дизайнера" in kws
        assert "нужен логотип" in kws


class TestCategoryStopWords:
    def test_get_stop_words_newline_separated(self, make_category):
        cat = make_category(keywords="")
        cat.stop_words = "предлагаю\nпродам\nуслуги"
        assert cat.get_stop_words() == ["предлагаю", "продам", "услуги"]

    def test_set_stop_words_roundtrip(self, make_category):
        cat = make_category(keywords="")
        cat.set_stop_words(["предлагаю", "продам"])
        assert cat.get_stop_words() == ["предлагаю", "продам"]

    def test_empty_stop_words(self, make_category):
        cat = make_category(keywords="")
        assert cat.get_stop_words() == []


class TestSubscriptionProperties:
    def test_is_active_true_when_not_expired(self, make_subscription):
        sub = make_subscription(user_id=1, days=10)
        assert sub.is_active is True

    def test_is_active_false_when_expired(self, make_subscription):
        sub = make_subscription(user_id=1, days=-1)
        assert sub.is_active is False

    def test_days_left_positive(self, make_subscription):
        sub = make_subscription(user_id=1, days=30)
        assert 28 <= sub.days_left <= 30

    def test_days_left_zero_when_expired(self, make_subscription):
        sub = make_subscription(user_id=1, days=-5)
        assert sub.days_left == 0

    def test_plan_stored_correctly(self, make_subscription):
        for plan in ("trial", "1m", "3m", "1y"):
            sub = make_subscription(user_id=1, plan=plan)
            assert sub.plan == plan

    def test_purchases_count_for_trial_is_zero(self, make_subscription):
        sub = make_subscription(user_id=1, plan="trial", purchases=0)
        assert sub.purchases_count == 0

    def test_purchases_count_increments_for_paid(self, make_subscription):
        sub = make_subscription(user_id=1, plan="1m", purchases=3)
        assert sub.purchases_count == 3


class TestUserDefaults:
    def test_new_user_receiving_disabled(self, make_user):
        assert make_user().receiving_enabled is False

    def test_new_user_captcha_not_passed(self, make_user):
        assert make_user().captcha_passed is False

    def test_new_user_trial_not_used(self, make_user):
        assert make_user().trial_used is False

    def test_user_not_blocked_by_default(self, make_user):
        assert make_user().is_blocked is False

    def test_messages_received_zero_by_default(self, make_user):
        assert make_user().messages_received == 0

    def test_user_id_stored_as_telegram_id(self, make_user):
        user = make_user(user_id=123456789)
        assert user.id == 123456789
