from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery, InlineQuery

from config import load_config


class AdminFilter(BaseFilter):
    """Проверяет, что событие пришло от админа.

    load_config() вызывается КАЖДЫЙ раз — без кеша. ADMIN_IDS может меняться
    в .env во время работы, и кешировать на импорте — значит, что любая правка
    требует рестарта. Стоимость: один os.getenv + split, копейки.
    """

    async def __call__(self, event: Message | CallbackQuery | InlineQuery) -> bool:
        user_id = event.from_user.id if event.from_user else 0
        return user_id in load_config().admin_ids
