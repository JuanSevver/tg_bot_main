from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery

from config import load_config

_config = load_config()


class AdminFilter(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user_id = event.from_user.id if event.from_user else 0
        return user_id in _config.admin_ids
