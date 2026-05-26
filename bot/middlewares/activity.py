from datetime import datetime
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery, InlineQuery
from sqlalchemy import update, select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User


class ActivityMiddleware(BaseMiddleware):
    """Обновляет users.last_active_at при любом событии от юзера.

    Регистрировать ОБЯЗАТЕЛЬНО на dp.message / dp.callback_query / dp.inline_query
    отдельно — не на dp.update. У объекта Update нет from_user, поэтому раньше
    middleware не делала ничего и поле росло только из-за onupdate=func.now()
    на побочных UPDATE'ах. Это ломало рассылку «по неактивным» и счётчики дашборда.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        result = await handler(event, data)

        session: AsyncSession | None = data.get("session")
        user = None
        if isinstance(event, (Message, CallbackQuery, InlineQuery)):
            user = event.from_user

        if session and user:
            exists = (
                await session.execute(sa_select(User.id).where(User.id == user.id))
            ).scalar_one_or_none()
            if exists:
                await session.execute(
                    update(User)
                    .where(User.id == user.id)
                    .values(last_active_at=datetime.utcnow())
                )
                await session.commit()

        return result
