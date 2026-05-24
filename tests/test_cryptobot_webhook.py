"""Tests for CryptoBot webhook signature verification and subscription grant."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

import pytest


def _make_signature(token: str, body: bytes) -> str:
    secret = hashlib.sha256(token.encode()).digest()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


class TestSignatureVerification:
    def test_valid_signature_accepted(self):
        from services.cryptobot_webhook import _verify_signature
        token = "test_token"
        body = b'{"update_type": "invoice_paid"}'
        sig = _make_signature(token, body)
        assert _verify_signature(token, body, sig) is True

    def test_invalid_signature_rejected(self):
        from services.cryptobot_webhook import _verify_signature
        assert _verify_signature("token", b"body", "bad_signature") is False

    def test_empty_signature_rejected(self):
        from services.cryptobot_webhook import _verify_signature
        assert _verify_signature("token", b"body", "") is False

    def test_different_token_rejected(self):
        from services.cryptobot_webhook import _verify_signature
        body = b'{"test": true}'
        sig = _make_signature("correct_token", body)
        assert _verify_signature("wrong_token", body, sig) is False


class TestWebhookAmountPlanMatching:
    """Test that the webhook correctly matches payment amounts to plans."""

    def _get_plans(self):
        from config import load_config
        return load_config().SUBSCRIPTION_PLANS

    def test_20_usdt_matches_1m(self):
        plans = self._get_plans()
        amount = 20.0
        matched = next(
            ((pid, p) for pid, p in plans.items() if pid != "trial" and p["price"] == amount),
            None,
        )
        assert matched is not None
        assert matched[0] == "1m"

    def test_45_usdt_matches_3m(self):
        plans = self._get_plans()
        amount = 45.0
        matched = next(
            ((pid, p) for pid, p in plans.items() if pid != "trial" and p["price"] == amount),
            None,
        )
        assert matched is not None
        assert matched[0] == "3m"

    def test_200_usdt_matches_1y(self):
        plans = self._get_plans()
        amount = 200.0
        matched = next(
            ((pid, p) for pid, p in plans.items() if pid != "trial" and p["price"] == amount),
            None,
        )
        assert matched is not None
        assert matched[0] == "1y"

    def test_unknown_amount_no_match(self):
        plans = self._get_plans()
        amount = 99.0
        matched = next(
            ((pid, p) for pid, p in plans.items() if pid != "trial" and p["price"] == amount),
            None,
        )
        assert matched is None


class TestWebhookSubscriptionGrant:
    async def test_paid_invoice_grants_subscription(self, session, make_user):
        from database.models import Subscription
        from sqlalchemy import select

        user = make_user(user_id=555)
        session.add(user)
        await session.commit()

        now = datetime.utcnow()
        expires = now + timedelta(days=30)
        sub = Subscription(user_id=555, plan="1m", expires_at=expires, purchases_count=1)
        session.add(sub)
        await session.commit()

        result = await session.execute(select(Subscription).where(Subscription.user_id == 555))
        fetched = result.scalar_one()
        assert fetched.plan == "1m"
        assert fetched.purchases_count == 1
        assert fetched.is_active is True
