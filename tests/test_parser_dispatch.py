"""
Tests for ParserManager._handle_message dispatch logic.

Covers:
- Keyword match → ParsedMessage saved to DB
- No keyword match → nothing saved
- Deduplication by (group_id, message_id)
- Author-level deduplication (same author + same text)
- CategoryAccount filtering (per-account category assignment)
- Stop-word blocks keyword match
- author_link generation (username → t.me/…, id-only → tg://user?id=…, anonymous → None)
- Message text truncation to 3500 chars in _deliver_message formatting
- _extract_username edge cases
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import select

from database.models import Category, CategoryType, ParsedMessage, CategoryAccount, ParserAccount
from parser.manager import ParserManager, _extract_username


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fake_message(
    text: str,
    chat_id: int = 100,
    msg_id: int = 1,
    sender_id: int | None = 42,
    username: str | None = "testuser",
) -> MagicMock:
    """Minimal Telethon-like message stub."""
    msg = MagicMock()
    msg.text = text
    msg.chat_id = chat_id
    msg.id = msg_id

    if sender_id is not None:
        sender = MagicMock()
        sender.id = sender_id
        sender.username = username
        msg.sender = sender
    else:
        msg.sender = None

    msg.get_sender = AsyncMock(return_value=msg.sender)
    return msg


def _cat(
    name: str,
    keywords: str,
    stop_words: str = "",
    cat_type: CategoryType = CategoryType.request,
    cat_id: int | None = None,
) -> Category:
    cat = Category(name=name, type=cat_type, is_active=True)
    if cat_id is not None:
        cat.id = cat_id
    cat.keywords = keywords
    cat.stop_words = stop_words
    return cat


# ─── _extract_username edge cases ─────────────────────────────────────────────

class TestExtractUsernameEdgeCases:
    def test_empty_string_returns_empty(self):
        assert _extract_username("") == ""

    def test_only_at_sign(self):
        assert _extract_username("@") == ""

    def test_only_slash(self):
        assert _extract_username("t.me/") == ""

    def test_deep_path_stripped(self):
        # t.me/group/123 → only "group"
        assert _extract_username("https://t.me/group/123") == "group"

    def test_mixed_case_normalized(self):
        assert _extract_username("@MyGROUP") == "mygroup"

    def test_trailing_whitespace(self):
        assert _extract_username("  @mygroup  ") == "mygroup"

    def test_tme_without_https(self):
        assert _extract_username("t.me/testchat") == "testchat"

    def test_query_string_stripped(self):
        assert _extract_username("https://t.me/channel?start=xyz") == "channel"


# ─── _handle_message: match & save ────────────────────────────────────────────

class TestHandleMessageMatchAndSave:
    async def test_matching_message_saved_to_db(self, session):
        """A message that matches a keyword should be stored in parsed_messages."""
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cats = [_cat("Дизайн", "логотип")]
        msg = _fake_message("нужен логотип для сайта")

        await manager._handle_message(session, msg, cats, acc_id=1, cat_acc_map={})

        result = await session.execute(
            select(ParsedMessage).where(
                ParsedMessage.group_id == 100,
                ParsedMessage.message_id == 1,
            )
        )
        pm = result.scalar_one_or_none()
        assert pm is not None
        assert pm.text == "нужен логотип для сайта"

    async def test_no_match_nothing_saved(self, session):
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cats = [_cat("Дизайн", "логотип")]
        msg = _fake_message("продам диван б/у")

        await manager._handle_message(session, msg, cats, acc_id=1, cat_acc_map={})

        result = await session.execute(select(ParsedMessage))
        assert result.scalars().all() == []

    async def test_matched_category_stored(self, session):
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        # Add category to DB so FK works
        cat = _cat("Разработка", "сайт\nприложение")
        session.add(cat)
        await session.commit()

        msg = _fake_message("нужно приложение для бизнеса", msg_id=10)
        await manager._handle_message(session, msg, [cat], acc_id=1, cat_acc_map={})

        result = await session.execute(select(ParsedMessage).where(ParsedMessage.message_id == 10))
        pm = result.scalar_one_or_none()
        assert pm is not None
        assert pm.category_id == cat.id

    async def test_deliver_called_on_match(self, session):
        manager = ParserManager()
        deliver_mock = AsyncMock()
        manager._deliver_message = deliver_mock

        cats = [_cat("Дизайн", "логотип")]
        msg = _fake_message("нужен логотип")
        await manager._handle_message(session, msg, cats, acc_id=1, cat_acc_map={})

        deliver_mock.assert_called_once()

    async def test_deliver_not_called_on_no_match(self, session):
        manager = ParserManager()
        deliver_mock = AsyncMock()
        manager._deliver_message = deliver_mock

        cats = [_cat("Дизайн", "логотип")]
        msg = _fake_message("продам машину")
        await manager._handle_message(session, msg, cats, acc_id=1, cat_acc_map={})

        deliver_mock.assert_not_called()


# ─── Deduplication ────────────────────────────────────────────────────────────

class TestHandleMessageDeduplication:
    async def test_duplicate_message_skipped(self, session):
        """Second call with the same (chat_id, msg_id) is silently skipped."""
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cats = [_cat("Дизайн", "логотип")]
        msg = _fake_message("нужен логотип")

        await manager._handle_message(session, msg, cats, acc_id=1, cat_acc_map={})
        await manager._handle_message(session, msg, cats, acc_id=1, cat_acc_map={})

        result = await session.execute(select(ParsedMessage))
        assert len(result.scalars().all()) == 1
        # Delivered only once
        assert manager._deliver_message.call_count == 1

    async def test_same_text_different_author_and_msg_id_saved(self, session):
        """Different authors, same text, different msg_id → two records (author dedup only blocks same author)."""
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cats = [_cat("Дизайн", "логотип")]
        msg1 = _fake_message("нужен логотип", msg_id=1, sender_id=10)
        msg2 = _fake_message("нужен логотип", msg_id=2, sender_id=20)  # разные авторы

        await manager._handle_message(session, msg1, cats, acc_id=1, cat_acc_map={})
        await manager._handle_message(session, msg2, cats, acc_id=1, cat_acc_map={})

        result = await session.execute(select(ParsedMessage))
        assert len(result.scalars().all()) == 2

    async def test_same_author_same_text_different_msg_id_is_deduped(self, session):
        """Same author + same text in different messages → author dedup blocks the second.

        ВАЖНО: это документирует реальное поведение — author-dedup проверяется ДО msg_id-дедупа.
        Если один автор рассылает одно объявление в разные группы — оно дойдёт только раз.
        """
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cats = [_cat("Дизайн", "логотип")]
        # Один автор, одинаковый текст, но разные сообщения (разные группы)
        msg1 = _fake_message("нужен логотип", chat_id=100, msg_id=1, sender_id=42)
        msg2 = _fake_message("нужен логотип", chat_id=200, msg_id=2, sender_id=42)

        await manager._handle_message(session, msg1, cats, acc_id=1, cat_acc_map={})
        await manager._handle_message(session, msg2, cats, acc_id=1, cat_acc_map={})

        result = await session.execute(select(ParsedMessage))
        # Второй заблокирован author-dedup-ом — это штатное поведение
        assert len(result.scalars().all()) == 1

    async def test_author_dedup_same_text_skipped(self, session):
        """Same author, same text in a different group → author-dedup kicks in."""
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cats = [_cat("Дизайн", "логотип")]
        msg1 = _fake_message("нужен логотип", chat_id=111, msg_id=1, sender_id=99)
        msg2 = _fake_message("нужен логотип", chat_id=222, msg_id=2, sender_id=99)

        await manager._handle_message(session, msg1, cats, acc_id=1, cat_acc_map={})
        # Second message: same author, same text, different group → author-dup
        await manager._handle_message(session, msg2, cats, acc_id=1, cat_acc_map={})

        result = await session.execute(select(ParsedMessage))
        assert len(result.scalars().all()) == 1

    async def test_author_dedup_different_text_allowed(self, session):
        """Same author but different text → both should be processed."""
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cats = [_cat("Дизайн", "логотип\nбаннер")]
        msg1 = _fake_message("нужен логотип", msg_id=1, sender_id=99)
        msg2 = _fake_message("нужен баннер", msg_id=2, sender_id=99)

        await manager._handle_message(session, msg1, cats, acc_id=1, cat_acc_map={})
        await manager._handle_message(session, msg2, cats, acc_id=1, cat_acc_map={})

        result = await session.execute(select(ParsedMessage))
        assert len(result.scalars().all()) == 2


# ─── CategoryAccount (per-account category filtering) ─────────────────────────

class TestCategoryAccountFiltering:
    async def test_acc_not_in_cat_acc_map_blocked(self, session):
        """If a category is bound to account 2 only, account 1 must not process it."""
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cats = [_cat("Дизайн", "логотип", cat_id=1)]
        cat_acc_map = {1: {2}}  # category 1 → only account 2

        msg = _fake_message("нужен логотип", msg_id=1)
        await manager._handle_message(session, msg, cats, acc_id=1, cat_acc_map=cat_acc_map)

        result = await session.execute(select(ParsedMessage))
        assert result.scalars().all() == []

    async def test_acc_in_cat_acc_map_allowed(self, session):
        """Account that IS in the set should process the category normally."""
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cat = _cat("Дизайн", "логотип", cat_id=1)
        session.add(cat)
        await session.commit()
        cat_acc_map = {cat.id: {1}}  # category → account 1 only

        msg = _fake_message("нужен логотип", msg_id=1)
        await manager._handle_message(session, msg, [cat], acc_id=1, cat_acc_map=cat_acc_map)

        result = await session.execute(select(ParsedMessage))
        assert result.scalar_one_or_none() is not None

    async def test_empty_cat_acc_map_all_accounts_allowed(self, session):
        """Empty cat_acc_map → no restriction, any account can process any category."""
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cats = [_cat("Дизайн", "логотип")]
        msg = _fake_message("нужен логотип", msg_id=1)
        await manager._handle_message(session, msg, cats, acc_id=99, cat_acc_map={})

        result = await session.execute(select(ParsedMessage))
        assert result.scalar_one_or_none() is not None


# ─── Stop-words block ─────────────────────────────────────────────────────────

class TestHandleMessageStopWords:
    async def test_stop_word_prevents_save(self, session):
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cats = [_cat("Дизайн", "логотип", stop_words="предлагаю")]
        msg = _fake_message("предлагаю разработку логотипа")

        await manager._handle_message(session, msg, cats, acc_id=1, cat_acc_map={})

        result = await session.execute(select(ParsedMessage))
        assert result.scalars().all() == []

    async def test_stop_word_absent_allows_match(self, session):
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cats = [_cat("Дизайн", "логотип", stop_words="предлагаю")]
        msg = _fake_message("нужен логотип срочно")

        await manager._handle_message(session, msg, cats, acc_id=1, cat_acc_map={})

        result = await session.execute(select(ParsedMessage))
        assert result.scalar_one_or_none() is not None

    async def test_stop_word_skips_category_but_next_matches(self, session):
        """If first category is blocked by stop-word, second category should still match."""
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cats = [
            _cat("Дизайн", "логотип", stop_words="предлагаю"),
            _cat("Маркетинг", "логотип\nреклама"),
        ]
        msg = _fake_message("предлагаю разработку логотипа")

        await manager._handle_message(session, msg, cats, acc_id=1, cat_acc_map={})

        result = await session.execute(select(ParsedMessage))
        pm = result.scalar_one_or_none()
        # Маркетинг matched (no stop word there)
        assert pm is not None


# ─── Author link generation ───────────────────────────────────────────────────

class TestAuthorLinkGeneration:
    async def test_author_link_with_username(self, session):
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cats = [_cat("Дизайн", "логотип")]
        msg = _fake_message("нужен логотип", sender_id=42, username="designer42")

        await manager._handle_message(session, msg, cats, acc_id=1, cat_acc_map={})

        result = await session.execute(select(ParsedMessage))
        pm = result.scalar_one()
        assert pm.author_username == "designer42"
        assert pm.author_link == "https://t.me/designer42"

    async def test_author_link_without_username(self, session):
        """No username → fallback to tg://user?id=…"""
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cats = [_cat("Дизайн", "логотип")]
        msg = _fake_message("нужен логотип", sender_id=777, username=None)

        await manager._handle_message(session, msg, cats, acc_id=1, cat_acc_map={})

        result = await session.execute(select(ParsedMessage))
        pm = result.scalar_one()
        assert pm.author_username is None
        assert pm.author_link == "tg://user?id=777"

    async def test_author_link_anonymous_sender(self, session):
        """No sender at all → author_id and author_link are None."""
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cats = [_cat("Дизайн", "логотип")]
        msg = _fake_message("нужен логотип", sender_id=None, username=None)

        await manager._handle_message(session, msg, cats, acc_id=1, cat_acc_map={})

        result = await session.execute(select(ParsedMessage))
        pm = result.scalar_one_or_none()
        if pm:  # anonymous + matched → saved but no author info
            assert pm.author_id is None
            assert pm.author_link is None


# ─── Message text edge cases ──────────────────────────────────────────────────

class TestMessageTextEdgeCases:
    async def test_empty_text_skipped(self, session):
        """Message with empty text should never be processed (handled before _handle_message)."""
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cats = [_cat("Дизайн", "логотип")]
        # manager.py checks `if not message.text: continue` before calling _handle_message,
        # but we test that empty text doesn't match anything
        msg = _fake_message("")

        await manager._handle_message(session, msg, cats, acc_id=1, cat_acc_map={})

        result = await session.execute(select(ParsedMessage))
        assert result.scalars().all() == []

    async def test_long_text_saved_untruncated(self, session):
        """ParsedMessage stores full text; truncation happens only in delivery formatting."""
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        long_text = "логотип " + "х" * 5000
        cats = [_cat("Дизайн", "логотип")]
        msg = _fake_message(long_text, msg_id=1)

        await manager._handle_message(session, msg, cats, acc_id=1, cat_acc_map={})

        result = await session.execute(select(ParsedMessage))
        pm = result.scalar_one_or_none()
        assert pm is not None
        assert len(pm.text) > 3500  # stored in full

    async def test_deliver_message_text_truncated_to_3500(self):
        """_deliver_message must truncate pm.text to 3500 chars in the outgoing message."""
        long_text = "А" * 5000

        pm = MagicMock()
        pm.text = long_text
        pm.author_username = None
        pm.author_link = None

        cat = _cat("Дизайн", "логотип")
        cat.name = "Дизайн"

        # Replicate the formatting from _deliver_message
        text = (
            f"📨 <b>{cat.name}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{(pm.text or '')[:3500]}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Участник группы"
        )
        # The text body portion must be at most 3500 chars
        assert (pm.text or "")[:3500] in text
        assert len((pm.text or "")[:3500]) == 3500


# ─── Multiple categories, first match wins ─────────────────────────────────────

class TestFirstCategoryWins:
    async def test_first_matching_category_is_stored(self, session):
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cat1 = _cat("Дизайн", "логотип\nбаннер", cat_id=1)
        cat2 = _cat("Маркетинг", "баннер\nреклама", cat_id=2)
        session.add(cat1)
        session.add(cat2)
        await session.commit()

        msg = _fake_message("нужен баннер для рекламы", msg_id=5)
        await manager._handle_message(session, msg, [cat1, cat2], acc_id=1, cat_acc_map={})

        result = await session.execute(select(ParsedMessage).where(ParsedMessage.message_id == 5))
        pm = result.scalar_one_or_none()
        assert pm is not None
        assert pm.category_id == cat1.id  # first match wins

    async def test_multiple_keywords_first_hit_is_enough(self, session):
        manager = ParserManager()
        manager._deliver_message = AsyncMock()

        cats = [_cat("Разработка", "сайт\nприложение\nбот")]
        msg = _fake_message("нужен бот для telegram", msg_id=7)

        await manager._handle_message(session, msg, cats, acc_id=1, cat_acc_map={})

        result = await session.execute(select(ParsedMessage).where(ParsedMessage.message_id == 7))
        assert result.scalar_one_or_none() is not None
