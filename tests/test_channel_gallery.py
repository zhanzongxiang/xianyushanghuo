from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from telethon.tl.types import MessageMediaPhoto, PhotoSize

from xianyu_pipeline.adapters import TelethonTelegramAdapter
from xianyu_pipeline.channel_catalog import ChannelCatalog
from test_channel_media import media
from test_workbench_actions import app
from test_image_recovery import image_server


@pytest.fixture
def album(tmp_path, monkeypatch):
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "session", tmp_path / "inbox")
    pictures = []
    for mid in range(97, 100):
        picture = media()
        picture.sizes.append(PhotoSize(type="w", w=2400, h=3200, size=500000))
        pictures.append(SimpleNamespace(id=mid, photo=picture, media=MessageMediaPhoto(), grouped_id=500))
    message = SimpleNamespace(id=100, file=SimpleNamespace(name="model.zip", size=1), document=None)
    fx = SimpleNamespace(adapter=adapter, message=message, photos=pictures, requests=[], failed=set(),
                         active=0, maximum=0, progress=[], root=tmp_path / "previews")
    fx.root.mkdir()

    class Client:
        def connect(self):
            pass

        def disconnect(self):
            pass

        def is_user_authorized(self):
            return True

        def get_messages(self, target, *, ids=None, limit=None, offset_id=None):
            if ids is not None:
                assert ids == 100
                return message
            assert limit == 6 and offset_id == 100
            return list(reversed(fx.photos))

    fx.client = Client()
    monkeypatch.setattr(adapter, "_client", lambda: fx.client)

    async def download(client, medium, mid, timeout, directory, thumb=-1):
        fx.requests.append((mid, thumb.type))
        fx.active += 1
        fx.maximum = max(fx.maximum, fx.active)
        try:
            await asyncio.sleep(.02)
            if mid in fx.failed:
                raise TimeoutError("fixture timeout")
            path = directory / f"{mid}-{thumb.type}.jpg"
            path.write_bytes(b"\xff\xd8\xfffixture")
            return str(path)
        finally:
            fx.active -= 1

    monkeypatch.setattr(adapter, "_download_preview", download)
    return fx


def fetch(fx, **options):
    return asyncio.run(fx.adapter._catalog_album(fx.client, "fixture_channel", fx.message, fx.root,
                                                on_progress=fx.progress.append, **options))


def test_all_associated_images_keep_order_and_publish_partial_progress(album):
    result = fetch(album)
    assert [image["message_id"] for image in result["full_images"]] == [97, 98, 99]
    assert result["full_status"] == "ready" and result["full_cover_message_id"] == 97
    assert album.maximum == 2 and len(album.requests) == 3
    assert [row["full_status"] for row in album.progress[0]["full_images"]] == ["loading"] * 3
    assert any(0 < sum(item["full_status"] == "ready" for item in row["full_images"]) < 3
               for row in album.progress)


def test_partial_album_retry_keeps_successful_images_and_upgrades_only_selected(album):
    album.failed.add(98)
    result = fetch(album)
    assert result["full_status"] == "failed"
    assert [item["full_status"] for item in result["full_images"]] == ["ready", "failed", "ready"]
    album.failed.clear()
    album.requests.clear()
    repaired = fetch(album, cached=result["full_images"], photo_id=98)
    assert album.requests == [(98, "y")] and repaired["full_status"] == "ready"
    album.requests.clear()
    original = fetch(album, cached=repaired["full_images"], original=True, photo_id=99)
    assert album.requests == [(99, "w")]
    assert [image["full_quality"] for image in original["full_images"]] == ["large", "large", "full"]


def test_album_revalidates_group_and_rejects_other_images(album):
    first = fetch(album)
    album.photos[0].grouped_id = 999
    album.requests.clear()
    result = fetch(album, cached=first["full_images"])
    assert [image["message_id"] for image in result["full_images"]] == [98, 99]
    assert not album.requests
    with pytest.raises(ValueError, match="不属于"):
        fetch(album, photo_id=97)


def test_single_document_thumbnail_and_no_images(album):
    album.photos.clear()
    with pytest.raises(ValueError, match="没有可用"):
        fetch(album)
    album.message.document = SimpleNamespace(thumbs=media().sizes)
    result = fetch(album)
    assert len(result["full_images"]) == 1
    assert result["full_images"][0]["message_id"] == 100
    assert result["full_images"][0]["full_quality"] == "document_thumbnail"


def test_cancelling_album_drains_pending_downloads(album):
    async def check():
        task = asyncio.create_task(album.adapter._catalog_album(album.client, "fixture_channel", album.message, album.root))
        while not album.active:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert album.active == 0
    asyncio.run(check())


def test_media_queue_streams_album_without_finishing_request_early(album):
    jobs = deque([{"kind": "cover", "gallery": True, "message_id": 100, "filename": "model.zip"}])
    progress, completed = [], []
    album.adapter.list_archives("fixture_channel", preview_dir=album.root, media_only=True,
        take_request=lambda: jobs.popleft() if jobs else None, close_requests=lambda: not jobs,
        on_request_progress=lambda request, result: progress.append(result),
        on_request_done=lambda request, result, error: completed.append((result, error)))
    assert len(progress) == 4 and len(completed) == 1
    assert completed[0][0]["full_status"] == "ready" and not completed[0][1]


def seed_catalog(output, scope="fixture_channel"):
    catalog = ChannelCatalog(output)
    images = []
    for mid in (97, 98, 99):
        path = catalog.preview_root / scope / f"{mid}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xd8\xff" + str(mid).encode())
        images.append({"message_id": mid, "full_cover_path": str(path), "full_status": "ready",
                       "full_cover_message_id": mid, "full_quality": "large"})
    catalog.merge(scope, {"messages": [{"message_id": 100, "chat": scope.split("~")[0], "filename": "model.zip",
        "cover_path": images[0]["full_cover_path"], "cover_quality": "preview", "cover_status": "ready",
        "cover_message_id": 97, "full_images": images}], "oldest_scanned_id": 95, "has_more": True})
    return catalog, images


def test_gallery_cache_is_scoped_persistent_and_does_not_expose_paths(tmp_path):
    catalog, images = seed_catalog(tmp_path, "-1001234567890~42")
    listing = catalog.snapshot("-1001234567890~42")
    assert str(tmp_path) not in json.dumps(listing)
    row = listing["messages"][0]
    assert len(row["full_images"]) == 3
    assert "photo=98" in row["full_images"][1]["full_cover_url"]
    assert catalog.preview("-1001234567890~42", 100, full=True, photo_id=98)[0] == Path(images[1]["full_cover_path"])
    for scope, mid in (("-1001234567890~43", 98), ("-1001234567890~42", 101)):
        with pytest.raises(ValueError):
            catalog.preview(scope, 100, full=True, photo_id=mid)
    Path(images[1]["full_cover_path"]).unlink()
    row = ChannelCatalog(tmp_path).snapshot("-1001234567890~42")["messages"][0]
    assert row["full_images"][1]["full_status"] == "failed"
    assert not row["full_images"][1]["full_cover_url"] and row["full_images"][0]["full_cover_url"]
    images[2]["full_cover_path"] = str(tmp_path / "private.jpg")
    (tmp_path / "private.jpg").write_bytes(b"\xff\xd8\xffprivate")
    catalog.update_cover("-1001234567890~42", 100, full_images=images)
    with pytest.raises(ValueError):
        catalog.preview("-1001234567890~42", 100, full=True, photo_id=99)


def test_interrupted_gallery_retains_ready_images_and_recovers_pending(tmp_path):
    catalog, images = seed_catalog(tmp_path)
    images[1].update(full_status="loading", full_cover_path="")
    catalog.update_cover("fixture_channel", 100, full_images=images, full_status="loading")
    catalog.recover_interrupted()
    row = catalog.snapshot("fixture_channel")["messages"][0]
    assert row["full_images"][0]["full_status"] == "ready"
    assert row["full_images"][1]["full_status"] == "cancelled"
    assert row["full_images"][2]["full_status"] == "ready"


def test_gallery_http_cache_and_selected_photo_validation(app):
    app.acquire()
    catalog, _ = seed_catalog(app.store.config.output_dir)
    assert app.post("/api/channel-cover", {"id": 100, "gallery": True})[1]["message"] == "图集已缓存"
    assert not app.workers
    for extra in ({"gallery": "yes"}, {"gallery": True, "photo_id": 123}, {"gallery": True, "photo_id": True}, {"photo_id": 98}):
        assert app.post("/api/channel-cover", {"id": 100, **extra})[0] == 400
    assert not app.workers
    assert app.post("/api/channel-cover", {"id": 100, "gallery": True, "photo_id": 98, "original": True})[0] == 200
    request = app.state.take_channel_request()
    assert request["gallery"] and request["photo_id"] == 98 and len(request["cached_images"]) == 3


def test_legacy_single_cover_does_not_prevent_gallery_discovery(app):
    app.acquire()
    catalog, images = seed_catalog(app.store.config.output_dir)
    catalog.update_cover("fixture_channel", 100, full_images=[], **{
        key: value for key, value in images[0].items() if key.startswith("full_")})
    assert app.post("/api/channel-cover", {"id": 100, "gallery": True})[0] == 200
    assert len(app.workers) == 1
    assert app.state.take_channel_request()["gallery"]


def test_gallery_http_serves_only_associated_photo(image_server):
    address, base = image_server
    catalog, _ = seed_catalog(base / "output", "example_channel")
    listing = catalog.snapshot("example_channel")["messages"][0]
    url = listing["full_images"][1]["full_cover_url"]
    with urlopen(address + url) as response:
        assert response.read() == b"\xff\xd8\xff98"
    with pytest.raises(HTTPError) as error:
        urlopen(address + url.replace("photo=98", "photo=777"))
    assert error.value.code == 404
