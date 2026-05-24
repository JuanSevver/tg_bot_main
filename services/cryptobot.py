"""CryptoBot (@CryptoBot) payment integration via official Crypto Pay API."""
from __future__ import annotations

import aiohttp

from config import load_config

_config = load_config()
_BASE = "https://pay.crypt.bot/api"


async def create_invoice(amount: float, description: str, user_id: int) -> str:
    """Create a USDT invoice and return the pay_url."""
    if not _config.cryptobot_token:
        raise RuntimeError("CRYPTOBOT_TOKEN not configured")

    payload = {
        "asset": "USDT",
        "amount": str(amount),
        "description": description,
        "payload": str(user_id),
        "paid_btn_name": "callback",
        "paid_btn_url": f"https://t.me/{(await _get_bot_username())}",
        "allow_comments": False,
        "allow_anonymous": False,
    }
    headers = {"Crypto-Pay-API-Token": _config.cryptobot_token}
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{_BASE}/createInvoice", json=payload, headers=headers) as resp:
            data = await resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"CryptoBot error: {data}")
    return data["result"]["pay_url"]


_bot_username: str = ""


async def _get_bot_username() -> str:
    global _bot_username
    if _bot_username:
        return _bot_username
    import aiohttp as _aiohttp
    url = f"https://api.telegram.org/bot{_config.bot_token}/getMe"
    async with _aiohttp.ClientSession() as s:
        async with s.get(url) as resp:
            data = await resp.json()
    _bot_username = data.get("result", {}).get("username", "bot")
    return _bot_username
