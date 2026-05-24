"""
Tests for ParserManager realtime event handler machinery.

Covers:
- _register_realtime_handlers: handler registered on each client
- _remove_rt_handlers: cleans up _rt_handlers dict and calls client.remove_event_handler
- Double-register (reload_clients) doesn't stack duplicate handlers
- Handler filters: ignores private chats, ignores messages without text
- Handler respects parse_joined_groups flag
  (non-joined account only processes explicit groups)
- Handler applies group_cat_map (per-group category filtering)
- Handler exception is swallowed — manager keeps running
- _collect_messages skips work when no clients are loaded
- _polling_loop delay is >= 300 seconds (not the old 30 s)
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import (
    Category, CategoryType, CategoryAccount,
    GroupCategory, ParsedMessage, TelegramGroup,
)
from parser.manager import ParserManager, _extract_username


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def session_factory(engine):
    """Async session factory backed by the test in-memory SQLite engine."""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
def patch_async_session(session_factory):
    """Patch parser.manager.async_session with the test session factory."""
    with patch("parser.manager.async_session", session_factory):
        yield session_factory


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _mock_client() -> MagicMock:
    """Minimal Telethon client stub with handler tracking."""
    client = MagicMock()
    client._handlers: list = []

    def _add(fn, event=None):
        client._handlers.append(fn)

    def _remove(fn):
        client._handlers = [h for h in client._handlers if h is not fn]

    client.add_event_handler.side_effect = _add
    client.remove_event_handler.side_effect = _remove
    return client


def _fake_event(
    is_group: bool = True,
    is_channel: bool = False,
    text: str = "нужен логотип",
    chat_username: str | None = "testgroup",
    chat_id: int = 100,
    msg_id: int = 1,
    sender_id: int = 42,
) -> MagicMock:
    """Minimal Telethon event stub."""
    event = MagicMock()
    event.is_group = is_group
    event.is_channel = is_channel
    event.chat_id = chat_id

    chat = MagicMock()
    chat.username = chat_username
    event.chat = chat

    msg = MagicMock()
    msg.text = text
    msg.chat_id = chat_id
    msg.id = msg_id
    sender = MagicMock()
    sender.id = sender_id
    sender.username = "user42"
    msg.sender = sender
    msg.get_sender = AsyncMock(return_value=sender)
    event.message = msg

    return event


def _manager_with_clients(
    client_pairs: list[tuple[MagicMock, int]],
    joined_pairs: list[tuple[MagicMock, int]] | None = None,
) -> ParserManager:
    """Create a ParserManager with pre-loaded mock clients."""
    manager = ParserManager()
    manager._client_pairs = list(client_pairs)
    manager._joined_pairs = list(joined_pairs or [])
    return manager


# ─── Handler registration ─────────────────────────────────────────────────────

class TestHandlerRegistration:
    async def test_one_handler_registered_per_client(self, patch_async_session):
        client = _mock_client()
        manager = _manager_with_clients([(client, 1)])

        await manager._register_realtime_handlers()

        assert client.add_event_handler.call_count == 1
        assert len(client._handlers) == 1

    async def test_two_clients_two_handlers(self, patch_async_session):
        c1, c2 = _mock_client(), _mock_client()
        manager = _manager_with_clients([(c1, 1), (c2, 2)])

        await manager._register_realtime_handlers()

        assert len(c1._handlers) == 1
        assert len(c2._handlers) == 1

    async def test_handler_fn_stored_in_rt_handlers(self, patch_async_session):
        client = _mock_client()
        manager = _manager_with_clients([(client, 1)])

        await manager._register_realtime_handlers()

        assert id(client) in manager._rt_handlers
        assert len(manager._rt_handlers[id(client)]) == 1

    async def test_double_register_does_not_stack(self, patch_async_session):
        """Calling _register_realtime_handlers twice must not add duplicate handlers."""
        client = _mock_client()
        manager = _manager_with_clients([(client, 1)])

        await manager._register_realtime_handlers()
        await manager._register_realtime_handlers()

        # Each call removes old handler first, then registers fresh one
        assert len(client._handlers) == 1
        assert len(manager._rt_handlers[id(client)]) == 1

    async def test_no_clients_no_registration(self, patch_async_session):
        manager = ParserManager()  # empty _client_pairs

        await manager._register_realtime_handlers()  # must not raise

        assert manager._rt_handlers == {}


# ─── _remove_rt_handlers ──────────────────────────────────────────────────────

class TestRemoveRtHandlers:
    async def test_removes_handler_from_client(self, patch_async_session):
        client = _mock_client()
        manager = _manager_with_clients([(client, 1)])
        await manager._register_realtime_handlers()

        assert len(client._handlers) == 1
        manager._remove_rt_handlers(client)
        assert len(client._handlers) == 0

    async def test_clears_rt_handlers_dict_entry(self, patch_async_session):
        client = _mock_client()
        manager = _manager_with_clients([(client, 1)])
        await manager._register_realtime_handlers()

        manager._remove_rt_handlers(client)

        assert id(client) not in manager._rt_handlers

    def test_remove_unknown_client_is_noop(self):
        manager = ParserManager()
        unknown_client = _mock_client()
        manager._remove_rt_handlers(unknown_client)  # must not raise


# ─── Handler filtering logic ──────────────────────────────────────────────────

class TestHandlerFiltering:
    async def _get_handler(self, manager: ParserManager) -> object:
        return manager._client_pairs[0][0]._handlers[0]

    async def test_private_message_ignored(self, patch_async_session):
        client = _mock_client()
        manager = _manager_with_clients([(client, 1)])
        manager._handle_message = AsyncMock()

        await manager._register_realtime_handlers()
        handler = await self._get_handler(manager)

        event = _fake_event(is_group=False, is_channel=False)
        await handler(event)

        manager._handle_message.assert_not_called()

    async def test_message_without_text_ignored(self, patch_async_session):
        client = _mock_client()
        manager = _manager_with_clients([(client, 1)])
        manager._handle_message = AsyncMock()

        await manager._register_realtime_handlers()
        handler = await self._get_handler(manager)

        event = _fake_event(text="нужен логотип")
        event.message.text = ""
        await handler(event)

        manager._handle_message.assert_not_called()

    async def test_none_message_ignored(self, patch_async_session):
        client = _mock_client()
        manager = _manager_with_clients([(client, 1)])
        manager._handle_message = AsyncMock()

        await manager._register_realtime_handlers()
        handler = await self._get_handler(manager)

        event = _fake_event()
        event.message = None
        await handler(event)

        manager._handle_message.assert_not_called()

    async def test_group_message_with_text_processed(self, session, patch_async_session):
        """Valid group message from an explicit group reaches _handle_message."""
        grp = TelegramGroup(link="https://t.me/testgroup", is_active=True)
        session.add(grp)
        await session.commit()

        client = _mock_client()
        manager = _manager_with_clients([(client, 1)])
        manager._handle_message = AsyncMock()

        await manager._register_realtime_handlers()
        handler = await self._get_handler(manager)

        event = _fake_event(is_group=True, text="нужен логотип", chat_username="testgroup")
        await handler(event)

        manager._handle_message.assert_called_once()

    async def test_channel_message_processed(self, session, patch_async_session):
        grp = TelegramGroup(link="https://t.me/newschan", is_active=True)
        session.add(grp)
        await session.commit()

        client = _mock_client()
        manager = _manager_with_clients([(client, 1)])
        manager._handle_message = AsyncMock()

        await manager._register_realtime_handlers()
        handler = await self._get_handler(manager)

        event = _fake_event(is_group=False, is_channel=True,
                            text="нужен баннер", chat_username="newschan")
        await handler(event)

        manager._handle_message.assert_called_once()


# ─── parse_joined_groups flag ─────────────────────────────────────────────────

class TestParseJoinedGroupsFlag:
    async def _get_handler(self, manager):
        return manager._client_pairs[0][0]._handlers[0]

    async def test_non_joined_account_skips_unknown_group(self, patch_async_session):
        """Account without parse_joined_groups skips groups not in TelegramGroup table."""
        client = _mock_client()
        manager = _manager_with_clients([(client, 1)], joined_pairs=[])
        manager._handle_message = AsyncMock()

        await manager._register_realtime_handlers()
        handler = await self._get_handler(manager)

        # "unknowngroup" is NOT in TelegramGroup table
        event = _fake_event(is_group=True, text="нужен логотип", chat_username="unknowngroup")
        await handler(event)

        manager._handle_message.assert_not_called()

    async def test_non_joined_account_processes_explicit_group(
        self, session, patch_async_session
    ):
        """Account without parse_joined_groups processes groups from TelegramGroup table."""
        grp = TelegramGroup(link="https://t.me/designchat", is_active=True)
        session.add(grp)
        await session.commit()

        client = _mock_client()
        manager = _manager_with_clients([(client, 1)], joined_pairs=[])
        manager._handle_message = AsyncMock()

        await manager._register_realtime_handlers()
        handler = await self._get_handler(manager)

        event = _fake_event(is_group=True, text="нужен логотип", chat_username="designchat")
        await handler(event)

        manager._handle_message.assert_called_once()

    async def test_joined_account_processes_any_group(self, patch_async_session):
        """Account with parse_joined_groups=True processes ANY group message."""
        client = _mock_client()
        manager = _manager_with_clients([(client, 1)], joined_pairs=[(client, 1)])
        manager._handle_message = AsyncMock()

        await manager._register_realtime_handlers()
        handler = await self._get_handler(manager)

        # "randomgroup" is NOT in TelegramGroup table
        event = _fake_event(is_group=True, text="нужен логотип", chat_username="randomgroup")
        await handler(event)

        manager._handle_message.assert_called_once()

    async def test_group_without_username_skipped_for_non_joined(self, patch_async_session):
        """Private group (no username) is skipped for accounts without parse_joined_groups."""
        client = _mock_client()
        manager = _manager_with_clients([(client, 1)], joined_pairs=[])
        manager._handle_message = AsyncMock()

        await manager._register_realtime_handlers()
        handler = await self._get_handler(manager)

        event = _fake_event(is_group=True, text="нужен логотип", chat_username=None)
        await handler(event)

        manager._handle_message.assert_not_called()

    async def test_group_without_username_processed_for_joined_account(
        self, patch_async_session
    ):
        """Private group (no username) is processed for parse_joined_groups accounts."""
        client = _mock_client()
        manager = _manager_with_clients([(client, 1)], joined_pairs=[(client, 1)])
        manager._handle_message = AsyncMock()

        await manager._register_realtime_handlers()
        handler = await self._get_handler(manager)

        event = _fake_event(is_group=True, text="нужен логотип", chat_username=None)
        await handler(event)

        manager._handle_message.assert_called_once()


# ─── group_cat_map in realtime handler ────────────────────────────────────────

class TestGroupCatMapInHandler:
    async def test_group_specific_categories_used(self, session, patch_async_session):
        """When a group has assigned categories, handler passes only those to _handle_message."""
        cat_design = Category(name="Дизайн", type=CategoryType.request, is_active=True)
        cat_design.keywords = "логотип"
        cat_design.stop_words = ""
        cat_dev = Category(name="Разработка", type=CategoryType.request, is_active=True)
        cat_dev.keywords = "сайт"
        cat_dev.stop_words = ""
        session.add(cat_design)
        session.add(cat_dev)
        await session.flush()

        grp = TelegramGroup(link="https://t.me/designonly", is_active=True)
        session.add(grp)
        await session.flush()

        # Group "designonly" → only "Дизайн" category
        gc = GroupCategory(group_id=grp.id, category_id=cat_design.id)
        session.add(gc)
        await session.commit()

        client = _mock_client()
        manager = _manager_with_clients([(client, 1)])
        captured: list = []

        async def _capture(sess, msg, cats, acc_id, cat_acc_map):
            captured.append(list(cats))

        manager._handle_message = _capture

        await manager._register_realtime_handlers()
        handler = client._handlers[0]

        event = _fake_event(is_group=True, text="нужен логотип", chat_username="designonly")
        await handler(event)

        assert len(captured) == 1
        cat_names = [c.name for c in captured[0]]
        assert "Дизайн" in cat_names
        assert "Разработка" not in cat_names

    async def test_no_group_assignment_uses_all_categories(self, session, patch_async_session):
        """When no GroupCategory rows exist, all active categories are passed."""
        cat1 = Category(name="Дизайн", type=CategoryType.request, is_active=True)
        cat1.keywords = "логотип"
        cat1.stop_words = ""
        cat2 = Category(name="Разработка", type=CategoryType.request, is_active=True)
        cat2.keywords = "сайт"
        cat2.stop_words = ""
        session.add(cat1)
        session.add(cat2)

        grp = TelegramGroup(link="https://t.me/allcats", is_active=True)
        session.add(grp)
        await session.commit()  # No GroupCategory rows

        client = _mock_client()
        manager = _manager_with_clients([(client, 1)])
        captured: list = []

        async def _capture(sess, msg, cats, acc_id, cat_acc_map):
            captured.append(list(cats))

        manager._handle_message = _capture

        await manager._register_realtime_handlers()
        handler = client._handlers[0]

        event = _fake_event(is_group=True, text="нужен логотип", chat_username="allcats")
        await handler(event)

        assert len(captured) == 1
        assert len(captured[0]) == 2  # both categories


# ─── Exception safety ─────────────────────────────────────────────────────────

class TestHandlerExceptionSafety:
    async def test_exception_in_handle_message_is_swallowed(
        self, session, patch_async_session
    ):
        """An exception inside the handler must not propagate — manager must survive."""
        grp = TelegramGroup(link="https://t.me/testgroup", is_active=True)
        session.add(grp)
        await session.commit()

        client = _mock_client()
        manager = _manager_with_clients([(client, 1)])
        manager._handle_message = AsyncMock(side_effect=RuntimeError("DB exploded"))

        await manager._register_realtime_handlers()
        handler = client._handlers[0]

        event = _fake_event(is_group=True, text="нужен логотип", chat_username="testgroup")
        await handler(event)  # must NOT raise

    async def test_second_message_processed_after_first_raises(
        self, session, patch_async_session
    ):
        """Handler stays functional after an exception on first message."""
        grp = TelegramGroup(link="https://t.me/testgroup", is_active=True)
        session.add(grp)
        await session.commit()

        client = _mock_client()
        manager = _manager_with_clients([(client, 1)])
        call_count = 0

        async def _flaky(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first call fails")

        manager._handle_message = _flaky

        await manager._register_realtime_handlers()
        handler = client._handlers[0]

        event1 = _fake_event(is_group=True, text="нужен логотип",
                             chat_username="testgroup", msg_id=1)
        await handler(event1)  # first: exception swallowed

        event2 = _fake_event(is_group=True, text="нужен логотип",
                             chat_username="testgroup", msg_id=2)
        await handler(event2)  # second: must be called normally

        assert call_count == 2


# ─── _collect_messages guard ──────────────────────────────────────────────────

class TestCollectMessagesGuard:
    async def test_collect_returns_early_when_no_clients(self, patch_async_session):
        """_collect_messages exits immediately when no parser clients are loaded."""
        manager = ParserManager()
        # Must not raise StopIteration from next(self._cycle)
        await manager._collect_messages()

    async def test_collect_returns_early_when_no_categories(self, patch_async_session):
        """_collect_messages skips processing when category table is empty."""
        client = _mock_client()
        manager = _manager_with_clients([(client, 1)])
        # No categories in DB → should return early without calling next(cycle)
        await manager._collect_messages()


# ─── Polling interval ─────────────────────────────────────────────────────────

class TestPollingInterval:
    def test_polling_sleep_is_300_seconds(self):
        """The catchup polling loop must sleep >= 300 s, not the old 30 s."""
        import parser.manager as mod

        src = inspect.getsource(mod.ParserManager._polling_loop)
        tree = ast.parse(textwrap.dedent(src))

        sleep_args = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "sleep"
                and node.args
            ):
                val = node.args[0]
                if isinstance(val, ast.Constant):
                    sleep_args.append(val.value)

        loop_sleeps = [s for s in sleep_args if s > 10]
        assert loop_sleeps, "No sleep > 10 s found in _polling_loop"
        assert all(s >= 300 for s in loop_sleeps), (
            f"Polling sleep must be >= 300 s, found: {loop_sleeps}"
        )
