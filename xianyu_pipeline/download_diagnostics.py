"""Bounded download traces; never serialize Telegram requests or credentials."""
from __future__ import annotations

import io
import json
import logging
import math
import re
import sys
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4


LOG_NAME = "download-diagnostics.jsonl"
_events = logging.getLogger("xianyu_pipeline.download_diagnostics.events")
_events.propagate = False
_events.addHandler(logging.NullHandler())


def _emit(event: str, **fields) -> None:
    try:
        _events.info(json.dumps(
            {"version": 1, "ts": time.time(), "event": event, **fields},
            ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    except Exception:
        # Diagnostics must never fail a transfer or log raw request objects.
        pass


class TransportLogHandler(logging.Handler):
    """Record allowlisted transport events, without raw messages or task attribution."""

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith("telethon."):
            return
        try:
            text = record.getMessage().lower()
            kind = next((kind for pattern, kind in (
                ("closing current connection to begin reconnect", "reconnect"),
                ("failed reconnection", "reconnect_failed"),
                ("at connecting failed", "connect_failed"),
                ("connecting to ", "connect_start"),
                ("connection to ", "connected"),
                ("file lives in another dc", "dc_migration"),
                ("exporting auth for new borrowed sender", "auth_export"),
                ("file ref expired during download", "file_reference_refresh"),
                ("timeout", "timeout"),
                ("flood", "flood_wait"),
                ("connection closed while", "connection_closed"),
                ("system clock is wrong", "clock_offset"),
            ) if pattern in text), None)
            if kind is None:
                return
            fields = {"kind": kind}
            if endpoint := re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})/", text):
                fields["endpoint"] = f"{endpoint[1]}:{endpoint[2]}"
            if dc := re.search(r"dcoption\(id=(\d+)", text):
                fields["dc_id"] = int(dc[1])
            if wait := re.search(r"for (\d+)s .*flood wait", text):
                fields["wait_s"] = int(wait[1])
            _emit("transport", **fields)
        except Exception:
            pass


def configure_diagnostics(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    for handler in list(_events.handlers):
        if isinstance(handler, RotatingFileHandler):
            _events.removeHandler(handler)
            handler.close()
    handler = RotatingFileHandler(log_dir / LOG_NAME, maxBytes=5 * 1024 * 1024,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    _events.addHandler(handler)
    _events.setLevel(logging.INFO)
    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing, TransportLogHandler):
            root.removeHandler(existing)
            existing.close()
    transport = TransportLogHandler()
    transport._pipeline_handler = True
    root.addHandler(transport)
    return (log_dir / LOG_NAME).resolve()


class DownloadTrace:
    def __init__(self, *, message, filename: str, client, workers: int,
                 file_concurrency: int, crypto_backend: str, proxy: dict | None):
        self.id = uuid4().hex
        self.started = time.monotonic()
        self.cpu_started = time.process_time()
        self.received = 0
        self.first_byte_s = None
        self.last_progress = self.started
        self.last_sample = self.started
        self.max_progress_gap_s = 0.0
        self.max_loop_lag_s = 0.0
        self.queue_s = self.fetch_s = self.write_s = self.callback_s = 0.0
        self.chunks = 0
        self.senders: dict[int, str] = {}
        self.inflight: dict[int, dict] = {}
        self.last_heartbeat = self.started
        self.transferred_at = None
        self.validation_at = None
        self.save_at = None
        self.stage = "prepare"
        session = getattr(client, "session", None)
        self.metadata = {
            "message_id": message.id,
            "channel_id": getattr(getattr(message, "peer_id", None), "channel_id", None),
            "filename": filename,
            "size": getattr(message.file, "size", None),
            "file_dc": getattr(getattr(message, "document", None), "dc_id", None),
            "session_dc": getattr(session, "dc_id", None),
            "workers": workers,
            "file_concurrency": file_concurrency,
            "crypto_backend": crypto_backend,
            "proxy": {key: proxy[key] for key in ("proxy_type", "addr", "port") if key in proxy}
            if proxy else None,
            "python_version": sys.version.split()[0],
            "telethon_version": getattr(sys.modules.get("telethon"), "__version__", "unknown"),
        }

    def event(self, kind: str, **fields) -> None:
        _emit(kind, download_id=self.id, **fields)

    def begin(self, parallel: bool, request_bytes: int) -> None:
        self.stage = "transfer"
        self.metadata["configured_workers"] = self.metadata["workers"]
        self.metadata["workers"] = min(self.metadata["workers"],
                                       math.ceil(self.metadata["size"] / request_bytes)) if parallel else 1
        self.event("start", **self.metadata, parallel=parallel,
                   request_bytes=request_bytes if parallel else None)

    def request_state(self, worker: int, offset: int, stage: str) -> None:
        self.inflight[worker] = {"offset": offset, "stage": stage, "since": time.monotonic()}

    def request_failed(self, worker: int, error: BaseException) -> None:
        state = self.inflight.pop(worker, None)
        if state is not None:
            self.event("request_failed", worker=worker, offset=state["offset"],
                       stage=state["stage"], elapsed_s=time.monotonic() - state["since"],
                       error_type=type(error).__name__)

    def heartbeat(self) -> None:
        now = time.monotonic()
        if now - self.last_heartbeat < 5:
            return
        self.event("heartbeat", received=self.received, elapsed_s=now - self.started,
                   no_progress_s=now - self.last_progress,
                   requests=[{"worker": worker, "offset": state["offset"],
                              "stage": state["stage"], "elapsed_s": now - state["since"]}
                             for worker, state in self.inflight.items()])
        self.last_heartbeat = now

    def progress(self, received: int) -> None:
        now = time.monotonic()
        if received > self.received:
            if self.first_byte_s is None:
                self.first_byte_s = now - self.started
                self.event("first_byte", elapsed_s=self.first_byte_s)
            self.max_progress_gap_s = max(self.max_progress_gap_s, now - self.last_progress)
            self.last_progress = now
            self.received = received
        if now - self.last_sample >= 5:
            self.event("progress", received=self.received, elapsed_s=now - self.started)
            self.last_sample = now

    def chunk(self, *, worker: int, offset: int, size: int, queue_s: float,
              fetch_s: float, write_s: float, sender=None) -> None:
        self.chunks += 1
        self.queue_s += queue_s
        self.fetch_s += fetch_s
        self.write_s += write_s
        self.inflight.pop(worker, None)
        connection = None
        if sender is not None:
            connection = self.senders.setdefault(id(sender), f"sender_{len(self.senders) + 1}")
        self.event("chunk", worker=worker, offset=offset, bytes=size,
                   queue_s=queue_s, fetch_s=fetch_s, write_s=write_s,
                   sender=connection, dc_id=getattr(sender, "dc_id", None))

    def transferred(self) -> None:
        self.transferred_at = time.monotonic()
        self.cpu_s = time.process_time() - self.cpu_started

    def validating(self) -> None:
        self.stage = "validation"
        self.validation_at = time.monotonic()
        self.event("validation_start")

    def saving(self) -> None:
        self.stage = "save"
        self.save_at = time.monotonic()

    def finish(self, status: str, error: BaseException | None = None) -> None:
        now = time.monotonic()
        transfer_s = max(0.000001, (self.transferred_at or now) - self.started)
        self.event(
            "finish", **self.metadata, status=status, stage=self.stage,
            received=self.received, first_byte_s=self.first_byte_s,
            transfer_s=transfer_s, total_s=now - self.started,
            average_bps=self.received / transfer_s,
            validation_s=(self.save_at or now) - self.validation_at if self.validation_at else 0,
            save_s=now - self.save_at if self.save_at else 0,
            chunks=self.chunks, queue_wait_sum_s=self.queue_s,
            fetch_wait_sum_s=self.fetch_s, disk_write_s=self.write_s,
            progress_callback_s=self.callback_s,
            process_cpu_s=getattr(self, "cpu_s", time.process_time() - self.cpu_started),
            max_progress_gap_s=max(self.max_progress_gap_s, now - self.last_progress)
            if self.transferred_at is None else self.max_progress_gap_s,
            max_event_loop_lag_s=self.max_loop_lag_s,
            observed_senders=len(self.senders),
            error_type=type(error).__name__ if error else None)


def diagnostic_files(log_dir: Path) -> list[tuple[str, bytes]]:
    # Oldest first, to preserve start/finish order across rotations.
    paths = [log_dir / f"{LOG_NAME}.{index}" for index in (3, 2, 1)] + [log_dir / LOG_NAME]
    handler = next((handler for handler in _events.handlers
                    if isinstance(handler, RotatingFileHandler)
                    and Path(handler.baseFilename) == (log_dir / LOG_NAME).resolve()), None)
    if handler:
        handler.acquire()
    try:
        return [(path.name, path.read_bytes()) for path in paths if path.is_file()]
    finally:
        if handler:
            handler.release()


def analyze_files(files: list[tuple[str, bytes]]) -> dict:
    downloads, transport = {}, Counter()
    invalid = 0
    for _, data in files:
        for line in data.decode("utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or row.get("version") != 1:
                    raise ValueError("unknown event")
                for key in ("ts", "workers", "observed_senders", "first_byte_s",
                            "transfer_s", "average_bps", "received", "validation_s",
                            "disk_write_s", "progress_callback_s", "queue_wait_sum_s",
                            "fetch_wait_sum_s", "max_event_loop_lag_s", "elapsed_s"):
                    value = row.get(key)
                    if value is not None and (type(value) not in (int, float)
                                              or not math.isfinite(value) or value < 0):
                        raise ValueError("invalid numeric field")
                kind = row.get("event")
                if kind == "transport":
                    if not isinstance(row.get("kind"), str):
                        raise ValueError("invalid transport event")
                    transport[row["kind"]] += 1
                    continue
                identity = row.get("download_id")
                if not isinstance(identity, str):
                    continue
                entry = downloads.setdefault(identity, {"download_id": identity, "samples": [],
                                                         "senders": set(), "summary": {},
                                                         "started": False})
                if kind == "chunk":
                    sample = row.get("fetch_s")
                    if isinstance(sample, (int, float)) and math.isfinite(sample) and sample >= 0:
                        entry["samples"].append(sample)
                    if isinstance(row.get("sender"), str):
                        entry["senders"].add(row["sender"])
                elif kind in {"progress", "heartbeat"} and entry["summary"].get("event") != "finish":
                    received, elapsed = row.get("received"), row.get("elapsed_s")
                    if received is not None and elapsed is not None and elapsed > 0:
                        entry["summary"].update(received=received, transfer_s=elapsed,
                                                average_bps=received / elapsed)
                elif kind == "first_byte":
                    entry["summary"]["first_byte_s"] = row.get("elapsed_s")
                elif kind in {"start", "finish"}:
                    entry["summary"].update(row)
                    if kind == "start":
                        entry["started"] = True
            except (ValueError, TypeError, KeyError):
                invalid += 1
    rows = []
    for entry in downloads.values():
        row = dict(entry["summary"])
        if not row:
            continue
        row["status"] = row.get("status", "unfinished")
        row["download_id"] = entry["download_id"]
        row.setdefault("observed_senders", len(entry["senders"]))
        samples = sorted(entry["samples"])
        row["recorded_chunks"] = len(samples)
        row["fetch_p50_s"] = samples[(len(samples) - 1) // 2] if samples else None
        row["fetch_p95_s"] = samples[max(0, math.ceil(len(samples) * .95) - 1)] if samples else None
        findings = []
        if row.get("crypto_backend") == "Python":
            findings.append("观察到 Python 解密后端；建议启用 cryptg 后用同一文件复测。")
        if row.get("workers", 1) > 1 and row.get("observed_senders") == 1:
            findings.append("分块使用同一个 sender；并发请求未形成独立媒体连接，是否限制速度需对照测试。")
        if (row.get("first_byte_s") or 0) >= 10:
            findings.append("首字节等待超过 10 秒；需结合连接与跨 DC 授权事件排查。")
        if row.get("disk_write_s", 0) >= max(1, row.get("transfer_s", 0) * .2):
            findings.append("写盘耗时占传输时间至少 20%；检查下载盘、杀毒扫描与磁盘负载。")
        if row.get("progress_callback_s", 0) >= max(1, row.get("transfer_s", 0) * .2):
            findings.append("进度回调耗时占传输时间至少 20%；需检查任务记录保存与界面更新。")
        if row.get("fetch_p95_s") is not None and row["fetch_p95_s"] >= 1:
            findings.append("分块等待 P95 至少 1 秒；等待包含连接、重试、解密与调度，不能单独证明代理或服务端限速。")
        if row.get("queue_wait_sum_s", 0) >= max(1, row.get("fetch_wait_sum_s", 0)):
            findings.append("媒体请求排队累计耗时较高；检查并行文件和图片是否争抢请求额度。")
        if row.get("max_event_loop_lag_s", 0) >= .5:
            findings.append("事件循环出现至少 0.5 秒延迟；需结合进程 CPU、写盘和进度回调判断本地阻塞。")
        if row.get("validation_s", 0) >= max(5, row.get("transfer_s", 0) * .25):
            findings.append("完整性校验耗时较长；该阶段已与传输耗时分开统计。")
        if not samples and row.get("received", 0) > 0 and row.get("workers") == 1:
            findings.append("顺序下载由 Telethon 内部处理分块和写盘，本次没有可拆分的逐块耗时。")
        if row["status"] == "unfinished":
            findings.append("没有结束记录：可能仍在下载、程序中断或日志已轮转，不能据此判断下载失败。")
        if not entry["started"]:
            findings.append("起始记录已缺失，部分分块统计可能不完整。")
        row["findings"] = findings or ["当前指标未触发明显异常；仍需同文件、同代理出口对照测速。"]
        rows.append(row)
    return {
        "downloads": rows, "transport_events": dict(transport), "invalid_lines": invalid,
        "notes": [
            "连接、迁移、重连事件是整个客户端的记录，可能来自图片或其他文件，不直接归因于某一文件。",
            "分块等待与排队是各任务耗时之和，存在并行重叠，不能与总传输时间直接相加。",
            "进程 CPU 包含同期其他任务；fetch 等待包含解密与调度，不是纯网络 RTT。",
            "日志保留最近约 20 MiB；未记录 API Hash、会话密钥、验证码或文件内容。",
        ],
    }


def render_report(report: dict) -> str:
    lines = ["Telegram 下载诊断", ""]
    if not report["downloads"]:
        lines.append("暂无文件下载诊断记录。请在更新后的程序中下载一次文件后再次导出。")
    for row in report["downloads"]:
        lines.extend([
            f"文件: {row.get('filename', '?')}  消息: {row.get('message_id', '?')}",
            f"诊断 ID: {row['download_id']}  状态: {row['status']}",
            f"传输: {row.get('transfer_s', 0):.2f} 秒  "
            f"平均: {row.get('average_bps', 0) / 1024 / 1024:.3f} MiB/s  "
            f"接收: {row.get('received', 0)} 字节",
            f"首字节: {row.get('first_byte_s')} 秒  校验: {row.get('validation_s', 0):.3f} 秒  "
            f"写盘: {row.get('disk_write_s', 0):.3f} 秒  "
            f"进度回调: {row.get('progress_callback_s', 0):.3f} 秒",
            f"分块: {row['recorded_chunks']}  "
            f"等待 P50/P95: {row['fetch_p50_s']}/{row['fetch_p95_s']} 秒  "
            f"观察到 sender: {row.get('observed_senders', '?')}",
            *[f"- {finding}" for finding in row["findings"]], "",
        ])
    lines.extend([f"客户端连接事件: {json.dumps(report['transport_events'], ensure_ascii=False)}",
                  f"无法解析的日志行: {report['invalid_lines']}", "", *report["notes"]])
    return "\n".join(lines) + "\n"


def export_diagnostics(log_dir: Path) -> bytes:
    files = diagnostic_files(log_dir)
    report = analyze_files(files)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files:
            archive.writestr(name, data)
        archive.writestr("analysis.txt", render_report(report))
        archive.writestr("analysis.json", json.dumps(report, ensure_ascii=False, indent=2))
        archive.writestr("exported-at.txt", datetime.now(timezone.utc).isoformat())
    return output.getvalue()
