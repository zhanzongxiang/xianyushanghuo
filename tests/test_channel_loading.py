import asyncio
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from telethon.tl.types import MessageMediaPhoto

from xianyu_pipeline.adapters import TelethonTelegramAdapter
from xianyu_pipeline.browser_download import DownloadCancelled
from xianyu_pipeline.channel_catalog import ChannelCatalog
from xianyu_pipeline.web_ui import WebState


def listing_client(tmp_path, monkeypatch, count=16):
    history = []
    for index in range(count):
        mid = 1000 - index * 7
        history.append(SimpleNamespace(id=mid, post=True, fwd_from=None, message=f"fixture {index}",
                                       file=SimpleNamespace(name=f"asset-{mid}.zip", size=123), date=None,
                                       document=None))
        for offset in range(1, 7):
            history.append(SimpleNamespace(id=mid - offset, post=True, fwd_from=None, file=None,
                                           message="", photo=object(), media=MessageMediaPhoto(),
                                           grouped_id=mid, date=None))

    class Client:
        connected = False
        lookups = 0

        def connect(self):
            self.connected = True

        def disconnect(self):
            self.connected = False

        def is_user_authorized(self):
            return True

        def iter_messages(self, target, **options):
            return iter(history[:options["limit"]])

        async def get_messages(self, target, limit, offset_id):
            self.lookups += 1
            return [item for item in history if item.id < offset_id][:limit]

    client = Client()
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session",
                                      tmp_path / "inbox", stop_event=Event())
    monkeypatch.setattr(adapter, "_client", lambda: client)
    return adapter, client


def test_messages_publish_before_covers_and_downloads_have_bounded_concurrency(tmp_path, monkeypatch):
    adapter, client = listing_client(tmp_path, monkeypatch)
    active = peak = completed = 0
    metadata_pages = []
    progress = []
    catalog = ChannelCatalog(tmp_path / "output")

    async def picture(client, media, message_id, timeout, directory, thumb=-1):
        nonlocal active, peak, completed
        assert catalog.snapshot("example_channel")["messages"], "metadata must appear before image requests"
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.03)
            path = directory / f"{message_id}.jpg"
            path.write_bytes(b"\xff\xd8\xfffixture")
            completed += 1
            return str(path)
        finally:
            active -= 1

    def publish(page):
        if completed == 0:
            metadata_pages.append(page)
        catalog.merge("example_channel", page, older=True)

    monkeypatch.setattr(adapter, "_download_preview", picture)
    result = adapter.list_archives("example_channel", limit=16,
                                   preview_dir=catalog.preview_root / "example_channel",
                                   on_page=publish, on_progress=progress.append)
    assert metadata_pages and len(metadata_pages[0]["messages"]) <= 10
    assert len(result["messages"]) == 16
    assert peak == 4 and active == 0 and completed == 16
    assert client.lookups == 1, "preceding groups in scanned history must not be fetched again"
    assert not client.connected
    assert progress[-1]["stage"] == "completed"
    assert progress[-1]["processed"] == progress[-1]["ready"] == 16
    assert catalog.snapshot("example_channel")["oldest_scanned_id"] == result["oldest_scanned_id"]
    assert all(row["cover_url"] for row in catalog.snapshot("example_channel")["messages"])


def test_covers_reuse_valid_cache_and_skip_picture_lookups(tmp_path, monkeypatch):
    adapter, client = listing_client(tmp_path, monkeypatch, count=3)
    covers = {mid: {"cover_path": str(tmp_path / f"{mid}.jpg"),
                    "cover_message_id": mid - 1, "cover_quality": "full"}
              for mid in (1000, 993, 986)}
    progress = []

    async def forbidden(*args, **kwargs):
        pytest.fail("cached full photos must not be downloaded")

    monkeypatch.setattr(adapter, "_download_preview", forbidden)
    result = adapter.list_archives("example_channel", limit=3, preview_dir=tmp_path / "previews",
                                   known_covers=covers, on_progress=progress.append)
    assert len(result["messages"]) == 3 and client.lookups == 0
    assert progress[-1]["cached"] == progress[-1]["processed"] == progress[-1]["ready"] == 3


def test_single_cover_failure_does_not_fail_messages(tmp_path, monkeypatch):
    adapter, client = listing_client(tmp_path, monkeypatch, count=4)
    progress = []

    async def picture(client, media, message_id, timeout, directory, thumb=-1):
        if message_id == 988:
            raise TimeoutError("fixture photo unavailable")
        path = directory / f"{message_id}.jpg"
        path.write_bytes(b"\xff\xd8\xfffixture")
        return str(path)

    monkeypatch.setattr(adapter, "_download_preview", picture)
    result = adapter.list_archives("example_channel", limit=4, preview_dir=tmp_path / "previews",
                                   on_progress=progress.append)
    assert len(result["messages"]) == 4
    assert result["messages"][1]["cover_status"] == "failed"
    assert progress[-1]["stage"] == "completed" and progress[-1]["failed"] == 1
    assert progress[-1]["processed"] == 4 and progress[-1]["ready"] == 3
    assert not client.connected


def test_stopping_cancels_all_inflight_photos_and_keeps_metadata(tmp_path, monkeypatch):
    adapter, client = listing_client(tmp_path, monkeypatch)
    active = 0
    catalog = ChannelCatalog(tmp_path / "output")

    async def picture(client, media, message_id, timeout, directory, thumb=-1):
        nonlocal active
        active += 1
        try:
            await asyncio.sleep(0.03)
            path = directory / f"{message_id}.jpg"
            path.write_bytes(b"\xff\xd8\xfffixture")
            return str(path)
        finally:
            active -= 1

    def progress(event):
        if event["processed"] >= 3:
            adapter.stop_event.set()

    monkeypatch.setattr(adapter, "_download_preview", picture)
    with pytest.raises(DownloadCancelled):
        adapter.list_archives("example_channel", limit=16,
                              preview_dir=catalog.preview_root / "example_channel",
                              on_page=lambda page: catalog.merge("example_channel", page, older=True),
                              on_progress=progress)
    rows = catalog.snapshot("example_channel")["messages"]
    assert active == 0 and not client.connected
    assert len(rows) == 16
    assert 3 <= sum(bool(row["cover_url"]) for row in rows) < 16


def test_scan_progress_elapsed_time_freezes_at_completion(tmp_path, monkeypatch):
    state = WebState(tmp_path / "config.json")
    state.channel_scan_started = 100
    monkeypatch.setattr("xianyu_pipeline.web_ui.time.monotonic", lambda: 105)
    state.scan_progress({"stage": "scanning", "found": 10, "scanned": 50})
    assert state.snapshot()["channel_scan"]["elapsed_seconds"] == 5
    state.scan_progress({"stage": "completed"})
    monkeypatch.setattr("xianyu_pipeline.web_ui.time.monotonic", lambda: 150)
    assert state.snapshot()["channel_scan"]["elapsed_seconds"] == 5
