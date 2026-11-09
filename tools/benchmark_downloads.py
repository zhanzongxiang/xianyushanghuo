"""Isolated Telegram engine trials. Never enqueue uploads or change app settings."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xianyu_pipeline.adapters import TelethonTelegramAdapter, _TransferEvents, parse_telegram_api_proxy
from xianyu_pipeline.archive_validation import validate_rar
from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.runtime import configure_logging, telegram_crypto_backend

TDL_VERSION = "0.20.4"
DEFAULT_TDL = ROOT / ".cache/tools/tdl" / ("v" + TDL_VERSION) / "tdl.exe"
TDL_STATE = ROOT / ".cache/tdl-benchmark-session"


def parse_message_url(value: str) -> tuple[str | int, int]:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or parsed.netloc != "t.me" or parsed.query or parsed.fragment:
        raise ValueError("Use a https://t.me/... message link without query parameters.")
    parts = parsed.path.strip("/").split("/")
    if parts[0] == "c" and len(parts) in (3, 4) and parts[1].isdigit():
        peer = -1000000000000 - int(parts[1])
        ids = parts[2:]
    elif len(parts) in (2, 3) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", parts[0]):
        peer, ids = parts[0], parts[1:]
    else:
        raise ValueError("The message link must identify a channel and a message.")
    if not all(part.isascii() and part.isdigit() and 0 < int(part) <= 2147483647 for part in ids):
        raise ValueError("Invalid Telegram message ID.")
    return peer, int(ids[-1])


def memory_session(path: Path):
    """Read the existing key without copying it into a new file or locking the UI DB."""
    from telethon.crypto import AuthKey
    from telethon.sessions import MemorySession

    if not path.is_file():
        raise RuntimeError("Telegram session is missing; log into Telegram in application settings.")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT dc_id, server_address, port, auth_key FROM sessions").fetchone()
    if not row or not row[3]:
        raise RuntimeError("Telegram session has no authorization key.")
    session = MemorySession()
    session.set_dc(*row[:3])
    session.auth_key = AuthKey(row[3])
    return session


def check_proxy(proxy_url: str) -> dict:
    proxy = parse_telegram_api_proxy(proxy_url)
    if not proxy:
        return {"configured": False, "reachable": None, "note": "Direct connection; not tested."}
    try:
        with socket.create_connection((proxy["addr"], proxy["port"]), timeout=3):
            pass
        return {"configured": True, "url": proxy_url, "reachable": True,
                "note": "TCP reachable only; Telegram connectivity still needs verification."}
    except OSError as exc:
        return {"configured": True, "url": proxy_url, "reachable": False,
                "error_code": getattr(exc, "winerror", None) or exc.errno,
                "note": "Proxy TCP connection failed. Start the proxy before login or download."}


def tdl_environment() -> dict:
    # Explicit CLI parameters must not inherit another tdl installation's settings.
    return {key: value for key, value in os.environ.items() if not key.upper().startswith("TDL_")}


def tdl_command(executable: Path, proxy: str, workers: int) -> list[str]:
    if not executable.is_file():
        raise RuntimeError("tdl is missing. Run tools/install_tdl.ps1 first.")
    parse_telegram_api_proxy(proxy)
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("Workers must be between 1 and 8.")
    TDL_STATE.mkdir(parents=True, exist_ok=True)
    # Paths are structured argv elements; no PowerShell/cmd interpolation.
    storage = "type=bolt,path=" + str(TDL_STATE.resolve() / "data")
    if "," in str(TDL_STATE):
        raise ValueError("tdl storage paths cannot contain commas.")
    return [str(executable.resolve()), "--storage", storage, "--ns", "benchmark",
            "--proxy", proxy, "--threads", str(workers), "--limit", "1",
            "--reconnect-timeout", "30s", "--disable-progress-ps"]


def tdl_version(executable: Path) -> str:
    result = subprocess.run([str(executable.resolve()), "version"], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=10,
                            env=tdl_environment(), check=True)
    match = re.search(r"^Version: ([\w.\-]+)$", result.stdout, re.M)
    if not match or match[1] != TDL_VERSION:
        raise RuntimeError("This trial runner is verified against tdl " + TDL_VERSION + ".")
    return match[1]


def preflight(config: PipelineConfig, executable: Path) -> dict:
    result = {"proxy": check_proxy(config.telegram_api_proxy_url), "tdl": {}, "session_key_present": False}
    try:
        result["session_key_present"] = bool(memory_session(Path(config.telegram_session)).auth_key)
    except (OSError, RuntimeError, sqlite3.Error):
        result["session_error"] = "Existing Telegram authorization is unavailable."
    try:
        result["tdl"] = {"installed": True, "version": tdl_version(executable)}
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        result["tdl"] = {"installed": False, "error_type": type(exc).__name__}
    result["crypto_backend"] = telegram_crypto_backend()
    result["tdl_login"] = "Separate authorization required; this check does not contact Telegram."
    result["ready_for_network_test"] = (result["session_key_present"] and result["tdl"]["installed"]
                                        and result["proxy"]["reachable"] is not False)
    return result


class Samples:
    def __init__(self):
        self.started = time.monotonic()
        self.first_byte_seconds = None
        self.transfer_seconds = None
        self.resumed_bytes = 0
        self.received = 0

    def __call__(self, event):
        self.resumed_bytes = max(self.resumed_bytes, event.get("resumed_bytes", 0))
        self.received = max(self.received, event.get("received", 0))
        elapsed = time.monotonic() - self.started
        if self.first_byte_seconds is None and self.received > self.resumed_bytes:
            self.first_byte_seconds = elapsed
        if event.get("download_status") == "validating" and self.transfer_seconds is None:
            self.transfer_seconds = elapsed

    def report(self, size: int, *, status: str, total_seconds: float) -> dict:
        comparable = status == "completed" and self.resumed_bytes == 0
        return {"status": status, "received_bytes": self.received, "resumed_bytes": self.resumed_bytes,
                "first_byte_seconds": self.first_byte_seconds, "download_seconds": self.transfer_seconds,
                "total_seconds": total_seconds, "fresh_download": comparable,
                "effective_mib_s": size / self.transfer_seconds / 1048576
                if comparable and self.transfer_seconds and self.transfer_seconds > 0 else None}


def validate_download(path: Path, size: int, config: PipelineConfig) -> str:
    if not path.is_file() or path.stat().st_size != size:
        raise RuntimeError("Downloaded file is missing or size does not match the Telegram message.")
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            if archive.testzip():
                raise RuntimeError("ZIP CRC verification failed.")
    elif path.suffix.lower() == ".rar":
        validate_rar(path, config.archive_test_executable_path, config.archive_test_timeout_seconds)
    else:
        raise ValueError("Only ZIP/RAR archives are supported in this trial.")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


async def run_tdl(command: list[str], url: str, directory: Path, suffix: str, timeout: float) -> Path:
    directory.mkdir(parents=True, exist_ok=False)
    target = directory / ("payload" + suffix)
    arguments = command + ["download", "--url", url, "--dir", str(directory.resolve()),
                           "--template", target.name, "--restart"]
    # Native parallel writes can extend EOF before all blocks arrive. Never use stat() for progress.
    with (directory / "tdl-console.log").open("wb") as output:
        launching = asyncio.create_task(asyncio.create_subprocess_exec(
            *arguments, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            env=tdl_environment(), cwd=directory,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {})))
        try:
            process = await asyncio.shield(launching)
        except asyncio.CancelledError:
            process = await launching
            await terminate_process(process)
            raise
        try:
            code = await asyncio.wait_for(process.wait(), timeout=timeout)
            if code:
                raise RuntimeError(f"tdl exited with code {code}; inspect the local tdl-console.log.")
            if not target.is_file():
                raise RuntimeError("tdl returned success without the requested file.")
        finally:
            if process.returncode is None:
                await terminate_process(process)
    return target


async def terminate_process(process) -> None:
    if os.name == "nt":
        # Also stop a launcher child; killing only the parent can leave downloads writing files.
        taskkill = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/taskkill.exe"
        try:
            await asyncio.to_thread(subprocess.run,
                                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


def make_adapter(config: PipelineConfig, directory: Path, engine: str):
    return TelethonTelegramAdapter(
        config.telegram_api_id, config.telegram_api_hash, config.telegram_session, directory,
        proxy_url=config.telegram_api_proxy_url, dc2_server=config.telegram_api_dc2_server,
        download_workers=config.download_workers, download_file_concurrency=1, download_engine=engine,
        download_start_timeout_seconds=config.download_start_timeout_seconds,
        download_transfer_timeout_seconds=config.download_transfer_timeout_seconds,
        archive_test_executable_path=config.archive_test_executable_path,
        archive_test_timeout_seconds=config.archive_test_timeout_seconds)


async def fetch_message(config: PipelineConfig, url: str):
    from telethon.tl.types import InputPeerChannel

    peer, message_id = parse_message_url(url)
    adapter = make_adapter(config, config.inbox_dir, "telethon")
    client = adapter._client(session=memory_session(Path(config.telegram_session)))
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
        if not await asyncio.wait_for(client.is_user_authorized(), timeout=15):
            raise RuntimeError("Telegram API authorization has expired.")
        if isinstance(peer, int):
            with closing(sqlite3.connect(Path(config.telegram_session).resolve().as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute("SELECT hash FROM entities WHERE id=?", (peer,)).fetchone()
            if not row:
                raise RuntimeError("Private channel is not cached; load it in the application first.")
            peer = InputPeerChannel(-peer - 1000000000000, row[0])
        message = await asyncio.wait_for(client.get_messages(peer, ids=message_id), timeout=30)
        if not message or not message.file or not message.document:
            raise RuntimeError("Message is missing or has no downloadable document.")
        name = message.file.name or ""
        if (not name or Path(name).name != name or any(char in name for char in '\\/:\x00')
                or Path(name).suffix.lower() not in {".zip", ".rar"} or not message.file.size):
            raise ValueError("Choose a ZIP/RAR attachment with a valid filename and size.")
        return client, message
    except BaseException:
        await client.disconnect()
        raise


def save_report(path: Path, report: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


async def benchmark(config: PipelineConfig, args, output: Path, report: dict) -> None:
    client, message = await fetch_message(config, args.url)
    report["message"] = {"url": args.url, "id": message.id, "filename": message.file.name,
                         "size": message.file.size, "file_dc": message.document.dc_id}
    report["tdl_account_verified"] = False
    report["notes"] = [
        "Log into tdl with the same account as the application. Account equality is not automatically verified.",
        "Effective speed includes engine setup and transfer, excludes validation; MiB/s = bytes/s / 1048576.",
        "tdl has no structured byte-progress interface here; first-byte time is deliberately unavailable.",
        "Resumed or cached downloads are excluded from fresh-download speed comparison.",
        "tdl partial-file recovery is not claimed. Its --continue option tracks finished batch items.",
        "TDLib uses its existing profile and refuses an active profile lock; stop other TDLib tasks first.",
        "No uploads or material records are created. Trials run sequentially with one file at a time.",
    ]
    records = report.setdefault("runs", [])
    expected_hash = None
    try:
        for trial in range(args.runs):
            # Rotate first engine to reduce systematic warm-network ordering bias.
            engines = args.engines[trial % len(args.engines):] + args.engines[:trial % len(args.engines)]
            for engine in engines:
                directory = output / f"{trial + 1}-{engine}"
                samples = Samples()
                row = {"engine": engine, "trial": trial + 1, "workers": config.download_workers}
                print(f"Trial {trial + 1}: {engine}; {message.file.size} bytes", flush=True)
                try:
                    if engine == "tdl":
                        command = tdl_command(args.tdl, config.telegram_api_proxy_url, config.download_workers)
                        target = await run_tdl(command, args.url, directory, Path(message.file.name).suffix.lower(),
                                               config.download_transfer_timeout_seconds)
                        samples.transfer_seconds = time.monotonic() - samples.started
                        samples.received = target.stat().st_size
                    else:
                        directory.mkdir(parents=True)
                        adapter = make_adapter(config, directory, engine)
                        peer = str(parse_message_url(args.url)[0])
                        events = _TransferEvents(samples, message.id, peer, message.file.name, "", message.file.size)
                        target = await adapter._download_archive_async(client, message, message.file.name, events)
                    digest = await asyncio.to_thread(validate_download, target, message.file.size, config)
                    row["sha256"] = digest
                    if expected_hash is not None and digest != expected_hash:
                        raise RuntimeError("The trial file differs from the previous verified download.")
                    expected_hash = digest
                    row.update(samples.report(message.file.size, status="completed",
                                              total_seconds=time.monotonic() - samples.started))
                    row["file"] = str(target.resolve())
                except (Exception, asyncio.CancelledError) as exc:
                    row.update(samples.report(message.file.size, status="cancelled" if isinstance(exc, asyncio.CancelledError)
                                              else "failed", total_seconds=time.monotonic() - samples.started))
                    # Reports intentionally omit exception text that may contain session or credential data.
                    row["error_type"] = type(exc).__name__
                    if engine == "tdl":
                        row["log"] = str(directory / "tdl-console.log")
                    if isinstance(exc, asyncio.CancelledError):
                        records.append(row)
                        save_report(output / "report.json", report)
                        raise
                records.append(row)
                save_report(output / "report.json", report)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        await client.disconnect()
        from xianyu_pipeline.tdlib_client import close_clients
        close_clients()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/test.local.json")
    parser.add_argument("--tdl", type=Path, default=DEFAULT_TDL)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Check installation, proxy TCP and existing session.")
    mode.add_argument("--login-tdl", action="store_true", help="Interactive QR authorization in this terminal.")
    mode.add_argument("--url", help="Telegram ZIP/RAR message link to benchmark.")
    parser.add_argument("--engines", nargs="+", choices=("telethon", "tdlib", "tdl"), default=["telethon", "tdl"])
    parser.add_argument("--runs", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/download-benchmarks")
    args = parser.parse_args(argv)
    config = PipelineConfig.from_json(args.config)
    if args.url:
        parse_message_url(args.url)
    output = args.output.resolve() / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "preflight": preflight(config, args.tdl)}
    report_path = output / "report.json"
    save_report(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print("Report: " + str(report_path), flush=True)
    checks = report["preflight"]
    if args.check:
        return 0 if checks["ready_for_network_test"] else 2
    if checks["proxy"]["reachable"] is False:
        return 2
    if args.login_tdl:
        if not checks["tdl"]["installed"]:
            return 2
        command = tdl_command(args.tdl, config.telegram_api_proxy_url, config.download_workers)
        print("Authorize the SAME account as the application. Credentials stay in the interactive terminal.", flush=True)
        return subprocess.call(command + ["login", "--type", "qr"], env=tdl_environment())
    if not checks["session_key_present"] or ("tdl" in args.engines and not checks["tdl"]["installed"]):
        return 2
    configure_logging(output / "logs")
    try:
        asyncio.run(benchmark(config, args, output, report))
    except KeyboardInterrupt:
        report["status"] = "cancelled"
        return 130
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        print("Benchmark failed: " + type(exc).__name__, file=sys.stderr)
        return 1
    finally:
        save_report(report_path, report)
    return 0 if report.get("runs") and all(row["status"] == "completed" for row in report["runs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
