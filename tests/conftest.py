"""Shared fixtures for all tests."""
from __future__ import annotations

import os
import pytest
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Set env vars before any project import
os.environ.setdefault("BOT_TOKEN", "123456789:AAtest_token_for_tests")
os.environ.setdefault("ADMIN_IDS", "100")
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "abc123")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from database.models import Base, User, Subscription, Category, UserCategory, CategoryType


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        yield s


@pytest.fixture
def make_user():
    def _make(user_id: int = 1, **kwargs) -> User:
        defaults = dict(
            id=user_id,
            username=f"user{user_id}",
            full_name=f"Test User {user_id}",
            captcha_passed=False,
            trial_used=False,
            receiving_enabled=False,
            is_blocked=False,
            messages_received=0,
        )
        defaults.update(kwargs)
        return User(**defaults)
    return _make


@pytest.fixture
def make_subscription():
    def _make(user_id: int, plan: str = "trial", days: int = 3, purchases: int = 0) -> Subscription:
        return Subscription(
            user_id=user_id,
            plan=plan,
            expires_at=datetime.utcnow() + timedelta(days=days),
            purchases_count=purchases,
        )
    return _make


@pytest.fixture
def make_category():
    def _make(
        cat_id: int = 1,
        name: str = "Дизайн",
        cat_type: CategoryType = CategoryType.request,
        keywords: str = "логотип\nдизайн\nбаннер",
    ) -> Category:
        return Category(id=cat_id, name=name, type=cat_type, keywords=keywords, is_active=True)
    return _make
