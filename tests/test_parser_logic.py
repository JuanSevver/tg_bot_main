"""Tests for parser keyword matching, stop-word filtering, link normalization, deduplication."""
from __future__ import annotations

import pytest

from database.models import Category, CategoryType
from parser.manager import _match_phrase, _has_stop_word, _extract_username


# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_cat(name: str, keywords: str, stop_words: str = "", cat_type=CategoryType.request) -> Category:
    """Create an in-memory Category with newline-separated keywords/stop_words."""
    cat = Category(name=name, type=cat_type, is_active=True)
    cat.keywords = keywords      # newline-separated phrases
    cat.stop_words = stop_words  # newline-separated words
    return cat


def first_matched(text: str, categories: list[Category]) -> Category | None:
    """Replicate the matching logic from ParserManager._handle_message."""
    text_lower = text.lower()
    for cat in categories:
        stop_words = cat.get_stop_words()
        if stop_words and _has_stop_word(stop_words, text_lower):
            continue
        for phrase in cat.get_keywords():
            if _match_phrase(phrase, text_lower):
                return cat
    return None


# ─── _extract_username ────────────────────────────────────────────────────────

class TestExtractUsername:
    def test_https_tme_url(self):
        assert _extract_username("https://t.me/mygroup") == "mygroup"

    def test_http_tme_url(self):
        assert _extract_username("http://t.me/mygroup") == "mygroup"

    def test_tme_without_scheme(self):
        assert _extract_username("t.me/mygroup") == "mygroup"

    def test_at_prefix(self):
        assert _extract_username("@mygroup") == "mygroup"

    def test_plain_username(self):
        assert _extract_username("mygroup") == "mygroup"

    def test_trailing_slash(self):
        assert _extract_username("https://t.me/mygroup/") == "mygroup"

    def test_query_params_stripped(self):
        assert _extract_username("https://t.me/mygroup?start=abc") == "mygroup"

    def test_uppercase_lowercased(self):
        assert _extract_username("https://t.me/MyGroup") == "mygroup"

    def test_whitespace_stripped(self):
        assert _extract_username("  @mygroup  ") == "mygroup"

    def test_joinchat_path_stripped(self):
        # Private group invite link — only the first path segment kept
        assert _extract_username("https://t.me/joinchat/abc123") == "joinchat"


# ─── _match_phrase ────────────────────────────────────────────────────────────

class TestMatchPhrase:
    # Точное вхождение
    def test_exact_word_match(self):
        assert _match_phrase("логотип", "нужен логотип для компании")

    def test_typo_no_match(self):
        # Строгий поиск — опечатки НЕ матчатся
        assert not _match_phrase("логотип", "нужен логатип срочно")

    def test_word_inside_longer_word(self):
        # "дизайн" найден внутри "дизайнера" как подстрока
        assert _match_phrase("дизайн", "ищу дизайнера")

    def test_no_match_unrelated_word(self):
        assert not _match_phrase("логотип", "ищу сантехника срочно")

    def test_case_insensitive(self):
        assert _match_phrase("логотип", "НУЖЕН ЛОГОТИП СРОЧНО")

    # Многословные фразы
    def test_exact_phrase_match(self):
        assert _match_phrase("ищу дизайнера", "срочно ищу дизайнера для проекта")

    def test_phrase_not_matched_partially(self):
        # "ищу дизайнера" не матчится, если только "ищу разработчика"
        assert not _match_phrase("ищу дизайнера", "ищу разработчика для проекта")

    def test_phrase_not_matched_with_word_between(self):
        # Строгий поиск: "нужен логотип" ≠ "нужен хороший логотип" (слово между)
        assert not _match_phrase("нужен логотип", "очень нужен хороший логотип")

    def test_empty_phrase_no_match(self):
        assert not _match_phrase("", "любой текст")

    def test_empty_text_no_match(self):
        assert not _match_phrase("логотип", "")


# ─── _has_stop_word ───────────────────────────────────────────────────────────

class TestHasStopWord:
    def test_exact_stop_word_found(self):
        assert _has_stop_word(["предлагаю"], "предлагаю услуги дизайна")

    def test_stop_word_as_substring(self):
        assert _has_stop_word(["продам"], "срочно продам аккаунт")

    def test_no_stop_word_in_text(self):
        assert not _has_stop_word(["предлагаю", "продам"], "ищу логотип срочно")

    def test_stop_word_typo_no_match(self):
        # Строгий поиск — опечатка в минус-слове НЕ блокирует
        assert not _has_stop_word(["предлагаю"], "предлогаю услуги")

    def test_empty_stop_words_never_blocks(self):
        assert not _has_stop_word([], "любой текст с чем угодно")

    def test_multiple_stop_words_one_matches(self):
        assert _has_stop_word(["продам", "отдам", "предлагаю"], "ищу дизайнера, предлагаю обмен")


# ─── Full matching pipeline ───────────────────────────────────────────────────

class TestFullMatchPipeline:
    def test_matches_correct_category(self):
        cats = [
            make_cat("Дизайн", "логотип\nбаннер"),
            make_cat("Разработка", "сайт\nприложение"),
        ]
        result = first_matched("нужен логотип для стартапа", cats)
        assert result is not None
        assert result.name == "Дизайн"

    def test_stop_word_blocks_match(self):
        cats = [make_cat("Дизайн", "логотип", stop_words="предлагаю")]
        # "предлагаю" in text → blocked
        result = first_matched("предлагаю разработку логотипа", cats)
        assert result is None

    def test_stop_word_absent_allows_match(self):
        cats = [make_cat("Дизайн", "логотип", stop_words="предлагаю")]
        result = first_matched("нужен логотип срочно", cats)
        assert result is not None

    def test_no_keywords_no_match(self):
        cats = [make_cat("Пустая", "")]
        assert first_matched("любой текст", cats) is None

    def test_inactive_category_not_filtered_here(self):
        """ParserManager filters is_active before calling; matching itself is agnostic."""
        cat = make_cat("Дизайн", "логотип")
        cat.is_active = False
        assert first_matched("нужен логотип", [cat]) is not None

    def test_first_matching_category_wins(self):
        cats = [
            make_cat("Дизайн", "логотип\nбаннер"),
            make_cat("Маркетинг", "баннер\nреклама"),
        ]
        # "баннер" matches both — first category wins
        result = first_matched("нужен баннер для рекламы", cats)
        assert result.name == "Дизайн"

    def test_offer_type_category_matched(self):
        cats = [make_cat("Предложение", "предлагаю", cat_type=CategoryType.offer)]
        result = first_matched("предлагаю услуги дизайнера", cats)
        assert result is not None
        assert result.type == CategoryType.offer

    def test_no_match_for_unrelated_text(self):
        cats = [
            make_cat("Дизайн", "логотип\nбаннер"),
            make_cat("Разработка", "сайт\nкод"),
        ]
        assert first_matched("продаю холодильник б/у", cats) is None


# ─── Deduplication ────────────────────────────────────────────────────────────

class TestDeduplicationLogic:
    async def test_new_message_not_in_db(self, session):
        from sqlalchemy import select
        from database.models import ParsedMessage

        result = await session.execute(
            select(ParsedMessage).where(
                ParsedMessage.group_id == 999,
                ParsedMessage.message_id == 1,
            )
        )
        assert result.scalar_one_or_none() is None

    async def test_inserted_message_found(self, session):
        from sqlalchemy import select
        from database.models import ParsedMessage

        session.add(ParsedMessage(group_id=999, message_id=1, text="hello"))
        await session.commit()

        result = await session.execute(
            select(ParsedMessage).where(
                ParsedMessage.group_id == 999,
                ParsedMessage.message_id == 1,
            )
        )
        assert result.scalar_one_or_none() is not None

    async def test_duplicate_message_raises_integrity_error(self, session):
        from sqlalchemy.exc import IntegrityError
        from database.models import ParsedMessage

        session.add(ParsedMessage(group_id=999, message_id=2, text="orig"))
        await session.commit()

        session.add(ParsedMessage(group_id=999, message_id=2, text="dup"))
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_same_message_id_different_group_allowed(self, session):
        from sqlalchemy import select
        from database.models import ParsedMessage

        session.add(ParsedMessage(group_id=111, message_id=1, text="a"))
        session.add(ParsedMessage(group_id=222, message_id=1, text="b"))
        await session.commit()

        result = await session.execute(select(ParsedMessage))
        assert len(result.scalars().all()) == 2

    async def test_author_dedup_same_author_same_text(self, session):
        """Same author + same text = duplicate — should be detected."""
        from sqlalchemy import select
        from database.models import ParsedMessage

        session.add(ParsedMessage(group_id=111, message_id=10, author_id=42, text="ищу логотип"))
        await session.commit()

        # Simulate author-dedup check
        result = await session.execute(
            select(ParsedMessage).where(
                ParsedMessage.author_id == 42,
                ParsedMessage.text == "ищу логотип",
            ).limit(1)
        )
        assert result.scalar_one_or_none() is not None  # duplicate detected

    async def test_author_dedup_different_text_allowed(self, session):
        from sqlalchemy import select
        from database.models import ParsedMessage

        session.add(ParsedMessage(group_id=111, message_id=10, author_id=42, text="ищу логотип"))
        await session.commit()

        result = await session.execute(
            select(ParsedMessage).where(
                ParsedMessage.author_id == 42,
                ParsedMessage.text == "нужен баннер",
            ).limit(1)
        )
        assert result.scalar_one_or_none() is None  # different text — not a dup
