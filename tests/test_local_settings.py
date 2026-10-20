import json
import logging
import os
from pathlib import Path

import pytest

from xianyu_pipeline import local_settings
from xianyu_pipeline.web_ui import write_config


@pytest.fixture
def defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    return tmp_path / "localappdata" / "XianYuMaterialPipeline" / "telegram-settings.json"


def save_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def account(**updates) -> dict:
    return {
        "telegram_api_id": 12345,
        "telegram_api_hash": "a" * 32,
        "telegram_api_proxy_url": "socks5://127.0.0.1:7890",
        "telegram_api_dc2_server": "149.154.167.50:443",
        **updates,
    }


def template(**updates) -> dict:
    return {
        "telegram_api_id": 0,
        "telegram_api_hash": "",
        "telegram_api_proxy_url": "",
        "telegram_api_dc2_server": "",
        "inbox_dir": "sample_data/inbox",
        "output_dir": "sample_data/output",
        "source_channel_url": "https://t.me/example_channel",
        "upload_provider": "quark_cli",
        "quark_cli_script_path": "/home/user/quark-drive.cjs",
        **updates,
    }


def test_saved_settings_restore_in_clean_portable_without_copying_other_fields(tmp_path, defaults):
    old = save_json(tmp_path / "old" / "test.local.json", {**template(), **account()})
    local_settings.remember_telegram_settings(old, json.loads(old.read_text()))
    assert json.loads(defaults.read_text()) == account()
    original = template(inbox_dir="custom/downloads")
    new = save_json(tmp_path / "new" / "test.local.json", original)
    local_settings.restore_telegram_settings(new)
    assert json.loads(new.read_text()) == {**original, **account()}
    restored = new.read_bytes()
    local_settings.restore_telegram_settings(new)
    assert new.read_bytes() == restored


def test_save_keeps_secret_proxy_and_payload_when_updating_unrelated_settings(tmp_path, defaults):
    path = save_json(tmp_path / "test.local.json", {**template(), **account()})
    updates = {"recent_message_limit": 17, "telegram_api_hash": ""}
    write_config(path, updates)
    assert updates == {"recent_message_limit": 17, "telegram_api_hash": ""}
    saved = json.loads(path.read_text())
    assert {key: saved[key] for key in local_settings.TELEGRAM_SETTINGS} == account()
    assert saved["recent_message_limit"] == 17
    assert json.loads(defaults.read_text()) == account()
    write_config(path, {"telegram_api_hash": "b" * 32})
    assert json.loads(defaults.read_text()) == account(telegram_api_hash="b" * 32)
    assert not list(path.parent.glob("*.tmp"))
    assert not list(defaults.parent.glob("*.tmp"))


def test_legacy_migration_uses_newest_valid_config_and_preserves_sources(tmp_path, defaults):
    root = tmp_path / "project"
    source = save_json(root / "config" / "test.local.json", account())
    newer = save_json(root / "outputs" / "XianYuMaterialPipeline_v38_quark_cli_wsl" / "test.local.json",
                      account(telegram_api_proxy_url="http://127.0.0.1:8888"))
    invalid = save_json(root / "outputs" / "XianYuMaterialPipeline_v37_channel_posts" / "test.local.json",
                        account(telegram_api_hash="********"))
    unrelated = save_json(root / "other" / "test.local.json", account(telegram_api_id=999))
    for timestamp, path in enumerate((source, newer, invalid, unrelated), start=1):
        os.utime(path, (timestamp, timestamp))
    before = {path: path.read_bytes() for path in (source, newer, invalid, unrelated)}
    new = save_json(root / "outputs" / "XianYuMaterialPipeline_v39_saved_settings" / "test.local.json", template())
    local_settings.restore_telegram_settings(new)
    expected = account(telegram_api_proxy_url="http://127.0.0.1:8888")
    assert json.loads(defaults.read_text()) == expected
    assert json.loads(new.read_text()) == {**template(), **expected}
    assert {path: path.read_bytes() for path in before} == before


def test_shared_settings_take_precedence_over_legacy_versions(tmp_path, defaults):
    save_json(defaults, account(telegram_api_id=23456, telegram_api_hash="b" * 32))
    root = tmp_path / "project"
    save_json(root / "config" / "test.local.json", account())
    new = save_json(root / "outputs" / "XianYuMaterialPipeline_v39_saved_settings" / "test.local.json", template())
    local_settings.restore_telegram_settings(new)
    assert json.loads(new.read_text())["telegram_api_id"] == 23456


@pytest.mark.parametrize("existing", [
    account(telegram_api_id=23456, telegram_api_hash="b" * 32, telegram_api_proxy_url=""),
    {"telegram_api_id": 23456, "telegram_api_hash": ""},
    {"telegram_api_id": 0, "telegram_api_hash": "b" * 32},
])
def test_existing_or_partial_account_is_never_overwritten(tmp_path, defaults, existing):
    save_json(defaults, account())
    path = save_json(tmp_path / "test.local.json", {**template(), **existing})
    before = path.read_bytes()
    local_settings.restore_telegram_settings(path)
    assert path.read_bytes() == before
    assert json.loads(defaults.read_text()) == account()


def test_existing_account_seeds_missing_shared_settings(tmp_path, defaults):
    path = save_json(tmp_path / "test.local.json", {**template(), **account()})
    before = path.read_bytes()
    local_settings.restore_telegram_settings(path)
    assert path.read_bytes() == before
    assert json.loads(defaults.read_text()) == account()


def test_clearing_proxy_is_remembered_and_restored_as_direct_mode(tmp_path, defaults):
    path = save_json(tmp_path / "old" / "test.local.json", {**template(), **account()})
    write_config(path, {"telegram_api_proxy_url": ""})
    assert json.loads(defaults.read_text()) == account(telegram_api_proxy_url="")
    new = save_json(tmp_path / "new" / "test.local.json", template())
    local_settings.restore_telegram_settings(new)
    assert json.loads(new.read_text())["telegram_api_proxy_url"] == ""


@pytest.mark.parametrize("filename", ["config.json", "offline.json", "pipeline.local.json"])
def test_custom_configs_do_not_read_or_write_shared_settings(tmp_path, defaults, filename):
    save_json(defaults, account())
    path = save_json(tmp_path / filename, template())
    before = path.read_bytes()
    local_settings.restore_telegram_settings(path)
    assert path.read_bytes() == before
    local_settings.remember_telegram_settings(path, account(telegram_api_id=23456))
    assert json.loads(defaults.read_text()) == account()


@pytest.mark.parametrize("invalid", [
    {"telegram_api_id": True},
    {"telegram_api_id": "12345"},
    {"telegram_api_hash": "********"},
    {"telegram_api_proxy_url": "socks5://user:pass@localhost:7890"},
    {"telegram_api_proxy_url": None},
    {"telegram_api_dc2_server": "149.154.167.40:443"},
])
def test_invalid_account_or_network_settings_are_not_remembered(tmp_path, defaults, invalid):
    local_settings.remember_telegram_settings(tmp_path / "test.local.json", account(**invalid))
    assert not defaults.exists()


@pytest.mark.parametrize("contents", ["not-json-private-secret", "[]", '{"telegram_api_id": 0}'])
def test_invalid_shared_settings_recover_from_known_legacy_without_logging_secrets(
    tmp_path, defaults, caplog, contents
):
    defaults.parent.mkdir(parents=True)
    defaults.write_text(contents, encoding="utf-8")
    root = tmp_path / "project"
    save_json(root / "config" / "test.local.json", account())
    path = save_json(root / "outputs" / "XianYuMaterialPipeline_v39_saved_settings" / "test.local.json", template())
    local_settings.restore_telegram_settings(path)
    assert json.loads(defaults.read_text()) == account()
    assert json.loads(path.read_text()) == {**template(), **account()}
    assert "private-secret" not in caplog.text and "a" * 32 not in caplog.text


def test_no_valid_saved_account_leaves_clean_template_unchanged(tmp_path, defaults):
    path = save_json(tmp_path / "test.local.json", template())
    before = path.read_bytes()
    local_settings.restore_telegram_settings(path)
    assert path.read_bytes() == before
    assert not defaults.exists()


def test_missing_localappdata_disables_shared_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    path = save_json(tmp_path / "test.local.json", template())
    before = path.read_bytes()
    local_settings.restore_telegram_settings(path)
    local_settings.remember_telegram_settings(path, account())
    assert path.read_bytes() == before
    assert not (tmp_path / "XianYuMaterialPipeline").exists()


def test_shared_write_failure_does_not_lose_successful_project_save(tmp_path, defaults, monkeypatch, caplog):
    path = save_json(tmp_path / "test.local.json", {**template(), **account()})

    def denied(*args):
        raise PermissionError("private-secret-must-not-be-logged")

    monkeypatch.setattr(local_settings, "_write_json", denied)
    with caplog.at_level(logging.WARNING):
        write_config(path, {"recent_message_limit": 17})
    saved = json.loads(path.read_text())
    assert saved["recent_message_limit"] == 17 and saved["telegram_api_hash"] == "a" * 32
    assert "本机 Telegram 升级配置未同步" in caplog.text
    assert "private-secret" not in caplog.text and "a" * 32 not in caplog.text
