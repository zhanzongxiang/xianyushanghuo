from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import types
import zipfile
from contextlib import nullcontext
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from xianyu_pipeline import adapters, browser_download
from xianyu_pipeline.adapters import ArchiveCard, MessageSnapshot, TelegramBrowserAdapter, TelethonTelegramAdapter


CHANNEL = "https://t.me/fixture_channel"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aH1sAAAAASUVORK5CYII=")
TERMINAL = {"completed", "failed", "cancelled"}


def zip_bytes():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("asset.txt", "offline transfer fixture")
    return stream.getvalue()


def message(mid=7, name="asset.zip", content=None):
    content = zip_bytes() if content is None else content
    return SimpleNamespace(id=mid, file=SimpleNamespace(name=name, size=len(content)), message="Asset title")


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now


class ApiClient:
    def __init__(self, items, payload, clock):
        self.items, self.payload, self.clock = items, payload, clock
        self.downloaded = []
        self.disconnected = False

    def connect(self):
        pass

    def disconnect(self):
        self.disconnected = True

    def is_user_authorized(self):
        return True

    def iter_messages(self, target, limit):
        return iter(self.items)

    def get_messages(self, target, ids=None, *, limit=None, offset_id=None):
        if offset_id is not None:
            return [item for item in self.items if item.id < offset_id][:limit]
        return next(item for item in self.items if item.id == ids)

    # Intentionally no thumb argument: absent document.thumbs must require nothing new.
    async def download_media(self, item, file, progress_callback):
        self.downloaded.append(item.id)
        split = len(self.payload) // 2
        with Path(file).open("wb") as output:
            for chunk in (self.payload[:split], self.payload[split:]):
                output.write(chunk)
                output.flush()
                self.clock.now += 2
                progress_callback(output.tell(), len(self.payload))
        return file


def api_fixture(tmp_path, monkeypatch, items=None, payload=None, callback=None, **options):
    payload = zip_bytes() if payload is None else payload
    items = [message(content=payload)] if items is None else items
    events, clock = [], Clock()
    monkeypatch.setattr(adapters, "time", clock)
    client = ApiClient(items, payload, clock)
    adapter = TelethonTelegramAdapter(123, "hash", tmp_path / "account.session", tmp_path / "inbox",
                                     on_event=events.append if callback is None else callback, **options)
    monkeypatch.setattr(adapter, "_client", lambda: client)
    return adapter, client, events


def assert_terminal(event, status):
    assert event["download_status"] == status
    assert event["speed"] == 0 and event["eta"] is None
    json.dumps(event)


def test_api_events_follow_real_bytes_then_validation(tmp_path, monkeypatch):
    payload = zip_bytes()
    adapter, client, events = api_fixture(tmp_path, monkeypatch, [message(8), message(7)])
    published = []

    def observe(event):
        events.append(event)
        if event["download_status"] == "completed":
            path = Path(event["local_path"])
            assert path.is_absolute() and path.is_file()
            with zipfile.ZipFile(path) as archive:
                published.append(archive.testzip())

    adapter.on_event = observe
    result = adapter.fetch_messages("", [], CHANNEL, "recent", 2)
    assert [event["download_status"] for event in events[:2]] == ["queued", "queued"]
    assert [item.message_id for item in result] == [7, 8]
    assert published == [None, None]
    assert client.disconnected
    for mid in (7, 8):
        own = [event for event in events if event["message_id"] == mid]
        assert [event["download_status"] for event in own] == [
            "queued", "downloading", "downloading", "downloading", "validating", "completed"]
        first = own[2]
        assert first["received"] == len(payload) // 2
        assert first["speed"] == first["received"] / 2
        assert first["eta"] == (len(payload) - first["received"]) / first["speed"]
        assert {event["chat"] for event in own} == {"fixture_channel"}
        assert {event["source_url"] for event in own} == {f"{CHANNEL}/{mid}"}
        assert own[0]["received"] == 0 and own[0]["image_paths"] == []
        assert_terminal(own[-1], "completed")
        assert own[-1]["received"] == own[-1]["size"] == len(payload)


@pytest.mark.parametrize("chat", ["-100123456789", "+invite_token", "123456", "joinchat"])
def test_api_does_not_invent_message_links_for_non_usernames(tmp_path, monkeypatch, chat):
    adapter, _, events = api_fixture(tmp_path, monkeypatch)
    adapter.fetch_messages(chat, [7])
    assert all("source_url" not in event for event in events)


@pytest.mark.parametrize("payload,expected_size", [(b"not zip", 7), (b"partial", 100), (b"", 20)])
def test_api_failed_events_include_error_and_retained_path(tmp_path, monkeypatch, payload, expected_size):
    item = message(content=payload)
    item.file.size = expected_size
    adapter, client, events = api_fixture(tmp_path, monkeypatch, [message(8), item], payload)
    with pytest.raises(RuntimeError):
        adapter.fetch_messages("", [], CHANNEL, "recent", 2)
    failure = next(event for event in events if event["download_status"] == "failed")
    assert_terminal(failure, "failed")
    expected_error = "BadZipFile" if expected_size == 7 else "RuntimeError"
    assert expected_error in failure["error"]
    assert failure["received"] == len(payload)
    if payload:
        retained = Path(failure["local_path"])
        assert retained.is_absolute() and ".unverified" in retained.parts
        assert retained.read_bytes() == payload
    else:
        assert "local_path" not in failure
    assert_terminal(events[-1], "cancelled")
    assert events[-1]["message_id"] == 8 and client.downloaded == [7]
    assert not any(event["download_status"] == "completed" for event in events)


def test_api_transport_error_preserves_original_reason(tmp_path, monkeypatch):
    adapter, client, events = api_fixture(tmp_path, monkeypatch)

    async def broken(item, file, progress_callback):
        Path(file).write_bytes(b"partial")
        progress_callback(7, 100)
        raise OSError("connection reset by fixture")

    client.download_media = broken
    with pytest.raises(RuntimeError, match="connection reset by fixture"):
        adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert_terminal(events[-1], "failed")
    assert "OSError: connection reset by fixture" in events[-1]["error"]
    assert Path(events[-1]["local_path"]).read_bytes() == b"partial"


@pytest.mark.parametrize("before_start", [True, False])
def test_api_stop_is_terminal_and_never_success(tmp_path, monkeypatch, before_start):
    stop = Event()
    adapter, client, events = api_fixture(tmp_path, monkeypatch, stop_event=stop)
    original = client.download_media

    async def stopping(*args, **kwargs):
        result = await original(*args, **kwargs)
        stop.set()
        return result

    client.download_media = stopping
    if before_start:
        stop.set()
    with pytest.raises(RuntimeError):
        adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert_terminal(events[-1], "cancelled")
    assert not any(event["download_status"] == "completed" for event in events)
    assert bool(client.downloaded) is not before_start


@pytest.mark.parametrize("document", [None, SimpleNamespace(), SimpleNamespace(thumbs=[])])
def test_api_missing_thumbnail_does_not_extend_fake_client_requirements(tmp_path, monkeypatch, document):
    item = message()
    item.document = document
    adapter, client, events = api_fixture(tmp_path, monkeypatch, [item])
    adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert_terminal(events[-1], "completed")
    assert events[-1]["image_paths"] == [] and client.downloaded == [7]
    assert not (adapter.download_dir / ".previews").exists()


@pytest.mark.parametrize("preview_result", ["image", "missing", "error", "timeout", "cancelled", "video"])
def test_api_document_fallback_previews_are_unique_and_best_effort(tmp_path, monkeypatch, preview_result):
    item = message()
    item.document = SimpleNamespace(thumbs=[object()])
    adapter, client, events = api_fixture(tmp_path, monkeypatch, [item])
    original, thumbnails, cancelled = client.download_media, [], []
    monkeypatch.setattr(adapters, "_PREVIEW_TIMEOUT_SECONDS", 0.01)

    async def with_thumbnail(media, file, progress_callback=None, thumb=None):
        if thumb is None:
            assert len(thumbnails) == len(client.downloaded) + 1
            if preview_result == "image":
                assert events[-1]["download_status"] == "downloading"
                assert Path(events[-1]["image_paths"][0]).read_bytes() == PNG
            return await original(media, file, progress_callback)
        assert media is item.document and thumb == -1 and progress_callback is None
        thumbnails.append(Path(file))
        if preview_result == "missing":
            return None
        Path(file).write_bytes(PNG)
        if preview_result == "error":
            raise OSError("thumbnail unavailable")
        if preview_result == "cancelled":
            raise asyncio.CancelledError("thumbnail cancelled")
        if preview_result == "timeout":
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.append(True)
        if preview_result == "video":
            Path(file).write_bytes(b"not a bitmap")
        return file

    client.download_media = with_thumbnail
    for _ in range(2):
        adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    finished = [event for event in events if event["download_status"] == "completed"]
    assert len(finished) == 2 and len(thumbnails) == 2
    assert thumbnails[0] != thumbnails[1]
    for event in finished:
        assert_terminal(event, "completed")
        if preview_result == "image":
            path = Path(event["image_paths"][0])
            assert path.parent == (adapter.download_dir / ".previews").resolve()
            assert path.is_absolute() and path.read_bytes() == PNG
        else:
            assert event["image_paths"] == []
            assert not list((adapter.download_dir / ".previews").iterdir())
    assert cancelled == ([True, True] if preview_result == "timeout" else [])
    if preview_result == "image":
        preview_queues = [event for event in events if event["download_status"] == "queued" and event["image_paths"]]
        assert len(preview_queues) == 2
        for queued, completed in zip(preview_queues, finished):
            assert queued["received"] == 0
            assert queued["image_paths"] == completed["image_paths"]
            assert events.index(queued) < events.index(completed)
        for event in events:
            if event["download_status"] in {"downloading", "validating", "completed"}:
                assert len(event["image_paths"]) == 1


def test_api_callback_errors_and_mutations_do_not_break_download(tmp_path, monkeypatch):
    calls = []

    def broken(event):
        calls.append(event["download_status"])
        event["image_paths"].append("not-real.jpg")
        event["received"] = -100
        raise ValueError("consumer failed")

    adapter, _, _ = api_fixture(tmp_path, monkeypatch, callback=broken)
    result = adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert result[0].attachment_paths[0].is_file() and calls[-1] == "completed"


def test_api_default_callback_does_not_download_thumbnails(tmp_path, monkeypatch):
    item = message()
    item.document = SimpleNamespace(thumbs=[object()])
    adapter, client, _ = api_fixture(tmp_path, monkeypatch, [item])
    adapter.on_event = None
    assert adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert client.downloaded == [7]
    assert not (adapter.download_dir / ".previews").exists()


def test_api_first_chunk_wait_keeps_watchdog_scope_and_terminal_event(tmp_path, monkeypatch):
    adapter, client, events = api_fixture(tmp_path, monkeypatch)
    original = client.download_media

    async def delayed(*args, **kwargs):
        await asyncio.sleep(1.05)
        return await original(*args, **kwargs)

    client.download_media = delayed
    adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert_terminal(events[-1], "completed")
    assert all("UnboundLocalError" not in event.get("error", "") for event in events)


def test_api_watchdog_failure_delivers_real_partial_progress(tmp_path, monkeypatch):
    adapter, client, events = api_fixture(tmp_path, monkeypatch, download_start_timeout_seconds=0.01)
    cancelled = []

    async def stalled(item, file, progress_callback):
        Path(file).write_bytes(b"partial")
        client.clock.now += 1
        progress_callback(7, 100)
        client.clock.now += 1
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)

    client.download_media = stalled
    with pytest.raises(RuntimeError):
        adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert cancelled == [True]
    assert_terminal(events[-1], "failed")
    assert events[-1]["received"] == 7 and events[-1]["size"] == 100
    assert "0.01" in events[-1]["error"]
    assert Path(events[-1]["local_path"]).read_bytes() == b"partial"


class Session:
    def __init__(self):
        self.handlers, self.calls = {}, []

    def on(self, name, callback):
        self.handlers[name] = callback

    def send(self, name, options):
        self.calls.append((name, options))


def browser_fixture(tmp_path, monkeypatch, payload=None, name="asset.zip", outcome="completed", callback=None):
    payload = zip_bytes() if payload is None else payload
    events, clock, session = [], Clock(), Session()
    monkeypatch.setattr(adapters, "time", clock)
    monkeypatch.setattr(browser_download, "time", clock)
    page = SimpleNamespace(url="https://web.telegram.org/k/", on=lambda *args: None, is_closed=lambda: False)
    context = SimpleNamespace(pages=[page], on=lambda *args: None, close=lambda: None,
                              browser=SimpleNamespace(new_browser_cdp_session=lambda: session))
    sync = types.ModuleType("playwright.sync_api")
    sync.sync_playwright = lambda: nullcontext(object())
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync)
    staging = []

    def launch(*args):
        staging.append(args[-1])
        args[-1].mkdir(parents=True)
        return context

    monkeypatch.setattr(adapters, "launch_telegram_context", launch)
    monkeypatch.setattr(adapters, "download_resource_status", lambda _: {"disk_free": 10**12})
    adapter = TelegramBrowserAdapter(tmp_path / "profile", tmp_path / "inbox", start_timeout_seconds=1,
                                     start_grace_seconds=0, transfer_timeout_seconds=2,
                                     on_event=events.append if callback is None else callback)
    card = ArchiveCard(name, "document")
    nodes = SimpleNamespace(count=lambda: 1)
    monkeypatch.setattr(adapter, "_open_channel_messages", lambda *args: nodes)
    snapshot = MessageSnapshot(7, "data-mid", "7", "Asset title", (card,))
    monkeypatch.setattr(adapter, "_unique_message_nodes", lambda *args: [snapshot])
    node = SimpleNamespace(inner_text=lambda **kwargs: "Asset title")
    monkeypatch.setattr(adapter, "_locate_message", lambda *args: node)
    monkeypatch.setattr(adapter, "_archive_cards", lambda *args: (card,))
    monkeypatch.setattr(adapter, "_card_still_matches", lambda *args: True)
    guid = "00000000-0000-0000-0000-000000000007"
    ticks = []

    def click(**kwargs):
        if outcome == "start_timeout":
            return
        session.handlers["Browser.downloadWillBegin"](
            {"guid": guid, "url": "blob:fixture", "suggestedFilename": name})
        ticks.append(1)

    def tick(milliseconds):
        clock.now += milliseconds / 1000
        if not ticks:
            return
        count = ticks.pop()
        size = len(payload) // 2 if count == 1 else len(payload)
        state = "inProgress" if count == 1 or outcome == "timeout" else outcome
        Path(staging[0] / guid).write_bytes(payload[:size])
        update = {"guid": guid, "state": "completed" if outcome == "unknown" and count > 1 else state}
        if outcome != "unknown":
            update.update(receivedBytes=size, totalBytes=len(payload))
        session.handlers["Browser.downloadProgress"](update)
        if count == 1:
            ticks.append(2)
        if outcome == "stop":
            adapter.stop_event.set()

    page.wait_for_timeout = tick
    button = SimpleNamespace(click=click, count=lambda: 1)
    monkeypatch.setattr(adapter, "_card_locator", lambda *args: button)
    return adapter, events, session


def test_browser_uses_real_cdp_bytes_and_validates_before_completed(tmp_path, monkeypatch):
    adapter, events, _ = browser_fixture(tmp_path, monkeypatch)
    result = adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert events[0]["download_status"] == "queued"
    progress = next(event for event in events if event["download_status"] == "downloading" and event["received"] > 0)
    assert progress["received"] == len(zip_bytes()) // 2
    assert progress["speed"] == progress["received"] / 0.25
    assert progress["eta"] == (len(zip_bytes()) - progress["received"]) / progress["speed"]
    assert events[-2]["download_status"] == "validating"
    assert_terminal(events[-1], "completed")
    assert Path(events[-1]["local_path"]) == result[0].attachment_paths[0].resolve()
    assert {event["source_url"] for event in events} == {f"{CHANNEL}/7"}
    assert all(event["image_paths"] == [] for event in events)
    state = json.loads((adapter.download_dir / ".telegram_archive_processed.json").read_text())
    assert state == {"fixture_channel": [7]}


@pytest.mark.parametrize("outcome,status", [("start_timeout", "failed"), ("timeout", "failed"),
                                             ("canceled", "cancelled"), ("stop", "cancelled")])
def test_browser_failures_and_cancellation_have_terminal_events(tmp_path, monkeypatch, outcome, status):
    adapter, events, session = browser_fixture(tmp_path, monkeypatch, outcome=outcome)
    with pytest.raises(RuntimeError):
        adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert_terminal(events[-1], status)
    assert events[-1]["error"]
    assert not any(event["download_status"] == "completed" for event in events)
    assert not (adapter.download_dir / ".telegram_archive_processed.json").exists()
    if outcome in {"timeout", "stop"}:
        assert any(call[0] == "Browser.cancelDownload" for call in session.calls)


def test_browser_failed_rar_reports_quarantine_path(tmp_path, monkeypatch):
    adapter, events, _ = browser_fixture(tmp_path, monkeypatch, payload=b"not rar", name="asset.rar")
    with pytest.raises(RuntimeError):
        adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert_terminal(events[-1], "failed")
    retained = Path(events[-1]["local_path"])
    assert retained.is_absolute() and ".unverified" in retained.parts
    assert retained.read_bytes() == b"not rar"
    assert "ArchiveValidationError" in events[-1]["error"]


def test_browser_copy_failure_reports_original_error_and_quarantine(tmp_path, monkeypatch):
    adapter, events, _ = browser_fixture(tmp_path, monkeypatch)

    def fail_copy(*args):
        raise OSError("copy denied by fixture")

    monkeypatch.setattr(adapters.shutil, "copyfile", fail_copy)
    with pytest.raises(RuntimeError):
        adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert_terminal(events[-1], "failed")
    assert "copy denied by fixture" in events[-1]["error"]
    path = Path(events[-1]["local_path"])
    assert ".unverified" in path.parts and path.read_bytes() == zip_bytes()


def test_browser_unknown_bytes_are_not_synthetic_progress(tmp_path, monkeypatch):
    adapter, events, _ = browser_fixture(tmp_path, monkeypatch, outcome="unknown")
    adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    progress = [event for event in events if event["download_status"] == "downloading"]
    assert all(event["received"] == event["size"] == event["speed"] == 0 for event in progress)
    assert all(event["eta"] is None for event in progress)
    assert_terminal(events[-1], "completed")
    assert events[-1]["size"] == len(zip_bytes())


def test_browser_callback_exception_does_not_break_download(tmp_path, monkeypatch):
    states = []

    def broken(event):
        states.append(event["download_status"])
        raise ValueError("consumer failed")

    adapter, _, _ = browser_fixture(tmp_path, monkeypatch, callback=broken)
    assert adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert states[-1] == "completed"


def test_browser_aborted_batch_does_not_leave_later_files_queued(tmp_path, monkeypatch):
    adapter, events, _ = browser_fixture(tmp_path, monkeypatch, outcome="start_timeout")

    def scan(*args):
        adapter._scanned_snapshots = {
            mid: MessageSnapshot(mid, "data-mid", str(mid), "Asset title", (ArchiveCard("asset.zip", "document"),))
            for mid in (7, 8)}
        return [7, 8]

    monkeypatch.setattr(adapter, "_scan_archive_ids", scan)
    with pytest.raises(RuntimeError):
        adapter.fetch_messages("", [], CHANNEL, "recent", 2)
    assert [event["download_status"] for event in events[:2]] == ["queued", "queued"]
    terminal = [event for event in events if event["download_status"] in TERMINAL]
    assert [(event["message_id"], event["download_status"]) for event in terminal] == [(7, "failed"), (8, "cancelled")]
    assert all(event["speed"] == 0 and event["eta"] is None for event in terminal)


def test_browser_monitor_filters_old_guids_and_isolates_observer_errors(tmp_path):
    session, calls = Session(), []
    context = SimpleNamespace(browser=SimpleNamespace(new_browser_cdp_session=lambda: session))

    def broken(entry):
        calls.append(entry.copy())
        entry["receivedBytes"] = -1
        raise ValueError("observer failed")

    monitor = browser_download.BrowserDownloadMonitor(object(), context, tmp_path, on_progress=broken)
    monitor._begin({"guid": "old", "url": "old"})
    monitor.active_guids = {"old"}
    monitor._begin({"guid": "current", "url": "current"})
    monitor._progress({"guid": "old", "receivedBytes": 500})
    monitor._progress({"guid": "current", "receivedBytes": 3, "totalBytes": 10, "state": "inProgress"})
    monitor._progress({"guid": "current", "receivedBytes": 10, "totalBytes": 10, "state": "completed"})
    assert [entry.get("receivedBytes", 0) for entry in calls] == [0, 3, 10]
    assert monitor.entries["current"]["receivedBytes"] == 10
