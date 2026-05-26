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

    type, если не указан явно, == "socks5". Чтобы пробовать оба варианта
    (HTTP vs SOCKS5) можно использовать опцию авто-определения в кнопке
    «Проверить» — там идёт live-проверка.

    Старый код использовал `not parts[1].isdigit() is False`, что из-за
    приоритета операторов работало «случайно правильно». Переписано на
    явные условия без двойных отрицаний.
    """
    text = text.strip()
    if not text:
        return None
    ptype = "socks5"

    # Определяем тип, если он указан первым словом без двоеточия
    parts = text.split()
    if parts and parts[0].lower() in ("socks5", "http", "socks4") and len(parts) > 1:
        ptype = parts[0].lower()
        text = " ".join(parts[1:])
        parts = parts[1:]

    # Формат через пробелы: host port [user] [pass]
    # Условие: ровно один разделитель — пробел (т.е. в parts[0] нет ":")
    # и parts[1] — это число (порт).
    if (
        len(parts) >= 2
        and ":" not in parts[0]
        and parts[1].isdigit()
    ):
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


async def _auto_detect_proxy_type(host: str, port: int, username: str | None, password: str | None) -> str | None:
    """Пробует SOCKS5 и HTTP по очереди — возвращает тот, что работает.

    Без этого админ, который ввёл «host:port:user:pass» от HTTP-прокси, молча
    сохраняет его как socks5 и потом удивляется почему ничего не работает.
    """
    for candidate in ("socks5", "http"):
        proxy_url = f"{candidate}://"
        if username:
            proxy_url += f"{username}:{password}@"
        proxy_url += f"{host}:{port}"
        timeout = aiohttp.ClientTimeout(total=8)
        try:
            if candidate == "socks5":
                connector = ProxyConnector.from_url(proxy_url)
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as s:
                    async with s.get("https://api.ipify.org") as resp:
                        if resp.status == 200:
                            return candidate
            else:
                async with aiohttp.ClientSession(timeout=timeout) as s:
                    async with s.get("https://api.ipify.org", proxy=proxy_url) as resp:
                        if resp.status == 200:
                            return candidate
        except Exception:
            continue
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
    # Если тип не был указан явно (по дефолту socks5), пробуем оба —
    # ввод от админа часто без префикса, а http-прокси не работает как socks5.
    text_lower = (message.text or "").strip().lower()
    type_was_explicit = text_lower.startswith(("socks5", "http", "socks4"))
    if not type_was_explicit:
        detected = await _auto_detect_proxy_type(host, port, username, password)
        if detected:
            ptype = detected
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
        import logging
        logging.getLogger(__name__).exception("proxy add failed")
        await message.answer(
            f"❌ Не удалось добавить прокси ({type(e).__name__}).",
            reply_markup=cancel_kb("adm:proxies"),
        )
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

    # Проверяем: (1) ходит ли трафик вообще, (2) доступен ли Telegram DC.
    # Второе важнее — httpbin может отвечать, а Telegram через тот же прокси нет.
    internet_ok = False
    telegram_ok = False
    error_msg: str | None = None
    try:
        if proxy.type.lower() in ("socks5", "socks4"):
            connector = ProxyConnector.from_url(proxy_url)
            http_proxy_arg = None
        else:
            connector = aiohttp.TCPConnector()
            http_proxy_arg = proxy_url

        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as s:
            # 1) ходит ли HTTPS наружу
            try:
                kwargs = {"proxy": http_proxy_arg} if http_proxy_arg else {}
                async with s.get("https://api.ipify.org", **kwargs) as resp:
                    internet_ok = resp.status == 200
            except Exception as e:
                error_msg = f"интернет: {type(e).__name__}"

            # 2) виден ли Telegram через этот же прокси (DC2)
            try:
                kwargs = {"proxy": http_proxy_arg} if http_proxy_arg else {}
                async with s.get("https://149.154.167.51/", **kwargs, ssl=False) as resp:
                    # Любой HTTP-ответ = TCP+TLS до Telegram прошёл
                    telegram_ok = resp.status in (200, 400, 404, 501)
            except Exception as e:
                if not error_msg:
                    error_msg = f"telegram: {type(e).__name__}"
    except Exception as e:
        error_msg = f"connector: {type(e).__name__}"

    is_working = internet_ok and telegram_ok
    proxy.is_working = is_working
    proxy.last_checked_at = datetime.utcnow()
    await session.commit()

    result2 = await session.execute(select(Proxy))
    proxies = result2.scalars().all()

    if is_working:
        status = "✅ работает (Telegram доступен)"
    elif internet_ok and not telegram_ok:
        status = "⚠️ интернет есть, но Telegram заблокирован"
    elif not internet_ok and telegram_ok:
        status = "⚠️ Telegram доступен, но HTTPS нестабилен"
    else:
        status = f"❌ не работает ({error_msg or 'таймаут'})"

    await callback.message.edit_text(
        f"Прокси {proxy.host}:{proxy.port} — {status}\n\n🛡 <b>Прокси</b>",
        reply_markup=proxies_list_kb(list(proxies)),
        parse_mode="HTML",
    )
