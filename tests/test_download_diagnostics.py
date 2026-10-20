from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from xianyu_pipeline import adapters, download_diagnostics as diagnostics
from xianyu_pipeline.adapters import TelethonTelegramAdapter


@pytest.fixture
def log_dir(tmp_path):
    directory = tmp_path / "logs"
    diagnostics.configure_diagnostics(directory)
    try:
        yield directory
    finally:
        for handler in list(diagnostics._events.handlers):
            if isinstance(handler, diagnostics.RotatingFileHandler):
                diagnostics._events.removeHandler(handler)
                handler.close()
        root = logging.getLogger()
        for handler in list(root.handlers):
            if isinstance(handler, diagnostics.TransportLogHandler):
                root.removeHandler(handler)
                handler.close()


def read_report(directory):
    return diagnostics.analyze_files(diagnostics.diagnostic_files(directory))


def encoded(*rows):
    return [("fixture.jsonl", b"\n".join(json.dumps(
        {"version": 1, **row}, allow_nan=True).encode() for row in rows))]


def test_real_telethon_iterator_trace_counts_bytes_and_shared_sender(log_dir, tmp_path, monkeypatch):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl import types as tl

    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("asset.bin", b"x" * (1024 * 1024 + 17))
    payload = content.getvalue()
    monkeypatch.setattr(adapters, "_ARCHIVE_PARALLEL_MIN_BYTES", 0)

    async def run():
        client = TelegramClient(StringSession(), 12345, "a" * 32)
        requests = []

        async def call(sender, request):
            assert sender is client._sender
            requests.append(request.offset)
            await asyncio.sleep(0)
            return tl.upload.File(type=tl.storage.FileUnknown(), mtime=0,
                                  bytes=payload[request.offset:request.offset + request.limit])

        monkeypatch.setattr(client, "_call", call)
        document = tl.Document(id=77, access_hash=123456789, file_reference=b"PRIVATE_REFERENCE",
                               date=None, mime_type="application/zip", size=len(payload),
                               dc_id=client.session.dc_id,
                               attributes=[tl.DocumentAttributeFilename("fixture.zip")])
        message = tl.Message(id=77, peer_id=tl.PeerChannel(123),
                             media=tl.MessageMediaDocument(document=document))
        adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "account.session",
                                          tmp_path / "inbox")
        saved = await adapter._download_archive_async(client, message, "fixture.zip")
        assert saved.read_bytes() == payload
        assert len(requests) == 3

    asyncio.run(run())
    report = read_report(log_dir)
    assert report["invalid_lines"] == 0
    row, = report["downloads"]
    assert row["status"] == "completed" and row["channel_id"] == 123
    assert row["received"] == len(payload) and row["chunks"] == row["recorded_chunks"] == 3
    assert row["observed_senders"] == 1
    assert row["first_byte_s"] >= 0 and row["validation_s"] >= 0
    assert row["average_bps"] > 0 and row["fetch_p95_s"] >= row["fetch_p50_s"]
    assert any("同一个 sender" in finding for finding in row["findings"])
    raw = (log_dir / diagnostics.LOG_NAME).read_text(encoding="utf-8")
    assert "PRIVATE_REFERENCE" not in raw and "123456789" not in raw and "a" * 32 not in raw
    assert "auth_key" not in raw and "api_hash" not in raw


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_and_cancelled_downloads_emit_terminal_trace(log_dir, tmp_path, cancel):
    class Client:
        async def download_media(self, *args, **kwargs):
            if cancel:
                raise asyncio.CancelledError()
            raise TimeoutError("PRIVATE_EXCEPTION_TEXT")

    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "session", tmp_path / "inbox")
    message = SimpleNamespace(id=7, file=SimpleNamespace(size=10, name="asset.zip"))
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        asyncio.run(adapter._download_archive_async(Client(), message, "asset.zip"))
    row, = read_report(log_dir)["downloads"]
    assert row["status"] == ("cancelled" if cancel else "failed")
    assert row["received"] == 0 and row["stage"] == "transfer"
    assert row["error_type"] == ("CancelledError" if cancel else "TimeoutError")
    assert "PRIVATE_EXCEPTION_TEXT" not in (log_dir / diagnostics.LOG_NAME).read_text(encoding="utf-8")


def test_transport_events_are_allowlisted_and_not_attributed_to_a_file(log_dir):
    handler = diagnostics.TransportLogHandler()
    for text in (
        "Connecting to 149.154.167.41:443/TcpFull...",
        "Connection to 149.154.167.41:443/TcpFull complete!",
        "Exporting auth for new borrowed sender in DcOption(id=2, secret=PRIVATE_AUTH)",
        "Closing current connection to begin reconnect...",
        "Got timeout while downloading file, retrying once",
        "Sleeping for 12s (0:00:12) on GetFileRequest flood wait",
        "PRIVATE_AUTH api_hash='secret' StringSession='session'",
    ):
        handler.emit(logging.LogRecord("telethon.network.mtprotosender", logging.INFO,
                                       __file__, 1, text, (), None))
    handler.emit(logging.LogRecord("other.library", logging.INFO, __file__, 1,
                                   "Connecting to private", (), None))
    files = diagnostics.diagnostic_files(log_dir)
    report = diagnostics.analyze_files(files)
    assert report["transport_events"] == {"connect_start": 1, "connected": 1, "auth_export": 1,
                                          "reconnect": 1, "timeout": 1, "flood_wait": 1}
    raw = files[0][1].decode()
    assert "PRIVATE_AUTH" not in raw and "api_hash" not in raw and "download_id" not in raw
    assert '"wait_s":12' in raw and '"dc_id":2' in raw


def test_trace_ids_separate_same_message_id_and_report_incomplete_records():
    report = diagnostics.analyze_files(encoded(
        {"event": "start", "download_id": "one", "message_id": 77, "filename": "one.rar"},
        {"event": "start", "download_id": "two", "message_id": 77, "filename": "two.rar"},
        {"event": "finish", "download_id": "two", "status": "failed", "average_bps": 10},
    ))
    first, second = report["downloads"]
    assert first["status"] == "unfinished" and first["filename"] == "one.rar"
    assert second["status"] == "failed" and second["filename"] == "two.rar"
    assert any("可能仍在下载" in finding for finding in first["findings"])


def test_analyzer_handles_rotation_missing_start_and_truncated_invalid_lines():
    data = encoded(
        {"event": "chunk", "download_id": "id", "fetch_s": 2},
        {"event": "chunk", "download_id": "id", "fetch_s": 4},
        {"event": "finish", "download_id": "id", "workers": 4, "status": "completed",
         "received": 1024 * 1024, "transfer_s": 10, "average_bps": 104857.6},
        {"event": "finish", "download_id": "bad", "transfer_s": "broken"},
        {"event": "transport", "kind": {}},
        {"event": "finish", "download_id": "nan", "average_bps": float("nan")},
    )
    data.append(("truncated.jsonl", b'{"version":1,"event":"finish"\n[]\n'))
    report = diagnostics.analyze_files(data)
    row, = report["downloads"]
    assert report["invalid_lines"] == 5
    assert row["fetch_p50_s"] == 2 and row["fetch_p95_s"] == 4
    assert any("起始记录已缺失" in finding for finding in row["findings"])
    assert any("不能单独证明" in finding for finding in row["findings"])
    assert "0.100 MiB/s" in diagnostics.render_report(report)


def test_active_download_report_uses_latest_heartbeat_without_claiming_completion():
    report = diagnostics.analyze_files(encoded(
        {"event": "start", "download_id": "active", "message_id": 77, "workers": 4},
        {"event": "first_byte", "download_id": "active", "elapsed_s": .5},
        {"event": "chunk", "download_id": "active", "fetch_s": .5, "sender": "sender_1"},
        {"event": "progress", "download_id": "active", "received": 1024 * 1024, "elapsed_s": 2},
        {"event": "heartbeat", "download_id": "active", "received": 2 * 1024 * 1024, "elapsed_s": 5},
    ))
    row, = report["downloads"]
    assert row["status"] == "unfinished"
    assert row["first_byte_s"] == .5 and row["received"] == 2 * 1024 * 1024
    assert row["transfer_s"] == 5 and row["average_bps"] == 2 * 1024 * 1024 / 5
    assert row["observed_senders"] == 1
    assert "0.400 MiB/s" in diagnostics.render_report(report)


def test_export_includes_only_diagnostic_files_and_analysis(log_dir):
    (log_dir / "pipeline.log").write_text("PRIVATE_PIPELINE_LOG", encoding="utf-8")
    (log_dir / "account.session").write_text("PRIVATE_SESSION", encoding="utf-8")
    diagnostics._emit("start", download_id="id", message_id=1, filename="asset.rar")
    with zipfile.ZipFile(io.BytesIO(diagnostics.export_diagnostics(log_dir))) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {diagnostics.LOG_NAME, "analysis.txt",
                                          "analysis.json", "exported-at.txt"}
        report = json.loads(archive.read("analysis.json"))
        assert report["downloads"][0]["status"] == "unfinished"
        assert not any(b"PRIVATE_" in archive.read(name) for name in archive.namelist())


def test_configure_again_rotates_and_does_not_duplicate_handlers(log_dir):
    handler = next(item for item in diagnostics._events.handlers
                   if isinstance(item, diagnostics.RotatingFileHandler))
    handler.maxBytes = 700
    for index in range(30):
        diagnostics._emit("start", download_id=str(index), filename="test.rar")
    diagnostics.configure_diagnostics(log_dir)
    assert sum(isinstance(item, diagnostics.TransportLogHandler)
               for item in logging.getLogger().handlers) == 1
    files = diagnostics.diagnostic_files(log_dir)
    assert 1 < len(files) <= 4
    assert files[-1][0] == diagnostics.LOG_NAME
    assert "0" not in {row["download_id"] for row in read_report(log_dir)["downloads"]}


def test_analyze_cli_does_not_read_config_or_connect(log_dir):
    diagnostics._emit("start", download_id="id", message_id=1, filename="asset.rar")
    result = subprocess.run(
        [sys.executable, "run_pipeline.py", "--config", "nonexistent.json",
         "--analyze-downloads", str(log_dir)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=15, check=True)
    assert "asset.rar" in result.stdout and "可能仍在下载" in result.stdout
    assert "Connecting" not in result.stderr
