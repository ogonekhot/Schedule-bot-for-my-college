import asyncio

import moduls.db as dbm


class FakeCursor:
    def __init__(self, rows: list[tuple[int]]) -> None:
        self.rows = rows
        self.query = ""
        self.params: tuple[str] | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def execute(self, query: str, params: tuple[str]) -> None:
        self.query = " ".join(query.split())
        self.params = params

    async def fetchall(self) -> list[tuple[int]]:
        return self.rows


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self.fake_cursor = cursor
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.fake_cursor

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
