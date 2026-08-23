import asyncio

import config
import main


def test_initial_schedule_and_addresses_load() -> None:
    main.reload_runtime_data()
    assert "Т9-ИП-24-1" in main.schedule
    assert main.reference["color"] in {"Белая неделя", "Зелёная неделя"}
    assert main._format_room("353", "лабораторная") == ("2-й корпус 353 лабораторная")
    assert main._format_room("9-208", "лекция") == "9-й корпус 208 лекция"


def test_local_bot_api_url(monkeypatch) -> None:
    monkeypatch.setattr(
        config,
        "TOKEN",
        "123456:abcdefghijklmnopqrstuvwxyzABCDE1234567890",
    )
    monkeypatch.setattr(config, "BOT_API_BASE_URL", "http://localhost:8081")
    monkeypatch.setattr(config, "BOT_API_IS_LOCAL", True)

    bot = main.build_bot()
    assert bot.session.api.api_url(bot.token, "getMe").startswith(
        "http://localhost:8081/bot"
    )
    asyncio.run(bot.session.close())

def test_scheduled_refresh_skips_when_auto_update_is_disabled(
    monkeypatch,
) -> None:
    called = False

    async def fake_refresh_schedule():
        nonlocal called
        called = True

    monkeypatch.setattr(
        main.runtime_settings,
        "is_auto_update_enabled",
        lambda: False,
    )
    monkeypatch.setattr(main, "refresh_schedule", fake_refresh_schedule)

    asyncio.run(main.scheduled_refresh())

    assert called is False

