from __future__ import annotations

import asyncio
import io
import zipfile
from collections import deque
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from telethon.tl.types import MessageMediaPhoto, PhotoSize, PhotoSizeProgressive, PhotoStrippedSize, VideoSize

from xianyu_pipeline.adapters import TelethonTelegramAdapter
from xianyu_pipeline.browser_download import DownloadCancelled


def media():
    return SimpleNamespace(sizes=[
        PhotoStrippedSize(type="i", bytes=b"tiny"),
        PhotoSize(type="s", w=32, h=40, size=800),
        PhotoSize(type="m", w=320, h=400, size=12000),
        PhotoSize(type="y", w=960, h=1280, size=120000),
        VideoSize(type="v", w=1080, h=1920, size=1200000),
    ])


def test_medium_and_full_size_selection_excludes_tiny_and_video_sizes():
    assert TelethonTelegramAdapter._catalog_photo_size(media()).type == "m"
    assert TelethonTelegramAdapter._catalog_photo_size(media(), full=True).type == "y"
    progressive = PhotoSizeProgressive(type="w", w=1200, h=1600, sizes=[1000, 20000])
    photo = media()
    photo.sizes.append(progressive)
    assert TelethonTelegramAdapter._catalog_photo_size(photo, full=True) is progressive


def test_catalog_medium_and_on_demand_full_use_distinct_quality_and_longer_timeouts(tmp_path, monkeypatch):
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session", tmp_path / "inbox")
    photo = SimpleNamespace(id=9, photo=media(), media=MessageMediaPhoto(), grouped_id=None)
    archive = SimpleNamespace(id=10)
    requests = []

    async def download(client, medium, mid, timeout, directory, thumb=-1):
        requests.append((thumb.type, timeout))
        return str(directory / f"{mid}-{thumb.type}.jpg")

    monkeypatch.setattr(adapter, "_download_preview", download)
    preview = asyncio.run(adapter._catalog_cover(None, "example_channel", archive, tmp_path, [photo]))
    full = asyncio.run(adapter._catalog_cover(None, "example_channel", archive, tmp_path, [photo], full=True))
    assert preview["cover_quality"] == "preview"
    assert full["full_status"] == "ready" and full["full_quality"] == "full"
    assert requests == [("m", 30), ("y", 90)]
    assert full["full_cover_path"] != preview["cover_path"]


def test_cached_picture_lookup_and_large_then_original(tmp_path, monkeypatch):
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "session", tmp_path / "inbox")
    picture = media()
    picture.sizes.append(PhotoSize(type="w", w=2400, h=3200, size=500000))
    photo = SimpleNamespace(id=9, photo=picture, media=MessageMediaPhoto(), grouped_id=None)
    class Client:
        async def get_messages(self, target, *, ids):
            assert ids == 9
            return photo
    sizes = []
    async def download(*args, **kwargs):
        sizes.append(kwargs["thumb"].type)
        return str(tmp_path / "image.jpg")
    monkeypatch.setattr(adapter, "_download_preview", download)
    async def run():
        large = await adapter._catalog_cover(Client(), "channel", SimpleNamespace(id=10), tmp_path, None,
                                             full=True, original=False, cover_message_id=9)
        original = await adapter._catalog_cover(Client(), "channel", SimpleNamespace(id=10), tmp_path, None,
                                                full=True, original=True, cover_message_id=9)
        assert large["full_quality"] == "large"
        assert original["full_quality"] == "full"
    asyncio.run(run())
    assert sizes == ["y", "w"]


def test_progressive_full_preview_uses_type_accepted_by_real_telethon(tmp_path):
    from telethon.client.downloads import DownloadMethods

    size = PhotoSizeProgressive(type="w", w=1200, h=1600, sizes=[1000, 20000])
    photo = media()
    photo.sizes.append(size)
    assert DownloadMethods._get_thumb(photo.sizes, size) is None
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session", tmp_path / "inbox")

    class Client:
        async def download_media(self, medium, file, thumb):
            assert thumb == "w"
            assert DownloadMethods._get_thumb(medium.sizes, thumb) is size
            Path(file).write_bytes(b"\xff\xd8\xfffixture")
            return file

    result = asyncio.run(adapter._download_preview(
        Client(), photo, 9, 1, tmp_path / "previews",
        thumb=adapter._catalog_photo_size(photo, full=True)))
    assert Path(result).read_bytes().startswith(b"\xff\xd8\xff")


def test_preview_retries_in_memory_when_telethon_returns_none_without_file(tmp_path):
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session", tmp_path / "inbox")

    class Client:
        def __init__(self):
            self.calls = []

        async def download_media(self, media, file, thumb):
            self.calls.append(file)
            if file is bytes:
                return b"\x89PNG\r\n\x1a\nfixture"
            return None

    client = Client()
    result = asyncio.run(adapter._download_preview(client, object(), 11, 1, tmp_path / "previews", thumb=-1))
    assert Path(result).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert client.calls[1] is bytes


def test_preview_accepts_written_file_when_telethon_returns_none_or_changes_suffix(tmp_path):
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session", tmp_path / "inbox")

    class Client:
        async def download_media(self, media, file, thumb):
            actual = Path(file).with_suffix(".png")
            actual.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            return None

    result = asyncio.run(adapter._download_preview(
        Client(), object(), 9, 1, tmp_path / "previews", thumb=-1))
    path = Path(result)
    assert path.suffix == ".png"
    assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def concurrent_fixture(tmp_path, monkeypatch, *, stop=False, album_delay=0.01):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("fixture.txt", "parallel download fixture")
    payload = stream.getvalue()
    photos, items = [], []
    for index in range(5):
        mid = 100 - index * 7
        archive = SimpleNamespace(id=mid, post=True, fwd_from=None, message="fixture",
                                  file=SimpleNamespace(name=f"{mid}.zip", size=len(payload)), date=None,
                                  document=None)
        items.append(archive)
        for offset in range(1, 7):
            photo = SimpleNamespace(id=mid - offset, post=True, fwd_from=None, message="",
                                    file=None, photo=media(), media=MessageMediaPhoto(), grouped_id=mid)
            items.append(photo)
            photos.append(photo)
    events, jobs, completed = [], deque(), []
    signals = {"scanning": True, "preview_started": False, "file_started": False,
               "file_done": False, "full_done": False, "lookup": 0}
    stop_event = Event()

    class Client:
        disconnected = False

        def connect(self):
            pass

        def disconnect(self):
            self.disconnected = True

        def is_user_authorized(self):
            return True

        def iter_messages(self, target, **options):
            async def history():
                for item in items:
                    await asyncio.sleep(0.002)
                    yield item
                signals["scanning"] = False

            return history()

        async def get_messages(self, target, ids=None, *, limit=None, offset_id=None):
            signals["lookup"] += 1
            if ids is not None:
                return next(item for item in items if item.id == ids)
            return [item for item in items if item.id < offset_id][:limit]

        async def download_media(self, item, file, progress_callback):
            assert signals["preview_started"], "File request must run while previews are active"
            signals["file_started"] = True
            Path(file).write_bytes(payload[:10])
            progress_callback(10, len(payload))
            if stop:
                stop_event.set()
                await asyncio.sleep(100)
            await asyncio.sleep(0.25)
            Path(file).write_bytes(payload)
            progress_callback(len(payload), len(payload))
            return file

    client = Client()
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session",
                                      tmp_path / "inbox", on_event=events.append, stop_event=stop_event)
    monkeypatch.setattr(adapter, "_client", lambda: client)
    scans = []

    async def picture(client, medium, mid, timeout, directory=None, thumb=-1):
        if not signals["preview_started"]:
            assert signals["scanning"], "The first preview must start before scanning finishes"
        signals["preview_started"] = True
        if getattr(thumb, "type", "") == "y":
            signals["full_done"] = True
        else:
            await asyncio.sleep(album_delay if thumb == -1 else 0.12)
        directory = directory or adapter.download_dir / ".previews"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{mid}-{getattr(thumb, 'type', 'album')}.jpg"
        path.write_bytes(b"\xff\xd8\xfffixture")
        return str(path)

    monkeypatch.setattr(adapter, "_download_preview", picture)

    def progress(event):
        scans.append(event)
        if event["stage"] == "covers":
            signals["scanning"] = False

    def publish(page):
        if not jobs and not signals["file_started"]:
            jobs.extend([{"kind": "download", "message_id": 100, "filename": "100.zip"},
                         {"kind": "cover", "message_id": 100, "filename": "100.zip"}])

    def done(request, result, error):
        completed.append((request["kind"], result, error))

    return adapter, client, jobs, events, signals, publish, progress, done, completed


def test_scanning_previews_full_image_and_file_download_overlap_in_one_client(tmp_path, monkeypatch):
    adapter, client, jobs, events, signals, publish, progress, done, completed = concurrent_fixture(tmp_path, monkeypatch)
    result = adapter.list_archives("example_channel", limit=5, preview_dir=tmp_path / "covers",
                                  on_page=publish, on_progress=progress,
                                  take_request=lambda: jobs.popleft() if jobs else None,
                                  close_requests=lambda: not jobs, on_request_done=done)
    assert len(result["messages"]) == 5 and client.disconnected
    assert signals["preview_started"] and signals["file_started"] and signals["full_done"]
    assert any(event["download_status"] == "completed" for event in events)
    assert any(event.get("image_source") == "preceding_album" for event in events)
    assert next(event for event in events if event["download_status"] == "completed")["image_paths"]
    with zipfile.ZipFile(tmp_path / "inbox" / "100.zip") as archive:
        assert archive.testzip() is None
    assert {kind for kind, _, _ in completed} == {"cover", "download"}
    assert all(not error for _, _, error in completed)


def test_stop_drains_concurrent_file_and_photo_tasks_and_keeps_partial_file(tmp_path, monkeypatch):
    adapter, client, jobs, events, signals, publish, progress, done, completed = concurrent_fixture(
        tmp_path, monkeypatch, stop=True)
    with pytest.raises(DownloadCancelled):
        adapter.list_archives("example_channel", limit=5, preview_dir=tmp_path / "covers",
                              on_page=publish, take_request=lambda: jobs.popleft() if jobs else None,
                              close_requests=lambda: not jobs, on_request_done=done)
    assert client.disconnected and not (tmp_path / "inbox" / "100.zip").exists()
    assert any(event["download_status"] == "cancelled" for event in events)
    assert list((tmp_path / "inbox" / ".unverified").rglob("100.zip"))


def test_file_completion_does_not_wait_for_a_slow_album(tmp_path, monkeypatch):
    adapter, client, jobs, events, signals, publish, progress, done, completed = concurrent_fixture(
        tmp_path, monkeypatch, album_delay=0.15)
    adapter.list_archives("example_channel", limit=5, preview_dir=tmp_path / "covers",
                          on_page=publish, on_progress=progress,
                          take_request=lambda: jobs.popleft() if jobs else None,
                          close_requests=lambda: not jobs, on_request_done=done)
    completed_events = [event for event in events if event["download_status"] == "completed"]
    assert not completed_events[0]["image_paths"], "File status must not wait for slow pictures"
    assert completed_events[-1]["image_paths"], "Pictures must arrive without resetting completed file bytes"
    assert completed_events[-1]["received"] == completed_events[-1]["size"] > 0
