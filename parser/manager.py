"""
Multi-account parser manager using Telethon.

Responsibilities:
- Pool of Telethon clients (one per ParserAccount).
- Round-robin message history collection.
- Deduplication via ParsedMessage table.
- Deliver matched messages to subscribed users.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.utils import get_peer_id
from telethon.errors import (
    AuthKeyUnregisteredError, AuthKeyDuplicatedError, UserDeactivatedError,
    UserDeactivatedBanError, FloodWaitError, SessionPasswordNeededError,
)
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from database.db import async_session
from database.models import (
    ParserAccount, TelegramGroup, Category, UserCategory,
    ParsedMessage, User, Subscription, CategoryAccount, GroupCategory,
)
from .client import make_client, proxy_tuple

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

def _extract_username(link: str) -> str:
    """Нормализует ссылку на группу к юзернейму (без @, в нижнем регистре).

    Примеры:
      https://t.me/mygroup  →  mygroup
      @mygroup              →  mygroup
      t.me/mygroup          →  mygroup
    """
    link = link.strip().lower()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if link.startswith(prefix):
            link = link[len(prefix):]
            break
    link = link.lstrip("@")
    # Убираем trailing slash, query params, пути вида joinchat/...
    link = link.split("/")[0].split("?")[0]
    return link


def _match_phrase(phrase: str, text: str) -> bool:
    """Проверяет наличие ключевой фразы в тексте — строгий поиск подстрок.

    Оба аргумента приводятся к нижнему регистру перед сравнением.
    «дизайн» найдёт «дизайнер», «дизайна», «графическим дизайном» и т.д.
    Многословная фраза «ищу дизайнера» найдёт только точное вхождение подстроки.
    """
    phrase = phrase.strip().lower()
    text = text.strip().lower()
    if not phrase or not text:
        return False
    return phrase in text


def _has_stop_word(stop_words: list[str], text: str) -> bool:
    """Возвращает True если в тексте найдено хотя бы одно минус-слово (подстрока)."""
    text_lower = text.lower()
    for sw in stop_words:
        sw = sw.strip().lower()
        if sw and sw in text_lower:
            return True
    return False

# Temporary storage for pending sign-ins: {phone: (client, phone_code_hash)}
_pending: dict[str, tuple[TelegramClient, str]] = {}


class ParserManager:
    def __init__(self) -> None:
        # (client, acc_id) — для round-robin по явным группам
        self._client_pairs: list[tuple[TelegramClient, int]] = []
        # (client, acc_id) — аккаунты с parse_joined_groups=True
        self._joined_pairs: list[tuple[TelegramClient, int]] = []
        self._cycle: itertools.cycle = itertools.cycle([])
        self._bot: "Bot | None" = None
        self._running = False
        # Зарегистрированные realtime-обработчики: client → list[handler_fn]
        # Нужны чтобы снять их перед повторной регистрацией при reload_clients()
        self._rt_handlers: dict[int, list] = {}  # id(client) → [fn, ...]
        # Кеш: group.id → acc_id клиента который умеет резолвить эту группу.
        # Заполняется при первом удачном get_entity и переиспользуется,
        # вместо слепого round-robin (который попадает мимо для приватных групп).
        self._group_owner: dict[int, int] = {}
        # Изменяемые наборы для realtime-фильтра. Хендлеры читают их по ссылке,
        # поэтому достаточно обновлять содержимое — повторно регистрировать
        # обработчики не нужно. Пополняются при старте и после каждого полла,
        # когда у новых приватных групп резолвится chat_id.
        self._explicit_chat_ids: set[int] = set()
        self._explicit_usernames: set[str] = set()

    @property
    def _clients(self) -> list[TelegramClient]:
        """Обратная совместимость: список клиентов без acc_id."""
        return [c for c, _ in self._client_pairs]

    def set_bot(self, bot: "Bot") -> None:
        self._bot = bot

    async def start(self) -> None:
        await self.reload_clients()
        self._running = True
        asyncio.create_task(self._polling_loop())

    async def stop(self) -> None:
        self._running = False
        for client in self._clients:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def reload_clients(self) -> None:
        # Снимаем realtime-обработчики перед дисконнектом
        for client, _ in self._client_pairs:
            self._remove_rt_handlers(client)

        for c, _ in self._client_pairs:
            try:
                await c.disconnect()
            except Exception:
                pass
        self._client_pairs.clear()
        self._joined_pairs.clear()
        self._group_owner.clear()

        async with async_session() as session:
            result = await session.execute(
                select(ParserAccount)
                .where(ParserAccount.is_active == True, ParserAccount.is_valid == True)
                .options(selectinload(ParserAccount.proxy))
            )
            accounts = result.scalars().all()

        for acc in accounts:
            if not acc.session_string:
                continue
            proxy = None
            if acc.proxy and acc.proxy.is_active:
                proxy = proxy_tuple(
                    acc.proxy.host, acc.proxy.port, acc.proxy.type,
                    acc.proxy.username, acc.proxy.password,
                )
            try:
                client = make_client(acc.session_string, proxy)
                await client.connect()
                if not await client.is_user_authorized():
                    raise AuthKeyUnregisteredError(request=None)
                self._client_pairs.append((client, acc.id))
                if acc.parse_joined_groups:
                    self._joined_pairs.append((client, acc.id))
                logger.info("Parser client account_%s started (joined=%s).", acc.id, acc.parse_joined_groups)
            except (
                AuthKeyUnregisteredError, AuthKeyDuplicatedError,
                UserDeactivatedError, UserDeactivatedBanError,
            ) as e:
                async with async_session() as db:
                    r = await db.execute(select(ParserAccount).where(ParserAccount.id == acc.id))
                    a = r.scalar_one_or_none()
                    if a:
                        a.is_valid = False
                        await db.commit()
                logger.warning(
                    "Account %s marked invalid (%s). Re-authorize via admin panel.",
                    acc.id, type(e).__name__,
                )
                try:
                    await client.disconnect()
                except Exception:
                    pass
            except Exception as e:
                logger.error("Failed to start client for account %s: %s", acc.id, e)
                try:
                    await client.disconnect()
                except Exception:
                    pass

        self._cycle = itertools.cycle(self._client_pairs) if self._client_pairs else itertools.cycle([])

        # Регистрируем realtime-обработчики после того как клиенты подключены
        await self._register_realtime_handlers()

    # ------------------------------------------------------------------
    # Real-time event handlers (Telethon NewMessage)
    # ------------------------------------------------------------------

    def _remove_rt_handlers(self, client: TelegramClient) -> None:
        """Снимает все ранее зарегистрированные realtime-обработчики с клиента."""
        key = id(client)
        for fn in self._rt_handlers.pop(key, []):
            try:
                client.remove_event_handler(fn)
            except Exception:
                pass

    async def _register_realtime_handlers(self) -> None:
        """Подписывает каждый Telethon-клиент на NewMessage.

        Обработчик срабатывает мгновенно при появлении сообщения в любой
        группе/канале, в которой состоит аккаунт. Дедупликация гарантирует
        что то же сообщение не будет доставлено повторно страховочным поллингом.

        Логика фильтрации по группам зеркалит поллинг:
        - аккаунт без parse_joined_groups → только явно добавленные группы
        - аккаунт с parse_joined_groups → все группы где он состоит
        """
        from telethon import events

        # Загружаем явно добавленные группы. Сравниваем по chat_id (Telethon
        # peer id вида -100...) — это надёжно для приватных групп без username
        # и для каналов. Username держим как запасной матчинг.
        # Наборы — instance attrs: handler читает по ссылке, поэтому
        # достаточно обновить их в _refresh_explicit_filter() без re-register.
        await self._refresh_explicit_filter()

        # Карта acc_id → parse_joined_groups
        acc_joined_flag: dict[int, bool] = {
            acc_id: any(
                acc_id == a_id
                for _, a_id in self._joined_pairs
            )
            for _, acc_id in self._client_pairs
        }

        for client, acc_id in self._client_pairs:
            self._remove_rt_handlers(client)  # чисто, без дублей при перезагрузке

            parse_joined = acc_joined_flag.get(acc_id, False)

            def _make_handler(bound_acc_id: int, bound_parse_joined: bool,
                               bound_chat_ids: set[int], bound_usernames: set[str]):
                async def _handler(event):
                    # Обрабатываем только группы и каналы, игнорируем личку/ботов
                    if not (event.is_group or event.is_channel):
                        return
                    msg = event.message
                    if not msg or not msg.text:
                        return

                    # Проверяем: входит ли чат в явно добавленные группы?
                    # Матчим по chat_id (надёжно для приватных), c username-фолбэком.
                    is_explicit = event.chat_id in bound_chat_ids
                    if not is_explicit and bound_usernames:
                        chat = getattr(event, "chat", None)
                        chat_username = getattr(chat, "username", None) if chat else None
                        if chat_username:
                            norm = _extract_username(chat_username)
                            is_explicit = norm in bound_usernames
                    if not is_explicit and not bound_parse_joined:
                        return

                    try:
                        async with async_session() as session:
                            cats_result = await session.execute(
                                select(Category).where(Category.is_active == True)
                            )
                            categories = cats_result.scalars().all()

                            ca_result = await session.execute(select(CategoryAccount))
                            cat_acc_map: dict[int, set[int]] = {}
                            for ca in ca_result.scalars().all():
                                cat_acc_map.setdefault(ca.category_id, set()).add(ca.account_id)

                            # Фильтрация категорий по группе (group_cat_map)
                            gc_result = await session.execute(select(GroupCategory))
                            grp_cat_map: dict[int, list[Category]] = {}
                            cat_by_id = {c.id: c for c in categories}
                            for gc in gc_result.scalars().all():
                                if gc.category_id in cat_by_id:
                                    grp_cat_map.setdefault(gc.group_id, []).append(
                                        cat_by_id[gc.category_id]
                                    )

                            # Ищем группу в БД (по chat_id, затем по username)
                            # чтобы получить group_id для grp_cat_map; fallback — все категории
                            applicable_cats = categories  # fallback
                            if grp_cat_map:
                                chat = getattr(event, "chat", None)
                                chat_username = getattr(chat, "username", None) if chat else None
                                norm = _extract_username(chat_username) if chat_username else ""
                                grp_db_result = await session.execute(
                                    select(TelegramGroup).where(
                                        TelegramGroup.is_active == True
                                    )
                                )
                                for grp_db in grp_db_result.scalars().all():
                                    matched = False
                                    if grp_db.chat_id and grp_db.chat_id == event.chat_id:
                                        matched = True
                                    elif norm and _extract_username(grp_db.link) == norm:
                                        matched = True
                                    if matched and grp_db.id in grp_cat_map:
                                        applicable_cats = grp_cat_map[grp_db.id]
                                        break

                            await self._handle_message(
                                session, msg, applicable_cats, bound_acc_id, cat_acc_map
                            )
                    except Exception as exc:
                        logger.error(
                            "Realtime handler error (acc %s, chat %s): %s",
                            bound_acc_id, getattr(event, "chat_id", "?"), exc,
                        )
                return _handler

            handler_fn = _make_handler(acc_id, parse_joined, self._explicit_chat_ids, self._explicit_usernames)
            client.add_event_handler(handler_fn, events.NewMessage)
            self._rt_handlers.setdefault(id(client), []).append(handler_fn)
            logger.info(
                "Realtime handler registered for account_%s (parse_joined=%s).",
                acc_id, parse_joined,
            )

    # ------------------------------------------------------------------
    # Phone-based sign-in helpers
    # ------------------------------------------------------------------

    async def request_code(self, phone: str) -> str:
        from config import load_config
        cfg = load_config()
        client = TelegramClient(StringSession(), cfg.tg_api_id, cfg.tg_api_hash)
        await client.connect()
        result = await client.send_code_request(phone)
        _pending[phone] = (client, result.phone_code_hash)
        return result.phone_code_hash

    async def sign_in(self, phone: str, code: str, phone_code_hash: str) -> str:
        client, _ = _pending[phone]
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        ss = client.session.save()
        await client.disconnect()
        _pending.pop(phone, None)
        return ss

    async def sign_in_2fa(self, phone: str, password: str) -> str:
        client, _ = _pending[phone]
        await client.sign_in(password=password)
        ss = client.session.save()
        await client.disconnect()
        _pending.pop(phone, None)
        return ss

    # ------------------------------------------------------------------
    # Join groups (admin tools)
    # ------------------------------------------------------------------

    async def join_group(self, link_or_chat_id) -> dict:
        """Подписывает все парсерные аккаунты на группу/канал по ссылке.

        Возвращает словарь {acc_id: status}, где status — одна из строк:
        "joined", "already", "error: <текст>".

        Поддерживает:
        - публичные username/ссылки (`@x`, `t.me/x`, `https://t.me/x`)
        - приватные инвайт-ссылки (`t.me/+abc`, `t.me/joinchat/abc`)
        - числовой chat_id (если бот уже резолвил группу)
        """
        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.tl.functions.messages import ImportChatInviteRequest
        from telethon.errors import (
            UserAlreadyParticipantError, InviteHashExpiredError,
            InviteHashInvalidError, ChannelsTooMuchError,
        )

        result: dict[int, str] = {}
        link = str(link_or_chat_id).strip()
        invite_hash: str | None = None

        # Распознаём инвайт-ссылку: t.me/+xxx или t.me/joinchat/xxx
        for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
            if link.startswith(prefix):
                tail = link[len(prefix):]
                if tail.startswith("+"):
                    invite_hash = tail[1:].split("?")[0].split("/")[0]
                elif tail.startswith("joinchat/"):
                    invite_hash = tail[len("joinchat/"):].split("?")[0].split("/")[0]
                break

        for client, acc_id in self._client_pairs:
            try:
                if invite_hash:
                    try:
                        await client(ImportChatInviteRequest(invite_hash))
                        result[acc_id] = "joined"
                    except UserAlreadyParticipantError:
                        result[acc_id] = "already"
                else:
                    # публичная группа/канал или числовой id
                    target = link_or_chat_id
                    if isinstance(target, str):
                        # обрезаем префиксы и @ — Telethon принимает чистый username
                        t = target
                        for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
                            if t.startswith(prefix):
                                t = t[len(prefix):]
                                break
                        t = t.lstrip("@").split("/")[0].split("?")[0]
                        target = t
                    try:
                        await client(JoinChannelRequest(target))
                        result[acc_id] = "joined"
                    except UserAlreadyParticipantError:
                        result[acc_id] = "already"
            except FloodWaitError as e:
                result[acc_id] = f"floodwait {e.seconds}s"
                logger.warning("FloodWait %s sec joining %s acc=%s", e.seconds, link, acc_id)
                await asyncio.sleep(min(e.seconds, 10))
            except (InviteHashExpiredError, InviteHashInvalidError) as e:
                result[acc_id] = f"bad invite: {type(e).__name__}"
            except ChannelsTooMuchError:
                result[acc_id] = "лимит каналов превышен"
            except Exception as e:
                result[acc_id] = f"error: {type(e).__name__}: {e}"
                logger.debug("Join %s via acc %s failed: %s", link, acc_id, e)
            await asyncio.sleep(0.5)  # лёгкий троттлинг чтобы не словить FloodWait

        # После массового вступления имеет смысл обновить realtime-фильтр —
        # у группы мог появиться chat_id (если резолвится через get_entity)
        try:
            await self._refresh_explicit_filter()
        except Exception:
            pass

        return result

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    async def _refresh_explicit_filter(self) -> None:
        """Обновляет наборы chat_id / username для realtime-фильтра.

        Хендлеры держат ссылки на self._explicit_chat_ids / _explicit_usernames,
        поэтому повторно регистрировать обработчики не нужно: меняем содержимое
        наборов in-place, и фильтр сразу видит новые chat_id (например, после
        первого полла, когда у приватных групп резолвится peer id).
        """
        async with async_session() as session:
            grp_result = await session.execute(
                select(TelegramGroup).where(TelegramGroup.is_active == True)
            )
            new_chat_ids: set[int] = set()
            new_usernames: set[str] = set()
            for g in grp_result.scalars().all():
                if g.chat_id:
                    new_chat_ids.add(g.chat_id)
                norm = _extract_username(g.link)
                if norm and not norm.startswith("+") and "joinchat" not in norm:
                    new_usernames.add(norm)

        # in-place — чтобы хендлеры, держащие ссылку, увидели изменения
        self._explicit_chat_ids.clear()
        self._explicit_chat_ids.update(new_chat_ids)
        self._explicit_usernames.clear()
        self._explicit_usernames.update(new_usernames)

    async def _polling_loop(self) -> None:
        """Страховочный поллинг — догоняет сообщения пропущенные при реконнекте.

        Основная доставка идёт через realtime-обработчики (NewMessage event).
        Этот цикл запускается раз в 5 минут и обрабатывает только то,
        что не попало в event stream (обрыв соединения, FloodWait и т.п.).
        """
        # Первая итерация — сразу, чтобы подобрать историю после рестарта
        await asyncio.sleep(5)
        while self._running:
            try:
                await self._collect_messages()
                # После полла у приватных групп мог появиться chat_id —
                # обновляем фильтр realtime, чтобы они начали ловиться мгновенно.
                await self._refresh_explicit_filter()
            except Exception as e:
                logger.error("Catchup polling error: %s", e)
            await asyncio.sleep(300)  # 5 минут

    async def _collect_messages(self) -> None:
        if not self._client_pairs:
            return

        async with async_session() as session:
            groups_result = await session.execute(
                select(TelegramGroup).where(TelegramGroup.is_active == True)
            )
            groups = groups_result.scalars().all()

            cats_result = await session.execute(
                select(Category).where(Category.is_active == True)
            )
            categories = cats_result.scalars().all()

            # Карта: category_id → set of account_ids (пусто = все аккаунты)
            ca_result = await session.execute(select(CategoryAccount))
            cat_acc_map: dict[int, set[int]] = {}
            for ca in ca_result.scalars().all():
                cat_acc_map.setdefault(ca.category_id, set()).add(ca.account_id)

            # Карта: group_id → list[Category]  (пусто = все категории)
            gc_result = await session.execute(select(GroupCategory))
            group_cat_map: dict[int, list[Category]] = {}
            cat_by_id = {c.id: c for c in categories}
            for gc in gc_result.scalars().all():
                if gc.category_id in cat_by_id:
                    group_cat_map.setdefault(gc.group_id, []).append(cat_by_id[gc.category_id])

        if not categories:
            return

        # Словарь username → TelegramGroup — для joined-групп
        link_to_group: dict[str, TelegramGroup] = {
            _extract_username(g.link): g for g in groups
        }
        explicit_links: set[str] = set(link_to_group.keys())

        # 1. Явно добавленные группы — пробуем клиентов по очереди, пока
        # кто-то не сможет резолвить entity (приватные группы видны только
        # тем аккаунтам, что в них состоят). Первого успешного запоминаем
        # в self._group_owner, чтобы в следующий цикл идти к нему сразу.
        for group in groups:
            group_cats = group_cat_map.get(group.id) or categories
            owner_acc_id = self._group_owner.get(group.id)

            ordered: list[tuple[TelegramClient, int]] = []
            if owner_acc_id is not None:
                for pair in self._client_pairs:
                    if pair[1] == owner_acc_id:
                        ordered.append(pair)
                        break
            for pair in self._client_pairs:
                if pair not in ordered:
                    ordered.append(pair)

            handled = False
            for client, acc_id in ordered:
                try:
                    ok = await self._process_group(client, acc_id, group, group_cats, cat_acc_map)
                    if ok:
                        self._group_owner[group.id] = acc_id
                        handled = True
                        break
                except FloodWaitError as e:
                    logger.warning("FloodWait %s sec for %s", e.seconds, group.link)
                    await asyncio.sleep(e.seconds)
                except Exception as e:
                    logger.debug("Group %s via acc %s failed: %s", group.link, acc_id, e)
            if not handled:
                logger.warning("Group %s: no account could access it", group.link)

        # 2. Группы в которых состоят аккаунты с parse_joined_groups=True
        for client, acc_id in self._joined_pairs:
            try:
                await self._process_joined_groups(
                    client, acc_id, categories, cat_acc_map,
                    explicit_links, link_to_group, group_cat_map,
                )
            except FloodWaitError as e:
                logger.warning("FloodWait %s sec scanning joined groups", e.seconds)
                await asyncio.sleep(e.seconds)
            except Exception as e:
                logger.error("Error scanning joined groups: %s", e)

    async def _get_last_seen_message_id(self, session, chat_id: int) -> int:
        """Возвращает максимальный message_id который уже обработан для данного чата.

        Возвращает 0 если группа обрабатывается впервые — тогда берём историю
        на глубину INITIAL_HISTORY_LIMIT сообщений.
        """
        result = await session.execute(
            select(func.max(ParsedMessage.message_id)).where(
                ParsedMessage.group_id == chat_id
            )
        )
        return result.scalar_one_or_none() or 0

    async def _process_group(
        self,
        client: TelegramClient,
        acc_id: int,
        group: TelegramGroup,
        categories: list[Category],
        cat_acc_map: dict[int, set[int]],
    ) -> bool:
        """Парсит сообщения из группы. Возвращает True если entity отрезолвился."""
        # Глубина истории при первом добавлении группы (сообщений)
        INITIAL_HISTORY_LIMIT = 500

        # Telethon принимает username (без @), полный URL t.me/... или числовой id.
        # Если у нас уже есть resolved chat_id — используем его (быстрее и работает
        # для приватных групп даже без актуального инвайт-токена в кэше).
        target = group.chat_id if group.chat_id else group.link
        async with async_session() as session:
            try:
                # Определяем тип группы при первом обходе (канал или нет)
                entity = await client.get_entity(target)
                from telethon.tl.types import Channel
                actually_channel = isinstance(entity, Channel) and entity.broadcast

                # Нормализованный peer id вида -100... — совпадает с message.chat_id
                resolved_chat_id = get_peer_id(entity)

                # Если в БД ещё нет chat_id или тип изменился — сохраняем
                if group.chat_id != resolved_chat_id or group.is_channel != actually_channel:
                    db_grp = await session.get(TelegramGroup, group.id)
                    if db_grp:
                        db_grp.chat_id = resolved_chat_id
                        db_grp.is_channel = actually_channel
                        await session.commit()
                    group.chat_id = resolved_chat_id
                    group.is_channel = actually_channel

                # last_seen_id ищем по тому же peer id, что хранится в ParsedMessage
                last_seen_id = await self._get_last_seen_message_id(session, resolved_chat_id)

                if last_seen_id > 0:
                    # Инкрементальный режим: только новые сообщения после last_seen_id
                    iter_kwargs = {"min_id": last_seen_id, "limit": None}
                    logger.debug("Group %s: incremental from msg_id=%s", group.link, last_seen_id)
                else:
                    # Первый запуск: берём историю на INITIAL_HISTORY_LIMIT сообщений
                    iter_kwargs = {"limit": INITIAL_HISTORY_LIMIT}
                    logger.info("Group %s: first run, fetching last %s messages", group.link, INITIAL_HISTORY_LIMIT)

                # Обычные сообщения группы / постов канала
                async for message in client.iter_messages(entity, **iter_kwargs):
                    if not message.text:
                        continue
                    await self._handle_message(session, message, categories, acc_id, cat_acc_map)

                # Если это канал — дополнительно парсим комментарии к постам
                if group.is_channel:
                    async for post in client.iter_messages(entity, limit=20):
                        if not (post.replies and post.replies.replies):
                            continue
                        try:
                            async for comment in client.iter_messages(
                                entity, reply_to=post.id, limit=30
                            ):
                                if not comment.text:
                                    continue
                                await self._handle_message(session, comment, categories, acc_id, cat_acc_map)
                        except Exception:
                            pass  # Не все каналы открыты для чтения комментариев
                return True
            except FloodWaitError:
                raise  # Пробрасываем — обрабатывается в _collect_messages
            except Exception as e:
                logger.debug("Could not process group %s via acc %s: %s", group.link, acc_id, e)
                return False

    async def _process_joined_groups(
        self,
        client: TelegramClient,
        acc_id: int,
        categories: list[Category],
        cat_acc_map: dict[int, set[int]],
        skip_links: set[str],
        link_to_group: dict[str, "TelegramGroup"],
        group_cat_map: dict[int, list[Category]],
    ) -> None:
        """Сканирует все группы/каналы в которых состоит аккаунт.

        Если joined-группа совпадает с явно добавленной (по username) — пропускаем,
        она уже обработана в round-robin. Если группа есть в БД и у неё назначены
        категории — используем только их; иначе — все категории.
        """
        try:
            dialogs = await client.get_dialogs()
        except Exception as e:
            logger.error("Could not get dialogs: %s", e)
            return

        for dialog in dialogs:
            # Только группы и каналы, пропускаем личные чаты и боты
            if not (dialog.is_group or dialog.is_channel):
                continue

            entity = dialog.entity
            username = getattr(entity, "username", None)
            norm_username = _extract_username(username) if username else None

            # Пропускаем явно добавленные группы (они уже обработаны round-robin)
            if norm_username and norm_username in skip_links:
                continue

            # Используем нормализованный peer id (тот же формат, что у
            # message.chat_id) — иначе last_seen_id никогда не совпадёт.
            try:
                chat_id = get_peer_id(entity)
            except Exception:
                chat_id = dialog.id
            is_channel = dialog.is_channel and not dialog.is_group

            # Выбираем категории: если группа есть в БД с назначенными → используем их
            if norm_username and norm_username in link_to_group:
                grp_db = link_to_group[norm_username]
                group_cats = group_cat_map.get(grp_db.id) or categories
            else:
                group_cats = categories

            try:
                async with async_session() as session:
                    last_seen_id = await self._get_last_seen_message_id(session, chat_id)
                    if last_seen_id > 0:
                        iter_kwargs = {"min_id": last_seen_id, "limit": None}
                    else:
                        iter_kwargs = {"limit": 500}

                    async for message in client.iter_messages(chat_id, **iter_kwargs):
                        if not message.text:
                            continue
                        await self._handle_message(session, message, group_cats, acc_id, cat_acc_map)

                    # Комментарии к постам канала
                    if is_channel:
                        async for post in client.iter_messages(chat_id, limit=20):
                            if not (post.replies and post.replies.replies):
                                continue
                            try:
                                async for comment in client.iter_messages(
                                    chat_id, reply_to=post.id, limit=30
                                ):
                                    if not comment.text:
                                        continue
                                    await self._handle_message(session, comment, group_cats, acc_id, cat_acc_map)
                            except Exception:
                                pass
            except FloodWaitError as e:
                logger.warning("FloodWait %s sec for joined group %s", e.seconds, chat_id)
                await asyncio.sleep(e.seconds)
            except Exception as e:
                logger.debug("Could not fetch joined group %s: %s", chat_id, e)

            # Небольшая пауза между группами чтобы не флудить
            await asyncio.sleep(0.5)

    async def _handle_message(
        self,
        session,
        message,
        categories: list[Category],
        acc_id: int,
        cat_acc_map: dict[int, set[int]],
    ) -> None:
        text = message.text or ""
        text_lower = text.lower()

        # 1. Дедупликация по (group_id, message_id) — одно и то же сообщение не обрабатываем дважды
        check = await session.execute(
            select(ParsedMessage).where(
                ParsedMessage.group_id == message.chat_id,
                ParsedMessage.message_id == message.id,
            )
        )
        if check.scalar_one_or_none():
            return

        # 2. Получаем отправителя — сначала из кеша сообщения, API не вызываем
        sender = message.sender  # уже загружен вместе с сообщением
        if sender is None:
            try:
                sender = await message.get_sender()
            except Exception:
                sender = None
        author_id = getattr(sender, "id", None)

        # 3. Дедупликация по автору: если тот же автор уже присылал идентичный текст — пропускаем
        if author_id and text:
            author_dup = await session.execute(
                select(ParsedMessage).where(
                    ParsedMessage.author_id == author_id,
                    ParsedMessage.text == text,
                ).limit(1)
            )
            if author_dup.scalar_one_or_none():
                return

        # 4. Фильтрация категорий по аккаунту
        applicable = [
            cat for cat in categories
            if not cat_acc_map.get(cat.id) or acc_id in cat_acc_map[cat.id]
        ]

        matched_cat = None
        for cat in applicable:
            # Проверяем минус-слова — если есть, категория не подходит
            stop_words = cat.get_stop_words()
            if stop_words and _has_stop_word(stop_words, text_lower):
                continue

            # Проверяем ключевые фразы
            for phrase in cat.get_keywords():
                if _match_phrase(phrase, text_lower):
                    matched_cat = cat
                    break

            if matched_cat:
                break

        if not matched_cat:
            return

        author_username = getattr(sender, "username", None)
        author_link = f"https://t.me/{author_username}" if author_username else (
            f"tg://user?id={author_id}" if author_id else None
        )

        pm = ParsedMessage(
            group_id=message.chat_id,
            message_id=message.id,
            author_id=author_id,
            category_id=matched_cat.id,
            text=message.text,
            author_username=author_username,
            author_link=author_link,
        )
        session.add(pm)
        await session.commit()

        await self._deliver_message(pm, matched_cat)

    async def _deliver_message(self, pm: ParsedMessage, cat: Category) -> None:
        if not self._bot:
            return

        async with async_session() as session:
            result = await session.execute(
                select(User)
                .join(UserCategory, UserCategory.user_id == User.id)
                .join(Subscription, Subscription.user_id == User.id)
                .where(
                    User.receiving_enabled == True,
                    User.is_blocked == False,
                    UserCategory.category_id == cat.id,
                    UserCategory.enabled == True,
                    Subscription.expires_at > datetime.utcnow(),
                )
            )
            users = result.scalars().all()

            from bot.keyboards.user_kb import message_action_kb
            from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError

            source = f"@{pm.author_username}" if pm.author_username else "Участник группы"
            text = (
                f"📨 <b>{cat.name}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{(pm.text or '')[:3500]}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 {source}"
            )
            kb = message_action_kb(pm.author_username, pm.author_link)

            for user in users:
                try:
                    await self._bot.send_message(
                        user.id, text, reply_markup=kb, parse_mode="HTML",
                    )
                    user.messages_received += 1
                except TelegramRetryAfter as e:
                    # Telegram просит подождать — соблюдаем
                    logger.warning("RetryAfter %s sec, pausing delivery.", e.retry_after)
                    await asyncio.sleep(e.retry_after)
                    try:
                        await self._bot.send_message(
                            user.id, text, reply_markup=kb, parse_mode="HTML",
                        )
                        user.messages_received += 1
                    except Exception:
                        pass
                except TelegramForbiddenError:
                    # Пользователь заблокировал бота — отключаем ему рассылку
                    user.receiving_enabled = False
                    logger.info("User %s blocked the bot, disabling delivery.", user.id)
                except Exception as e:
                    logger.debug("Deliver to user %s failed: %s", user.id, e)

                # Throttle: не более 25 сообщений/сек (лимит Telegram — 30/сек)
                await asyncio.sleep(0.04)

            await session.commit()


parser_manager = ParserManager()
