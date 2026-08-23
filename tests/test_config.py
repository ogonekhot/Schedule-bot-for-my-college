import json

import pytest

import config


def test_schedule_accounts_merge_all_sources(monkeypatch, tmp_path) -> None:
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(
        json.dumps(
            {
                "Т9-ИП-24-3": {
                    "login": "runtime-login",
                    "password": "runtime-password",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SCHEDULE_ACCOUNTS_FILE", accounts_file)
    monkeypatch.setenv(
        "SCHEDULE_ACCOUNTS_JSON",
        json.dumps(
            {
                "Т9-ИП-24-2": {
                    "login": "env-login",
                    "password": "env-password",
                }
            }
        ),
    )

    accounts = config.load_schedule_accounts(
        {
            "accounts": {
                "Т9-ИП-24-1": {
                    "login": "settings-login",
                    "password": "settings-password",
                }
            }
        }
    )

    assert list(accounts) == [
        "Т9-ИП-24-1",
        "Т9-ИП-24-2",
        "Т9-ИП-24-3",
    ]
    assert accounts["Т9-ИП-24-3"]["login"] == "runtime-login"


def test_runtime_accounts_override_same_group(monkeypatch, tmp_path) -> None:
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(
        json.dumps(
            {
                "Т9-ИП-24-1": {
                    "login": "runtime-login",
                    "password": "runtime-password",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SCHEDULE_ACCOUNTS_FILE", accounts_file)
    monkeypatch.setenv(
        "SCHEDULE_ACCOUNTS_JSON",
        json.dumps(
            {
                "Т9-ИП-24-1": {
                    "login": "env-login",
                    "password": "env-password",
                }
            }
        ),
    )

    accounts = config.load_schedule_accounts({"accounts": {}})

    assert accounts["Т9-ИП-24-1"] == {
        "login": "runtime-login",
        "password": "runtime-password",
    }


def test_recovery_requires_target_group(monkeypatch) -> None:
    monkeypatch.setattr(config, "TOKEN", "test-token")
    monkeypatch.setattr(config, "SCHEDULE_RECOVERY_ENABLED", True)
    monkeypatch.setattr(config, "SCHEDULE_RECOVERY_GROUP", "")

    with pytest.raises(RuntimeError, match="SCHEDULE_RECOVERY_GROUP"):
        config.validate_runtime_config()
