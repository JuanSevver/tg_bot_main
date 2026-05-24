from datetime import datetime
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User


class ActivityMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        result = await handler(event, data)

        session: AsyncSession | None = data.get("session")
        user = None
        if isinstance(event, (Message, CallbackQuery)):
            user = event.from_user

        if session and user:
            from sqlalchemy import select as sa_select
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
