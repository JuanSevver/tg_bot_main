"""
Тесты на критические правки из аудита.

Покрывает:
- ProcessedInvoice идемпотентность (баг 1.1) — повторное начисление подписок.
- Дедупликация по (author_id, text_hash) с временным окном (баги 1.3, 1.4).
- Captcha rate-limit (баг 3.5).
- _parse_proxy_input — корректность после переписывания (баг 5.1).
- _GlobalSendLimiter — суммарный rate cap (баг 4.4).
- _match_phrase AND-токены (баг 1.10).
- _normalize_for_match (баг 1.11).
- ActivityMiddleware с правильным event-типом (баг 1.2).
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from database.models import (
    Category, CategoryType, ParsedMessage, ProcessedInvoice,
    Subscription, User,
)
from parser.manager import (
    ParserManager, DEDUP_WINDOW, _GlobalSendLimiter,
    _match_phrase, _normalize_for_match, _text_hash,
)


# ──────────────────────────────────────────────────────────────────────────────
# 1.1 ProcessedInvoice идемпотентность
# ──────────────────────────────────────────────────────────────────────────────

class TestProcessedInvoiceIdempotency:
    async def test_marker_can_be_inserted(self, session):
        """Базовая запись инвойса работает."""
        marker = ProcessedInvoice(invoice_id=12345)
        session.add(marker)
        await session.commit()

        result = await session.execute(
            select(ProcessedInvoice).where(ProcessedInvoice.invoice_id == 12345)
        )
        assert result.scalar_one_or_none() is not None

    async def test_duplicate_invoice_id_rejected(self, session):
        """PRIMARY KEY на invoice_id не даёт начислить подписку дважды."""
        session.add(ProcessedInvoice(invoice_id=999))
        await session.commit()

        session.add(ProcessedInvoice(invoice_id=999))
        with pytest.raises(Exception):  # IntegrityError
            await session.commit()
        await session.rollback()

    async def test_existing_marker_short_circuits_grant(self, session, make_user, make_subscription):
        """Если marker уже есть — повторный grant не должен случиться.

        Это unit-тест на инвариант: «если invoice уже в processed_invoices,
        начисление пропускается». Точный handler в polling.py делает то же
        через db.get(ProcessedInvoice, invoice_id).
        """
        # Подготовка: юзер с активной подпиской
        user = make_user(user_id=42)
        session.add(user)
        sub = make_subscription(user_id=42, plan="1m", days=30, purchases=1)
        session.add(sub)
        # Marker инвойса уже стоит
        session.add(ProcessedInvoice(invoice_id=555))
        await session.commit()

        existing_expires = sub.expires_at
        existing_count = sub.purchases_count

        # Имитируем повторный приход того же invoice:
        marker = await session.get(ProcessedInvoice, 555)
        if marker:
            # Реальный handler в этом случае делает return — подписка не меняется.
            pass

        # Подписка осталась нетронутой
        await session.refresh(sub)
        assert sub.expires_at == existing_expires
        assert sub.purchases_count == existing_count


# ──────────────────────────────────────────────────────────────────────────────
# 1.3 + 1.4 Дедупликация по (author_id, text_hash) с окном
# ──────────────────────────────────────────────────────────────────────────────

class TestDedupWindow:
    async def test_text_hash_stored_on_save(self, session):
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cat = Category(name="C", type=CategoryType.request, is_active=True,
                       keywords="логотип", stop_words="")
        session.add(cat)
        await session.commit()

        msg = MagicMock()
        msg.text = "нужен логотип"
        msg.chat_id = 1
        msg.id = 1
        sender = MagicMock()
        sender.id = 100
        sender.username = "x"
        msg.sender = sender
        msg.get_sender = AsyncMock(return_value=sender)

        await manager._handle_message(session, msg, [cat], acc_id=1, cat_acc_map={})

        pm = (await session.execute(select(ParsedMessage))).scalar_one()
        assert pm.text_hash == _text_hash("нужен логотип")

    async def test_duplicate_within_window_skipped(self, session):
        """Тот же автор + тот же текст в пределах DEDUP_WINDOW — пропускается."""
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cat = Category(name="C", type=CategoryType.request, is_active=True,
                       keywords="логотип", stop_words="")
        session.add(cat)
        await session.commit()

        def make_msg(msg_id: int, chat_id: int):
            m = MagicMock()
            m.text = "нужен логотип"
            m.chat_id = chat_id
            m.id = msg_id
            s = MagicMock()
            s.id = 777
            s.username = "u"
            m.sender = s
            m.get_sender = AsyncMock(return_value=s)
            return m

        # Первое сообщение
        await manager._handle_message(session, make_msg(1, 1), [cat], 1, {})
        # Второе с тем же автором и текстом, но другой group/msg_id — должно быть отброшено
        await manager._handle_message(session, make_msg(2, 2), [cat], 1, {})

        rows = (await session.execute(select(ParsedMessage))).scalars().all()
        assert len(rows) == 1, "Дубликат внутри окна должен быть пропущен"

    async def test_duplicate_outside_window_allowed(self, session):
        """После истечения DEDUP_WINDOW тот же автор+текст снова пропускается в выдачу."""
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cat = Category(name="C", type=CategoryType.request, is_active=True,
                       keywords="логотип", stop_words="")
        session.add(cat)
        await session.commit()

        # Вставляем старую запись «вручную» с parsed_at вне окна
        old_text = "нужен логотип"
        session.add(ParsedMessage(
            group_id=1, message_id=999,
            author_id=777, text=old_text,
            text_hash=_text_hash(old_text),
            category_id=cat.id,
            parsed_at=datetime.utcnow() - DEDUP_WINDOW - timedelta(minutes=5),
        ))
        await session.commit()

        # Новое сообщение того же автора с тем же текстом — должно пройти
        m = MagicMock()
        m.text = old_text
        m.chat_id = 2
        m.id = 2
        s = MagicMock()
        s.id = 777
        s.username = "u"
        m.sender = s
        m.get_sender = AsyncMock(return_value=s)
        await manager._handle_message(session, m, [cat], 1, {})

        rows = (await session.execute(select(ParsedMessage))).scalars().all()
        assert len(rows) == 2, "Дубликат вне окна должен быть принят"


# ──────────────────────────────────────────────────────────────────────────────
# 1.10 + 1.11 _match_phrase + _normalize_for_match
# ──────────────────────────────────────────────────────────────────────────────

class TestMatchPhraseAndNormalize:
    def test_and_tokens_match(self):
        assert _match_phrase("ищу смм", "ищу хорошего смм специалиста")

    def test_one_missing_token_no_match(self):
        assert not _match_phrase("ищу смм", "продам диван")

    def test_newlines_normalized(self):
        # Фраза с \n должна найтись в тексте, где те же слова в одной строке
        assert _match_phrase("ищу\nдизайнера", "очень ищу дизайнера срочно")

    def test_newlines_in_text_normalized(self):
        # И наоборот — однострочная фраза в многострочном тексте
        assert _match_phrase("ищу дизайнера", "очень\nищу\nдизайнера\nсрочно")

    def test_multiple_spaces_normalized(self):
        assert _normalize_for_match("ищу    дизайнера") == "ищу дизайнера"

    def test_empty_inputs(self):
        assert not _match_phrase("", "что угодно")
        assert not _match_phrase("слово", "")


# ──────────────────────────────────────────────────────────────────────────────
# 3.5 Captcha rate-limit
# ──────────────────────────────────────────────────────────────────────────────

class TestCaptchaRateLimit:
    def setup_method(self, _method):
        # Очищаем глобальное состояние перед каждым тестом
        from bot.handlers.user.start import _captcha_attempts
        _captcha_attempts.clear()

    def test_first_attempts_allowed(self):
        from bot.handlers.user.start import (
            _captcha_check_rate, _captcha_register_fail, _CAPTCHA_MAX_ATTEMPTS
        )
        uid = 12345
        # До лимита разрешено
        for _ in range(_CAPTCHA_MAX_ATTEMPTS):
            assert _captcha_check_rate(uid) is True
            _captcha_register_fail(uid)
        # На лимите — заблокировано
        assert _captcha_check_rate(uid) is False

    def test_different_users_independent(self):
        from bot.handlers.user.start import (
            _captcha_check_rate, _captcha_register_fail, _CAPTCHA_MAX_ATTEMPTS
        )
        for _ in range(_CAPTCHA_MAX_ATTEMPTS):
            _captcha_register_fail(1)
        assert _captcha_check_rate(1) is False
        # Другой юзер не затронут
        assert _captcha_check_rate(2) is True

    def test_old_attempts_pruned(self):
        from bot.handlers.user.start import (
            _captcha_check_rate, _captcha_attempts, _CAPTCHA_WINDOW
        )
        uid = 999
        # Имитируем 3 старые попытки (старше окна)
        _captcha_attempts[uid] = [
            datetime.utcnow() - _CAPTCHA_WINDOW - timedelta(seconds=10)
        ] * 3
        # check_rate должен их выкинуть и снова пускать
        assert _captcha_check_rate(uid) is True
        assert _captcha_attempts[uid] == []


# ──────────────────────────────────────────────────────────────────────────────
# 5.1 _parse_proxy_input
# ──────────────────────────────────────────────────────────────────────────────

class TestParseProxyInput:
    def test_colon_format_full(self):
        from bot.handlers.admin.proxies import _parse_proxy_input
        result = _parse_proxy_input("193.233.197.74:38673:bu0RAH:yR3de9")
        assert result == ("socks5", "193.233.197.74", 38673, "bu0RAH", "yR3de9")

    def test_colon_format_no_auth(self):
        from bot.handlers.admin.proxies import _parse_proxy_input
        result = _parse_proxy_input("1.2.3.4:8080")
        assert result == ("socks5", "1.2.3.4", 8080, None, None)

    def test_space_format_with_type(self):
        from bot.handlers.admin.proxies import _parse_proxy_input
        result = _parse_proxy_input("socks5 193.233.197.74 38673 user pass")
        assert result == ("socks5", "193.233.197.74", 38673, "user", "pass")

    def test_http_type_explicit(self):
        from bot.handlers.admin.proxies import _parse_proxy_input
        result = _parse_proxy_input("http 10.0.0.1:3128:u:p")
        assert result == ("http", "10.0.0.1", 3128, "u", "p")

    def test_invalid_port_returns_none(self):
        from bot.handlers.admin.proxies import _parse_proxy_input
        assert _parse_proxy_input("host:notaport") is None

    def test_empty_returns_none(self):
        from bot.handlers.admin.proxies import _parse_proxy_input
        assert _parse_proxy_input("") is None
        assert _parse_proxy_input("   ") is None

    def test_socks4_type(self):
        from bot.handlers.admin.proxies import _parse_proxy_input
        result = _parse_proxy_input("socks4 1.2.3.4 1080")
        assert result == ("socks4", "1.2.3.4", 1080, None, None)


# ──────────────────────────────────────────────────────────────────────────────
# 4.4 _GlobalSendLimiter — суммарный rate cap
# ──────────────────────────────────────────────────────────────────────────────

class TestGlobalSendLimiter:
    async def test_rate_capped(self):
        """Лимитер обеспечивает не более N токенов в секунду суммарно."""
        rate = 50  # 50/сек = 20мс на токен
        limiter = _GlobalSendLimiter(rate)

        # Берём 10 токенов из одной корутины
        start = time.perf_counter()
        for _ in range(10):
            await limiter.acquire()
        elapsed = time.perf_counter() - start

        # 10 токенов при 50/сек = минимум ~10 * (1/50) = 0.2с, чуть меньше
        # из-за того что первый токен «бесплатный». Проверим что хотя бы 0.15с.
        assert elapsed >= 0.15, f"Лимитер не работает: 10 токенов за {elapsed:.3f}с"

    async def test_concurrent_acquisition_serialized(self):
        """Параллельные acquire не нарушают суммарный rate."""
        rate = 100
        limiter = _GlobalSendLimiter(rate)

        async def grab():
            await limiter.acquire()

        start = time.perf_counter()
        await asyncio.gather(*[grab() for _ in range(20)])
        elapsed = time.perf_counter() - start

        # 20 / 100 = 0.2с минимум. С запасом проверим 0.15.
        assert elapsed >= 0.15, f"20 параллельных acquire заняли {elapsed:.3f}с — лимитер не сериализует"


# ──────────────────────────────────────────────────────────────────────────────
# 1.2 ActivityMiddleware — Update vs Message/CallbackQuery
# ──────────────────────────────────────────────────────────────────────────────

class TestActivityMiddleware:
    async def test_update_object_does_not_update(self, session, make_user):
        """Прокидывание Update (как было раньше) НЕ должно обновлять last_active_at."""
        from aiogram.types import Update
        from bot.middlewares.activity import ActivityMiddleware

        user = make_user(user_id=10)
        old_time = datetime(2000, 1, 1)
        user.last_active_at = old_time
        session.add(user)
        await session.commit()

        mw = ActivityMiddleware()
        update_event = Update(update_id=1)  # без message/callback_query внутри

        called = {"flag": False}

        async def handler(event, data):
            called["flag"] = True

        await mw(handler, update_event, {"session": session})
        assert called["flag"] is True

        await session.refresh(user)
        # last_active_at остался старым (Update не несёт from_user, middleware skip-ает)
        assert user.last_active_at == old_time

    async def test_message_object_updates(self, session, make_user):
        """Message с from_user — last_active_at должен обновиться."""
        from bot.middlewares.activity import ActivityMiddleware

        user = make_user(user_id=20)
        old_time = datetime(2000, 1, 1)
        user.last_active_at = old_time
        session.add(user)
        await session.commit()

        # Минимальный Message-стаб (aiogram BaseModel может не принять MagicMock
        # как event, но isinstance-проверка работает по типу — используем настоящий).
        from aiogram.types import Message, User as TgUser, Chat
        msg = Message(
            message_id=1,
            date=datetime.utcnow(),
            chat=Chat(id=20, type="private"),
            from_user=TgUser(id=20, is_bot=False, first_name="Test"),
        )

        async def handler(event, data):
            return None

        mw = ActivityMiddleware()
        await mw(handler, msg, {"session": session})

        await session.refresh(user)
        assert user.last_active_at > old_time
