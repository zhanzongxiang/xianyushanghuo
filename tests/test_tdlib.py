from __future__ import annotations

import asyncio
import copy
import io
import json
import threading
import time
import zipfile
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from xianyu_pipeline import tdlib_client, tdlib_download
from xianyu_pipeline.adapters import TelethonTelegramAdapter, _TransferEvents
from xianyu_pipeline.browser_download import DownloadCancelled
from xianyu_pipeline.config import PipelineConfig


@pytest.fixture
def download(tmp_path, monkeypatch):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("model.stl", b"model fixture" * 3000)
    payload = stream.getvalue()
    events, calls = [], []
    cache = tmp_path / "tdlib-cache"
    cache.mkdir()
    stop = threading.Event()
    message = SimpleNamespace(id=77, peer_id=SimpleNamespace(channel_id=123),
                              file=SimpleNamespace(name="model.zip", size=len(payload)))

    class Service:
        def __init__(self):
            self.lock = threading.RLock()
            self.files_dir = cache
            self.active_files = set()
            self.file = {"@type": "file", "id": 91, "size": len(payload), "local": {
                "path": str(cache / "file.bin"), "downloaded_size": 0, "can_be_downloaded": True,
                "is_downloading_completed": False}}
            self.files = {91: self.file}
            self.authorized = True
            self.mode = "complete"
            self.user_id = 7
            self.filename = "model.zip"
            self.file_size = len(payload)

        def status(self):
            return {"authorized": self.authorized, "state": "ready" if self.authorized else "phone", "error": ""}

        async def call_async(self, query, timeout=30):
            calls.append(copy.deepcopy(query))
            kind = query["@type"]
            if kind == "getMe":
                return {"id": self.user_id}
            if kind == "searchPublicChat":
                return {"@type": "chat", "id": -100123}
            if kind == "getMessageLinkInfo":
                value = copy.deepcopy(self.file)
                value["size"] = self.file_size
                return {"message": {"id": 77 * 1048576, "content": {
                    "@type": "messageDocument",
                    "document": {"file_name": self.filename, "document": value}}}}
            if kind == "downloadFile":
                path = Path(self.file["local"]["path"])
                if self.mode == "stop":
                    path.write_bytes(payload[:4096])
                    self.file["local"].update(downloaded_size=4096)
                    stop.set()
                elif self.mode == "complete":
                    path.write_bytes(payload)
                    self.file["local"].update(downloaded_size=len(payload), is_downloading_completed=True)
                elif self.mode == "corrupt":
                    path.write_bytes(payload[:80] + bytes([payload[80] ^ 1]) + payload[81:])
                    self.file["local"].update(downloaded_size=len(payload), is_downloading_completed=True)
                self.files[91] = copy.deepcopy(self.file)
                return copy.deepcopy(self.file)
            if kind == "cancelDownloadFile":
                return {"@type": "ok"}
            if kind == "getFile":
                return copy.deepcopy(self.file)
            if kind == "deleteFile":
                Path(self.file["local"]["path"]).unlink(missing_ok=True)
                self.file["local"].update(downloaded_size=0, is_downloading_completed=False)
                return {"@type": "ok"}
            raise AssertionError(kind)

    service = Service()
    adapter = TelethonTelegramAdapter(
        12345, "a" * 32, tmp_path / "account.session", tmp_path / "inbox",
        download_engine="tdlib", on_event=events.append, stop_event=stop)
    adapter.download_dir.mkdir()
    monkeypatch.setattr(tdlib_client, "get_client", lambda *args: service)

    async def get_me():
        return SimpleNamespace(id=7)

    def transfer():
        return _TransferEvents(events.append, 77, "fixture_channel", "model.zip", "", len(payload))

    return SimpleNamespace(adapter=adapter, service=service, message=message, payload=payload, stop=stop,
                           original=SimpleNamespace(get_me=get_me), transfer=transfer, events=events, calls=calls)


def run_download(fx):
    return asyncio.run(fx.adapter._download_archive_async(
        fx.original, fx.message, "model.zip", fx.transfer()))


def test_tdlib_download_uses_message_link_and_native_file_progress_then_validates(download):
    fx = download
    existing = fx.adapter.download_dir / "model.zip"
    existing.write_bytes(b"existing")
    target = run_download(fx)
    assert target.name == "model-2.zip" and target.read_bytes() == fx.payload
    assert existing.read_bytes() == b"existing"
    assert not fx.service.active_files
    assert fx.events[-1]["download_status"] == "completed" and not fx.events[-1]["resumable"]
    assert [row["download_status"] for row in fx.events[-2:]] == ["validating", "completed"]
    assert fx.events[-1]["download_engine"] == "tdlib"
    assert next(call for call in fx.calls if call["@type"] == "getMessageLinkInfo")["url"] == "https://t.me/fixture_channel/77"
    assert next(call for call in fx.calls if call["@type"] == "searchPublicChat")["username"] == "fixture_channel"
    request = next(call for call in fx.calls if call["@type"] == "downloadFile")
    assert request["file_id"] == 91 and request["limit"] == 0 and not request["synchronous"]
    assert not Path(fx.service.file["local"]["path"]).exists()


def test_stop_and_resume_keeps_tdlib_cache_and_resumes_native_bytes(download):
    fx = download
    fx.service.mode = "stop"
    with pytest.raises(DownloadCancelled):
        run_download(fx)
    assert fx.events[-1]["download_status"] == "cancelled"
    assert fx.events[-1]["resumable"] and fx.events[-1]["received"] == 4096
    assert Path(fx.service.file["local"]["path"]).read_bytes() == fx.payload[:4096]
    assert any(call["@type"] == "cancelDownloadFile" for call in fx.calls)
    assert not fx.service.active_files
    fx.stop.clear()
    fx.service.mode = "complete"
    assert run_download(fx).read_bytes() == fx.payload
    assert fx.events[-1]["resumed_bytes"] == 4096
    assert fx.events[-1]["received"] == len(fx.payload)


@pytest.mark.parametrize("reason", ["account", "filename", "size", "login", "busy"])
def test_tdlib_checks_account_message_and_authorization_before_downloading(download, reason):
    fx = download
    if reason == "account":
        fx.service.user_id = 8
    elif reason == "filename":
        fx.service.filename = "changed.zip"
    elif reason == "size":
        fx.service.file_size += 1
    elif reason == "login":
        fx.service.authorized = False
    else:
        fx.service.active_files.add(91)
    with pytest.raises(RuntimeError, match="账号不同|不一致|尚未登录|已经在"):
        run_download(fx)
    assert not any(call["@type"] == "downloadFile" for call in fx.calls)
    assert fx.events[-1]["download_status"] == "failed"


@pytest.mark.parametrize("reason", ["idle", "total"])
def test_tdlib_transfer_timeouts_keep_partial_files(download, reason):
    fx = download
    fx.service.mode = "waiting"
    Path(fx.service.file["local"]["path"]).write_bytes(fx.payload[:4096])
    fx.service.file["local"]["downloaded_size"] = 4096
    fx.adapter.download_start_timeout_seconds = .01 if reason == "idle" else 60
    fx.adapter.download_transfer_timeout_seconds = .01 if reason == "total" else 60
    with pytest.raises(RuntimeError, match="无进展|总时限"):
        run_download(fx)
    assert fx.events[-1]["resumable"]
    assert not fx.service.active_files
    assert Path(fx.service.file["local"]["path"]).is_file()


def test_tdlib_zip_validation_failure_isolated_and_native_cache_invalidated(download):
    fx = download
    fx.service.mode = "corrupt"
    with pytest.raises(RuntimeError):
        run_download(fx)
    assert fx.events[-1]["download_status"] == "failed" and not fx.events[-1]["resumable"]
    assert ".unverified" in Path(fx.events[-1]["local_path"]).parts
    assert not Path(fx.service.file["local"]["path"]).exists()
    assert not (fx.adapter.download_dir / "model.zip").exists()


def test_tdlib_rejects_outside_cache_path_without_exposing_it(download):
    fx = download
    outside = fx.adapter.download_dir / "private.bin"
    outside.write_bytes(b"private")
    fx.service.file["local"]["path"] = str(outside)
    fx.service.mode = "waiting"
    with pytest.raises(RuntimeError, match="超出本机缓存目录"):
        run_download(fx)
    assert "local_path" not in fx.events[-1]
    assert outside.read_bytes() == b"private"


def test_tdlib_rar_uses_existing_validator_and_does_not_move_native_cache(download, monkeypatch):
    fx = download
    fx.message.file.name = fx.service.filename = "model.rar"
    fx.adapter.archive_test_executable_path = "fixture-unrar"
    fx.adapter.archive_test_timeout_seconds = 123
    checked = []

    def validate(path, executable, timeout):
        checked.append((path.read_bytes(), executable, timeout))

    monkeypatch.setattr(tdlib_download, "validate_rar", validate)
    target = asyncio.run(fx.adapter._download_archive_async(
        fx.original, fx.message, "model.rar", fx.transfer()))
    assert target.read_bytes() == fx.payload
    assert checked == [(fx.payload, "fixture-unrar", 123)]


def test_tdlib_cached_file_validates_without_counting_old_bytes_as_new(download):
    fx = download
    cache = Path(fx.service.file["local"]["path"])
    cache.write_bytes(fx.payload)
    fx.service.file["local"].update(downloaded_size=len(fx.payload), is_downloading_completed=True)
    fx.service.mode = "waiting"
    assert run_download(fx).read_bytes() == fx.payload
    assert fx.events[-1]["resumed_bytes"] == len(fx.payload)
    assert fx.events[-1]["average_speed"] == 0


def test_tdlib_configuration_validation(tmp_path):
    assert PipelineConfig().telegram_download_engine == "telethon"
    assert PipelineConfig(telegram_download_engine="tdlib").telegram_download_engine == "tdlib"
    for value in ("browser", "", None, 1, True, [], {}):
        with pytest.raises(ValueError):
            PipelineConfig(telegram_download_engine=value)
        with pytest.raises(ValueError):
            TelethonTelegramAdapter(1, "a" * 32, tmp_path / "session", tmp_path, download_engine=value)


def test_tdlib_database_lease_is_exclusive_and_released(tmp_path):
    first = tdlib_client._DatabaseLease(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="其他程序实例"):
            tdlib_client._DatabaseLease(tmp_path)
    finally:
        first.close()
    tdlib_client._DatabaseLease(tmp_path).close()


def test_tdlib_qr_status_contains_image_url_not_authorization_token():
    client = object.__new__(tdlib_client.TdLibClient)
    client.lock = threading.RLock()
    client.state = {"@type": "authorizationStateWaitOtherDeviceConfirmation", "link": "tg://login?token=PRIVATE"}
    client.login_method = "qr"
    client.error, client.active_files, client.auth_pending, client.network_ready = "", set(), False, False
    status = client.status()
    assert status["qr_url"].startswith("/api/tdlib/qr?v=")
    assert "PRIVATE" not in json.dumps(status)
    assert client.qr_png().startswith(b"\x89PNG")


@pytest.mark.parametrize("proxy_type", [None, "socks5", "http"])
def test_real_tdlib_initializes_proxy_and_database_without_network(tmp_path, monkeypatch, proxy_type):
    pytest.importorskip("tdjson")
    hub = tdlib_client._get_hub()
    request = hub.request
    requests = []

    def offline(identity, query):
        requests.append(query["@type"])
        if query["@type"] == "setTdlibParameters":
            request(identity, {"@type": "setNetworkType", "type": {"@type": "networkTypeNone"}})
        return request(identity, query)

    monkeypatch.setattr(hub, "request", offline)
    proxy = {"proxy_type": proxy_type, "addr": "127.0.0.1", "port": 9,
             "username": "fixture", "password": "fixture-password"} if proxy_type else None
    client = tdlib_client.TdLibClient(12345, "a" * 32, tmp_path / "account.session", tmp_path / "inbox", proxy)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = client.status()
            if client.network_ready or status["error"]:
                break
            time.sleep(.03)
        assert client.network_ready, client.status()
        assert client.status()["state"] == "phone"
        assert not client.status()["authorized"]
        assert client.call({"@type": "getOption", "name": "version"})["value"] == "1.8.69"
        proxies = client.call({"@type": "getProxies"})["proxies"]
        if proxy_type:
            assert proxies and proxies[0]["is_enabled"]
            assert proxies[0]["proxy"]["type"]["username"] == "fixture"
            assert proxies[0]["proxy"]["type"]["password"] == "fixture-password"
        assert "setTdlibParameters" in requests
    finally:
        client.close()
    assert client.closed


def test_tdlib_auth_submission_is_nonblocking_and_never_echoes_credentials():
    client = object.__new__(tdlib_client.TdLibClient)
    client.lock = threading.RLock()
    client.changed = threading.Condition(client.lock)
    client.state = {"@type": "authorizationStateWaitPassword", "password_hint": "hint"}
    client.error, client.active_files, client.auth_pending, client.network_ready = "", set(), False, False
    client.login_method = "qr"
    future = Future()
    client.send = lambda query: future
    result = client.submit("password", "PRIVATE-PASSWORD")
    assert result["auth_pending"] and "PRIVATE" not in json.dumps(result)
    with pytest.raises(ValueError, match="正在验证"):
        client.submit("password", "again")
    future.set_exception(tdlib_client.TdLibError(400, "PRIVATE-ERROR"))
    status = client.status()
    assert not status["auth_pending"] and status["error"]
    assert "PRIVATE" not in json.dumps(status)


def test_tdlib_private_channel_loaded_from_archive_before_resolving_message(download):
    fx = download
    original = fx.service.call_async
    loaded = []

    async def request(query, timeout=30):
        if query["@type"] == "loadChats":
            loaded.append(query["chat_list"]["@type"])
            if loaded[-1] == "chatListMain":
                raise tdlib_client.TdLibError(404, "All chats have been loaded")
            return {"@type": "ok"}
        if query["@type"] == "getMessageLinkInfo" and "chatListArchive" not in loaded:
            raise tdlib_client.TdLibError(400, "Chat not found")
        return await original(query, timeout)

    fx.service.call_async = request
    transfer = fx.transfer()
    transfer.data["chat"] = "-100123"
    result = asyncio.run(fx.adapter._download_archive_async(
        fx.original, fx.message, "model.zip", transfer))
    assert result.read_bytes() == fx.payload
    assert loaded == ["chatListMain", "chatListArchive"]
    assert next(row for row in fx.calls if row["@type"] == "getMessageLinkInfo")["url"] == "https://t.me/c/123/77"


def test_tdlib_private_channel_lookup_is_bounded_and_cancellable():
    calls = []
    stop = False

    async def request(query, timeout=30):
        calls.append(query["@type"])
        if query["@type"] == "getMessageLinkInfo":
            raise tdlib_client.TdLibError(400, "Chat not found")
        return {"@type": "ok"}

    def check():
        if stop:
            raise DownloadCancelled("stopped")

    client = SimpleNamespace(call_async=request)
    with pytest.raises(RuntimeError, match="找不到该频道消息"):
        asyncio.run(tdlib_download.resolve_message(client, "https://t.me/c/123/77", check))
    assert calls.count("loadChats") == 10
    stop = True
    with pytest.raises(DownloadCancelled):
        asyncio.run(tdlib_download.resolve_message(client, "https://t.me/c/123/77", check))


def test_tdlib_qr_waits_for_proxy_and_can_retry_without_echoing_native_error():
    client = object.__new__(tdlib_client.TdLibClient)
    client.lock = threading.RLock()
    client.changed = threading.Condition(client.lock)
    client.state = {"@type": "authorizationStateWaitPhoneNumber"}
    client.login_method, client.qr_requested, client.network_ready = "qr", False, False
    client.error, client.active_files, client.auth_pending = "", set(), False
    futures = []

    def send(query):
        future = Future()
        futures.append((query, future))
        return future

    client.send = send
    client._request_qr()
    assert futures == []
    client.network_ready = True
    client._request_qr()
    client._request_qr()
    assert len(futures) == 1
    futures[-1][1].set_exception(tdlib_client.TdLibError(400, "PRIVATE-TOKEN"))
    assert not client.qr_requested and client.status()["error"]
    assert "PRIVATE" not in json.dumps(client.status())
    client.login("qr")
    assert len(futures) == 2 and not client.status()["error"]
    for field, value in (([], "1"), ({}, "1"), ("phone", [])):
        with pytest.raises(ValueError):
            client.submit(field, value)
