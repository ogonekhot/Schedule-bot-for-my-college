import asyncio

import pytest

import moduls.db as dbm


class FakeCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.query = ""
        self.params: tuple[object, ...] | None = None
        self.executions: list[tuple[str, tuple[object, ...]]] = []
        self.rowcount = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def execute(self, query: str, params: tuple[object, ...]) -> None:
        self.query = " ".join(query.split())
        self.params = params
        self.executions.append((self.query, params))
        if self.query.startswith("DELETE FROM users"):
            self.rowcount = len(set(params))

    async def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None

    async def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self.fake_cursor = cursor
        self.closed = False
        self.committed = False
        self.rolled_back = False

    def cursor(self) -> FakeCursor:
        return self.fake_cursor

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def test_group_recipients_are_filtered_and_unique(monkeypatch) -> None:
    cursor = FakeCursor([(101,), (202,), (101,)])
    connection = FakeConnection(cursor)

    async def fake_connect() -> FakeConnection:
        return connection

    monkeypatch.setattr(dbm, "connect", fake_connect)

    recipients = asyncio.run(dbm.get_telegram_ids_by_group("Т9-ИП-24-1"))

    assert recipients == [101, 202]
    assert "INNER JOIN settings ON settings.id = users.id" in cursor.query
    assert "WHERE settings.group_name = %s" in cursor.query
    assert cursor.params == ("Т9-ИП-24-1",)
    assert connection.closed is True

def test_blocked_users_and_settings_are_deleted(monkeypatch) -> None:
    cursor = FakeCursor([])
    connection = FakeConnection(cursor)

    async def fake_connect() -> FakeConnection:
        return connection

    monkeypatch.setattr(dbm, "connect", fake_connect)

    deleted = asyncio.run(dbm.delete_users_by_telegram_ids([202, 303, 202]))

    assert deleted == 2
    assert len(cursor.executions) == 3
    assert cursor.executions[0][0].startswith(
        "DELETE college_account_links FROM college_account_links INNER JOIN users"
    )
    assert cursor.executions[1][0].startswith(
        "DELETE settings FROM settings INNER JOIN users"
    )
    assert cursor.executions[2][0].startswith("DELETE FROM users")
    assert cursor.executions[0][1] == (202, 303)
    assert cursor.executions[1][1] == (202, 303)
    assert cursor.executions[2][1] == (202, 303)
    assert connection.committed is True
    assert connection.rolled_back is False
    assert connection.closed is True


def test_delete_blocked_users_skips_empty_input(monkeypatch) -> None:
    async def fail_connect():
        raise AssertionError("Database connection must not be opened")

    monkeypatch.setattr(dbm, "connect", fail_connect)

    deleted = asyncio.run(dbm.delete_users_by_telegram_ids([]))

    assert deleted == 0



def test_profile_schema_is_created(monkeypatch) -> None:
    cursor = FakeCursor([])
    connection = FakeConnection(cursor)

    async def fake_connect() -> FakeConnection:
        return connection

    monkeypatch.setattr(dbm, "connect", fake_connect)

    asyncio.run(dbm.ensure_profile_schema())

    assert "CREATE TABLE IF NOT EXISTS college_account_links" in cursor.query
    assert connection.committed is True
    assert connection.closed is True


def test_college_account_link_is_loaded(monkeypatch) -> None:
    cursor = FakeCursor([("s1241000754", "Т9-ИП-24-1")])
    connection = FakeConnection(cursor)

    async def fake_connect() -> FakeConnection:
        return connection

    monkeypatch.setattr(dbm, "connect", fake_connect)

    link = asyncio.run(dbm.get_college_account_link(42))

    assert link == dbm.CollegeAccountLink(
        login="s1241000754",
        group_name="Т9-ИП-24-1",
    )
    assert cursor.params == (42,)
    assert connection.closed is True


def test_college_account_link_is_saved_without_password(monkeypatch) -> None:
    cursor = FakeCursor([])
    connection = FakeConnection(cursor)

    async def fake_connect() -> FakeConnection:
        return connection

    monkeypatch.setattr(dbm, "connect", fake_connect)

    asyncio.run(
        dbm.save_college_account_link(42, " s1241000754 ", " Т9-ИП-24-1 ")
    )

    assert "INSERT INTO college_account_links" in cursor.query
    assert cursor.params == (42, "s1241000754", "Т9-ИП-24-1")
    assert "password" not in cursor.query.casefold()
    assert connection.committed is True
    assert connection.closed is True



def test_group_and_college_link_are_committed_together(monkeypatch) -> None:
    cursor = FakeCursor([])
    connection = FakeConnection(cursor)

    async def fake_connect() -> FakeConnection:
        return connection

    monkeypatch.setattr(dbm, "connect", fake_connect)

    asyncio.run(
        dbm.save_user_group_and_college_account_link(
            42,
            " s1241000754 ",
            " Т9-ИП-24-1 ",
        )
    )

    assert cursor.executions[0] == (
        "UPDATE settings SET group_name=%s WHERE id=%s",
        ("Т9-ИП-24-1", 42),
    )
    assert cursor.executions[1][0].startswith(
        "INSERT INTO college_account_links"
    )
    assert cursor.executions[1][1] == (42, "s1241000754", "Т9-ИП-24-1")
    assert connection.committed is True
    assert connection.rolled_back is False
    assert connection.closed is True


def test_group_and_college_link_roll_back_together(monkeypatch) -> None:
    class FailingCursor(FakeCursor):
        async def execute(
            self,
            query: str,
            params: tuple[object, ...],
        ) -> None:
            await super().execute(query, params)
            if self.query.startswith("INSERT INTO college_account_links"):
                raise RuntimeError("database write failed")

    cursor = FailingCursor([])
    connection = FakeConnection(cursor)

    async def fake_connect() -> FakeConnection:
        return connection

    monkeypatch.setattr(dbm, "connect", fake_connect)

    with pytest.raises(RuntimeError, match="database write failed"):
        asyncio.run(
            dbm.save_user_group_and_college_account_link(
                42,
                "s1241000754",
                "Т9-ИП-24-1",
            )
        )

    assert connection.committed is False
    assert connection.rolled_back is True
    assert connection.closed is True
