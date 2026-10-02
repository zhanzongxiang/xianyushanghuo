from __future__ import annotations

import asyncio
import io
import json
import sys
import types
import zipfile
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from xianyu_pipeline.adapters import (TelethonTelegramAdapter, parse_telegram_api_proxy,
                                      parse_telegram_dc2_server)
from xianyu_pipeline.archive_validation import ArchiveValidationError
from xianyu_pipeline.config import PipelineConfig


def fake_client(monkeypatch, messages, authorized=True, payloads=None):
    payloads = payloads or {}
    instances = []

    class Client:
        def __init__(self, session, api_id, api_hash):
            self.session = session
            self.connected = False
            self.downloaded = []
            instances.append(self)

        def connect(self):
            self.connected = True

        def disconnect(self):
            self.connected = False

        def is_user_authorized(self):
            return authorized

        def iter_messages(self, target, limit):
            assert target == "free_3d_models_download" and limit == 200
            return iter(messages)

        def download_media(self, message, file, progress_callback=None):
            self.downloaded.append(message.id)
            Path(file).write_bytes(payloads[message.id])
            if progress_callback:
                progress_callback(len(payloads[message.id]), len(payloads[message.id]))
            return file

    telethon = types.ModuleType("telethon")
    sync = types.ModuleType("telethon.sync")
    sync.TelegramClient = Client
    monkeypatch.setitem(sys.modules, "telethon", telethon)
    monkeypatch.setitem(sys.modules, "telethon.sync", sync)
    return instances


def make_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("asset.txt", "safe fixture")
    return buffer.getvalue()


def test_api_connection_options_are_validated():
    assert parse_telegram_api_proxy("") is None
    assert parse_telegram_api_proxy("socks5://127.0.0.1:7890") == {
        "proxy_type": "socks5", "addr": "127.0.0.1", "port": 7890, "rdns": True}
    assert parse_telegram_api_proxy("http://localhost:8080")["proxy_type"] == "http"
    assert parse_telegram_dc2_server("") is None
    assert parse_telegram_dc2_server("149.154.167.50:443") == ("149.154.167.50", 443)
    for invalid in ("socks5://user:pass@localhost:7890", "socks5://localhost:0",
                    "socks5://localhost", "https://localhost:7890", "socks5://localhost:7890/path"):
        with pytest.raises(ValueError):
            parse_telegram_api_proxy(invalid)
    with pytest.raises(ValueError, match="不使用测试服务器"):
        parse_telegram_dc2_server("149.154.167.40:443")


def test_api_client_uses_proxy_and_only_changes_unauthed_dc2_session(tmp_path, monkeypatch):
    clients = []
    class Session:
        dc_id = 2
        auth_key = None
        server_address = "149.154.167.51"
        port = 443
        saves = 0
        def set_dc(self, dc_id, host, port):
            self.dc_id, self.server_address, self.port = dc_id, host, port
        def save(self):
            self.saves += 1
    class Client:
        def __init__(self, session_path, api_id, api_hash, **kwargs):
            self.options = kwargs
            self.session = Session()
            clients.append(self)
    sync = types.ModuleType("telethon.sync")
    sync.TelegramClient = Client
    monkeypatch.setitem(sys.modules, "telethon.sync", sync)
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session",
                                     tmp_path / "inbox", proxy_url="socks5://127.0.0.1:7890",
                                     dc2_server="149.154.167.50:443")
    first = adapter._client()
    assert first.options["proxy"]["port"] == 7890
    assert first.session.server_address == "149.154.167.50" and first.session.saves == 1
    class AuthorizedClient(Client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.session.auth_key = object()
    sync.TelegramClient = AuthorizedClient
    second = adapter._client()
    assert second.session.server_address == "149.154.167.51" and second.session.saves == 0


def test_real_telethon_client_accepts_proxy_and_dc2_endpoint_offline(tmp_path, monkeypatch):
    from telethon.sync import TelegramClient

    async def original_dc(self, dc_id, cdn=False):
        return SimpleNamespace(ip_address="149.154.167.41", port=443, id=dc_id, cdn=cdn)

    monkeypatch.setattr(TelegramClient, "_get_dc", original_dc)
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session",
                                     tmp_path / "inbox", proxy_url="socks5://127.0.0.1:7890",
                                     dc2_server="149.154.167.50:443")
    client = adapter._client()
    assert client.session.dc_id == 2
    assert client.session.server_address == "149.154.167.50"
    assert client.session.port == 443
    assert client._proxy["proxy_type"] == "socks5"
    selected = client.loop.run_until_complete(client._get_dc(2))
    assert selected.ip_address == "149.154.167.50"
    assert client.loop.run_until_complete(client._get_dc(2, cdn=True)).ip_address == "149.154.167.41"
    assert client.loop.run_until_complete(client._get_dc(5)).ip_address == "149.154.167.41"
    client.session.close()


def test_api_scans_archive_names_and_validates_before_publishing(tmp_path, monkeypatch):
    good = make_zip()
    items = [
        SimpleNamespace(id=4, file=SimpleNamespace(name="first.zip", size=len(good)), message="new"),
        SimpleNamespace(id=3, file=SimpleNamespace(name="photo.jpg", size=2), message="image"),
        SimpleNamespace(id=2, file=SimpleNamespace(name=None, size=2), message="caption.zip"),
        SimpleNamespace(id=1, file=SimpleNamespace(name="old.zip", size=len(good)), message="old"),
    ]
    clients = fake_client(monkeypatch, items, payloads={4: good, 1: good})
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session",
                                     tmp_path / "inbox")
    messages = adapter.fetch_messages("", [], "https://t.me/free_3d_models_download", "recent", 2)
    assert [item.message_id for item in messages] == [1, 4]
    assert [item.attachment_paths[0].name for item in messages] == ["old.zip", "first.zip"]
    assert clients[0].downloaded == [1, 4]
    assert not clients[0].connected
    for item in messages:
        with zipfile.ZipFile(item.attachment_paths[0]) as archive:
            assert archive.testzip() is None


@pytest.mark.parametrize("content,size", [(b"not a zip", 9), (b"partial", 100)])
def test_api_does_not_publish_invalid_download(tmp_path, monkeypatch, content, size):
    item = SimpleNamespace(id=8, file=SimpleNamespace(name="broken.zip", size=size), message="")
    fake_client(monkeypatch, [item], payloads={8: content})
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session",
                                     tmp_path / "inbox")
    with pytest.raises(RuntimeError, match="文件保留待检查"):
        adapter.fetch_messages("", [], "https://t.me/free_3d_models_download", "recent", 1)
    assert not (tmp_path / "inbox/broken.zip").exists()
    assert len(list((tmp_path / "inbox/.unverified").glob("*/broken.zip"))) == 1


def test_api_unauthorized_never_downloads(tmp_path, monkeypatch):
    clients = fake_client(monkeypatch, [], authorized=False)
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session",
                                     tmp_path / "inbox")
    with pytest.raises(RuntimeError, match="尚未登录"):
        adapter.fetch_messages("", [], "https://t.me/free_3d_models_download", "recent", 1)
    assert clients[0].downloaded == [] and not clients[0].connected


def test_api_rar_validation_failure_is_quarantined(tmp_path, monkeypatch):
    item = SimpleNamespace(id=9, file=SimpleNamespace(name="asset.rar", size=8), message="")
    fake_client(monkeypatch, [item], payloads={9: b"Rar!test"})
    def reject(*args):
        raise ArchiveValidationError("password_required", "需要密码")
    monkeypatch.setattr("xianyu_pipeline.adapters.validate_rar", reject)
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session",
                                     tmp_path / "inbox")
    with pytest.raises(RuntimeError, match="文件保留待检查"):
        adapter.fetch_messages("", [], "https://t.me/free_3d_models_download", "recent", 1)
    assert not (tmp_path / "inbox/asset.rar").exists()
    assert len(list((tmp_path / "inbox/.unverified").glob("*/asset.rar"))) == 1


@pytest.mark.parametrize(
    ("request_stop", "idle_limit", "total_limit", "expected_error"),
    [(False, 0.1, 100, "连续 0.1 秒无进展"),
     (True, 10, 100, "已请求停止"),
     (False, 10, 0.1, "超过 0.1 秒总时限")],
)
def test_api_download_watchdog_keeps_partial_and_exits(
        tmp_path, monkeypatch, request_stop, idle_limit, total_limit, expected_error):
    item = SimpleNamespace(id=11, file=SimpleNamespace(name="partial.rar", size=100), message="")
    stop = Event()

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            pass

        def disconnect(self):
            pass

        def is_user_authorized(self):
            return True

        def iter_messages(self, target, limit):
            return iter([item])

        async def download_media(self, message, file, progress_callback):
            Path(file).write_bytes(b"partial")
            progress_callback(7, 100)
            if request_stop:
                stop.set()
            await asyncio.sleep(60)

    sync = types.ModuleType("telethon.sync")
    sync.TelegramClient = Client
    monkeypatch.setitem(sys.modules, "telethon.sync", sync)
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session",
                                     tmp_path / "inbox", download_start_timeout_seconds=idle_limit,
                                     download_transfer_timeout_seconds=total_limit, stop_event=stop)
    with pytest.raises(RuntimeError, match=expected_error):
        adapter.fetch_messages("", [], "https://t.me/free_3d_models_download", "recent", 1)
    kept = list((tmp_path / "inbox/.unverified").glob("*/partial.rar"))
    assert len(kept) == 1 and kept[0].read_bytes() == b"partial"


def test_api_download_monitor_waits_for_first_chunk_without_crashing(tmp_path, monkeypatch):
    content = make_zip()
    item = SimpleNamespace(id=13, file=SimpleNamespace(name="delayed.zip", size=len(content)), message="")

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            pass

        def disconnect(self):
            pass

        def is_user_authorized(self):
            return True

        def iter_messages(self, target, limit):
            return iter([item])

        async def download_media(self, message, file, progress_callback):
            await asyncio.sleep(1.2)
            Path(file).write_bytes(content)
            progress_callback(len(content), len(content))
            return file

    sync = types.ModuleType("telethon.sync")
    sync.TelegramClient = Client
    monkeypatch.setitem(sys.modules, "telethon.sync", sync)
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session",
                                     tmp_path / "inbox", download_start_timeout_seconds=5,
                                     download_transfer_timeout_seconds=10)
    messages = adapter.fetch_messages("", [], "https://t.me/free_3d_models_download", "recent", 1)
    assert messages[0].attachment_paths[0].read_bytes() == content


def test_api_zero_byte_failure_does_not_leave_fake_rar(tmp_path, monkeypatch):
    item = SimpleNamespace(id=12, file=SimpleNamespace(name="empty.rar", size=100), message="")
    fake_client(monkeypatch, [item], payloads={12: b""})
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session",
                                     tmp_path / "inbox")
    with pytest.raises(RuntimeError, match="未收到文件字节"):
        adapter.fetch_messages("", [], "https://t.me/free_3d_models_download", "recent", 1)
    assert not list((tmp_path / "inbox").rglob("empty.rar"))


def test_api_session_survives_portable_upgrade(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "user"))
    paths = []
    for version in ("v27", "v28"):
        folder = tmp_path / version
        folder.mkdir()
        config = folder / "test.local.json"
        config.write_text(json.dumps({"telegram_session": "sample_data/telegram.session"}), encoding="utf-8")
        paths.append(PipelineConfig.from_json(config).telegram_session)
    assert paths[0] == paths[1] == str(tmp_path / "user/XianYuMaterialPipeline/telegram_api.session")
