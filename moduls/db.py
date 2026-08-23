"""Small async MySQL data-access layer used by the bot."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import aiomysql

import config

ALLOWED_SETTINGS_FIELDS = {"group_name"}


@dataclass(frozen=True)
class CollegeAccountLink:
    """Personal college account linked to one bot user."""

    login: str
    group_name: str


async def connect() -> aiomysql.Connection:
    return await aiomysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        db=config.DB_NAME,
        autocommit=False,
    )


async def ensure_profile_schema() -> None:
    """Create storage for personal college-account links when upgrading."""

    conn = await connect()
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS college_account_links (
                    user_id BIGINT NOT NULL,
                    login VARCHAR(256) NOT NULL,
                    group_name VARCHAR(64) NOT NULL,
                    linked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    COLLATE=utf8mb4_unicode_ci
                """,
                (),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()


async def get_college_account_link(user_id: int) -> CollegeAccountLink | None:
    """Return the user's verified college login without exposing its password."""

    conn = await connect()
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT login, group_name
                FROM college_account_links
                WHERE user_id=%s
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return CollegeAccountLink(login=str(row[0]), group_name=str(row[1]))
    finally:
        conn.close()


def _normalize_college_account_link(
    login: str,
    group_name: str,
) -> tuple[str, str]:
    normalized_login = login.strip()
    normalized_group = group_name.strip()
    if not normalized_login or not normalized_group:
        raise ValueError("Логин и группа ЛГТУ не могут быть пустыми")
    if len(normalized_login) > 256 or len(normalized_group) > 64:
        raise ValueError("Логин или группа ЛГТУ слишком длинные")
    return normalized_login, normalized_group


async def save_college_account_link(
    user_id: int,
    login: str,
    group_name: str,
) -> None:
    """Save or replace a verified personal college-account link."""

    normalized_login, normalized_group = _normalize_college_account_link(
        login,
        group_name,
    )

    conn = await connect()
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO college_account_links (user_id, login, group_name)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    login=VALUES(login),
                    group_name=VALUES(group_name)
                """,
                (user_id, normalized_login, normalized_group),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()


async def save_user_group_and_college_account_link(
    user_id: int,
    login: str,
    group_name: str,
) -> None:
    """Persist the selected group and verified personal link atomically."""

    normalized_login, normalized_group = _normalize_college_account_link(
        login,
        group_name,
    )
    conn = await connect()
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "UPDATE settings SET group_name=%s WHERE id=%s",
                (normalized_group, user_id),
            )
            await cursor.execute(
                """
                INSERT INTO college_account_links (user_id, login, group_name)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    login=VALUES(login),
                    group_name=VALUES(group_name)
                """,
                (user_id, normalized_login, normalized_group),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()


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

async def get_all_telegram_ids() -> list[int]:
    """Return every unique Telegram chat id registered in the bot."""

    conn = await connect()
    try:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT tg_id FROM users ORDER BY id")
            rows = await cursor.fetchall()
        return list(dict.fromkeys(int(row[0]) for row in rows))
    finally:
        conn.close()


async def get_telegram_ids_by_group(group_name: str) -> list[int]:
    """Return unique Telegram chat ids whose saved group matches group_name."""

    conn = await connect()
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT users.tg_id
                FROM users
                INNER JOIN settings ON settings.id = users.id
                WHERE settings.group_name = %s
                ORDER BY users.id
                """,
                (group_name,),
            )
            rows = await cursor.fetchall()
        return list(dict.fromkeys(int(row[0]) for row in rows))
    finally:
        conn.close()

async def delete_users_by_telegram_ids(telegram_ids: Iterable[int]) -> int:
    """Delete unreachable users and their settings, returning the user count."""

    unique_ids = tuple(dict.fromkeys(int(telegram_id) for telegram_id in telegram_ids))
    if not unique_ids:
        return 0

    placeholders = ", ".join(["%s"] * len(unique_ids))
    conn = await connect()
    try:
        async with conn.cursor() as cursor:
            # Production may use the legacy schema without ON DELETE CASCADE,
            # so related rows are removed explicitly before their users.
            await cursor.execute(
                f"""
                DELETE college_account_links
                FROM college_account_links
                INNER JOIN users ON users.id = college_account_links.user_id
                WHERE users.tg_id IN ({placeholders})
                """,
                unique_ids,
            )
            await cursor.execute(
                f"""
                DELETE settings
                FROM settings
                INNER JOIN users ON users.id = settings.id
                WHERE users.tg_id IN ({placeholders})
                """,
                unique_ids,
            )
            await cursor.execute(
                f"DELETE FROM users WHERE tg_id IN ({placeholders})",
                unique_ids,
            )
            deleted_users = int(cursor.rowcount)
        await conn.commit()
        return deleted_users
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()
