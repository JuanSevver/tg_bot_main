"""
CryptoBot invoice paid webhook handler.

CryptoBot sends a POST to your webhook URL when invoice is paid.
Register this in main.py on a separate aiohttp server.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from config import load_config
from database.db import async_session
from database.models import User, Subscription

logger = logging.getLogger(__name__)
_config = load_config()


def _verify_signature(token: str, body: bytes, signature: str) -> bool:
    secret = hashlib.sha256(token.encode()).digest()
    computed = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)


async def cryptobot_webhook_handler(request: web.Request) -> web.Response:
    body = await request.read()
    signature = request.headers.get("Crypto-Pay-Api-Signature", "")

    if not _verify_signature(_config.cryptobot_token, body, signature):
        logger.warning("Invalid CryptoBot signature")
        return web.Response(status=401)

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return web.Response(status=400)

    if data.get("update_type") != "invoice_paid":
        return web.Response(status=200)

    invoice = data.get("payload", {})
    user_id_str = invoice.get("payload", "")
    try:
        user_id = int(user_id_str)
    except ValueError:
        return web.Response(status=200)

    amount = float(invoice.get("amount", 0))
    plans = _config.SUBSCRIPTION_PLANS
    matched_plan = None
    for plan_id, plan in plans.items():
        if plan_id != "trial" and plan["price"] == amount:
            matched_plan = (plan_id, plan)
            break

    if not matched_plan:
        logger.warning("No plan matched for amount %s", amount)
        return web.Response(status=200)

    plan_id, plan = matched_plan
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.id == user_id).options(selectinload(User.subscription))
        )
        user = result.scalar_one_or_none()
        if not user:
            return web.Response(status=200)

        now = datetime.utcnow()
        days = plan["days"]
        if user.subscription and user.subscription.is_active:
            expires = user.subscription.expires_at + timedelta(days=days)
        else:
            expires = now + timedelta(days=days)

        if user.subscription:
            user.subscription.plan = plan_id
            user.subscription.expires_at = expires
            user.subscription.purchases_count += 1
        else:
            sub = Subscription(
                user_id=user.id, plan=plan_id,
                expires_at=expires, purchases_count=1,
            )
            session.add(sub)

        await session.commit()
        logger.info("Subscription %s granted to user %s via CryptoBot", plan_id, user_id)

    return web.Response(status=200)
