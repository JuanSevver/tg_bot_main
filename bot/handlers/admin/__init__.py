from aiogram import Router

from bot.filters import AdminFilter
from .dashboard import router as dashboard_router
from .users import router as users_router
from .broadcast import router as broadcast_router
from .groups import router as groups_router
from .accounts import router as accounts_router
from .proxies import router as proxies_router
from .categories import router as categories_router
from .inline import router as inline_router

admin_router = Router(name="admin")
admin_router.message.filter(AdminFilter())
admin_router.callback_query.filter(AdminFilter())


# Заглушка для noop-кнопок (например, «⚠️ Нет аккаунтов» в категориях).
# Без неё каждое нажатие летит в errors() как «query is too old».
from aiogram import F
from aiogram.types import CallbackQuery


@admin_router.callback_query(F.data == "noop")
async def _noop_handler(callback: CallbackQuery) -> None:
    await callback.answer()

admin_router.include_routers(
    dashboard_router,
    users_router,
    broadcast_router,
    groups_router,
    accounts_router,
    proxies_router,
    categories_router,
)

# Inline-router: дополнительная защита фильтром на уровне router (defence-in-depth).
# Проверка дублируется внутри handler — но если когда-нибудь там появится новый
# inline-handler без проверки, фильтр router-уровня его всё равно поймает.
inline_router.inline_query.filter(AdminFilter())
admin_router.include_router(inline_router)

__all__ = ["admin_router"]
