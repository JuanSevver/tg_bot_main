from aiogram import Router

from .start import router as start_router
from .profile import router as profile_router
from .categories import router as categories_router
from .subscription import router as subscription_router

user_router = Router(name="user")
user_router.include_routers(start_router, profile_router, categories_router, subscription_router)

__all__ = ["user_router"]
