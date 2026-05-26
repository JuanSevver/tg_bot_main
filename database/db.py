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
    # telegram_groups.chat_id
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

    # parsed_messages.text_hash + индексы для дедупликации и retention
    res = await conn.exec_driver_sql("PRAGMA table_info(parsed_messages)")
    pm_cols = {row[1] for row in res.fetchall()}
    if "text_hash" not in pm_cols:
        await conn.exec_driver_sql(
            "ALTER TABLE parsed_messages ADD COLUMN text_hash VARCHAR(32)"
        )
        # Backfill для существующих записей: MD5(text) средствами Python — SQLite
        # без расширений не умеет md5(). Делаем батчем, чтобы не висеть на лочке.
        import hashlib
        rows = await conn.exec_driver_sql(
            "SELECT id, text FROM parsed_messages WHERE text_hash IS NULL AND text IS NOT NULL"
        )
        items = rows.fetchall()
        for row_id, txt in items:
            h = hashlib.md5((txt or "").encode("utf-8")).hexdigest()
            await conn.exec_driver_sql(
                "UPDATE parsed_messages SET text_hash = ? WHERE id = ?", (h, row_id)
            )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_parsed_author_hash_time "
        "ON parsed_messages(author_id, text_hash, parsed_at)"
    )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_parsed_parsed_at "
        "ON parsed_messages(parsed_at)"
    )

    # processed_invoices создаётся через create_all, но безопасно проверим
    await conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS processed_invoices ("
        " invoice_id BIGINT PRIMARY KEY,"
        " processed_at DATETIME"
        ")"
    )
