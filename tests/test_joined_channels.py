from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from test_workbench_actions import app
from xianyu_pipeline import adapters, web_ui
from xianyu_pipeline.adapters import TelethonTelegramAdapter
from xianyu_pipeline.browser_download import DownloadCancelled
from xianyu_pipeline.channel_sources import ChannelDirectory
from xianyu_pipeline.config import PipelineConfig


def joined_row(identity=123, username="fixture_channel", title="Fixture", avatar_url=""):
    key = username.lower() if username else str(-(1000000000000 + identity))
    url = f"https://t.me/{username}" if username else ""
    return {"id": identity, "title": title, "username": username, "key": key,
            "target": url or key, "url": url, "public": bool(username),
            "avatar_url": avatar_url}


def dialog(identity, username="", **fields):
    entity = SimpleNamespace(id=identity, title=f"Channel {identity}", username=username,
                             broadcast=True, megagroup=False, access_hash=123456789,
                             photo=SimpleNamespace(photo_id=identity))
    for key, value in fields.items():
        setattr(entity, key, value)
    return SimpleNamespace(entity=entity, name=entity.title, is_channel=True)


def adapter(tmp_path, monkeypatch, dialogs, *, authorized=True, stop_event=None):
    result = TelethonTelegramAdapter(123, "a" * 32, tmp_path / "session", tmp_path / "inbox",
                                     stop_event=stop_event)

    class Client:
        disconnected = False
        downloaded = []

        def connect(self):
            pass

        def is_user_authorized(self):
            return authorized

        def iter_dialogs(self, *, limit, folder):
            assert limit is None and folder is None
            return iter(dialogs)

        async def download_profile_photo(self, entity, *, file, download_big):
            assert download_big is False
            self.downloaded.append(entity.id)
            if entity.id == 2:
                raise OSError("No avatar")
            Path(file).write_bytes(b"\xff\xd8\xfffixture")
            return file

        def disconnect(self):
            self.disconnected = True

    client = Client()
    monkeypatch.setattr(result, "_client", lambda: client)
    return result, client


def test_joined_scan_includes_all_public_private_and_archived_broadcasts(tmp_path, monkeypatch):
    forbidden = type("ChannelForbidden", (), {"id": 8, "broadcast": True})()
    data = [dialog(1, "Example"), dialog(2), dialog(3, folder_id=1),
            dialog(4, megagroup=True), dialog(5, broadcast=False),
            dialog(6, left=True), dialog(7, deactivated=True),
            SimpleNamespace(entity=forbidden), dialog(1, "Example")]
    telegram, client = adapter(tmp_path, monkeypatch, data)
    rows = telegram.list_joined_channels(avatar_dir=tmp_path / ".channel-avatars")
    assert [row["id"] for row in rows] == [1, 2, 3]
    assert rows[0]["key"] == "example" and rows[0]["target"] == "https://t.me/Example"
    assert rows[1]["key"] == rows[1]["target"] == "-1000000000002"
    assert rows[1]["url"] == rows[1]["avatar_url"] == ""
    assert rows[0]["avatar_url"] == "/api/channel-avatar/1?v=1"
    assert (tmp_path / ".channel-avatars" / "1.jpg").read_bytes().startswith(b"\xff\xd8\xff")
    assert not list((tmp_path / ".channel-avatars").glob("*-*"))
    assert "access_hash" not in json.dumps(rows) and client.disconnected
    assert rows[0]["verified"] is False and rows[0]["scam"] is False and rows[0]["fake"] is False


def test_joined_scan_reuses_unchanged_avatar_and_refreshes_changed_photo(tmp_path, monkeypatch):
    data = [dialog(1)]
    telegram, client = adapter(tmp_path, monkeypatch, data)
    root = tmp_path / ".channel-avatars"
    first = telegram.list_joined_channels(avatar_dir=root)
    second = telegram.list_joined_channels(avatar_dir=root)
    assert first[0]["avatar_url"] == second[0]["avatar_url"]
    assert client.downloaded == [1]
    data[0].entity.photo.photo_id = 999
    latest = telegram.list_joined_channels(avatar_dir=root)
    assert latest[0]["avatar_url"] == "/api/channel-avatar/1?v=999"
    assert client.downloaded == [1, 1]


def test_joined_scan_avatar_timeout_is_best_effort(tmp_path, monkeypatch):
    telegram, client = adapter(tmp_path, monkeypatch, [dialog(1)])
    cancelled = []

    async def photo(*args, **options):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(client, "download_profile_photo", photo)
    monkeypatch.setattr(adapters, "_CHANNEL_AVATAR_TIMEOUT_SECONDS", 0.01)
    rows = telegram.list_joined_channels(avatar_dir=tmp_path / ".channel-avatars")
    assert len(rows) == 1 and not rows[0]["avatar_url"]
    assert cancelled and client.disconnected


def test_joined_scan_cancel_interrupts_a_waiting_dialog_request(tmp_path, monkeypatch):
    stop = Event()
    telegram, client = adapter(tmp_path, monkeypatch, [], stop_event=stop)
    cancelled = []

    async def dialogs():
        stop.set()
        try:
            await asyncio.sleep(10)
            yield dialog(1)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(client, "iter_dialogs", lambda **options: dialogs())
    started = time.monotonic()
    with pytest.raises(DownloadCancelled):
        telegram.list_joined_channels()
    assert time.monotonic() - started < 2
    assert cancelled and client.disconnected


def test_joined_scan_requires_authorized_account_and_disconnects(tmp_path, monkeypatch):
    telegram, client = adapter(tmp_path, monkeypatch, [], authorized=False)
    with pytest.raises(RuntimeError, match="尚未登录"):
        telegram.list_joined_channels()
    assert client.disconnected


def test_private_channel_requests_use_integer_and_storage_key_stays_string():
    assert TelethonTelegramAdapter.normalize_channel("-100123456789") == -100123456789
    assert TelethonTelegramAdapter.normalize_channel("https://t.me/Example") == "Example"
    transfer = adapters._TransferEvents(None, 41, "-1000000000789", "asset.zip", "Private")
    assert transfer.data["source_url"] == "https://t.me/c/789/41"


def test_private_catalog_uses_marked_peer_and_original_id_in_message_link(tmp_path, monkeypatch):
    telegram, client = adapter(tmp_path, monkeypatch, [])
    requested = []
    message = SimpleNamespace(id=41, file=SimpleNamespace(name="asset.zip", size=12),
                              message="Private archive", post=True, date=None,
                              document=None, photo=None, media=None)

    def history(target, **options):
        requested.append(target)
        return iter([message])

    def lookup(target, **options):
        requested.append(target)
        return []

    monkeypatch.setattr(client, "iter_messages", history, raising=False)
    monkeypatch.setattr(client, "get_messages", lookup, raising=False)
    listing = telegram.list_archives("-1000000000789", limit=1,
                                     preview_dir=tmp_path / "preview")
    assert requested and all(target == -1000000000789 for target in requested)
    row = listing["messages"][0]
    assert row["chat"] == "-1000000000789"
    assert row["source_url"] == "https://t.me/c/789/41"


def test_saved_channels_are_ready_without_account_scan(app):
    assert app.get("/api/telegram-channels")[1]["stage"] == "idle"
    listing = app.get("/api/channel-messages")[1]
    assert listing["channel"] == "fixture_channel" and listing["messages"] == []
    assert app.post("/api/channels/select", {"key": "fixture_channel"})[0] == 200
    assert app.post("/api/channel-scan")[0] == 200
    assert len(app.workers) == 1


def test_unknown_source_cannot_access_message_actions(app):
    values = json.loads(app.config_path.read_text(encoding="utf-8"))
    values["source_channel_url"] = "https://t.me/unknown"
    app.config_path.write_text(json.dumps(values), encoding="utf-8")
    for path, payload in (("/api/channel-scan", {}), ("/api/channel-download", {"ids": [123]}),
                          ("/api/channel-cover", {"id": 123}), ("/api/download", {}), ("/api/run", {})):
        status, response = app.post(path, payload)
        assert status == 400 and "来源频道" in response["message"]
    assert not app.workers


def test_acquisition_auto_saves_channels_and_selects_private_channel(app, monkeypatch):
    values = json.loads(app.config_path.read_text(encoding="utf-8"))
    values.update(source_channel_url="https://invalid.example/channel",
                  browser_executable_path="", upload_provider="quark_cli")
    app.config_path.write_text(json.dumps(values), encoding="utf-8")
    observed = []

    def fetch(self, **options):
        observed.append(options["avatar_dir"])
        return [joined_row(789, "", "Private"), joined_row()]

    monkeypatch.setattr(web_ui.TelethonTelegramAdapter, "list_joined_channels", fetch)
    assert app.post("/api/telegram-channels/select", {"key": "-1000000000789"})[0] == 400
    assert app.post("/api/telegram-channels-scan", {"limit": 1})[0] == 400
    assert app.post("/api/telegram-channels-scan")[0] == 200
    assert app.post("/api/telegram-channels/select", {"key": "-1000000000789"})[0] == 409
    app.run()
    joined = app.get("/api/telegram-channels")[1]
    assert joined["stage"] == "completed" and joined["scanned_at"]
    assert joined["found"] == 2 and joined["added"] == 1 and joined["updated"] == 1
    assert "channels" not in joined and "selected_key" not in joined
    saved = app.get("/api/channels")[1]
    assert len(saved["channels"]) == 2
    assert {row["name"] for row in saved["channels"]} == {"Fixture", "Private"}
    assert observed == [app.store.config.output_dir / ".channel-avatars"]
    assert app.post("/api/telegram-channels/select", {"key": "unjoined"})[0] == 400
    assert app.post("/api/telegram-channels/select", {"key": 789})[0] == 400
    status, result = app.post("/api/telegram-channels/select", {"key": "-1000000000789"})
    assert status == 200 and result["channel"]["url"] == "-1000000000789"
    assert result["channel"]["name"] == "Private"
    assert PipelineConfig.from_json(app.config_path).source_channel_url == "-1000000000789"
    assert app.get("/api/channels")[1]["active"] == "-1000000000789"
    assert app.get("/api/channel-messages")[1]["channel"] == "-1000000000789"
    reloaded = ChannelDirectory(app.store.config.output_dir)
    assert reloaded.get("-1000000000789")["name"] == "Private"


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "empty", "stopped"])
def test_rescan_retains_saved_selection_and_cached_messages(app, monkeypatch, outcome):
    app.acquire()
    previous = app.get("/api/channels")[1]
    messages = app.get("/api/channel-messages")[1]

    def scan(self, **options):
        if outcome == "failed":
            raise RuntimeError("Fixture failed")
        if outcome == "cancelled":
            raise DownloadCancelled("fixture")
        if outcome == "stopped":
            app.state.stop_event.set()
            return [joined_row(789, "", "Private")]
        return []

    monkeypatch.setattr(web_ui.TelethonTelegramAdapter, "list_joined_channels", scan)
    assert app.post("/api/telegram-channels-scan")[0] == 200
    assert app.get("/api/channels")[1] == previous
    app.run()
    joined = app.get("/api/telegram-channels")[1]
    assert joined["stage"] == ("completed" if outcome == "empty" else
                               "cancelled" if outcome == "stopped" else outcome)
    if outcome == "failed":
        assert joined["error"] == "Fixture failed"
    assert app.get("/api/channels")[1] == previous
    assert app.get("/api/channel-messages")[1] == messages
    assert app.post("/api/channel-scan")[0] == 200


def test_source_change_and_account_change_keep_saved_directory(app):
    app.acquire()
    assert app.post("/api/config", {"source_channel_url": "https://t.me/other_channel"})[0] == 200
    assert app.get("/api/channels")[1]["active"] == "other_channel"
    assert app.get("/api/channel-messages")[1]["channel"] == "other_channel"
    app.acquire()
    assert app.post("/api/config", {"telegram_api_hash": "b" * 32})[0] == 200
    joined = app.get("/api/telegram-channels")[1]
    assert joined["stage"] == "idle" and "channels" not in joined
    saved = app.get("/api/channels")[1]
    assert saved["active"] == "fixture_channel" and len(saved["channels"]) == 2


def test_externally_changed_source_hides_stale_selection_and_messages(app):
    app.acquire()
    values = json.loads(app.config_path.read_text(encoding="utf-8"))
    values["source_channel_url"] = "https://t.me/other_channel"
    app.config_path.write_text(json.dumps(values), encoding="utf-8")
    assert len(app.get("/api/channels")[1]["channels"]) == 1
    assert app.get("/api/channel-messages")[1]["channel"] == ""


def test_avatar_endpoint_serves_only_known_safe_cache_files(app, tmp_path):
    app.acquire(avatar_url="/api/channel-avatar/123")
    root = app.store.config.output_dir / ".channel-avatars"
    root.mkdir(parents=True)
    (root / "123.jpg").write_bytes(b"\xff\xd8\xfffixture")
    app.state.telegram_channels = {"stage": "idle", "error": ""}
    # A fresh directory instance proves avatar access survives a service restart.
    app.channels.data = ChannelDirectory(app.store.config.output_dir).data

    def fetch(path):
        handler = app.handler.__new__(app.handler)
        handler.path = path
        handler.headers = {"Host": "127.0.0.1:18765"}
        results = []
        handler.file_response = lambda asset, mime: results.append((200, asset, mime))
        handler.send_error = lambda status: results.append((status, None, None))
        handler.do_GET()
        assert len(results) == 1
        return results[0]

    status, asset, mime = fetch("/api/channel-avatar/123")
    assert status == 200 and asset == (root / "123.jpg").resolve() and mime == "image/jpeg"
    for path in ("/api/channel-avatar/124", "/api/channel-avatar/../config.json",
                 "/api/channel-avatar/123.jpg", "/api/channel-avatar/-100123"):
        assert fetch(path)[0] == 404
    (root / "123.jpg").write_bytes(b"Not an image")
    assert fetch("/api/channel-avatar/123")[0] == 404
