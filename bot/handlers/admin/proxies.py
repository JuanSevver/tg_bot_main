from __future__ import annotations

import aiohttp
from aiohttp_socks import ProxyConnector
from datetime import datetime

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import proxies_list_kb, cancel_kb
from bot.states import ProxySG
from database.models import Proxy
from parser.manager import parser_manager

router = Router(name="admin_proxies")


@router.callback_query(F.data == "adm:proxies")
async def cb_proxies(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    result = await session.execute(select(Proxy))
    proxies = result.scalars().all()
    await state.set_state(ProxySG.list)
    await callback.message.edit_text(
        f"🛡 <b>Прокси</b> ({len(proxies)})\n\nНажмите для проверки:",
        reply_markup=proxies_list_kb(list(proxies)),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "adm:proxy:add")
async def cb_proxy_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProxySG.add)
    await callback.message.edit_text(
        "Введите прокси в одном из форматов:\n\n"
        "<code>host:port:user:pass</code>\n"
        "<code>host:port</code>\n"
        "<code>socks5 host port user pass</code>\n\n"
        "Тип по умолчанию — socks5. Чтобы указать http:\n"
        "<code>http host:port:user:pass</code>",
        reply_markup=cancel_kb("adm:proxies"),
        parse_mode="HTML",
    )
    await callback.answer()


def _parse_proxy_input(text: str) -> tuple[str, str, int, str | None, str | None] | None:
    """
    Парсит строку прокси. Возвращает (type, host, port, user, password) или None.

    Поддерживаемые форматы:
      host:port
      host:port:user:pass
      [type] host:port:user:pass
      type host port [user] [pass]
    """
    text = text.strip()
    ptype = "socks5"

    # Определяем тип если он указан первым словом без двоеточия
    parts = text.split()
    if parts[0].lower() in ("socks5", "http") and len(parts) > 1:
        ptype = parts[0].lower()
        text = " ".join(parts[1:])
        parts = parts[1:]

    # Формат через пробелы: host port [user] [pass]
    if len(parts) >= 2 and not parts[1].isdigit() is False or (len(parts) >= 2 and ":" not in parts[0]):
        if len(parts) >= 2:
            try:
                host = parts[0]
                port = int(parts[1])
                username = parts[2] if len(parts) > 2 else None
                password = parts[3] if len(parts) > 3 else None
                return ptype, host, port, username, password
            except (ValueError, IndexError):
                pass

    # Формат через двоеточие: host:port[:user:pass]
    colon_parts = text.split(":")
    if len(colon_parts) >= 2:
        try:
            host = colon_parts[0]
            port = int(colon_parts[1])
            username = colon_parts[2] if len(colon_parts) > 2 else None
            password = colon_parts[3] if len(colon_parts) > 3 else None
            return ptype, host, port, username, password
        except (ValueError, IndexError):
            pass

    return None


@router.message(ProxySG.add)
async def process_proxy_add(message: Message, state: FSMContext, session: AsyncSession) -> None:
    parsed = _parse_proxy_input(message.text or "")
    if not parsed:
        await message.answer(
            "❌ Не удалось распознать прокси.\n\n"
            "Примеры:\n"
            "<code>193.233.197.74:38673:bu0RAH:yR3de9</code>\n"
            "<code>socks5 193.233.197.74 38673 bu0RAH yR3de9</code>",
            reply_markup=cancel_kb("adm:proxies"),
            parse_mode="HTML",
        )
        return

    ptype, host, port, username, password = parsed
    try:
        proxy = Proxy(host=host, port=port, type=ptype, username=username, password=password)
        session.add(proxy)
        await session.commit()
        await parser_manager.reload_clients()

        result = await session.execute(select(Proxy))
        proxies = result.scalars().all()
        await message.answer(
            f"✅ Прокси {host}:{port} ({ptype}) добавлен.\n\n"
            f"🛡 <b>Прокси</b> ({len(proxies)})",
            reply_markup=proxies_list_kb(list(proxies)),
            parse_mode="HTML",
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}", reply_markup=cancel_kb("adm:proxies"))
    await state.set_state(ProxySG.list)


@router.callback_query(F.data.startswith("adm:proxy:delete:"))
async def cb_proxy_delete(callback: CallbackQuery, session: AsyncSession) -> None:
    proxy_id = int(callback.data.split(":")[-1])
    result = await session.execute(select(Proxy).where(Proxy.id == proxy_id))
    proxy = result.scalar_one_or_none()
    if proxy:
        await session.delete(proxy)
        await session.commit()
        await parser_manager.reload_clients()
        await callback.answer("✅ Прокси удалён.", show_alert=True)
    else:
        await callback.answer("Не найден.", show_alert=True)
    result2 = await session.execute(select(Proxy))
    proxies = result2.scalars().all()
    await callback.message.edit_reply_markup(reply_markup=proxies_list_kb(list(proxies)))


@router.callback_query(F.data.startswith("adm:proxy:check:"))
async def cb_proxy_check(callback: CallbackQuery, session: AsyncSession) -> None:
    proxy_id = int(callback.data.split(":")[-1])
    result = await session.execute(select(Proxy).where(Proxy.id == proxy_id))
    proxy = result.scalar_one_or_none()
    if not proxy:
        await callback.answer("Прокси не найден.", show_alert=True)
        return

    await callback.answer("⏳ Проверяю прокси...")

    proxy_url = f"{proxy.type}://"
    if proxy.username:
        proxy_url += f"{proxy.username}:{proxy.password}@"
    proxy_url += f"{proxy.host}:{proxy.port}"

    is_working = False
    try:
        if proxy.type.lower() in ("socks5", "socks4"):
            connector = ProxyConnector.from_url(proxy_url)
        else:
            connector = aiohttp.TCPConnector()

        async with aiohttp.ClientSession(connector=connector) as s:
            kwargs = {"timeout": aiohttp.ClientTimeout(total=10)}
            if proxy.type.lower() == "http":
                kwargs["proxy"] = proxy_url
            async with s.get("https://httpbin.org/ip", **kwargs) as resp:
                is_working = resp.status == 200
    except Exception:
        is_working = False

    proxy.is_working = is_working
    proxy.last_checked_at = datetime.utcnow()
    await session.commit()

    result2 = await session.execute(select(Proxy))
    proxies = result2.scalars().all()
    status = "✅ работает" if is_working else "❌ не работает"
    await callback.message.edit_text(
        f"Прокси {proxy.host}:{proxy.port} — {status}\n\n🛡 <b>Прокси</b>",
        reply_markup=proxies_list_kb(list(proxies)),
        parse_mode="HTML",
    )
