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

from xianyu_pipeline import adapters
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


def test_archive_series_metadata_groups_split_volumes():
    from xianyu_pipeline.adapters import TelethonTelegramAdapter

    rows = [
        {"filename": "Heroes Infinite 04-2026 - Blood Tribes.part2.rar"},
        {"filename": "Heroes Infinite 04-2026 - Blood Tribes.part1.rar"},
        {"filename": "standalone.zip"},
    ]
    TelethonTelegramAdapter._annotate_series(rows)
    assert rows[0]["series_key"] == rows[1]["series_key"]
    assert rows[0]["series_name"] == "Heroes Infinite 04-2026 - Blood Tribes"
    assert rows[0]["series_index"] == 2
    assert rows[1]["series_index"] == 1
    assert rows[0]["series_total"] == 2
    assert rows[1]["series_total"] == 2
    assert rows[2]["series"] is False


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


def test_api_reads_channel_posts_only_and_can_exclude_forwarded_posts(tmp_path, monkeypatch):
    payload = make_zip()
    items = [
        SimpleNamespace(id=5, post=False, fwd_from=None,
                        file=SimpleNamespace(name="comment.zip", size=len(payload)), message="comment"),
        SimpleNamespace(id=4, post=True, fwd_from=SimpleNamespace(),
                        file=SimpleNamespace(name="forwarded.zip", size=len(payload)), message="forwarded"),
        SimpleNamespace(id=3, post=True, fwd_from=None,
                        file=SimpleNamespace(name="original.zip", size=len(payload)), message="original"),
    ]
    clients = fake_client(monkeypatch, items, payloads={3: payload, 4: payload})

    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session",
                                      tmp_path / "inbox")
    messages = adapter.fetch_messages("", [], "https://t.me/free_3d_models_download", "recent", 2)
    assert [item.message_id for item in messages] == [3, 4]
    assert clients[0].downloaded == [3, 4]

    clients.clear()
    filtered = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "filtered.session",
                                       tmp_path / "filtered-inbox", exclude_forwarded_posts=True)
    messages = filtered.fetch_messages("", [], "https://t.me/free_3d_models_download", "recent", 2)
    assert [item.message_id for item in messages] == [3]
    assert clients[0].downloaded == [3]


def test_channel_listing_reads_only_channel_posts_and_marks_forwards(tmp_path, monkeypatch):
    archive = lambda mid, name, **flags: SimpleNamespace(
        id=mid, file=SimpleNamespace(name=name, size=123), message=f"caption {mid}",
        date=None, document=None, **flags)
    items = [
        archive(103, "comment.rar", post=False),
        archive(102, "forwarded.rar", post=True, fwd_from=SimpleNamespace()),
        archive(101, "original.rar", post=True, fwd_from=None),
    ]
    clients = []

    class Client:
        def __init__(self, *args):
            self.connected = False
            clients.append(self)

        def connect(self):
            self.connected = True

        def disconnect(self):
            self.connected = False

        def is_user_authorized(self):
            return True

        def iter_messages(self, target, **options):
            return iter(items)

        def get_messages(self, *args, **kwargs):
            return []

    sync = types.ModuleType("telethon.sync")
    sync.TelegramClient = Client
    monkeypatch.setitem(sys.modules, "telethon.sync", sync)
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session",
                                      tmp_path / "inbox")
    page = adapter.list_archives("https://t.me/free_3d_models_download",
                                 preview_dir=tmp_path / "previews", limit=5)
    assert [row["message_id"] for row in page["messages"]] == [102, 101]
    assert [row["forwarded"] for row in page["messages"]] == [True, False]
    assert all(not client.connected for client in clients)

    filtered = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "filtered.session",
                                       tmp_path / "filtered-inbox", exclude_forwarded_posts=True)
    page = filtered.list_archives("https://t.me/free_3d_models_download",
                                  preview_dir=tmp_path / "filtered-previews", limit=5)
    assert [row["message_id"] for row in page["messages"]] == [101]


def test_channel_listing_paginates_and_fetches_only_cover(tmp_path, monkeypatch):
    archive = lambda mid, name: SimpleNamespace(
        id=mid, file=SimpleNamespace(name=name, size=123), message=f"caption {mid}",
        date=None, document=None)
    items = [archive(103, "new.rar"), archive(102, "photo.jpg"), archive(101, "old.zip")]
    clients = []

    class Client:
        def __init__(self, *args):
            self.lookups = []
            self.connected = False
            clients.append(self)

        def connect(self):
            self.connected = True

        def disconnect(self):
            self.connected = False

        def is_user_authorized(self):
            return True

        def iter_messages(self, target, **options):
            assert target == "free_3d_models_download" and options["limit"] == 200
            return iter(item for item in items if item.id < options.get("offset_id", 9999))

        def get_messages(self, target, limit, offset_id):
            self.lookups.append(offset_id)
            return [SimpleNamespace(id=offset_id - 1, photo=object())]

        def download_media(self, *args, **kwargs):
            raise AssertionError("archive bytes must not be downloaded by listing")

    sync = types.ModuleType("telethon.sync")
    sync.TelegramClient = Client
    monkeypatch.setitem(sys.modules, "telethon.sync", sync)
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session", tmp_path / "inbox")
    monkeypatch.setattr(adapter, "_preceding_photo_group", lambda history, mid: history)

    async def cover(client, media, message_id, timeout, directory, thumb=-1):
        assert thumb == -1
        path = directory / f"{message_id}.jpg"
        path.write_bytes(b"\xff\xd8\xffthumbnail")
        return str(path)

    monkeypatch.setattr(adapter, "_download_preview", cover)
    preview_dir = tmp_path / "output" / ".channel-previews" / "free_3d_models_download"
    first = adapter.list_archives("https://t.me/free_3d_models_download", preview_dir=preview_dir, limit=1)
    assert [row["message_id"] for row in first["messages"]] == [103]
    assert first["oldest_scanned_id"] == 103 and first["has_more"]
    assert first["messages"][0]["cover_message_id"] == 102
    assert first["messages"][0]["cover_quality"] == "preview"
    second = adapter.list_archives("https://t.me/free_3d_models_download", before_id=103,
                                   preview_dir=preview_dir, limit=1)
    assert [row["message_id"] for row in second["messages"]] == [101]
    assert second["oldest_scanned_id"] == 101
    assert all(not client.connected for client in clients)
    assert clients[0].lookups == [103] and clients[1].lookups == [101]
    cached = adapter.list_archives("https://t.me/free_3d_models_download", preview_dir=preview_dir, limit=1,
                                   known_covers={103: {"cover_path": first["messages"][0]["cover_path"],
                                                       "cover_message_id": 102, "cover_quality": "full"}})
    assert cached["messages"][0]["cover_message_id"] == 102
    assert clients[2].lookups == []

    async def fallback_cover(client, media, message_id, timeout, directory, thumb=-1):
        assert thumb != 0, "Never degrade list previews to the smallest thumbnail"
        raise TimeoutError("preview unavailable")

    monkeypatch.setattr(adapter, "_download_preview", fallback_cover)
    fallback = adapter.list_archives("https://t.me/free_3d_models_download", preview_dir=preview_dir, limit=1)
    assert fallback["messages"][0]["cover_status"] == "failed"
    assert not fallback["messages"][0].get("cover_path")


def test_channel_listing_allows_100_archives_and_expands_raw_scan_window(tmp_path, monkeypatch):
    archive = lambda mid: SimpleNamespace(
        id=mid, file=SimpleNamespace(name=f"asset-{mid}.zip", size=123),
        message=f"caption {mid}", date=None, document=None)
    items = [archive(mid) for mid in range(100, 0, -1)]

    class Client:
        def connect(self):
            pass

        def disconnect(self):
            pass

        def is_user_authorized(self):
            return True

        def get_messages(self, *args, **kwargs):
            return []

        def iter_messages(self, target, **options):
            assert target == "free_3d_models_download"
            assert options["limit"] == 1000
            return iter(items)

    sync = types.ModuleType("telethon.sync")
    sync.TelegramClient = lambda *args: Client()
    monkeypatch.setitem(sys.modules, "telethon.sync", sync)
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session",
                                      tmp_path / "inbox", scan_limit=200)
    page = adapter.list_archives("https://t.me/free_3d_models_download",
                                 preview_dir=tmp_path / "previews", limit=100)
    assert len(page["messages"]) == 100
    assert page["messages"][0]["message_id"] == 100


def test_channel_listing_explains_inactive_local_proxy(tmp_path, monkeypatch):
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session",
                                      tmp_path / "inbox")
    adapter.proxy = {"addr": "127.0.0.1", "port": 7890}

    class Client:
        disconnected = False

        def connect(self):
            raise ConnectionError("connection retries exhausted")

        def disconnect(self):
            self.disconnected = True

    client = Client()
    monkeypatch.setattr(adapter, "_client", lambda: client)

    def refused(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("xianyu_pipeline.adapters.socket.create_connection", refused)
    with pytest.raises(RuntimeError, match="127.0.0.1:7890 未监听"):
        adapter.list_archives("example_channel", preview_dir=tmp_path / ".channel-previews")
    assert client.disconnected


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


@pytest.fixture
def large_zip():
    buffer = io.BytesIO()
    length = adapters._ARCHIVE_PARALLEL_MIN_BYTES + adapters._ARCHIVE_REQUEST_BYTES + 37
    content = (bytes(range(251)) * (length // 251 + 1))[:length]
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("asset.bin", content)
    return buffer.getvalue()


class ChunkClient:
    def __init__(self, content, before_chunk=None, transform=None):
        self.content = content
        self.before_chunk, self.transform = before_chunk, transform
        self.calls, self.delivered, self.tasks, self.closed = [], [], [], []
        self.active = self.max_active = self.sequential_calls = 0
        self.close_gate = None
        self.close_started = asyncio.Event()

    def iter_download(self, message, *, offset, limit, request_size):
        self.calls.append((offset, limit, request_size))
        client = self

        class Stream:
            def __init__(self):
                self.offset = self.position = offset
                self.index = 0

            def __aiter__(self):
                client.active += 1
                client.max_active = max(client.max_active, client.active)
                client.tasks.append(asyncio.current_task())
                return self

            async def __anext__(self):
                if self.index >= limit:
                    raise StopAsyncIteration
                await asyncio.sleep(0)
                if client.before_chunk:
                    await client.before_chunk(self)
                data = memoryview(client.content)[self.position:self.position + request_size]
                if not data:
                    raise StopAsyncIteration
                if client.transform:
                    data = client.transform(self, data)
                client.delivered.append((self.position, len(data)))
                self.position += request_size
                self.index += 1
                return data

            async def close(self):
                client.close_started.set()
                if client.close_gate is not None:
                    await client.close_gate.wait()
                await asyncio.sleep(0)
                assert offset not in client.closed
                client.closed.append(offset)
                client.active -= 1

        return Stream()

    def download_media(self, message, file, progress_callback):
        self.sequential_calls += 1
        Path(file).write_bytes(self.content)
        progress_callback(len(self.content), len(self.content))
        return file


def archive_download_fixture(tmp_path, content, *, name="asset.zip", client=None, **options):
    client = client or ChunkClient(content)
    events = []

    def observe(event):
        if event["download_status"] in {"completed", "failed", "cancelled"}:
            assert client.active == 0
            assert all(task.done() for task in client.tasks)
        events.append(event)

    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session",
                                      tmp_path / "inbox", on_event=observe, **options)
    message = SimpleNamespace(id=77, file=SimpleNamespace(name=name, size=len(content)))
    transfer = adapters._TransferEvents(observe, message.id, "fixture_channel", name, "", len(content))
    return adapter, client, message, transfer, events


@pytest.mark.parametrize("workers", [2, 4, 8])
@pytest.mark.parametrize("aligned", [False, True])
def test_parallel_archive_offsets_tail_and_concurrency(tmp_path, large_zip, workers, aligned):
    request = adapters._ARCHIVE_REQUEST_BYTES
    content = large_zip
    if aligned:
        padding = -len(large_zip) % request
        with zipfile.ZipFile(io.BytesIO(large_zip)) as archive:
            payload = archive.read("asset.bin")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("asset.bin", payload + b"\0" * padding)
        content = buffer.getvalue()
        assert len(content) % request == 0
    adapter, client, message, transfer, events = archive_download_fixture(
        tmp_path, content, download_workers=workers)
    adapter.download_dir.mkdir()
    existing = adapter.download_dir / message.file.name
    existing.write_bytes("不能覆盖已有文件".encode("utf-8"))
    saved = asyncio.run(adapter._download_archive_async(client, message, message.file.name, transfer))
    assert saved.name == "asset-2.zip" and saved.read_bytes() == content
    assert existing.read_bytes() == "不能覆盖已有文件".encode("utf-8")
    assert client.max_active == workers and client.active == 0
    assert len(client.calls) == len(client.closed) == workers and client.sequential_calls == 0
    ranges = sorted(client.calls)
    cursor = 0
    for offset, limit, request_size in ranges:
        assert offset == cursor and offset % 4096 == 0 and request_size == request
        assert limit >= 1
        cursor = min(len(content), offset + limit * request)
    assert cursor == len(content)
    delivered = sorted(client.delivered)
    assert [offset for offset, _ in delivered] == list(range(0, len(content), request))
    assert delivered[-1][1] == (request if aligned else len(content) % request)
    progress = [row for row in events if row["download_status"] == "downloading"]
    counts = [row["received"] for row in progress]
    assert counts == sorted(counts) and counts[-1] == len(content)
    assert counts[1] == request
    assert all(row["speed"] > 0 and row["eta"] is not None for row in progress[1:])
    assert [row["download_status"] for row in events[-2:]] == ["validating", "completed"]
    assert sum(row["download_status"] == "completed" for row in events) == 1


@pytest.mark.parametrize("fallback", ["single_worker", "small_file", "no_iterator", "unknown_size"])
def test_parallel_selection_keeps_download_media_compatibility(tmp_path, large_zip, fallback):
    content = make_zip() if fallback == "small_file" else large_zip
    adapter, client, message, transfer, _ = archive_download_fixture(
        tmp_path, content, download_workers=1 if fallback == "single_worker" else 4)
    if fallback == "no_iterator":
        client.iter_download = None
    if fallback == "unknown_size":
        message.file.size = None
    saved = asyncio.run(adapter._download_archive_async(client, message, message.file.name, transfer))
    assert saved.read_bytes() == content
    assert client.sequential_calls == 1 and client.calls == []


@pytest.mark.parametrize("problem", ["short", "long", "early_eof", "metadata_too_small", "metadata_too_large"])
def test_parallel_archive_rejects_incomplete_and_wrong_length_chunks(tmp_path, large_zip, problem):
    def transform(stream, data):
        if stream.offset == 0 and stream.index == 0:
            return data[:-1] if problem == "short" else bytes(data) + b"x"
        return data

    content = large_zip[:-(len(large_zip) % adapters._ARCHIVE_REQUEST_BYTES)] if problem == "early_eof" else large_zip
    client = ChunkClient(content,
                         transform=transform if problem in {"short", "long"} else None)
    adapter, client, message, transfer, events = archive_download_fixture(tmp_path, large_zip, client=client)
    if problem == "metadata_too_small":
        message.file.size -= 1
    elif problem == "metadata_too_large":
        message.file.size += adapters._ARCHIVE_REQUEST_BYTES
    with pytest.raises(RuntimeError, match="分块长度不一致|分块提前结束"):
        asyncio.run(adapter._download_archive_async(client, message, message.file.name, transfer))
    assert not (adapter.download_dir / message.file.name).exists()
    assert client.active == 0 and all(task.done() for task in client.tasks)
    assert len(client.closed) == len(client.calls) and client.sequential_calls == 0
    assert events[-1]["download_status"] == "failed"
    assert not any(row["download_status"] in {"validating", "completed"} for row in events)


def test_parallel_failure_cancels_workers_before_isolation_and_counts_real_bytes(tmp_path, large_zip):
    async def run():
        ready, blocked = asyncio.Event(), asyncio.Event()
        started = set()

        async def before(stream):
            if stream.index == 0:
                return
            started.add(stream.offset)
            if len(started) == 4:
                ready.set()
            await ready.wait()
            if stream.offset == 0:
                raise OSError("离线分块故障")
            await blocked.wait()

        client = ChunkClient(large_zip, before_chunk=before)
        adapter, _, message, transfer, events = archive_download_fixture(tmp_path, large_zip, client=client)
        with pytest.raises(RuntimeError, match="离线分块故障"):
            await adapter._download_archive_async(client, message, message.file.name, transfer)
        assert client.active == 0 and len(client.closed) == 4
        assert all(task.done() for task in client.tasks) and client.sequential_calls == 0
        final = events[-1]
        assert final["download_status"] == "failed" and final["speed"] == 0 and final["eta"] is None
        assert final["received"] == 4 * adapters._ARCHIVE_REQUEST_BYTES
        retained = Path(final["local_path"])
        assert ".unverified" in retained.parts and retained.stat().st_size > final["received"]
        before_bytes = retained.read_bytes()
        await asyncio.sleep(0)
        assert retained.read_bytes() == before_bytes
        assert not any(row["download_status"] == "completed" for row in events)

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["iterator", "close"])
def test_parallel_iterator_creation_and_close_errors_never_trigger_retransmit(tmp_path, large_zip, phase):
    class Client(ChunkClient):
        def iter_download(self, message, *, offset, **options):
            if phase == "iterator" and offset == 0:
                raise OSError("离线 iterator 初始化失败")
            stream = super().iter_download(message, offset=offset, **options)
            if phase == "close" and offset == 0:
                original = stream.close

                async def close():
                    await original()
                    raise OSError("离线 iterator 关闭失败")

                stream.close = close
            return stream

    adapter, client, message, transfer, events = archive_download_fixture(
        tmp_path, large_zip, client=Client(large_zip))
    with pytest.raises(RuntimeError, match="离线 iterator"):
        asyncio.run(adapter._download_archive_async(client, message, message.file.name, transfer))
    assert client.active == 0 and all(task.done() for task in client.tasks)
    assert len(client.closed) == len(client.calls) and client.sequential_calls == 0
    assert events[-1]["download_status"] == "failed"
    assert not (adapter.download_dir / message.file.name).exists()


@pytest.mark.parametrize("reason", ["stop", "idle", "total", "cancel", "cancel_twice"])
def test_parallel_watchdog_and_parent_cancellation_wait_for_every_worker(tmp_path, large_zip, reason):
    async def run():
        ready, blocked = asyncio.Event(), asyncio.Event()
        started, stop = set(), Event()

        async def before(stream):
            if stream.index == 0:
                return
            started.add(stream.offset)
            if len(started) == 4:
                ready.set()
                if reason == "stop":
                    stop.set()
            await blocked.wait()

        client = ChunkClient(large_zip, before_chunk=before)
        adapter, _, message, transfer, events = archive_download_fixture(
            tmp_path, large_zip, client=client, stop_event=stop,
            download_start_timeout_seconds=0.05 if reason == "idle" else 60,
            download_transfer_timeout_seconds=0.05 if reason == "total" else 60)
        task = asyncio.create_task(adapter._download_archive_async(client, message, message.file.name, transfer))
        await asyncio.wait_for(ready.wait(), timeout=5)
        if reason.startswith("cancel"):
            client.close_gate = asyncio.Event()
            task.cancel()
            await asyncio.wait_for(client.close_started.wait(), timeout=5)
            assert not task.done()
            if reason == "cancel_twice":
                task.cancel()
            client.close_gate.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            expected = {"stop": "已请求停止", "idle": "无进展", "total": "总时限"}[reason]
            with pytest.raises(RuntimeError, match=expected):
                await task
        assert client.active == 0 and len(client.closed) == 4 and client.sequential_calls == 0
        assert all(worker.done() for worker in client.tasks)
        assert events[-1]["download_status"] == ("failed" if reason in {"idle", "total"} else "cancelled")
        assert events[-1]["received"] == 4 * adapters._ARCHIVE_REQUEST_BYTES
        assert ".unverified" in Path(events[-1]["local_path"]).parts
        assert not any(row["download_status"] == "completed" for row in events)

    asyncio.run(run())


def test_parallel_no_first_chunk_timeout_removes_empty_staging_file(tmp_path, large_zip):
    async def before(stream):
        await asyncio.Event().wait()

    client = ChunkClient(large_zip, before_chunk=before)
    adapter, _, message, transfer, events = archive_download_fixture(
        tmp_path, large_zip, client=client, download_start_timeout_seconds=0.05)
    with pytest.raises(RuntimeError, match="未收到文件字节"):
        asyncio.run(adapter._download_archive_async(client, message, message.file.name, transfer))
    assert client.active == 0 and len(client.closed) == 4
    assert all(task.done() for task in client.tasks) and client.sequential_calls == 0
    assert events[-1]["download_status"] == "failed" and events[-1]["received"] == 0
    assert "local_path" not in events[-1]
    assert not list(adapter.download_dir.rglob(message.file.name))


def test_parallel_download_still_checks_zip_crc(tmp_path, large_zip):
    content = bytearray(large_zip)
    content[1024] ^= 1
    adapter, client, message, transfer, events = archive_download_fixture(tmp_path, bytes(content))
    with pytest.raises(RuntimeError, match="CRC|ZIP 内容校验失败"):
        asyncio.run(adapter._download_archive_async(client, message, message.file.name, transfer))
    assert client.active == 0 and len(client.closed) == 4 and client.sequential_calls == 0
    assert events[-2]["download_status"] == "validating" and events[-1]["download_status"] == "failed"
    assert Path(events[-1]["local_path"]).read_bytes() == content
    assert not (adapter.download_dir / message.file.name).exists()


def test_parallel_download_preserves_rar_validation_and_options(tmp_path, monkeypatch, large_zip):
    adapter, client, message, transfer, events = archive_download_fixture(
        tmp_path, large_zip, name="asset.rar", archive_test_executable_path="offline-tester.exe",
        archive_test_timeout_seconds=123)
    checked = []

    def reject(path, executable, timeout):
        assert client.active == 0 and all(task.done() for task in client.tasks)
        checked.append((path.read_bytes(), executable, timeout))
        raise ArchiveValidationError("password_required", "需要密码")

    monkeypatch.setattr(adapters, "validate_rar", reject)
    with pytest.raises(RuntimeError, match="需要密码"):
        asyncio.run(adapter._download_archive_async(client, message, message.file.name, transfer))
    assert checked == [(large_zip, "offline-tester.exe", 123)]
    assert events[-1]["download_status"] == "failed" and client.sequential_calls == 0
    assert Path(events[-1]["local_path"]).read_bytes() == large_zip
    assert not (adapter.download_dir / message.file.name).exists()


@pytest.mark.parametrize("workers", [0, 9, True, 1.5, "4", None])
def test_download_workers_rejects_invalid_config_and_adapter(tmp_path, workers):
    with pytest.raises(ValueError, match="download_workers"):
        PipelineConfig(download_workers=workers)
    with pytest.raises(ValueError, match="download_workers"):
        TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "session", tmp_path / "inbox",
                                download_workers=workers)


def test_real_telethon_public_iterator_downloads_offline_ranges(tmp_path, large_zip, monkeypatch):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl import types as tl

    async def run():
        client = TelegramClient(StringSession(), 12345, "a" * 32)
        requests, active, maximum = [], 0, 0

        async def call(sender, request):
            nonlocal active, maximum
            assert sender is client._sender
            assert request.offset % 4096 == 0 and request.limit == 512 * 1024
            requests.append((request.offset, request.limit))
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0)
                return tl.upload.File(type=tl.storage.FileUnknown(), mtime=0,
                                      bytes=large_zip[request.offset:request.offset + request.limit])
            finally:
                active -= 1

        def sequential(*args, **kwargs):
            raise AssertionError("真实公开 iterator 测试不能切回顺序下载")

        monkeypatch.setattr(client, "_call", call)
        monkeypatch.setattr(client, "download_media", sequential)
        document = tl.Document(id=77, access_hash=0, file_reference=b"offline", date=None,
                               mime_type="application/zip", size=len(large_zip),
                               dc_id=client.session.dc_id,
                               attributes=[tl.DocumentAttributeFilename("real-iterator.zip")])
        message = tl.Message(id=77, peer_id=tl.PeerChannel(123),
                             media=tl.MessageMediaDocument(document=document))
        adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "session", tmp_path / "inbox")
        saved = await adapter._download_archive_async(client, message, message.file.name)
        assert saved.read_bytes() == large_zip and maximum == 4 and active == 0
        assert sorted(offset for offset, _ in requests) == list(range(0, len(large_zip), 512 * 1024))

    asyncio.run(run())


@pytest.mark.parametrize("login", [False, True])
def test_cli_api_entry_points_pass_download_workers(tmp_path, monkeypatch, login):
    import run_pipeline as cli

    config = tmp_path / "cli.json"
    config.write_text(json.dumps({"telegram_api_id": 12345, "telegram_api_hash": "a" * 32,
                                  "download_workers": 2, "download_file_concurrency": 1}), encoding="utf-8")
    recorded = []

    class Telegram(TelethonTelegramAdapter):
        def login(self):
            recorded.append((self.download_workers, self.download_file_concurrency))

    def run(config, telegram, cloud):
        recorded.append((telegram.download_workers, telegram.download_file_concurrency))
        return []

    monkeypatch.setattr(cli, "TelethonTelegramAdapter", Telegram)
    monkeypatch.setattr(cli, "run_pipeline", run)
    monkeypatch.setattr(cli, "restore_telegram_settings", lambda path: None)
    monkeypatch.setattr(cli, "configure_logging", lambda path: None)
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--config", str(config)]
                        + (["--telegram-api-login"] if login else []))
    cli.main()
    assert recorded == [(2, 1)]


def test_download_workers_default_and_legacy_config(tmp_path):
    assert PipelineConfig().download_workers == 4
    assert PipelineConfig().download_file_concurrency == 2
    config = tmp_path / "legacy.json"
    config.write_text("{}", encoding="utf-8")
    assert PipelineConfig.from_json(config).download_workers == 4
    config.write_text('{"download_workers": 1}', encoding="utf-8")
    assert PipelineConfig.from_json(config).download_workers == 1
