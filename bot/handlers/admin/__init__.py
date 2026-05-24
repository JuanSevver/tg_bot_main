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

admin_router.include_routers(
    dashboard_router,
    users_router,
    broadcast_router,
    groups_router,
    accounts_router,
    proxies_router,
    categories_router,
)

# Inline router without AdminFilter (filter checked inside handler)
admin_router.include_router(inline_router)

__all__ = ["admin_router"]
