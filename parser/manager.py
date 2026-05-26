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
import hashlib
import itertools
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.utils import get_peer_id
from telethon.errors import (
    AuthKeyUnregisteredError, AuthKeyDuplicatedError, UserDeactivatedError,
    UserDeactivatedBanError, FloodWaitError, SessionPasswordNeededError,
)
from sqlalchemy import select, func, delete as sa_delete
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

# Окно дедупликации по (author_id, text_hash): сколько времени один и тот же автор
# с тем же текстом считается «уже виден». Раньше дедуп был навсегда — биржам
# вакансий/тендеров это ломает воспроизведение легитимных повторов.
DEDUP_WINDOW = timedelta(minutes=1)

# Сколько новых сообщений в минуту суммарно бот отправляет пользователям.
# Telegram ограничивает 30/сек — берём запас, чтобы при параллельных _deliver
# не словить RetryAfter. См. _GlobalSendLimiter ниже.
GLOBAL_SEND_RATE_PER_SEC = 25

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


def _normalize_for_match(s: str) -> str:
    """Приводит строку к виду, удобному для матчинга:
    lower-case + переносы строк → пробелы + схлопывание whitespace.

    Без этого фраза, скопированная админом с \\n, никогда не находилась бы в
    тексте поста, который реально написан в одну строку (и наоборот).
    """
    return " ".join(s.lower().split())


def _match_phrase(phrase: str, text: str) -> bool:
    """AND-матч: все слова фразы должны присутствовать в тексте (как подстроки).

    Соответствует обещанию админ-UI: «Слова внутри строки ищутся все вместе (AND)».
    Однословная фраза вырождается в обычное substring-вхождение.
    «ищу смм специалиста» найдёт «ищу хорошего смм специалиста» — между токенами
    может быть любой текст. Это намеренно слабее, чем строгая подстрока, потому
    что объявления редко повторяют слово-в-слово фразу из настроек категории.
    """
    phrase = _normalize_for_match(phrase)
    text = _normalize_for_match(text)
    if not phrase or not text:
        return False
    tokens = phrase.split()
    if len(tokens) == 1:
        return tokens[0] in text
    return all(tok in text for tok in tokens)


def _has_stop_word(stop_words: list[str], text: str) -> bool:
    """Возвращает True если в тексте найдено хотя бы одно минус-слово (подстрока)."""
    text_norm = _normalize_for_match(text)
    for sw in stop_words:
        sw = _normalize_for_match(sw)
        if sw and sw in text_norm:
            return True
    return False


def _text_hash(text: str) -> str:
    """Короткий идентификатор текста для индексной дедупликации.

    md5 достаточно — задача не криптографическая, а индексная: нам нужно
    дёшево находить «тот же текст» по equality без полного скана колонки Text.
    """
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


class _GlobalSendLimiter:
    """Глобальный токен-бакет + RetryAfter-aware pause.

    Две задачи:
    1) Сериализовать выдачу токенов: при нескольких параллельных _deliver_message
       суммарный rate не превышает rate_per_sec/сек. Без этого 5 параллельных
       доставок по 25 msg/сек дают 125 msg/сек → каскад RetryAfter.
    2) Если Telegram прислал RetryAfter (per-bot троттл), ВСЕ pending sends
       должны подождать общую паузу. Раньше каждый _deliver_message спал свой
       retry_after отдельно — остальные параллельные доставки продолжали слать
       в throttle, получали тот же RetryAfter, удлиняли его, и накапливался
       backoff в десятки минут.
    """

    def __init__(self, rate_per_sec: int) -> None:
        self._interval = 1.0 / rate_per_sec
        self._lock = asyncio.Lock()
        self._next_at = 0.0
        # Абсолютное loop.time() до которого все send'ы заблокированы из-за RetryAfter.
        # Когда любой sender ловит TelegramRetryAfter, он зовёт pause_until() — и
        # все остальные acquire() уйдут спать до того же момента.
        self._paused_until: float = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            # Глобальная пауза от RetryAfter — приоритетнее, чем токен-интервал.
            if self._paused_until > now:
                await asyncio.sleep(self._paused_until - now)
                now = loop.time()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = loop.time()
            self._next_at = max(now, self._next_at) + self._interval

    def pause(self, seconds: float) -> None:
        """Заявить глобальную паузу — все следующие acquire() будут её ждать."""
        loop = asyncio.get_event_loop()
        new_until = loop.time() + max(0.0, seconds)
        if new_until > self._paused_until:
            self._paused_until = new_until


_send_limiter = _GlobalSendLimiter(GLOBAL_SEND_RATE_PER_SEC)

# Temporary storage for pending sign-ins: {phone: (client, phone_code_hash, created_at)}.
# TTL автоматически выкидывает протухшие записи (админ нажал Отмена/закрыл вкладку),
# иначе они держат соединение и file-descriptors на каждую неуспешную попытку.
_pending: dict[str, tuple[TelegramClient, str, datetime]] = {}
_PENDING_TTL = timedelta(minutes=15)


async def _evict_expired_pending() -> None:
    """Освобождает зависшие sign-in клиенты по TTL."""
    now = datetime.utcnow()
    expired = [p for p, (_, _, ts) in _pending.items() if now - ts > _PENDING_TTL]
    for phone in expired:
        client, _, _ = _pending.pop(phone, (None, None, None))
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


def cancel_pending_signin(phone: str) -> None:
    """Снимает зависший sign-in: вызывается из FSM на отмену / ошибке.

    Делает best-effort disconnect, не падает если ничего не висит.
    Возвращать корутину неудобно (вызывается из synchronous handler-context),
    поэтому шедулим disconnect в loop.
    """
    record = _pending.pop(phone, None)
    if not record:
        return
    client, _, _ = record
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(client.disconnect())
    except Exception:
        pass


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
        # Связь канал-обсуждение: chat_id канала → chat_id discussion-группы.
        # Комментарии к постам канала прилетают в discussion-группу как обычные
        # NewMessage, и фильтр должен пропускать её chat_id, иначе аккаунт,
        # не подписанный на discussion, не получит realtime по комментариям.
        self._channel_to_discussion: dict[int, int] = {}
        # Lock на reload + сбор сообщений — иначе reload_clients() во время
        # активного _collect_messages даёт RuntimeError на итераторе и/или
        # дёргает методы на уже дисконнектнутом клиенте.
        self._reload_lock = asyncio.Lock()
        # Polling background task — храним чтобы корректно отменить в stop().
        # Без этого SIGTERM оставляет «Task was destroyed but it is pending».
        self._polling_task: asyncio.Task | None = None
        # Background task периодической попытки переподнять «invalid» аккаунты
        # без ручного вмешательства админа (бан мог быть временным).
        self._retry_task: asyncio.Task | None = None
        # Алерт админам делается один раз на инвалидацию каждого acc_id —
        # без этого при каждом reload_clients шлём дубль.
        self._alerted_invalid: set[int] = set()

    @property
    def _clients(self) -> list[TelegramClient]:
        """Обратная совместимость: список клиентов без acc_id."""
        return [c for c, _ in self._client_pairs]

    def set_bot(self, bot: "Bot") -> None:
        self._bot = bot

    async def start(self) -> None:
        await self.reload_clients()
        self._running = True
        self._polling_task = asyncio.create_task(self._polling_loop())
        self._retry_task = asyncio.create_task(self._invalid_retry_loop())

    async def stop(self) -> None:
        self._running = False
        for task in (self._polling_task, self._retry_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        for client in self._clients:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _invalidate_account(self, acc_id: int, reason: str) -> None:
        """Помечает аккаунт is_valid=False и единожды алертит админам."""
        async with async_session() as db:
            r = await db.execute(select(ParserAccount).where(ParserAccount.id == acc_id))
            a = r.scalar_one_or_none()
            if a:
                a.is_valid = False
                await db.commit()
        logger.warning("Account %s marked invalid (%s).", acc_id, reason)
        if acc_id in self._alerted_invalid:
            return
        self._alerted_invalid.add(acc_id)
        await self._notify_admins(
            f"⚠️ <b>Парсерный аккаунт acc_{acc_id} отвалился</b>\n"
            f"Причина: <code>{reason}</code>\n\n"
            f"Перевыдайте сессию через панель администратора → «Аккаунты»."
        )

    async def _notify_admins(self, text: str) -> None:
        """Шлёт alert всем admin_ids. Используется при инвалидации аккаунтов
        и других чрезвычайных событиях, чтобы прод не «тихо умирал»."""
        if not self._bot:
            return
        try:
            from config import load_config
            cfg = load_config()
            for admin_id in cfg.admin_ids:
                try:
                    await self._bot.send_message(admin_id, text, parse_mode="HTML")
                except Exception as e:
                    logger.debug("Cannot notify admin %s: %s", admin_id, e)
        except Exception as e:
            logger.error("Admin alert failed: %s", e)

    def _install_disconnect_hook(self, client: TelegramClient, acc_id: int) -> None:
        """Подписывается на disconnect клиента, чтобы поймать фоновый
        AuthKeyDuplicated (Telethon роняет его из recv-loop, не из connect())
        и убрать мёртвый клиент из пула, иначе round-robin будет дёргать его."""

        loop = asyncio.get_event_loop()

        def _on_disconnect(_client):
            # Запускаем cleanup в loop — disconnect-хук может вызваться из любого треда.
            asyncio.run_coroutine_threadsafe(
                self._handle_client_disconnect(client, acc_id), loop
            )

        try:
            client.on(_on_disconnect)  # type: ignore[attr-defined]
        except Exception:
            # Не все версии Telethon экспортируют on(); fallback тихий.
            pass

    async def _handle_client_disconnect(self, client: TelegramClient, acc_id: int) -> None:
        """Удаляет мёртвого клиента из пулов. Вызывается из disconnect-хука."""
        try:
            authorized = await asyncio.wait_for(client.is_user_authorized(), timeout=3)
        except Exception:
            authorized = False
        if authorized:
            return  # дисконнект был временный, Telethon переподключится сам
        self._client_pairs = [p for p in self._client_pairs if p[0] is not client]
        self._joined_pairs = [p for p in self._joined_pairs if p[0] is not client]
        self._cycle = itertools.cycle(self._client_pairs) if self._client_pairs else itertools.cycle([])
        await self._invalidate_account(acc_id, "AuthKey/disconnect")

    async def reload_clients(self) -> None:
        # Lock защищает от race condition с _collect_messages, где идёт
        # итерация self._client_pairs — без него dict/list changed during iteration.
        async with self._reload_lock:
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
                client = make_client(acc.session_string, proxy)
                try:
                    await client.connect()
                    if not await client.is_user_authorized():
                        raise AuthKeyUnregisteredError(request=None)
                    self._client_pairs.append((client, acc.id))
                    if acc.parse_joined_groups:
                        self._joined_pairs.append((client, acc.id))
                    self._install_disconnect_hook(client, acc.id)
                    # Если аккаунт раньше алертили — это успешный re-auth,
                    # сбрасываем флаг, чтобы будущая инвалидация снова уведомила.
                    self._alerted_invalid.discard(acc.id)
                    logger.info("Parser client account_%s started (joined=%s).", acc.id, acc.parse_joined_groups)
                except (
                    AuthKeyUnregisteredError, AuthKeyDuplicatedError,
                    UserDeactivatedError, UserDeactivatedBanError,
                ) as e:
                    await self._invalidate_account(acc.id, type(e).__name__)
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
        await _evict_expired_pending()
        from config import load_config
        cfg = load_config()
        client = TelegramClient(StringSession(), cfg.tg_api_id, cfg.tg_api_hash)
        await client.connect()
        result = await client.send_code_request(phone)
        _pending[phone] = (client, result.phone_code_hash, datetime.utcnow())
        return result.phone_code_hash

    async def sign_in(self, phone: str, code: str, phone_code_hash: str) -> str:
        client, _, _ = _pending[phone]
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            # Клиент остаётся в _pending для последующего sign_in_2fa.
            raise
        except Exception:
            cancel_pending_signin(phone)
            raise
        ss = client.session.save()
        await client.disconnect()
        _pending.pop(phone, None)
        return ss

    async def sign_in_2fa(self, phone: str, password: str) -> str:
        client, _, _ = _pending[phone]
        try:
            await client.sign_in(password=password)
        except Exception:
            cancel_pending_signin(phone)
            raise
        ss = client.session.save()
        await client.disconnect()
        _pending.pop(phone, None)
        return ss

    # ------------------------------------------------------------------
    # Join groups (admin tools)
    # ------------------------------------------------------------------

    async def join_group(self, link_or_chat_id) -> dict:
        """Подписывает все парсерные аккаунты на ПРИВАТНУЮ группу по инвайт-ссылке.

        Публичные группы (`@x`, `t.me/x`) пропускаются — для них вступление не
        требуется: парсер читает историю через get_entity+iter_messages у любого
        аккаунта. Лимит ~500 каналов/аккаунт ценный, тратим его только на
        приватные группы, где realtime без членства не работает.

        Возвращает словарь {acc_id: status}, где status — одна из строк:
        "joined", "already", "skipped: public", "error: <текст>".
        """
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

        if not invite_hash:
            # Публичная группа — парсим напрямую, вступать не нужно.
            for _, acc_id in self._client_pairs:
                result[acc_id] = "skipped: public"
            return result

        for client, acc_id in self._client_pairs:
            try:
                try:
                    await client(ImportChatInviteRequest(invite_hash))
                    result[acc_id] = "joined"
                except UserAlreadyParticipantError:
                    result[acc_id] = "already"
            except FloodWaitError as e:
                result[acc_id] = f"floodwait {e.seconds}s"
                logger.warning("FloodWait %s sec joining %s acc=%s", e.seconds, link, acc_id)
                # Раньше было min(e.seconds, 10) — Telegram считает, что вы должны
                # выждать ПОЛНУЮ задержку, иначе на следующем запросе flood удваивается
                # вплоть до временного бана. Капать НЕЛЬЗЯ.
                await asyncio.sleep(e.seconds)
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

        Discussion-группы каналов (см. self._channel_to_discussion) тоже добавляются —
        иначе realtime-комментарии не доходят: они приходят как NewMessage с
        chat_id = id группы обсуждения, а не канала.
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
        self._explicit_chat_ids.update(self._channel_to_discussion.values())
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

    async def _invalid_retry_loop(self) -> None:
        """Раз в час пытается переподнять аккаунты, помеченные is_valid=False.

        Бан Telegram бывает временным; без авто-retry парсинг тихо умирает
        и админ узнаёт об этом только по жалобам пользователей.
        """
        await asyncio.sleep(3600)
        while self._running:
            try:
                async with async_session() as db:
                    res = await db.execute(
                        select(ParserAccount).where(
                            ParserAccount.is_active == True,
                            ParserAccount.is_valid == False,
                        )
                    )
                    invalid = res.scalars().all()
                if invalid:
                    logger.info("Retry %d invalid accounts...", len(invalid))
                    # Поднимаем их обратно: ставим is_valid=True и зовём reload_clients.
                    # Те, что реально мертвы, пометятся обратно в _invalidate_account.
                    async with async_session() as db:
                        for acc in invalid:
                            a = await db.get(ParserAccount, acc.id)
                            if a:
                                a.is_valid = True
                        await db.commit()
                    await self.reload_clients()
            except Exception as e:
                logger.error("Invalid retry loop error: %s", e)
            await asyncio.sleep(3600)

    async def _collect_messages(self) -> None:
        # Снапшот self._client_pairs — на случай если reload_clients произойдёт
        # параллельно: lock в reload_clients частично защищает, но дополнительная
        # копия гарантирует стабильный итератор.
        client_pairs_snapshot = list(self._client_pairs)
        joined_pairs_snapshot = list(self._joined_pairs)
        if not client_pairs_snapshot:
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
                for pair in client_pairs_snapshot:
                    if pair[1] == owner_acc_id:
                        ordered.append(pair)
                        break
            for pair in client_pairs_snapshot:
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
        for client, acc_id in joined_pairs_snapshot:
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

                # Запоминаем связь канал → discussion-группа, чтобы realtime-фильтр
                # пропускал комментарии (они приходят в discussion как обычный NewMessage).
                # У discussion-группы свой chat_id, отличный от канала, и без этой
                # привязки фильтр их режет → realtime для комментариев не работает.
                discussion_chat_id: int | None = None
                linked_id = getattr(entity, "linked_chat_id", None)
                if actually_channel and linked_id:
                    try:
                        linked_entity = await client.get_entity(linked_id)
                        discussion_chat_id = get_peer_id(linked_entity)
                        if discussion_chat_id and discussion_chat_id != self._channel_to_discussion.get(resolved_chat_id):
                            self._channel_to_discussion[resolved_chat_id] = discussion_chat_id
                            # Чтобы realtime сразу подхватил discussion-id
                            self._explicit_chat_ids.add(discussion_chat_id)
                    except Exception:
                        pass

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

                # Если это канал — дополнительно парсим комментарии к постам.
                # last_seen для комментариев ведём по chat_id discussion-группы
                # (message.chat_id комментариев = discussion id), иначе каждый
                # цикл прогоняли бы те же 30 комментариев × 20 постов вхолостую.
                if group.is_channel:
                    comments_last_seen = 0
                    if discussion_chat_id:
                        comments_last_seen = await self._get_last_seen_message_id(
                            session, discussion_chat_id
                        )
                    async for post in client.iter_messages(entity, limit=20):
                        if not (post.replies and post.replies.replies):
                            continue
                        try:
                            comment_kwargs = {"reply_to": post.id, "limit": 30}
                            if comments_last_seen > 0:
                                comment_kwargs["min_id"] = comments_last_seen
                                comment_kwargs.pop("limit", None)
                            async for comment in client.iter_messages(entity, **comment_kwargs):
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
        text_h = _text_hash(text)

        # 1. Дедупликация по (group_id, message_id) — одно и то же сообщение не обрабатываем дважды
        check = await session.execute(
            select(ParsedMessage.id).where(
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

        # 3. Дедупликация по автору в коротком окне (DEDUP_WINDOW).
        # Раньше дедуп был навсегда + по колонке Text без индекса → full table scan
        # и потеря легитимных повторов от того же автора. Теперь равенство по
        # text_hash (md5) с composite-index'ом и WHERE parsed_at > NOW()-window.
        if author_id and text:
            window_start = datetime.utcnow() - DEDUP_WINDOW
            author_dup = await session.execute(
                select(ParsedMessage.id).where(
                    ParsedMessage.author_id == author_id,
                    ParsedMessage.text_hash == text_h,
                    ParsedMessage.parsed_at > window_start,
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
            text_hash=text_h,
            author_username=author_username,
            author_link=author_link,
        )
        session.add(pm)
        await session.commit()

        await self._deliver_message(pm, matched_cat)

    async def _deliver_message(self, pm: ParsedMessage, cat: Category) -> None:
        if not self._bot:
            return

        # 1. Снимаем снапшот получателей одним запросом и СРАЗУ ЗАКРЫВАЕМ сессию.
        # Раньше сессия держалась открытой всю доставку: autoflush на
        # user.messages_received += 1 открывал write-транзакцию SQLite сразу
        # же на первом sub_check, и она висела все ~N*0.04с до commit.
        # Под нагрузкой это давало "database is locked" в realtime-handler'ах,
        # потому что parsed_messages-инсерты ждали освобождения write-лока.
        async with async_session() as session:
            result = await session.execute(
                select(
                    User.id, User.username,
                    Subscription.expires_at,
                ).join(UserCategory, UserCategory.user_id == User.id)
                .join(Subscription, Subscription.user_id == User.id)
                .where(
                    User.receiving_enabled == True,
                    User.is_blocked == False,
                    UserCategory.category_id == cat.id,
                    UserCategory.enabled == True,
                    Subscription.expires_at > datetime.utcnow(),
                )
            )
            recipients = result.all()

        from bot.keyboards.user_kb import message_action_kb
        from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError
        from sqlalchemy import update

        source = f"@{pm.author_username}" if pm.author_username else "Участник группы"
        text = (
            f"📨 <b>{cat.name}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{(pm.text or '')[:3500]}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 {source}"
        )
        kb = message_action_kb(pm.author_username, pm.author_link)

        # 2. Шлём сообщения БЕЗ открытой сессии — никаких locks на parsed_messages
        # пока крутится цикл доставки.
        now = datetime.utcnow()
        delivered_user_ids: list[int] = []
        forbidden_user_ids: list[int] = []
        for user_id, _username, expires_at in recipients:
            # Recheck: за время доставки подписка могла истечь.
            if not expires_at or expires_at <= now:
                continue
            try:
                await _send_limiter.acquire()
                await self._bot.send_message(
                    user_id, text, reply_markup=kb, parse_mode="HTML",
                )
                delivered_user_ids.append(user_id)
            except TelegramRetryAfter as e:
                # Telegram per-bot throttle. Сообщаем лимитеру — он притормозит
                # ВСЕ параллельные доставки на ту же паузу, чтобы они не
                # продолжали долбить API и не удлиняли backoff.
                logger.warning("RetryAfter %s sec, pausing all delivery globally.", e.retry_after)
                _send_limiter.pause(e.retry_after)
                # Retry'ить тот же send в этом же цикле бесполезно — троттл
                # ещё активен. Этот юзер не получит ЭТО сообщение, но следующее
                # дойдёт нормально (потеря 1 сообщения на 1 юзера, не страшно).
            except TelegramForbiddenError:
                forbidden_user_ids.append(user_id)
                logger.info("User %s blocked the bot, disabling delivery.", user_id)
            except Exception as e:
                logger.debug("Deliver to user %s failed: %s", user_id, e)

        # 3. Один короткий write-транзакт в конце: обновляем счётчики
        # и снимаем receiving_enabled у тех, кто заблокировал бота.
        if delivered_user_ids or forbidden_user_ids:
            async with async_session() as session:
                if delivered_user_ids:
                    await session.execute(
                        update(User)
                        .where(User.id.in_(delivered_user_ids))
                        .values(messages_received=User.messages_received + 1)
                    )
                if forbidden_user_ids:
                    await session.execute(
                        update(User)
                        .where(User.id.in_(forbidden_user_ids))
                        .values(receiving_enabled=False)
                    )
                await session.commit()


parser_manager = ParserManager()
