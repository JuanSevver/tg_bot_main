import asyncio
import logging
import os
import signal

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

from config import load_config
from database import init_db
from bot.middlewares import DatabaseMiddleware, ActivityMiddleware, AutoAnswerMiddleware
from bot.handlers.user import user_router
from bot.handlers.admin import admin_router
from parser.manager import parser_manager
from services.cryptobot_polling import cryptobot_poller
from services.scheduler import scheduler
from services.observability import start_http_server
from bot.commands import setup_commands

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    config = load_config()

    await init_db()
    logger.info("Database initialized.")

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Suppress "query is too old" errors — happens when bot restarts and
    # receives stale callback queries that Telegram queued while offline.
    # Also suppresses the duplicate answer() call from handlers after
    # AutoAnswerMiddleware already answered.
    @dp.errors()
    async def _suppress_stale_callback(event: ErrorEvent) -> bool:
        exc = event.exception
        if isinstance(exc, TelegramBadRequest):
            msg = str(exc).lower()
            if "query is too old" in msg or "query id is invalid" in msg:
                return True  # Suppress — don't log as error
        return False

    dp.update.middleware(DatabaseMiddleware())
    # ActivityMiddleware читает from_user; Update его не имеет, поэтому
    # подключаем на конкретные observer-ы, иначе условие не сработает никогда.
    activity = ActivityMiddleware()
    dp.message.middleware(activity)
    dp.callback_query.middleware(activity)
    dp.inline_query.middleware(activity)
    dp.callback_query.middleware(AutoAnswerMiddleware())

    dp.include_router(admin_router)
    dp.include_router(user_router)

    await setup_commands(bot)
    logger.info("Bot commands configured.")

    parser_manager.set_bot(bot)
    await parser_manager.start()
    logger.info("Parser manager started.")

    # CryptoBot polling (не требует домена и HTTPS)
    cryptobot_poller.set_bot(bot)
    await cryptobot_poller.start()

    # Retention + subscription-expiry notifications.
    scheduler.set_bot(bot)
    scheduler.start()

    # /healthz + /metrics на отдельном порту для Docker HEALTHCHECK.
    http_port = int(os.getenv("OBSERVABILITY_PORT", "8080"))
    http_runner = await start_http_server(port=http_port)

    # Graceful shutdown: на SIGTERM/SIGINT останавливаем polling вежливо,
    # чтобы дать parser_manager.stop() корректно cancel-нуть фоновые задачи.
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received.")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows / non-unix — fallback на стандартный KeyboardInterrupt.
            pass

    async def _polling_runner():
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=True,  # Skip stale updates accumulated while bot was offline
        )

    polling_task = asyncio.create_task(_polling_runner())
    try:
        logger.info("Bot started.")
        done, pending = await asyncio.wait(
            {polling_task, asyncio.create_task(stop_event.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        await dp.stop_polling()
        if not polling_task.done():
            polling_task.cancel()
            try:
                await polling_task
            except (asyncio.CancelledError, Exception):
                pass
        await parser_manager.stop()
        await cryptobot_poller.stop()
        scheduler.stop()
        try:
            await http_runner.cleanup()
        except Exception:
            pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
