from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from telethon.tl.types import MessageMediaPhoto

from xianyu_pipeline import adapters
from test_transfer_events import CHANNEL, PNG, api_fixture, assert_terminal, message


def photo(mid, group=9007199254740993123):
    media = SimpleNamespace(id=mid)
    return SimpleNamespace(id=mid, photo=media, media=MessageMediaPhoto(photo=media), grouped_id=group)


def album_fixture(tmp_path, monkeypatch, history, *, thumbnail=False, **options):
    archive = message(100)
    if thumbnail:
        archive.document = SimpleNamespace(id=100, thumbs=[object()])
    adapter, client, events = api_fixture(tmp_path, monkeypatch, [archive], **options)
    original = client.download_media
    client.lookups, client.previews = [], []

    async def get_messages(target, *, limit, offset_id):
        assert target == "fixture_channel" and limit == 6 and offset_id == 100
        client.lookups.append(offset_id)
        return history[:limit]

    async def download_media(media, file, progress_callback=None, thumb=None):
        if thumb is None:
            return await original(media, file, progress_callback)
        assert thumb == -1
        client.previews.append(media.id)
        Path(file).write_bytes(PNG)
        return file

    client.get_messages, client.download_media = get_messages, download_media
    return adapter, client, events


@pytest.mark.parametrize("count", range(1, 6))
@pytest.mark.parametrize("mode", ["recent", "message_ids"])
def test_preceding_album_in_message_order_with_gaps(tmp_path, monkeypatch, count, mode):
    photos = [photo(10 + i * 3) for i in range(count)]
    adapter, client, events = album_fixture(tmp_path, monkeypatch, list(reversed(photos)), thumbnail=True)
    lookup = client.get_messages

    async def messages(target, ids=None, **kwargs):
        return client.items[0] if ids is not None else await lookup(target, **kwargs)

    # Explicit-ID scans are synchronous, while preview history runs inside the loop.
    def sync_messages(target, ids=None, **kwargs):
        return client.items[0] if ids is not None else messages(target, **kwargs)

    client.get_messages = sync_messages
    adapter.fetch_messages("", [100] if mode == "message_ids" else [], CHANNEL, mode, 1)
    expected = [item.id for item in photos]
    assert client.previews == expected and client.lookups == [100]
    assert client.downloaded == [100]
    completed = events[-1]
    assert_terminal(completed, "completed")
    assert completed["image_source"] == "preceding_album"
    assert completed["image_message_ids"] == expected
    assert completed["image_group_id"] == str(photos[0].grouped_id)
    assert len(completed["image_paths"]) == count
    for path in completed["image_paths"]:
        assert Path(path).suffix == ".png" and Path(path).read_bytes() == PNG
    preview_event = next(event for event in events if event["image_paths"])
    assert preview_event["download_status"] == "queued" and preview_event["received"] == 0


@pytest.mark.parametrize("boundary", [None, message(90), SimpleNamespace(id=90, photo=None),
                                     SimpleNamespace(id=90, photo=object(), media=object())])
def test_never_skip_text_files_web_previews_or_service_messages(tmp_path, monkeypatch, boundary):
    adapter, client, events = album_fixture(tmp_path, monkeypatch, [boundary, photo(80)], thumbnail=True)
    adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert client.previews == [100]
    assert events[-1]["image_source"] == "document_thumbnail"
    assert events[-1]["image_message_ids"] == [100]


@pytest.mark.parametrize("history,expected", [
    ([photo(90), photo(80), photo(70, 999)], [80, 90]),
    ([photo(90, None), photo(80, None)], [90]),
    ([photo(90), message(80), photo(70)], [90]),
    ([photo(90), photo(80, None), photo(70)], [90]),
    ([photo(i) for i in range(90, 20, -10)], [50, 60, 70, 80, 90]),
    ([photo(100), photo(90)], []),
    ([], []),
])
def test_album_boundaries_and_five_image_limit(tmp_path, monkeypatch, history, expected):
    adapter, client, events = album_fixture(tmp_path, monkeypatch, history)
    adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert client.previews == expected and events[-1]["image_message_ids"] == expected
    if expected == [90] and history[0].grouped_id is None:
        assert events[-1]["image_source"] == "preceding_photo"
    assert_terminal(events[-1], "completed")


@pytest.mark.parametrize("outcome", ["lookup_error", "lookup_timeout", "photo_error", "photo_timeout", "video", "large"])
def test_best_effort_fallback_and_partial_file_cleanup(tmp_path, monkeypatch, outcome):
    adapter, client, events = album_fixture(tmp_path, monkeypatch, [photo(90)], thumbnail=True)
    original_lookup, original_download = client.get_messages, client.download_media
    monkeypatch.setattr(adapters, "_PHOTO_LOOKUP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(adapters, "_PHOTO_TIMEOUT_SECONDS", 0.01)

    async def lookup(*args, **kwargs):
        if outcome == "lookup_error":
            raise OSError("lookup unavailable")
        if outcome == "lookup_timeout":
            await asyncio.sleep(60)
        return await original_lookup(*args, **kwargs)

    async def download(media, file, **kwargs):
        if media.id != 90:
            return await original_download(media, file, **kwargs)
        Path(file).write_bytes(PNG)
        if outcome == "photo_error":
            raise OSError("photo unavailable")
        if outcome == "photo_timeout":
            await asyncio.sleep(60)
        if outcome == "video":
            Path(file).write_bytes(b"not an image")
        if outcome == "large":
            Path(file).write_bytes(PNG + b"0" * (12 * 1024 * 1024))
        return file

    client.get_messages, client.download_media = lookup, download
    adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert_terminal(events[-1], "completed")
    assert events[-1]["image_source"] == "document_thumbnail"
    assert client.downloaded == [100]
    assert len(list((adapter.download_dir / ".previews").iterdir())) == 1


@pytest.mark.parametrize("failure", ["one_photo", "total_timeout"])
def test_partial_album_keeps_successful_photos(tmp_path, monkeypatch, failure):
    adapter, client, events = album_fixture(tmp_path, monkeypatch, [photo(90), photo(80)], thumbnail=True)
    original = client.download_media
    monkeypatch.setattr(adapters, "_ALBUM_TIMEOUT_SECONDS", 0.02)

    async def download(media, file, **kwargs):
        if media.id == 90:
            Path(file).write_bytes(b"partial")
            if failure == "total_timeout":
                await asyncio.sleep(60)
            raise OSError("one photo unavailable")
        return await original(media, file, **kwargs)

    client.download_media = download
    adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert_terminal(events[-1], "completed")
    assert events[-1]["image_message_ids"] == [80]
    assert events[-1]["image_source"] == "preceding_album"
    assert client.previews == [80]
    assert len(list((adapter.download_dir / ".previews").iterdir())) == 1


def test_cli_has_no_photo_lookup_or_preview_requests(tmp_path, monkeypatch):
    adapter, client, _ = album_fixture(tmp_path, monkeypatch, [photo(90)], thumbnail=True)
    adapter.on_event = None
    adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert not client.lookups and not client.previews and client.downloaded == [100]


def test_stop_cancels_preview_and_never_starts_archive(tmp_path, monkeypatch):
    stop = Event()
    adapter, client, events = album_fixture(tmp_path, monkeypatch, [photo(90), photo(80)], stop_event=stop)
    original = client.download_media
    cancelled = []

    async def download(media, file, **kwargs):
        if media.id != 90:
            return await original(media, file, **kwargs)
        Path(file).write_bytes(b"partial")
        stop.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)

    client.download_media = download
    with pytest.raises(RuntimeError, match="停止"):
        adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert cancelled == [True] and not client.downloaded
    assert_terminal(events[-1], "cancelled")
    assert not list((adapter.download_dir / ".previews").iterdir())


def test_rar_previews_do_not_depend_on_archive_extraction(tmp_path, monkeypatch):
    adapter, client, events = album_fixture(tmp_path, monkeypatch, [photo(90)])
    client.items[0].file.name = "asset.rar"
    validations = []
    monkeypatch.setattr(adapters, "validate_rar", lambda *args: validations.append(args[0]))
    adapter.fetch_messages("", [], CHANNEL, "recent", 1)
    assert validations and events[-1]["image_message_ids"] == [90]
    assert_terminal(events[-1], "completed")
