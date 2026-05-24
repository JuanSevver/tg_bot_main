"""
AutoAnswerMiddleware — pre-answers CallbackQuery before the handler runs.

Protects against slow handlers (DB queries) hitting Telegram's 10-second
callback answer deadline. The spinner is removed immediately; the message
edit happens whenever the handler finishes.

Note: CallbackQuery is a frozen Pydantic model — we cannot patch event.answer.
Instead we call the real answer() here and let handlers call it again.
The second call will raise TelegramBadRequest("query ID is invalid") which
is caught by the global error handler registered in main.py.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery


class AutoAnswerMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        try:
            await event.answer()
        except Exception:
            pass  # Already answered or query expired — safe to ignore.
        return await handler(event, data)
