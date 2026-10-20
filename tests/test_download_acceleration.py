from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
import zipfile
from collections import deque
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from xianyu_pipeline import adapters, runtime
from xianyu_pipeline.adapters import TelethonTelegramAdapter
from xianyu_pipeline.browser_download import DownloadCancelled
from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.download_scheduler import DownloadScheduler


def payload():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("file.txt", "download acceleration fixture")
    return stream.getvalue()


def test_native_crypto_self_test_and_cli_without_config():
    assert runtime.check_download_acceleration() == {"crypto_backend": "cryptg", "self_test": "passed"}
    result = subprocess.run([sys.executable, "run_pipeline.py", "--config", "missing-config.json",
                             "--download-check"], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, check=True, timeout=15)
    assert json.loads(result.stdout) == {"crypto_backend": "cryptg", "self_test": "passed"}


@pytest.mark.parametrize("backend", ["cryptg", "OpenSSL", "Python"])
def test_crypto_status_reports_actual_backend(monkeypatch, backend):
    from telethon.crypto import aes

    monkeypatch.setattr(aes, "cryptg", object() if backend == "cryptg" else None)
    monkeypatch.setattr(aes.libssl, "encrypt_ige", object() if backend == "OpenSSL" else None)
    monkeypatch.setattr(aes.libssl, "decrypt_ige", object() if backend == "OpenSSL" else None)
    assert runtime.telegram_crypto_backend() == backend


@pytest.mark.parametrize("value", [0, 3, True, 1.5, "2", None])
def test_invalid_file_concurrency_rejected(value, tmp_path):
    with pytest.raises(ValueError, match="download_file_concurrency"):
        PipelineConfig(download_file_concurrency=value)
    with pytest.raises(ValueError, match="download_file_concurrency"):
        TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "session", tmp_path / "inbox",
                                download_file_concurrency=value)


def test_legacy_config_uses_dual_download_by_default(tmp_path):
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    assert PipelineConfig.from_json(config).download_file_concurrency == 2


def test_background_images_reduce_to_one_and_recover_after_files():
    async def run():
        scheduler = DownloadScheduler(2)
        file_started, file_finish, image_started, image_finish = (asyncio.Event() for _ in range(4))
        running = maximum = 0

        async def file():
            async with scheduler.file():
                file_started.set()
                await file_finish.wait()

        async def picture():
            nonlocal running, maximum
            async with scheduler.background():
                running += 1
                maximum = max(maximum, running)
                image_started.set()
                try:
                    await image_finish.wait()
                finally:
                    running -= 1

        file_task = asyncio.create_task(file())
        await file_started.wait()
        images = [asyncio.create_task(picture()) for _ in range(4)]
        await image_started.wait()
        await asyncio.sleep(0)
        assert running == maximum == 1
        file_finish.set()
        await file_task
        for _ in range(20):
            if running == 4:
                break
            await asyncio.sleep(0)
        assert running == maximum == 4
        image_finish.set()
        await asyncio.gather(*images)
        assert scheduler.background_active == scheduler.pending_files == 0

    asyncio.run(run())


def test_cancelled_waiting_file_and_image_release_capacity():
    async def run():
        scheduler = DownloadScheduler(1)
        async with scheduler.file(), scheduler.background():
            async def file():
                async with scheduler.file():
                    raise AssertionError("waiting file must stay queued")

            async def picture():
                async with scheduler.background():
                    raise AssertionError("background image must wait")

            jobs = [asyncio.create_task(file()), asyncio.create_task(picture())]
            await asyncio.sleep(0)
            assert scheduler.pending_files == 2
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            assert scheduler.pending_files == scheduler.background_active == 1
        assert scheduler.pending_files == scheduler.background_active == 0
        async with scheduler.file(), scheduler.background():
            pass

    asyncio.run(run())


def media_client(tmp_path, monkeypatch, *, file_concurrency=2, stop=False):
    content = payload()
    items = [SimpleNamespace(id=mid, post=True, fwd_from=None, message="fixture", document=None,
                             file=SimpleNamespace(name=f"{mid}.zip", size=len(content)), date=None)
             for mid in range(101, 105)]
    active, maximum, finished, cancelled, downloads, events = set(), [], set(), set(), [], []
    stop_event = Event()

    class Client:
        disconnected = False

        def connect(self):
            pass

        def disconnect(self):
            assert not active
            self.disconnected = True

        def is_user_authorized(self):
            return True

        def iter_messages(self, target, **options):
            return iter(items)

        def get_messages(self, target, ids=None, **options):
            return next(item for item in items if item.id == ids) if ids else []

        async def download_media(self, message, file, progress_callback):
            mid = message.id
            downloads.append(mid)
            active.add(mid)
            maximum.append(len(active))
            try:
                Path(file).write_bytes(content[:16])
                progress_callback(16, len(content))
                if stop:
                    if len(active) == file_concurrency:
                        stop_event.set()
                    await asyncio.Event().wait()
                await asyncio.sleep(0.02)
                Path(file).write_bytes(content)
                progress_callback(len(content), len(content))
                finished.add(mid)
                return file
            except asyncio.CancelledError:
                cancelled.add(mid)
                raise
            finally:
                active.remove(mid)

    client = Client()
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "session", tmp_path / "inbox",
                                      download_file_concurrency=file_concurrency,
                                      stop_event=stop_event, on_event=events.append)
    monkeypatch.setattr(adapter, "_client", lambda: client)
    return adapter, client, items, active, maximum, finished, cancelled, downloads, events, content


@pytest.mark.parametrize("entry", ["catalog", "recent"])
@pytest.mark.parametrize("concurrency", [1, 2])
def test_both_download_entry_points_obey_file_limit_and_validate(tmp_path, monkeypatch, entry, concurrency):
    adapter, client, items, active, maximum, finished, _, downloads, events, content = media_client(
        tmp_path, monkeypatch, file_concurrency=concurrency)
    if entry == "catalog":
        jobs = deque({"kind": "download", "message_id": item.id, "filename": item.file.name} for item in items)
        adapter.list_archives("fixture_channel", preview_dir=tmp_path / "previews", media_only=True,
                              take_request=lambda: jobs.popleft() if jobs else None,
                              close_requests=lambda: not jobs)
    else:
        result = adapter.fetch_messages("", [], "fixture_channel", "recent", 4)
        assert [message.message_id for message in result] == [104, 103, 102, 101]
    assert max(maximum) == concurrency and not active
    assert client.disconnected and len(downloads) == len(finished) == 4
    assert len([event for event in events if event["download_status"] == "completed"]) == 4
    for item in items:
        assert (adapter.download_dir / item.file.name).read_bytes() == content


@pytest.mark.parametrize("entry", ["catalog", "recent"])
def test_stopping_two_files_drains_writers_and_isolates_partial_bytes(tmp_path, monkeypatch, entry):
    adapter, client, items, active, maximum, finished, cancelled, downloads, events, content = media_client(
        tmp_path, monkeypatch, stop=True)
    with pytest.raises((RuntimeError, DownloadCancelled)):
        if entry == "catalog":
            jobs = deque({"kind": "download", "message_id": item.id, "filename": item.file.name} for item in items)
            adapter.list_archives("fixture_channel", preview_dir=tmp_path / "previews", media_only=True,
                                  take_request=lambda: jobs.popleft() if jobs else None,
                                  close_requests=lambda: not jobs)
        else:
            adapter.fetch_messages("", [], "fixture_channel", "recent", 4)
    assert max(maximum) == 2 and not active and not finished and client.disconnected
    assert len(downloads) == len(cancelled) == 2
    assert not list(adapter.download_dir.glob("*.zip"))
    partials = list((adapter.download_dir / ".unverified").rglob("*.zip"))
    assert len(partials) == 2 and all(path.read_bytes() == content[:16] for path in partials)
    assert not any(event["download_status"] == "completed" for event in events)


def test_one_catalog_file_failure_keeps_other_file_and_queue_running(tmp_path, monkeypatch):
    adapter, client, items, _, maximum, finished, _, _, events, _ = media_client(tmp_path, monkeypatch)
    original = client.download_media

    async def download(message, file, progress_callback):
        if message.id == 101:
            raise OSError("offline failure")
        return await original(message, file, progress_callback)

    client.download_media = download
    jobs = deque({"kind": "download", "message_id": item.id, "filename": item.file.name} for item in items)
    adapter.list_archives("fixture_channel", preview_dir=tmp_path / "previews", media_only=True,
                          take_request=lambda: jobs.popleft() if jobs else None,
                          close_requests=lambda: not jobs)
    assert finished == {102, 103, 104} and max(maximum) == 2
    assert any(event["message_id"] == 101 and event["download_status"] == "failed" for event in events)


def test_recent_batch_failure_cancels_sibling_and_never_starts_waiting_files(tmp_path, monkeypatch):
    adapter, client, _, active, maximum, finished, cancelled, downloads, events, content = media_client(
        tmp_path, monkeypatch)
    original = client.download_media
    entered = set()

    async def download(message, file, progress_callback):
        entered.add(message.id)
        if message.id == 104:
            while len(entered) < 2:
                await asyncio.sleep(0)
            raise OSError("batch failure")
        return await original(message, file, progress_callback)

    client.download_media = download
    with pytest.raises(RuntimeError, match="batch failure"):
        adapter.fetch_messages("", [], "fixture_channel", "recent", 4)
    assert entered == {103, 104} and not active and not finished
    assert client.disconnected and cancelled == {103} and downloads == [103]
    assert (next((adapter.download_dir / ".unverified").rglob("103.zip"))).read_bytes() == content[:16]
    assert not list(adapter.download_dir.glob("*.zip"))
    assert not any(event["download_status"] == "completed" for event in events)
    assert all(any(event["message_id"] == mid and event["download_status"] == "cancelled" for event in events)
               for mid in (101, 102, 103))


def test_two_chunked_files_and_foreground_image_share_eight_request_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, "_ARCHIVE_PARALLEL_MIN_BYTES", 1)
    content = payload()
    in_flight = maximum = 0
    all_active = set()

    class Client:
        def iter_download(self, message, *, offset, limit, request_size):
            async def stream():
                nonlocal in_flight, maximum
                for index in range(limit):
                    in_flight += 1
                    all_active.add(message.id)
                    maximum = max(maximum, in_flight)
                    try:
                        await asyncio.sleep(0.005)
                        position = offset + index * request_size
                        chunk = content[position:position + request_size]
                    finally:
                        in_flight -= 1
                    yield chunk
            return stream()

    monkeypatch.setattr(adapters, "_ARCHIVE_REQUEST_BYTES", 16)
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "session", tmp_path / "inbox",
                                      download_workers=8)

    async def run():
        nonlocal in_flight, maximum
        scheduler = DownloadScheduler(2)

        async def file(mid):
            async with scheduler.file():
                message = SimpleNamespace(id=mid, file=SimpleNamespace(size=len(content)))
                return await adapter._download_archive_async(Client(), message, f"{mid}.zip", scheduler=scheduler)

        async def image():
            nonlocal in_flight, maximum
            async with scheduler.requests:
                in_flight += 1
                maximum = max(maximum, in_flight)
                try:
                    await asyncio.sleep(0.02)
                finally:
                    in_flight -= 1

        # The image uses a permit while both files each request eight streams.
        await asyncio.gather(image(), file(1), file(2))

    asyncio.run(run())
    assert maximum == 8 and in_flight == 0 and all_active == {1, 2}
    assert all((adapter.download_dir / f"{mid}.zip").read_bytes() == content for mid in (1, 2))
