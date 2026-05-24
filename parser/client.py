from __future__ import annotations

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import load_config

_config = load_config()


def make_client(session_string: str, proxy: tuple | None = None, name: str = "parser") -> TelegramClient:
    return TelegramClient(
        StringSession(session_string),
        api_id=_config.tg_api_id,
        api_hash=_config.tg_api_hash,
        proxy=proxy,  # (socks.SOCKS5, host, port) or None
    )


def proxy_tuple(host: str, port: int, ptype: str, username: str | None, password: str | None):
    import socks
    scheme_map = {"socks5": socks.SOCKS5, "http": socks.HTTP, "socks4": socks.SOCKS4}
    scheme = scheme_map.get(ptype.lower(), socks.SOCKS5)
    if username:
        return (scheme, host, port, True, username, password)
    return (scheme, host, port)
