from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from xianyu_pipeline import adapters
from xianyu_pipeline.adapters import TelethonTelegramAdapter, _TransferEvents
from xianyu_pipeline.download_resume import DownloadCheckpoint


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, "_ARCHIVE_PARALLEL_MIN_BYTES", 1)
    monkeypatch.setattr(adapters, "_ARCHIVE_REQUEST_BYTES", 4096)
    monkeypatch.setattr(adapters, "_ARCHIVE_CHUNK_TIMEOUT_SECONDS", .03)
    monkeypatch.setattr(adapters, "_ARCHIVE_RETRY_DELAY_SECONDS", .001)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("model.stl", b"model fixture" * 4000)
    payload = stream.getvalue()
    message = SimpleNamespace(id=77, peer_id="-100123", document=SimpleNamespace(id=100),
                              file=SimpleNamespace(name="model.zip", size=len(payload)))
    events = []

    def adapter(workers=2, stop_event=None):
        return TelethonTelegramAdapter(
            12345, "a" * 32, tmp_path / "account.session", tmp_path / "inbox",
            download_workers=workers, on_event=events.append, stop_event=stop_event)

    def transfer():
        return _TransferEvents(events.append, 77, "fixture_channel", "model.zip", "", len(payload))

    class Client:
        def __init__(self, faults=None):
            self.faults = faults or {}
            self.attempts = Counter()
            self.delivered = []
            self.closed = 0
            self.active = 0

        def iter_download(self, message, *, offset, limit, request_size):
            async def chunks():
                self.active += 1
                try:
                    for position in range(offset, offset + limit * request_size, request_size):
                        self.attempts[position] += 1
                        fault = self.faults.get(position)
                        if callable(fault):
                            await fault(self.attempts[position])
                        elif fault:
                            raise fault
                        await asyncio.sleep(0)
                        chunk = payload[position:position + request_size]
                        self.delivered.append(position)
                        yield chunk
                finally:
                    self.active -= 1
                    self.closed += 1
            return chunks()

    return SimpleNamespace(adapter=adapter, transfer=transfer, payload=payload,
                           message=message, events=events, Client=Client, tmp_path=tmp_path)


def test_restart_resumes_only_verified_chunks_with_different_worker_count(fixture):
    fx = fixture
    first = fx.adapter()
    client = fx.Client({4096: RuntimeError("interrupted")})
    with pytest.raises(RuntimeError, match="断点已保留"):
        asyncio.run(first._download_archive_async(client, fx.message, "model.zip", fx.transfer()))
    saved_offsets = set(client.delivered)
    assert saved_offsets and client.active == 0
    failed = fx.events[-1]
    assert failed["resumable"] and failed["received"] < len(fx.payload)
    checkpoints = list(first.download_dir.rglob("resume.json"))
    assert len(checkpoints) == 1
    persisted = json.loads(checkpoints[0].read_text())
    assert {int(index) * 4096 for index in persisted["chunks"]} == saved_offsets

    second, resumed = fx.adapter(workers=1), fx.Client()
    target = asyncio.run(second._download_archive_async(resumed, fx.message, "model.zip", fx.transfer()))
    assert target.read_bytes() == fx.payload
    assert set(resumed.delivered).isdisjoint(saved_offsets)
    assert saved_offsets | set(resumed.delivered) == set(range(0, len(fx.payload), 4096))
    assert not list(first.download_dir.rglob("resume.json"))
    assert fx.events[-1]["download_status"] == "completed"
    assert fx.events[-1]["resumed_bytes"] == failed["received"]


def test_corrupt_saved_chunk_is_fetched_again_but_other_chunks_are_reused(fixture):
    fx = fixture
    adapter, first = fx.adapter(), fx.Client({4096: RuntimeError("interrupted")})
    with pytest.raises(RuntimeError):
        asyncio.run(adapter._download_archive_async(first, fx.message, "model.zip", fx.transfer()))
    source = Path(fx.events[-1]["local_path"])
    with source.open("r+b") as stream:
        stream.write(b"corrupt")
    resumed = fx.Client()
    target = asyncio.run(fx.adapter()._download_archive_async(resumed, fx.message, "model.zip", fx.transfer()))
    assert target.read_bytes() == fx.payload
    assert 0 in resumed.delivered
    assert set(first.delivered) - {0}
    assert (set(first.delivered) - {0}).isdisjoint(resumed.delivered)


@pytest.mark.parametrize("fault", ["timeout", "connection", "rpc_timeout", "incomplete_read"])
def test_failed_chunk_is_retried_at_same_offset_without_counting_bytes_twice(fixture, fault):
    fx = fixture

    async def once(attempt):
        if attempt == 1:
            if fault == "timeout":
                await asyncio.Event().wait()
            if fault == "rpc_timeout":
                from telethon.errors import TimedOutError
                raise TimedOutError(None, "TIMEOUT")
            if fault == "incomplete_read":
                raise asyncio.IncompleteReadError(b"", 8)
            raise ConnectionResetError("fixture reset")

    client = fx.Client({4096: once})
    target = asyncio.run(fx.adapter()._download_archive_async(client, fx.message, "model.zip", fx.transfer()))
    assert target.read_bytes() == fx.payload and client.attempts[4096] == 2
    assert len(client.delivered) == len(set(client.delivered))
    assert any(event["download_phase"] == "retrying" for event in fx.events)
    assert fx.events[-1]["received"] == len(fx.payload) and client.active == 0


def test_retry_budget_is_bounded_and_checkpoint_survives(fixture):
    fx = fixture
    client = fx.Client({4096: ConnectionResetError("persistent failure")})
    with pytest.raises(RuntimeError, match="断点已保留"):
        asyncio.run(fx.adapter()._download_archive_async(client, fx.message, "model.zip", fx.transfer()))
    assert client.attempts[4096] == 3 and client.active == 0
    assert fx.events[-1]["resumable"] and fx.events[-1]["download_status"] == "failed"
    assert list(fx.adapter().download_dir.rglob("resume.json"))


def test_stalled_shared_connection_recovers_with_private_client_and_fewer_workers(fixture, monkeypatch):
    fx = fixture
    adapter = fx.adapter(workers=8)
    original = fx.Client({4096: TimeoutError()})
    original.session = SimpleNamespace(auth_key=object())
    recovered = fx.Client()
    connections, disconnections = [], []
    saved_offsets = set()

    async def connect():
        assert original.active == 0
        saved_offsets.update(original.delivered)
        connections.append(True)

    async def disconnect():
        assert recovered.active == 0
        disconnections.append(True)

    recovered.connect, recovered.disconnect = connect, disconnect
    monkeypatch.setattr(adapter, "_new_download_client", lambda client: recovered if client is original else None)
    target = asyncio.run(adapter._download_archive_async(original, fx.message, "model.zip", fx.transfer()))
    assert target.read_bytes() == fx.payload
    assert connections == disconnections == [True]
    assert saved_offsets and saved_offsets.isdisjoint(recovered.delivered)
    assert any("并发降至 4" in row["download_note"] for row in fx.events)
    assert fx.events[-1]["received"] == len(fx.payload)
    assert original.attempts[4096] == 1


def test_connection_recovery_budget_is_bounded_and_does_not_close_shared_client(fixture, monkeypatch):
    fx = fixture
    adapter = fx.adapter(workers=8)
    clients = []

    class StalledClient(fx.Client):
        def __init__(self):
            super().__init__({4096: TimeoutError()})
            self.session = SimpleNamespace(auth_key=object())
            self.connects = self.disconnects = 0
            clients.append(self)

        async def connect(self):
            self.connects += 1

        async def disconnect(self):
            assert self.active == 0
            self.disconnects += 1

    original = StalledClient()
    monkeypatch.setattr(adapter, "_new_download_client", lambda client: StalledClient())
    with pytest.raises(RuntimeError, match="offset=4096.*重建连接 3/3"):
        asyncio.run(adapter._download_archive_async(original, fx.message, "model.zip", fx.transfer()))
    assert len(clients) == 4
    assert original.disconnects == 0
    assert all(client.connects == client.disconnects == 1 for client in clients[1:])
    assert all(client.active == 0 for client in clients)
    assert fx.events[-1]["resumable"] and "等待分块超过" in fx.events[-1]["error"]
    notes = [row["download_note"] for row in fx.events if "并发降至" in row["download_note"]]
    assert any("并发降至 1" in note for note in notes)


@pytest.mark.parametrize("stop", [False, True])
def test_failed_or_cancelled_reconnect_keeps_chunks_and_closes_private_client(fixture, monkeypatch, stop):
    fx, event = fixture, Event()
    adapter = fx.adapter(stop_event=event)
    original = fx.Client({4096: TimeoutError()})
    original.session = SimpleNamespace(auth_key=object())
    disconnected = []
    recovered = fx.Client()

    async def connect():
        if stop:
            event.set()
            await asyncio.Event().wait()
        raise ConnectionResetError("fixture reconnect failed")

    async def disconnect():
        disconnected.append(True)

    recovered.connect, recovered.disconnect = connect, disconnect
    monkeypatch.setattr(adapter, "_new_download_client", lambda client: recovered)
    with pytest.raises(RuntimeError, match="停止|重建失败"):
        asyncio.run(adapter._download_archive_async(original, fx.message, "model.zip", fx.transfer()))
    assert disconnected == [True] and original.active == recovered.active == 0
    assert fx.events[-1]["resumable"]
    assert fx.events[-1]["download_status"] == ("cancelled" if stop else "failed")
    assert list(adapter.download_dir.rglob("resume.json"))


def test_recovery_client_uses_only_an_in_memory_session(fixture, monkeypatch):
    from telethon.sessions import MemorySession

    adapter = fixture.adapter()
    auth_key = object()
    original = SimpleNamespace(session=SimpleNamespace(
        dc_id=5, server_address="91.108.56.138", port=443, auth_key=auth_key))
    sessions = []
    monkeypatch.setattr(adapter, "_client", lambda *, session: sessions.append(session) or "client")
    assert adapter._new_download_client(original) == "client"
    session, = sessions
    assert isinstance(session, MemorySession)
    assert session.auth_key is auth_key
    assert session.dc_id == 5 and session.server_address == "91.108.56.138"
    assert not list(fixture.tmp_path.rglob("*.session"))


def test_real_telethon_iterator_recovers_without_reusing_stalled_transport(fixture, monkeypatch):
    from telethon import TelegramClient
    from telethon.crypto import AuthKey
    from telethon.sessions import MemorySession
    from telethon.tl import types as tl

    fx = fixture

    async def run():
        session = MemorySession()
        session.set_dc(2, "149.154.167.50", 443)
        session.auth_key = AuthKey(bytes(range(256)))
        original = TelegramClient(session, 12345, "a" * 32)
        delivered, closed = {}, []

        def calls(client, stalled=False):
            delivered[client] = []

            async def call(sender, request):
                assert sender is client._sender
                if stalled and request.offset == 4096:
                    raise TimeoutError()
                delivered[client].append(request.offset)
                return tl.upload.File(type=tl.storage.FileUnknown(), mtime=0,
                                      bytes=fx.payload[request.offset:request.offset + request.limit])

            monkeypatch.setattr(client, "_call", call)

        calls(original, stalled=True)
        adapter = fx.adapter()
        factory = adapter._new_download_client

        def recovery(parent):
            assert parent is original
            client = factory(parent)
            assert isinstance(client.session, MemorySession)
            assert client._no_updates
            calls(client)

            async def connect():
                return True

            async def disconnect():
                closed.append(client)

            monkeypatch.setattr(client, "connect", connect)
            monkeypatch.setattr(client, "disconnect", disconnect)
            return client

        monkeypatch.setattr(adapter, "_new_download_client", recovery)
        document = tl.Document(id=100, access_hash=0, file_reference=b"fixture", date=None,
                               mime_type="application/zip", size=len(fx.payload), dc_id=2,
                               attributes=[tl.DocumentAttributeFilename("model.zip")])
        message = tl.Message(id=77, peer_id=tl.PeerChannel(123),
                             media=tl.MessageMediaDocument(document=document))
        target = await adapter._download_archive_async(original, message, "model.zip", fx.transfer())
        assert target.read_bytes() == fx.payload
        assert len(delivered) == 2 and len(closed) == 1 and original not in closed
        assert set(delivered[original]).isdisjoint(delivered[closed[0]])
        assert sorted(position for positions in delivered.values() for position in positions) == list(
            range(0, len(fx.payload), 4096))

    asyncio.run(run())


def test_stop_during_retry_keeps_checkpoint_and_drains_writers(fixture):
    fx, stop = fixture, Event()

    async def stop_request(attempt):
        stop.set()
        await asyncio.Event().wait()

    client = fx.Client({4096: stop_request})
    with pytest.raises(RuntimeError, match="停止"):
        asyncio.run(fx.adapter(stop_event=stop)._download_archive_async(
            client, fx.message, "model.zip", fx.transfer()))
    assert client.active == 0 and fx.events[-1]["download_status"] == "cancelled"
    assert fx.events[-1]["resumable"]
    target = asyncio.run(fx.adapter()._download_archive_async(
        fx.Client(), fx.message, "model.zip", fx.transfer()))
    assert target.read_bytes() == fx.payload


def test_validation_failure_discards_checkpoint_and_cannot_skip_invalid_data(fixture):
    fx = fixture
    client = fx.Client()
    iterator = client.iter_download

    def corrupt(*args, **kwargs):
        async def chunks():
            async for chunk in iterator(*args, **kwargs):
                if kwargs["offset"] == 0 and chunk.startswith(b"PK"):
                    chunk = chunk[:100] + bytes([chunk[100] ^ 1]) + chunk[101:]
                yield chunk
        return chunks()

    client.iter_download = corrupt
    with pytest.raises(RuntimeError):
        asyncio.run(fx.adapter()._download_archive_async(client, fx.message, "model.zip", fx.transfer()))
    assert fx.events[-1]["download_status"] == "failed"
    assert not fx.events[-1]["resumable"]
    assert not list(fx.adapter().download_dir.rglob("resume.json"))


def test_stop_after_successful_validation_preserves_all_chunks(fixture, monkeypatch):
    fx, stop = fixture, Event()
    original = adapters.zipfile.ZipFile.testzip

    def validate(archive):
        result = original(archive)
        stop.set()
        return result

    monkeypatch.setattr(adapters.zipfile.ZipFile, "testzip", validate)
    with pytest.raises(RuntimeError, match="停止"):
        asyncio.run(fx.adapter(stop_event=stop)._download_archive_async(
            fx.Client(), fx.message, "model.zip", fx.transfer()))
    assert fx.events[-1]["resumable"]
    assert fx.events[-1]["received"] == len(fx.payload)
    monkeypatch.setattr(adapters.zipfile.ZipFile, "testzip", original)
    client = fx.Client()
    target = asyncio.run(fx.adapter()._download_archive_async(
        client, fx.message, "model.zip", fx.transfer()))
    assert target.read_bytes() == fx.payload and not client.delivered


def test_hard_process_exit_releases_lock_and_resumes_only_durable_chunks(fixture, monkeypatch):
    fx = fixture
    monkeypatch.setattr(adapters, "_ARCHIVE_REQUEST_BYTES", 1024)
    payload_path = fx.tmp_path / "fixture.zip"
    payload_path.write_bytes(fx.payload)
    program = """
import asyncio, os, sys
from pathlib import Path
from types import SimpleNamespace
from xianyu_pipeline import adapters
adapters._ARCHIVE_PARALLEL_MIN_BYTES = 1
adapters._ARCHIVE_REQUEST_BYTES = 1024
root = Path(sys.argv[1])
payload = (root / "fixture.zip").read_bytes()
message = SimpleNamespace(id=77, peer_id="-100123", document=SimpleNamespace(id=100),
                          file=SimpleNamespace(name="model.zip", size=len(payload)))
class Client:
    def iter_download(self, message, *, offset, limit, request_size):
        async def stream():
            for position in range(offset, offset + limit * request_size, request_size):
                if position >= 17 * request_size:
                    os._exit(19)
                yield payload[position:position + request_size]
        return stream()
adapter = adapters.TelethonTelegramAdapter(
    12345, "a" * 32, root / "account.session", root / "inbox", download_workers=1)
asyncio.run(adapter._download_archive_async(Client(), message, "model.zip"))
"""
    process = subprocess.run(
        [sys.executable, "-c", program, str(fx.tmp_path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
    assert process.returncode == 19, process.stderr.decode(errors="replace")
    checkpoint, = fx.adapter().download_dir.rglob("resume.json")
    assert len(json.loads(checkpoint.read_text())["chunks"]) == 16
    client = fx.Client()
    saved = asyncio.run(fx.adapter()._download_archive_async(client, fx.message, "model.zip", fx.transfer()))
    assert saved.read_bytes() == fx.payload
    assert set(range(0, 16 * 1024, 1024)).isdisjoint(client.delivered)
    assert 16 * 1024 in client.delivered
    assert fx.events[-1]["resumed_bytes"] == 16 * 1024


def test_checkpoint_identity_isolated_by_account_channel_message_and_document(tmp_path):
    values = dict(session=str(tmp_path / "account"), peer="channel-1", message_id=7,
                  document_id=11, filename="model.zip", size=1024, chunk_size=512)
    base = DownloadCheckpoint(tmp_path, **values).directory
    for key, value in (("session", str(tmp_path / "other")), ("peer", "channel-2"),
                       ("message_id", 8), ("document_id", 12), ("size", 1025)):
        assert DownloadCheckpoint(tmp_path, **{**values, key: value}).directory != base


def test_checkpoint_lock_prevents_simultaneous_writers_and_releases_on_exit(tmp_path):
    values = dict(session=str(tmp_path / "account"), peer="channel", message_id=7,
                  document_id=11, filename="model.zip", size=1024, chunk_size=512)
    first, second = (DownloadCheckpoint(tmp_path, **values) for _ in range(2))
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="其他任务"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_rate_uses_five_second_window_and_excludes_resumed_bytes(monkeypatch):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(adapters, "time", SimpleNamespace(monotonic=lambda: clock.now))
    events = []
    transfer = _TransferEvents(events.append, 77, "fixture_channel", "model.zip", "", 100000000)
    transfer.start(10000000)
    clock.now = 1
    transfer.progress(11000000, None)
    clock.now = 1.001
    transfer.progress(11500000, None)
    assert len(events) == 2  # Do not publish two chunk arrivals one millisecond apart.
    assert transfer.data["speed"] < 2000000
    clock.now = 2
    transfer.progress(12000000, None)
    assert events[-1]["speed"] == events[-1]["average_speed"] == 1000000
    clock.now = 10
    transfer.progress(12500000, None)
    assert events[-1]["speed"] < 1000000
    assert events[-1]["resumed_bytes"] == 10000000
