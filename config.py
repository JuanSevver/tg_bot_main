from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    bot_token: str
    admin_ids: list[int]
    support_username: str
    cryptobot_token: str
    database_url: str
    tg_api_id: int
    tg_api_hash: str

    SUBSCRIPTION_PLANS: dict = field(default_factory=lambda: {
        "trial": {"days": 3, "price": 0, "label": "Пробный (3 дня)"},
        "1m":    {"days": 30,  "price": 20,  "label": "1 месяц — 20 USDT"},
        "3m":    {"days": 90,  "price": 45,  "label": "3 месяца — 45 USDT"},
        "1y":    {"days": 365, "price": 200, "label": "1 год — 200 USDT"},
    })


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        raise ValueError("BOT_TOKEN is not set")

    admin_ids_raw = os.getenv("ADMIN_IDS", "")
    admin_ids = [int(i.strip()) for i in admin_ids_raw.split(",") if i.strip().isdigit()]

    return Config(
        bot_token=token,
        admin_ids=admin_ids,
        support_username=os.getenv("SUPPORT_USERNAME", ""),
        cryptobot_token=os.getenv("CRYPTOBOT_TOKEN", ""),
        database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///bot.db"),
        tg_api_id=int(os.getenv("TELEGRAM_API_ID", "0")),
        tg_api_hash=os.getenv("TELEGRAM_API_HASH", ""),
    )
