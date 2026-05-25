from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import load_config
from .models import Base

_config = load_config()
engine = create_async_engine(_config.database_url, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@event.listens_for(engine.sync_engine, "connect")
def _enable_sqlite_fk(dbapi_conn, _):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate(conn)


async def _migrate(conn) -> None:
    """Лёгкие in-place миграции для уже существующих БД (без Alembic).

    Добавляем недостающие колонки, которые появились после первого деплоя.
    SQLite поддерживает только ADD COLUMN, остальное no-op.
    """
    from sqlalchemy import text

    res = await conn.exec_driver_sql("PRAGMA table_info(telegram_groups)")
    cols = {row[1] for row in res.fetchall()}
    if "chat_id" not in cols:
        await conn.exec_driver_sql(
            "ALTER TABLE telegram_groups ADD COLUMN chat_id BIGINT"
        )
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_telegram_groups_chat_id "
            "ON telegram_groups(chat_id)"
        )
