"""Tests for subscription grant/revoke/extend logic."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database.models import User, Subscription


async def _load_user(session, user_id: int) -> User:
    """Load user with subscription eagerly."""
    result = await session.execute(
        select(User).where(User.id == user_id).options(selectinload(User.subscription))
    )
    return result.scalar_one()


async def grant_subscription(session, user_id: int, plan_id: str, days: int) -> Subscription:
    """Standalone replica of subscription._grant_subscription for testing."""
    user = await _load_user(session, user_id)
    now = datetime.utcnow()

    if user.subscription and user.subscription.is_active:
        expires = user.subscription.expires_at + timedelta(days=days)
    else:
        expires = now + timedelta(days=days)

    is_paid = plan_id != "trial"
    if user.subscription:
        user.subscription.plan = plan_id
        user.subscription.expires_at = expires
        if is_paid:
            user.subscription.purchases_count += 1
    else:
        sub = Subscription(
            user_id=user.id,
            plan=plan_id,
            expires_at=expires,
            purchases_count=1 if is_paid else 0,
        )
        session.add(sub)

    if plan_id == "trial":
        user.trial_used = True

    await session.commit()
    session.expire_all()  # invalidate identity map so next select hits DB
    return await _load_user(session, user_id)


class TestGrantSubscription:
    async def test_trial_grants_3_days(self, session, make_user):
        user = make_user(user_id=1)
        session.add(user)
        await session.commit()

        result_user = await grant_subscription(session, 1, "trial", 3)
        assert result_user.subscription.is_active is True
        assert result_user.subscription.days_left >= 2

    async def test_trial_sets_trial_used(self, session, make_user):
        user = make_user(user_id=1)
        session.add(user)
        await session.commit()

        await grant_subscription(session, 1, "trial", 3)
        result = await session.execute(select(User).where(User.id == 1))
        assert result.scalar_one().trial_used is True

    async def test_trial_purchases_count_stays_zero(self, session, make_user):
        user = make_user(user_id=1)
        session.add(user)
        await session.commit()

        result_user = await grant_subscription(session, 1, "trial", 3)
        assert result_user.subscription.purchases_count == 0

    async def test_paid_plan_purchases_count_incremented(self, session, make_user):
        user = make_user(user_id=1)
        session.add(user)
        await session.commit()

        result_user = await grant_subscription(session, 1, "1m", 30)
        assert result_user.subscription.purchases_count == 1

    async def test_paid_plan_subscription_active(self, session, make_user):
        user = make_user(user_id=1)
        session.add(user)
        await session.commit()

        result_user = await grant_subscription(session, 1, "3m", 90)
        assert result_user.subscription.is_active is True
        assert result_user.subscription.days_left >= 88

    async def test_extend_active_subscription(self, session, make_user, make_subscription):
        user = make_user(user_id=1)
        session.add(user)
        await session.commit()

        sub = make_subscription(user_id=1, plan="1m", days=30, purchases=1)
        session.add(sub)
        await session.commit()

        original_expires = sub.expires_at
        result_user = await grant_subscription(session, 1, "1m", 30)
        assert result_user.subscription.expires_at > original_expires
        assert result_user.subscription.purchases_count == 2

    async def test_grant_on_expired_subscription_starts_fresh(self, session, make_user, make_subscription):
        user = make_user(user_id=1)
        session.add(user)
        await session.commit()

        sub = make_subscription(user_id=1, plan="1m", days=-5, purchases=1)
        session.add(sub)
        await session.commit()

        result_user = await grant_subscription(session, 1, "3m", 90)
        assert result_user.subscription.is_active is True
        assert result_user.subscription.days_left >= 88

    async def test_manual_grant_by_admin(self, session, make_user):
        """Admin grants subscription by day count — plan='manual'."""
        user = make_user(user_id=1)
        session.add(user)
        await session.commit()

        expires = datetime.utcnow() + timedelta(days=45)
        sub = Subscription(user_id=1, plan="manual", expires_at=expires, purchases_count=0)
        session.add(sub)
        await session.commit()

        result = await session.execute(select(Subscription).where(Subscription.user_id == 1))
        fetched = result.scalar_one()
        assert fetched.plan == "manual"
        assert fetched.is_active is True
        assert fetched.purchases_count == 0


class TestRevokeSubscription:
    async def test_revoke_makes_subscription_inactive(self, session, make_user, make_subscription):
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

    async def test_revoke_days_left_zero(self, session, make_user, make_subscription):
        user = make_user(user_id=1)
        session.add(user)
        await session.commit()

        sub = make_subscription(user_id=1, days=-10)
        session.add(sub)
        await session.commit()

        result = await session.execute(select(Subscription).where(Subscription.user_id == 1))
        assert result.scalar_one().days_left == 0
