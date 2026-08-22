"""Small async MySQL data-access layer used by the bot."""

from __future__ import annotations

import aiomysql

import config

ALLOWED_SETTINGS_FIELDS = {"group_name"}


async def connect() -> aiomysql.Connection:
    return await aiomysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        db=config.DB_NAME,
        autocommit=False,
    )


async def check_tg_id(tg_id: int) -> list:
    conn = await connect()
    try:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT id FROM users WHERE tg_id=%s", (tg_id,))
            db_user_id = await cursor.fetchone()

            if db_user_id is None:
                await cursor.execute("INSERT INTO users (tg_id) VALUES (%s)", (tg_id,))
                await conn.commit()
                db_user_id = (cursor.lastrowid,)

            await cursor.execute("SELECT * FROM settings WHERE id=%s", (db_user_id[0],))
            user_settings = await cursor.fetchone()
            if user_settings is None:
                await cursor.execute(
                    "INSERT INTO settings (id) VALUES (%s)", (db_user_id[0],)
                )
                await conn.commit()
                await cursor.execute(
                    "SELECT * FROM settings WHERE id=%s", (db_user_id[0],)
                )
                user_settings = await cursor.fetchone()
        return list(user_settings)
    finally:
        conn.close()


async def update_user_settings(user_id: int, field: str, value: str) -> str:
    if field not in ALLOWED_SETTINGS_FIELDS:
        raise ValueError(f"Изменение поля {field!r} запрещено")

    conn = await connect()
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"UPDATE settings SET {field}=%s WHERE id=%s", (value, user_id)
            )
        await conn.commit()
        return "Настройки сохранены"
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()
