from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import zipfile
from types import SimpleNamespace

import pytest

from tools import benchmark_downloads as bench
from xianyu_pipeline.config import PipelineConfig


@pytest.mark.parametrize("url,expected", [
    ("https://t.me/fixture_channel/123", ("fixture_channel", 123)),
    ("https://t.me/c/123456789/55", (-1000123456789, 55)),
    ("https://t.me/c/123456789/22/55", (-1000123456789, 55)),
    ("https://t.me/fixture_channel/22/55", ("fixture_channel", 55)),
])
def test_message_link_is_scoped_to_one_message(url, expected):
    assert bench.parse_message_url(url) == expected


@pytest.mark.parametrize("url", [
    "http://t.me/fixture_channel/123", "https://t.me.evil/fixture_channel/123",
    "https://t.me/fixture_channel", "https://t.me/fixture_channel/0",
    "https://t.me/fixture_channel/2147483648", "https://t.me/fixture_channel/123?comment=4",
    "https://t.me/c/123/abc/4", "https://t.me/+invite", "--dir=C:/private",
])
def test_invalid_or_ambiguous_message_link_is_rejected(url):
    with pytest.raises(ValueError):
        bench.parse_message_url(url)


def test_session_read_does_not_change_or_copy_credentials(tmp_path):
    path = tmp_path / "account.session"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions (dc_id INTEGER, server_address TEXT, port INTEGER, auth_key BLOB)")
    key = b"x" * 256
    db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (5, "127.0.0.2", 443, key))
    db.commit()
    db.close()
    original = path.read_bytes()
    session = bench.memory_session(path)
    assert session.dc_id == 5 and session.auth_key.key == key
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
    path.rename(tmp_path / "closed.session")


def test_proxy_failure_has_no_invented_telegram_result(monkeypatch):
    def fail(*args, **kwargs):
        raise ConnectionRefusedError(10061, "connection refused")

    monkeypatch.setattr(bench.socket, "create_connection", fail)
    result = bench.check_proxy("socks5://127.0.0.1:7890")
    assert not result["reachable"] and result["error_code"] == 10061
    assert bench.check_proxy("")["reachable"] is None


def test_tdl_command_keeps_paths_as_arguments_and_session_isolated(tmp_path, monkeypatch):
    executable = tmp_path / "a path with spaces" / "tdl.exe"
    executable.parent.mkdir()
    executable.touch()
    state = tmp_path / "private state"
    monkeypatch.setattr(bench, "TDL_STATE", state)
    monkeypatch.setenv("TDL_PROXY", "http://wrong-proxy:7890")
    command = bench.tdl_command(executable, "socks5://127.0.0.1:7890", 4)
    assert command[0] == str(executable.resolve())
    assert command[command.index("--storage") + 1] == "type=bolt,path=" + str(state / "data")
    assert command[command.index("--limit") + 1] == "1"
    assert command[command.index("--proxy") + 1] == "socks5://127.0.0.1:7890"
    assert "TDL_PROXY" not in bench.tdl_environment()


def test_progress_never_counts_resumed_bytes_as_fresh_download(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(bench.time, "monotonic", lambda: now[0])
    samples = bench.Samples()
    samples({"received": 1048576, "resumed_bytes": 1048576, "download_status": "downloading"})
    assert samples.first_byte_seconds is None
    now[0] = 12.0
    samples({"received": 2097152, "resumed_bytes": 1048576, "download_status": "validating"})
    report = samples.report(2097152, status="completed", total_seconds=2.5)
    assert report["first_byte_seconds"] == 2
    assert report["effective_mib_s"] is None and not report["fresh_download"]


def test_failed_downloads_and_validation_are_excluded_from_speed():
    samples = bench.Samples()
    samples.transfer_seconds = 2
    good = samples.report(4194304, status="completed", total_seconds=7)
    assert good["effective_mib_s"] == 2 and good["total_seconds"] == 7
    bad = samples.report(4194304, status="failed", total_seconds=7)
    assert bad["effective_mib_s"] is None


def test_archive_size_and_crc_verified_before_comparing_hashes(tmp_path):
    archive = tmp_path / "model.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
        output.writestr("model.stl", b"fixture-content")
    config = PipelineConfig()
    data = archive.read_bytes()
    digest = bench.validate_download(archive, len(data), config)
    assert len(digest) == 64
    with pytest.raises(RuntimeError, match="size"):
        bench.validate_download(archive, len(data) + 1, config)
    archive.write_bytes(data.replace(b"fixture-content", b"broken--content"))
    with pytest.raises(RuntimeError, match="CRC"):
        bench.validate_download(archive, len(data), config)


def test_rar_uses_project_archive_validator(tmp_path, monkeypatch):
    archive = tmp_path / "model.rar"
    archive.write_bytes(b"fixture")
    seen = []
    monkeypatch.setattr(bench, "validate_rar", lambda *args: seen.append(args))
    config = PipelineConfig(archive_test_executable_path="unrar.exe", archive_test_timeout_seconds=30)
    assert bench.validate_download(archive, 7, config)
    assert seen == [(archive, "unrar.exe", 30)]


def test_tdl_process_exit_zero_without_file_is_not_success(tmp_path):
    async def run():
        with pytest.raises(RuntimeError, match="without the requested file"):
            await bench.run_tdl([sys.executable, "-c", "pass"],
                                "https://t.me/fixture_channel/1", tmp_path / "trial", ".zip", 5)

    asyncio.run(run())


def test_tdl_process_success_uses_fixed_target(tmp_path):
    async def run():
        command = [sys.executable, "-c", "from pathlib import Path; Path('payload.zip').write_bytes(b'fixture')"]
        path = await bench.run_tdl(command, "https://t.me/fixture_channel/1", tmp_path / "trial", ".zip", 5)
        assert path.read_bytes() == b"fixture"

    asyncio.run(run())


def test_tdl_timeout_terminates_process_and_retains_local_log(tmp_path):
    async def run():
        directory = tmp_path / "trial"
        with pytest.raises(asyncio.TimeoutError):
            await bench.run_tdl([sys.executable, "-c", "import time; print('started', flush=True); time.sleep(60)"],
                                "https://t.me/fixture_channel/1", directory, ".zip", .2)
        log = directory / "tdl-console.log"
        assert log.is_file()
        # Windows cannot rename an open log: verifies the child and file handle are closed.
        log.rename(directory / "closed.log")

    asyncio.run(run())


def test_tdl_cancellation_terminates_child(tmp_path, monkeypatch):
    processes = []
    create = asyncio.create_subprocess_exec

    async def tracked(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)

    async def run():
        task = asyncio.create_task(bench.run_tdl(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            "https://t.me/fixture_channel/1", tmp_path / "trial", ".zip", 60))
        while not processes:
            await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode is not None

    asyncio.run(run())


def test_failed_preflight_stops_before_auth_and_leaves_config_unchanged(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(bench, "preflight", lambda *args: {
        "proxy": {"reachable": False}, "tdl": {"installed": True},
        "session_key_present": True, "ready_for_network_test": False})
    monkeypatch.setattr(bench.subprocess, "call", lambda *args, **kwargs: pytest.fail("Started login with no proxy"))
    assert bench.main(["--config", str(config_path), "--login-tdl", "--output", str(tmp_path / "output")]) == 2
    assert config_path.read_text() == "{}"
    report = next((tmp_path / "output").glob("*/report.json"))
    assert not json.loads(report.read_text(encoding="utf-8"))["preflight"]["proxy"]["reachable"]


def test_cancel_during_process_creation_does_not_leave_download_running(tmp_path, monkeypatch):
    processes = []
    create = asyncio.create_subprocess_exec

    async def slow_create(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        await asyncio.sleep(.05)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_create)

    async def run():
        task = asyncio.create_task(bench.run_tdl(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            "https://t.me/fixture_channel/1", tmp_path / "trial", ".zip", 60))
        while not processes:
            await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode is not None

    asyncio.run(run())


@pytest.mark.parametrize("mismatch", [False, True])
def test_benchmark_records_only_verified_matching_archives(tmp_path, monkeypatch, mismatch):
    import io

    def archive_bytes(content):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(zipfile.ZipInfo("model.stl", date_time=(2026, 1, 1, 0, 0, 0)), content)
        return stream.getvalue()

    payload = archive_bytes(b"first-model")
    different = archive_bytes(b"other-model")
    closed = []

    async def disconnect():
        closed.append(True)

    async def fetch(*args):
        return (SimpleNamespace(disconnect=disconnect),
                SimpleNamespace(id=77, file=SimpleNamespace(name="model.zip", size=len(payload)),
                                document=SimpleNamespace(dc_id=2)))

    async def download(command, url, directory, suffix, timeout):
        directory.mkdir()
        target = directory / ("payload" + suffix)
        target.write_bytes(different if mismatch and directory.name.startswith("2-") else payload)
        return target

    monkeypatch.setattr(bench, "fetch_message", fetch)
    monkeypatch.setattr(bench, "run_tdl", download)
    monkeypatch.setattr(bench, "tdl_command", lambda *args: ["fixture"])
    args = SimpleNamespace(url="https://t.me/fixture_channel/77", runs=2, engines=["tdl"], tdl=tmp_path / "tdl.exe")
    report = {}
    config = PipelineConfig()
    asyncio.run(bench.benchmark(config, args, tmp_path, report))
    assert closed == [True]
    assert report["runs"][0]["status"] == "completed"
    second = report["runs"][1]
    assert second["status"] == ("failed" if mismatch else "completed")
    assert (second["effective_mib_s"] is None) == mismatch
    assert second["first_byte_seconds"] is None
    assert not report["tdl_account_verified"]
    assert len(json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))["runs"]) == 2
