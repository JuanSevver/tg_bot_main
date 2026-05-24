"""Integration tests against in-memory SQLite database."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from database.models import (
    Category, CategoryType, Subscription, User, UserCategory,
    ParsedMessage, TelegramGroup, Proxy,
)


class TestUserPersistence:
    async def test_create_and_fetch_user(self, session, make_user):
        user = make_user(user_id=42)
        session.add(user)
        await session.commit()

        result = await session.execute(select(User).where(User.id == 42))
        fetched = result.scalar_one()
        assert fetched.username == "user42"
        assert fetched.receiving_enabled is False

    async def test_update_receiving_enabled(self, session, make_user):
        user = make_user(user_id=1)
        session.add(user)
        await session.commit()

        user.receiving_enabled = True
        await session.commit()

        result = await session.execute(select(User).where(User.id == 1))
        assert result.scalar_one().receiving_enabled is True

    async def test_captcha_passed_updates(self, session, make_user):
        user = make_user(user_id=1)
        session.add(user)
        await session.commit()

        user.captcha_passed = True
        user.trial_used = True
        await session.commit()

        result = await session.execute(select(User).where(User.id == 1))
        u = result.scalar_one()
        assert u.captcha_passed is True
        assert u.trial_used is True


class TestSubscriptionPersistence:
    async def test_grant_trial_subscription(self, session, make_user, make_subscription):
        user = make_user(user_id=1)
        session.add(user)
        await session.commit()

        sub = make_subscription(user_id=1, plan="trial", days=3, purchases=0)
        session.add(sub)
        await session.commit()

        result = await session.execute(select(Subscription).where(Subscription.user_id == 1))
        fetched = result.scalar_one()
        assert fetched.plan == "trial"
        assert fetched.is_active is True
        assert fetched.purchases_count == 0

    async def test_extend_subscription(self, session, make_user, make_subscription):
        user = make_user(user_id=1)
        session.add(user)
        await session.commit()

        sub = make_subscription(user_id=1, plan="1m", days=30, purchases=1)
        session.add(sub)
        await session.commit()

        original_expires = sub.expires_at
        sub.expires_at = sub.expires_at + timedelta(days=30)
        sub.purchases_count += 1
        await session.commit()

        result = await session.execute(select(Subscription).where(Subscription.user_id == 1))
        fetched = result.scalar_one()
        assert fetched.purchases_count == 2
        assert fetched.expires_at > original_expires

    async def test_revoke_subscription(self, session, make_user, make_subscription):
        user = make_user(user_id=1)
        session.add(user)
        await session.commit()

        sub = make_subscription(user_id=1, days=30)
        session.add(sub)
        await session.commit()

        sub.expires_at = datetime.utcnow() - timedelta(seconds=1)
        await session.commit()

        result = await session.execute(select(Subscription).where(Subscription.user_id == 1))
        assert result.scalar_one().is_active is False


class TestCategoryPersistence:
    async def test_create_category(self, session, make_category):
        cat = make_category(cat_id=1, name="Дизайн", keywords="лого\nбаннер")
        session.add(cat)
        await session.commit()

        result = await session.execute(select(Category).where(Category.id == 1))
        fetched = result.scalar_one()
        assert fetched.name == "Дизайн"
        assert "лого" in fetched.get_keywords()

    async def test_toggle_category_active(self, session, make_category):
        cat = make_category()
        session.add(cat)
        await session.commit()

        cat.is_active = False
        await session.commit()

        result = await session.execute(select(Category).where(Category.id == cat.id))
        assert result.scalar_one().is_active is False

    async def test_new_category_added_to_all_users(self, session, make_user):
        """Simulate admin creating a new category — all users get UserCategory entry."""
        users = [make_user(user_id=i) for i in range(1, 4)]
        for u in users:
            session.add(u)
        await session.commit()

        cat = Category(name="Новая", type=CategoryType.request)
        session.add(cat)
        await session.flush()

        for user in users:
            uc = UserCategory(user_id=user.id, category_id=cat.id, enabled=True)
            session.add(uc)
        await session.commit()

        result = await session.execute(select(UserCategory).where(UserCategory.category_id == cat.id))
        ucs = result.scalars().all()
        assert len(ucs) == 3
        assert all(uc.enabled for uc in ucs)

    async def test_add_keyword_dedup(self, session, make_category):
        cat = make_category(keywords="дизайн\nлоготип")
        session.add(cat)
        await session.commit()

        existing = cat.get_keywords()
        new_kws = ["логотип", "баннер"]
        merged = list(dict.fromkeys(existing + new_kws))
        cat.set_keywords(merged)
        await session.commit()

        result = await session.execute(select(Category).where(Category.id == cat.id))
        fetched = result.scalar_one()
        kws = fetched.get_keywords()
        assert kws.count("логотип") == 1
        assert "баннер" in kws

    async def test_delete_keyword(self, session, make_category):
        cat = make_category(keywords="дизайн\nлоготип\nбаннер")
        session.add(cat)
        await session.commit()

        kws = [k for k in cat.get_keywords() if k != "логотип"]
        cat.set_keywords(kws)
        await session.commit()

        result = await session.execute(select(Category).where(Category.id == cat.id))
        assert "логотип" not in result.scalar_one().get_keywords()


class TestUserCategoryToggles:
    async def test_toggle_user_category_off(self, session, make_user, make_category):
        user = make_user(user_id=1)
        session.add(user)
        await session.flush()
        cat = make_category()
        session.add(cat)
        await session.commit()

        uc = UserCategory(user_id=1, category_id=cat.id, enabled=True)
        session.add(uc)
        await session.commit()

        uc.enabled = False
        await session.commit()

        result = await session.execute(
            select(UserCategory).where(UserCategory.user_id == 1, UserCategory.category_id == cat.id)
        )
        assert result.scalar_one().enabled is False

    async def test_unique_constraint_user_category(self, session, make_user, make_category):
        from sqlalchemy.exc import IntegrityError
        user = make_user(user_id=1)
        session.add(user)
        await session.flush()
        cat = make_category()
        session.add(cat)
        await session.commit()

        uc1 = UserCategory(user_id=1, category_id=cat.id, enabled=True)
        session.add(uc1)
        await session.commit()

        session2_result = await session.execute(
            select(UserCategory).where(
                UserCategory.user_id == 1, UserCategory.category_id == cat.id
            )
        )
        assert session2_result.scalar_one() is not None  # first record exists

        uc2 = UserCategory(user_id=1, category_id=cat.id, enabled=False)
        session.add(uc2)
        with pytest.raises(IntegrityError):
            await session.commit()


class TestDeduplication:
    async def test_parsed_message_dedup(self, session):
        """Second insert of same (group_id, message_id) must raise IntegrityError."""
        from sqlalchemy.exc import IntegrityError

        pm1 = ParsedMessage(group_id=100, message_id=1, text="test")
        session.add(pm1)
        await session.commit()

        pm2 = ParsedMessage(group_id=100, message_id=1, text="duplicate")
        session.add(pm2)
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_different_message_ids_allowed(self, session):
        pm1 = ParsedMessage(group_id=100, message_id=1, text="first")
        pm2 = ParsedMessage(group_id=100, message_id=2, text="second")
        session.add(pm1)
        session.add(pm2)
        await session.commit()

        result = await session.execute(select(ParsedMessage).where(ParsedMessage.group_id == 100))
        assert len(result.scalars().all()) == 2

    async def test_same_message_id_different_groups_allowed(self, session):
        pm1 = ParsedMessage(group_id=100, message_id=1, text="group A")
        pm2 = ParsedMessage(group_id=200, message_id=1, text="group B")
        session.add(pm1)
        session.add(pm2)
        await session.commit()

        result = await session.execute(select(ParsedMessage))
        assert len(result.scalars().all()) == 2


class TestProxyPersistence:
    async def test_create_proxy(self, session):
        proxy = Proxy(host="127.0.0.1", port=1080, type="socks5")
        session.add(proxy)
        await session.commit()

        result = await session.execute(select(Proxy))
        p = result.scalar_one()
        assert p.host == "127.0.0.1"
        assert p.port == 1080
        assert p.is_working is None

    async def test_delete_proxy(self, session):
        proxy = Proxy(host="1.2.3.4", port=8080, type="http")
        session.add(proxy)
        await session.commit()

        await session.delete(proxy)
        await session.commit()

        result = await session.execute(select(Proxy))
        assert result.scalar_one_or_none() is None

    async def test_proxy_check_status_update(self, session):
        proxy = Proxy(host="1.2.3.4", port=1080, type="socks5")
        session.add(proxy)
        await session.commit()

        proxy.is_working = True
        proxy.last_checked_at = datetime.utcnow()
        await session.commit()

        result = await session.execute(select(Proxy))
        assert result.scalar_one().is_working is True


class TestTelegramGroups:
    async def test_add_group(self, session):
        group = TelegramGroup(link="https://t.me/testgroup", title="Test Group")
        session.add(group)
        await session.commit()

        result = await session.execute(select(TelegramGroup))
        g = result.scalar_one()
        assert g.link == "https://t.me/testgroup"
        assert g.is_active is True

    async def test_toggle_group_active(self, session):
        group = TelegramGroup(link="https://t.me/testgroup2")
        session.add(group)
        await session.commit()

        group.is_active = False
        await session.commit()

        result = await session.execute(select(TelegramGroup))
        assert result.scalar_one().is_active is False

    async def test_duplicate_link_rejected(self, session):
        from sqlalchemy.exc import IntegrityError
        g1 = TelegramGroup(link="https://t.me/same")
        g2 = TelegramGroup(link="https://t.me/same")
        session.add(g1)
        await session.commit()
        session.add(g2)
        with pytest.raises(IntegrityError):
            await session.commit()
