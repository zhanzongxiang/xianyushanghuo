from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event
from urllib.parse import urlparse

from .adapters import (LocalCloudStorageAdapter, LocalTelegramAdapter,
                       TelethonTelegramAdapter, parse_telegram_api_proxy, parse_telegram_dc2_server)
from .config import PipelineConfig
from .pipeline import run_pipeline
from .quark_browser import QuarkBrowserStorageAdapter
from .quark_cli import QuarkCLIStorageAdapter, validate_quark_cli_settings
from .quark_api import QuarkAPIStorageAdapter, validate_cookie, validate_quark_api_settings
from .runtime import configure_logging, validate_browser_path, telegram_crypto_backend
from .archive_validation import find_archive_tester, validate_rar
from .browser_profiles import check_browser, resolve_browser_profile, version_text
from .browser_download import DownloadCancelled
from .channel_catalog import ChannelCatalog
from .channel_sources import ChannelDirectory, normalize_public_channel
from .local_settings import remember_telegram_settings, restore_telegram_settings
from .workbench import TransferStore, channel_key, image_type
from .admin_auth import AuthHandlerMixin, AdminAuth, LOGOUT_HTML
from .download_diagnostics import export_diagnostics
from .naming import NameTranslator, validate_name, validate_naming_settings


class MemoryLogHandler(logging.Handler):
    def __init__(self, state: "WebState"):
        super().__init__()
        self.state = state

    def emit(self, record: logging.LogRecord) -> None:
        self.state.add_log(self.format(record))


class WebState:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.logs: list[str] = []
        self.running = False
        self.action = ""
        self.error = ""
        self.stop_event = Event()
        self.channel_scan: dict = {}
        self.channel_scan_started = 0.0
        self.channel_requests = deque()
        self.channel_request_keys = set()
        self.channel_requests_open = False
        self.telegram_channels = {"stage": "idle", "scanned_at": "", "error": "",
                                  "found": 0, "added": 0, "updated": 0}
        self.lock = threading.Lock()
        # Serialize configuration saves and validated starts without holding the log lock.
        self.control_lock = threading.Lock()

    def add_log(self, message: str) -> None:
        with self.lock:
            self.logs.append(message)
            self.logs = self.logs[-500:]

    def snapshot(self) -> dict:
        with self.lock:
            scan = dict(self.channel_scan)
            if scan and "elapsed_seconds" not in scan:
                scan["elapsed_seconds"] = round(time.monotonic() - self.channel_scan_started, 1)
            telegram_channels = dict(self.telegram_channels)
            return {"running": self.running, "logs": list(self.logs), "action": self.action,
                    "error": self.error, "channel_scan": scan,
                    "channel_requests_open": self.channel_requests_open,
                    "telegram_channels": telegram_channels}

    def take_channel_request(self) -> dict | None:
        with self.lock:
            return self.channel_requests.popleft() if self.channel_requests else None

    def close_channel_requests(self) -> bool:
        with self.lock:
            if self.channel_requests:
                return False
            self.channel_requests_open = False
            return True

    def scan_progress(self, progress: dict) -> None:
        with self.lock:
            self.channel_scan.update(progress)
            if progress.get("stage") in {"completed", "failed", "cancelled"}:
                self.channel_scan["elapsed_seconds"] = round(time.monotonic() - self.channel_scan_started, 1)


HTML = (Path(__file__).parent / "static" / "workbench.html").read_text(encoding="utf-8")


def config_payload(config: PipelineConfig) -> dict:
    try:
        parse_telegram_api_proxy(config.telegram_api_proxy_url)
        visible_proxy_url = config.telegram_api_proxy_url
    except ValueError:
        visible_proxy_url = ""
    return {
        "source_chat": "",
        "browser_executable_path": config.browser_executable_path,
        "telegram_api_id": config.telegram_api_id,
        "telegram_api_configured": bool(config.telegram_api_hash),
        "telegram_api_proxy_url": visible_proxy_url,
        "telegram_api_dc2_server": config.telegram_api_dc2_server,
        "download_start_timeout_seconds": config.download_start_timeout_seconds,
        "download_transfer_timeout_seconds": config.download_transfer_timeout_seconds,
        "download_workers": config.download_workers,
        "download_file_concurrency": config.download_file_concurrency,
        "telegram_crypto_backend": telegram_crypto_backend(),
        "archive_test_executable_path": config.archive_test_executable_path,
        "inbox_dir": str(config.inbox_dir.resolve()),
        "download_dir": str(config.inbox_dir.resolve()),
        "unverified_dir": str((config.inbox_dir / ".unverified").resolve()),
        "log_path": str((config.output_dir / "logs" / "pipeline.log").resolve()),
        "source_channel_url": config.source_channel_url,
        "fetch_mode": "recent",
        "recent_message_limit": config.recent_message_limit,
        "channel_message_limit": config.channel_message_limit,
        "telegram_run_mode": "once",
        "message_ids": [],
        "telegram_access_mode": "api",
        "exclude_forwarded_posts": config.exclude_forwarded_posts,
        "upload_provider": config.upload_provider,
        "quark_cookie_configured": bool(config.quark_cookie),
        "quark_api_parent_fid": config.quark_api_parent_fid,
        "quark_api_timeout_seconds": config.quark_api_timeout_seconds,
        "quark_api_file_concurrency": config.quark_api_file_concurrency,
        "quark_folder_url": config.quark_folder_url,
        "quark_headless": config.quark_headless,
        "quark_share_expire_days": config.quark_share_expire_days,
        "auto_upload_after_download": config.auto_upload_after_download,
        "rename_translation_provider": config.rename_translation_provider,
        "rename_glossary": config.rename_glossary,
        "quark_cli_mode": config.quark_cli_mode,
        "quark_cli_executable_path": config.quark_cli_executable_path,
        "quark_cli_wsl_distribution": config.quark_cli_wsl_distribution,
        "quark_cli_node_command": config.quark_cli_node_command,
        "quark_cli_script_path": config.quark_cli_script_path,
        "quark_cli_parent_fid": config.quark_cli_parent_fid,
        "quark_cli_timeout_seconds": config.quark_cli_timeout_seconds,
        "quark_cli_share_expired_type": config.quark_cli_share_expired_type,
        "quark_cli_share_url_type": config.quark_cli_share_url_type,
    }


def browser_payload(config: PipelineConfig) -> dict:
    if config.upload_provider != "quark_browser":
        return {"browser_version": "", "browser_profile_dir": "", "browser_profile_status": "",
                "browser_profile_error": ""}
    try:
        profile = resolve_browser_profile(config.quark_browser_profile, config.browser_executable_path)
        return {"browser_version": version_text(profile.version), "browser_profile_dir": str(profile.directory),
                "browser_profile_status": profile.status, "browser_profile_error": ""}
    except (ValueError, RuntimeError, OSError) as exc:
        return {"browser_version": "", "browser_profile_dir": "", "browser_profile_status": "",
                "browser_profile_error": str(exc)}


def write_config(path: Path, payload: dict) -> None:
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    updates = dict(payload)
    for secret in ("telegram_api_hash", "quark_cookie"):
        if not updates.get(secret):
            updates.pop(secret, None)
    existing.update(updates)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    remember_telegram_settings(path, existing)


def prepare_download_directory(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请填写下载保存目录")
    path = Path(value.strip())
    if not path.is_absolute():
        raise ValueError("下载保存目录必须是绝对路径")
    path = path.resolve()
    if path == Path(path.anchor):
        raise ValueError("请选择磁盘根目录下的文件夹，不要使用磁盘根目录")
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path):
            pass
    except OSError as exc:
        raise ValueError(f"下载保存目录不可写: {exc}") from exc
    return path


def validate_upload_batch_names(tasks: list[dict]) -> None:
    names = set()
    for task in tasks:
        name = (task.get("upload_filename", "") if task["upload_status"] == "link_pending"
                else Path(task.get("local_path", "")).name)
        keys = {value.casefold() for value in (name, task.get("rename_target", "")) if value}
        if keys & names:
            raise ValueError("本批次包含同名文件，请分批上传并核对内容，避免在目标目录生成重名文件")
        names.update(keys)


def validate_browser_settings(payload: dict, require_browser: bool = True) -> None:
    if not isinstance(payload.get("browser_executable_path", ""), str):
        raise ValueError("浏览器程序路径必须为字符串")
    if require_browser:
        validate_browser_path(payload.get("browser_executable_path", ""))
    for key, maximum in (("download_start_timeout_seconds", 86400),
                         ("download_transfer_timeout_seconds", 604800)):
        value = payload.get(key)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"{key} 必须为 1 到 {maximum} 的整数秒")
    if not isinstance(payload.get("archive_test_executable_path", ""), str):
        raise ValueError("RAR 校验程序路径必须为字符串")
    if payload.get("archive_test_executable_path"):
        find_archive_tester(payload["archive_test_executable_path"])


def validate_source_settings(payload: dict, existing: PipelineConfig) -> None:
    mode = payload.get("telegram_access_mode", existing.telegram_access_mode)
    if mode != "api":
        raise ValueError("仅支持 Telegram API 获取消息，请先保存 API 配置")
    if type(payload.get("exclude_forwarded_posts", existing.exclude_forwarded_posts)) is not bool:
        raise ValueError("排除转发帖选项必须为布尔值")
    workers = payload.get("download_workers", existing.download_workers)
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("download_workers 必须为 1 到 8 的整数")
    file_concurrency = payload.get("download_file_concurrency", existing.download_file_concurrency)
    if type(file_concurrency) is not int or not 1 <= file_concurrency <= 2:
        raise ValueError("download_file_concurrency 必须为 1 到 2 的整数")
    source = payload.get("source_channel_url", existing.source_channel_url)
    if source:
        normalize_public_channel(source)
    channel_limit = payload.get("channel_message_limit", existing.channel_message_limit)
    if type(channel_limit) is not int or not 1 <= channel_limit <= 100:
        raise ValueError("频道消息每轮读取数量必须为 1 到 100 的整数")
    if type(payload.get("auto_upload_after_download", existing.auto_upload_after_download)) is not bool:
        raise ValueError("下载后自动上传开关必须为布尔值")
    expiry = payload.get("quark_share_expire_days", existing.quark_share_expire_days)
    if type(expiry) is not int or expiry not in {0, 1, 7, 30}:
        raise ValueError("分享有效期仅支持1、7、30天或永久")
    provider = payload.get("upload_provider", existing.upload_provider)
    if provider not in {"local", "quark_browser", "quark_cli", "quark_api"}:
        raise ValueError("上传方式必须为本地模拟、夸克网页、夸克 CLI / WSL 或夸克 API")
    if "quark_cookie" in payload:
        validate_cookie(payload["quark_cookie"], required=False)
    validate_quark_api_settings(payload, existing, require_cookie=provider == "quark_api")
    validate_naming_settings(payload, existing)
    validate_browser_settings(payload, require_browser=provider == "quark_browser")
    if provider == "quark_cli":
        validate_quark_cli_settings(payload, existing)
    validate_telegram_settings(payload, existing)


def validate_telegram_settings(payload: dict, existing: PipelineConfig) -> None:
    """Validate account access independently of source links and upload setup."""
    if payload.get("telegram_access_mode", existing.telegram_access_mode) != "api":
        raise ValueError("仅支持 Telegram API 获取消息，请先保存 API 配置")
    parse_telegram_api_proxy(payload.get("telegram_api_proxy_url", existing.telegram_api_proxy_url))
    parse_telegram_dc2_server(payload.get("telegram_api_dc2_server", existing.telegram_api_dc2_server))
    api_id = payload.get("telegram_api_id", existing.telegram_api_id)
    api_hash = payload.get("telegram_api_hash") or existing.telegram_api_hash
    if type(api_id) is not int or not 1 <= api_id <= 2147483647:
        raise ValueError("Telegram API ID 必须为正整数")
    if not isinstance(api_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise ValueError("Telegram API Hash 必须为 32 位十六进制字符串")
    if payload.get("telegram_run_mode", existing.telegram_run_mode) != "once":
        raise ValueError("Telegram API 仅支持抓取一次")


def start_web_ui(config_path: str | Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    config_path = Path(config_path).resolve()
    restore_telegram_settings(config_path)
    state = WebState(config_path)
    configure_logging(PipelineConfig.from_json(config_path).output_dir / "logs", MemoryLogHandler(state))
    transfers = TransferStore(PipelineConfig.from_json(config_path))
    catalog = ChannelCatalog(PipelineConfig.from_json(config_path).output_dir)
    channels = ChannelDirectory(PipelineConfig.from_json(config_path).output_dir)
    initial_config = PipelineConfig.from_json(config_path)
    if initial_config.source_channel_url:
        channels.ensure(initial_config.source_channel_url)
    logging.info("Telegram API 频道工作台; 配置文件=%s", config_path)

    auto_upload_candidates: set[str] = set()

    def transfer_event(event: dict) -> None:
        identity = transfers.event(event)
        if event.get("download_status") == "completed":
            transfers.enrich(identity)
            auto_upload_candidates.add(identity)

    def cloud_adapter(config: PipelineConfig, on_stage=None, on_progress=None, on_remote=None, on_phase=None):
        if config.upload_provider == "quark_api":
            return QuarkAPIStorageAdapter(
                config.quark_cookie, parent_fid=config.quark_api_parent_fid,
                timeout_seconds=config.quark_api_timeout_seconds,
                share_expire_days=config.quark_share_expire_days,
                on_stage=on_stage, on_progress=on_progress, on_remote=on_remote,
                on_phase=on_phase, stop_event=state.stop_event,
            )
        if config.upload_provider == "quark_browser":
            return QuarkBrowserStorageAdapter(folder_url=config.quark_folder_url, profile_dir=config.quark_browser_profile,
                headless=config.quark_headless, upload_input_selector=config.quark_upload_input_selector,
                file_row_selector=config.quark_file_row_selector, share_button_selector=config.quark_share_button_selector,
                share_link_selector=config.quark_share_link_selector, share_expire_days=config.quark_share_expire_days, share_create_button_selector=config.quark_share_create_button_selector, folder_ready_selector=config.quark_folder_ready_selector, timeout_seconds=config.quark_timeout_seconds,
                browser_executable_path=config.browser_executable_path, on_stage=on_stage,
                on_progress=on_progress)
        if config.upload_provider == "quark_cli":
            return QuarkCLIStorageAdapter(
                mode=config.quark_cli_mode,
                executable_path=config.quark_cli_executable_path,
                wsl_distribution=config.quark_cli_wsl_distribution,
                node_command=config.quark_cli_node_command,
                script_path=config.quark_cli_script_path,
                parent_fid=config.quark_cli_parent_fid,
                timeout_seconds=config.quark_cli_timeout_seconds,
                share_expired_type=config.quark_cli_share_expired_type,
                share_url_type=config.quark_cli_share_url_type,
                on_stage=on_stage,
                on_progress=on_progress,
                stop_event=state.stop_event,
            )
        if config.upload_provider != "local":
            raise ValueError("上传方式必须为本地模拟、夸克网页上传或夸克 CLI / WSL")
        return LocalCloudStorageAdapter(config.output_dir / "cloud_fixture")

    def api_adapter(config: PipelineConfig) -> TelethonTelegramAdapter:
        return TelethonTelegramAdapter(
            config.telegram_api_id, config.telegram_api_hash, config.telegram_session, config.inbox_dir,
            scan_limit=config.telegram_scan_limit, archive_test_executable_path=config.archive_test_executable_path,
            archive_test_timeout_seconds=config.archive_test_timeout_seconds,
            proxy_url=config.telegram_api_proxy_url, dc2_server=config.telegram_api_dc2_server,
            download_start_timeout_seconds=config.download_start_timeout_seconds,
            download_transfer_timeout_seconds=config.download_transfer_timeout_seconds,
            download_workers=config.download_workers,
            download_file_concurrency=config.download_file_concurrency,
            exclude_forwarded_posts=config.exclude_forwarded_posts,
            stop_event=state.stop_event, on_event=transfer_event)

    def selected_channel(config: PipelineConfig) -> dict | None:
        if not config.source_channel_url:
            return None
        try:
            return channels.get(config.source_channel_url)
        except ValueError:
            return None

    def require_selected_channel(config: PipelineConfig) -> None:
        if selected_channel(config) is None:
            raise ValueError("请先在来源频道中保存并选择频道，再获取频道消息")

    def media_job_result(config: PipelineConfig, request: dict, result: dict, error: str) -> None:
        try:
            if request["kind"] == "cover":
                catalog.update_cover(config.source_channel_url, request["message_id"], **(
                    {"full_status": "cancelled" if state.stop_event.is_set() else "failed",
                     "full_error": error} if error else result))
            elif error:
                transfers.event({"chat": str(TelethonTelegramAdapter.normalize_channel(config.source_channel_url)),
                                 "message_id": request["message_id"], "filename": request["filename"],
                                 "download_status": "cancelled" if state.stop_event.is_set() else "failed",
                                 "error": error})
        except ValueError:
            logging.warning("媒体状态未写入，消息已不在当前缓存中: %s", request["message_id"])
        finally:
            with state.lock:
                state.channel_request_keys.discard((request["kind"], request["message_id"]))

    class Handler(AuthHandlerMixin, BaseHTTPRequestHandler):
        auth = AdminAuth.from_environment(host, port)

        def json_response(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if not self.auth_gate("GET"):
                return
            path = urlparse(self.path).path
            allowed_hosts = self.auth.hosts if self.auth else {f"{host}:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in allowed_hosts:
                self.json_response({"message": "访问地址未获允许"}, 403)
                return
            config = PipelineConfig.from_json(config_path)
            active_channel = selected_channel(config)
            if path == "/":
                body = (HTML + (LOGOUT_HTML if self.auth else "")).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            elif path == "/api/config":
                config = PipelineConfig.from_json(config_path)
                self.json_response({**config_payload(config), **browser_payload(config)})
            elif path == "/api/logs":
                self.json_response(state.snapshot())
            elif path == "/api/download-diagnostics/export":
                try:
                    body = export_diagnostics(config.output_dir / "logs")
                except OSError:
                    self.json_response({"message": "下载诊断日志暂时无法读取"}, 500)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/workbench":
                self.json_response({**transfers.snapshot(), **{key: value for key, value in state.snapshot().items() if key != "logs"}})
            elif path == "/api/channels":
                config = PipelineConfig.from_json(config_path)
                self.json_response(channels.list(config.source_channel_url))
            elif path == "/api/telegram-channels":
                self.json_response(state.snapshot()["telegram_channels"])
            elif path == "/api/channel-messages":
                if active_channel:
                    self.json_response(catalog.snapshot(config.source_channel_url,
                                                        exclude_forwarded_posts=config.exclude_forwarded_posts))
                else:
                    self.json_response({"channel": "", "messages": [], "oldest_scanned_id": 0,
                                        "has_more": False, "scanned_at": ""})
            elif path in {"/static/workbench.css", "/static/workbench.js", "/static/lucide.js"}:
                asset = Path(__file__).parent / "static" / Path(path).name
                content_type = "text/css" if asset.suffix == ".css" else "text/javascript"
                self.file_response(asset, content_type + "; charset=utf-8")
            elif re.fullmatch(r"/api/preview/[a-f0-9]{24}/[0-9]+", path):
                try:
                    _, _, _, identity, index = path.split("/")
                    asset, content_type = transfers.preview(identity, int(index))
                    self.file_response(asset, content_type)
                except (ValueError, OSError):
                    self.send_error(404)
            elif re.fullmatch(r"/api/channel-(?:full-)?preview/[0-9]+", path):
                try:
                    require_selected_channel(config)
                    message_id = int(path.rsplit("/", 1)[1])
                    config = PipelineConfig.from_json(config_path)
                    asset, content_type = catalog.preview(config.source_channel_url, message_id,
                                                          exclude_forwarded_posts=config.exclude_forwarded_posts,
                                                          full=path.startswith("/api/channel-full-preview/"))
                    self.file_response(asset, content_type)
                except (ValueError, OSError):
                    self.send_error(404)
            elif re.fullmatch(r"/api/channel-avatar/[1-9][0-9]*", path):
                try:
                    identity = int(path.rsplit("/", 1)[1])
                    row = next((row for row in channels.list()["channels"]
                                if row.get("id") == identity and row.get("avatar_url")), None)
                    if row is None:
                        raise ValueError("频道头像不存在")
                    root = (config.output_dir / ".channel-avatars").resolve()
                    asset = (root / f"{identity}.jpg").resolve()
                    if asset.parent != root or image_type(asset) != "image/jpeg":
                        raise ValueError("频道头像不存在")
                    self.file_response(asset, "image/jpeg")
                except (ValueError, OSError):
                    self.send_error(404)
            else:
                self.send_error(404)

        def file_response(self, asset: Path, content_type: str) -> None:
            try:
                body = asset.read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if not self.auth_gate("POST"):
                return
            expected_hosts = {f"{host}:{port}", f"localhost:{port}"}
            if (self.headers.get("Host") not in expected_hosts
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"
                    or (self.headers.get("Origin") and self.headers["Origin"] not in
                        {f"http://{value}" for value in expected_hosts})):
                self.json_response({"message": "仅允许本机同源请求"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                self.json_response({"message": "请求长度无效"}, 400)
                return
            if length < 0 or length > 65536:
                self.json_response({"message": "请求体过大"}, 413)
                return
            body = self.rfile.read(length)
            path = urlparse(self.path).path
            try:
                arguments = json.loads(body or b"{}")
                if not isinstance(arguments, dict):
                    raise ValueError("请求必须为 JSON 对象")
            except (ValueError, UnicodeDecodeError) as exc:
                self.json_response({"message": str(exc)}, 400)
                return
            if path == "/api/config":
                with state.control_lock:
                    with state.lock:
                        busy = state.running
                    if busy:
                        self.json_response({"message": "任务运行中，不能修改配置"}, 409)
                        return
                    try:
                        existing = PipelineConfig.from_json(config_path)
                        allowed = set(config_payload(existing)) - {
                            "download_dir", "log_path", "unverified_dir", "telegram_api_configured",
                            "telegram_crypto_backend", "quark_cookie_configured",
                        }
                        allowed.update({"telegram_scan_limit", "cloud_folder", "telegram_api_hash", "quark_cookie"})
                        payload = {key: value for key, value in arguments.items() if key in allowed}
                        if arguments.get("telegram_access_mode", "api") != "api" or arguments.get("telegram_run_mode", "once") != "once":
                            raise ValueError("仅支持 Telegram API 抓取一次")
                        validate_source_settings({**config_payload(existing), **payload}, existing)
                        if "inbox_dir" in payload:
                            payload["inbox_dir"] = str(prepare_download_directory(payload["inbox_dir"]))
                        payload.update({"fetch_mode": "recent", "source_chat": "", "message_ids": [],
                                        "telegram_access_mode": "api", "telegram_run_mode": "once"})
                        write_config(config_path, payload)
                        saved_config = PipelineConfig.from_json(config_path)
                        transfers.use_config(saved_config)
                        with state.lock:
                            if (saved_config.telegram_api_id != existing.telegram_api_id
                                    or saved_config.telegram_api_hash != existing.telegram_api_hash):
                                state.telegram_channels = {
                                    "stage": "idle", "scanned_at": "", "error": "",
                                    "found": 0, "added": 0, "updated": 0,
                                }
                            elif saved_config.source_channel_url != existing.source_channel_url:
                                state.channel_scan = {}
                        if saved_config.source_channel_url:
                            channels.ensure(saved_config.source_channel_url)
                    except (ValueError, RuntimeError, OSError) as exc:
                        self.json_response({"message": str(exc)}, 400)
                        return
                self.json_response({"message": "配置已保存"})
            elif path == "/api/names/save":
                with state.control_lock:
                    if state.running:
                        self.json_response({"message": "任务运行中，不能修改中文名称"}, 409)
                        return
                    try:
                        entries = validated_name_entries(arguments)
                        for identity, candidate in entries:
                            transfers.update(identity, chinese_filename=candidate, naming_source="manual",
                                             naming_status="review", naming_error="", naming_quality="manual",
                                             naming_untranslated=[], naming_note="手动编辑，待确认")
                    except (ValueError, RuntimeError, OSError) as exc:
                        self.json_response({"message": str(exc)}, 400)
                        return
                self.json_response({"message": "中文候选名已保存"})
            elif path in {"/api/channels", "/api/channels/select", "/api/telegram-channels/select",
                          "/api/channels/rename", "/api/channels/remove"}:
                with state.control_lock:
                    with state.lock:
                        if state.running:
                            self.json_response({"message": "任务运行中，不能切换或修改频道列表"}, 409)
                            return
                    try:
                        config = PipelineConfig.from_json(config_path)
                        if path == "/api/channels":
                            url = arguments.get("url")
                            if not isinstance(url, str) or not url.strip():
                                raise ValueError("请填写 Telegram 频道公开链接")
                            normalized = normalize_public_channel(url)
                            name = arguments.get("name", "")
                            if not isinstance(name, str):
                                raise ValueError("频道名称必须为字符串")
                            row = channels.add(channels.canonical_url(normalized), name)
                            self.json_response({"message": "频道已添加", "channel": row})
                            return
                        key = arguments.get("key")
                        if not isinstance(key, str) or not key.strip():
                            raise ValueError("频道标识无效")
                        if path in {"/api/channels/select", "/api/telegram-channels/select"}:
                            row = channels.get(key)
                            write_config(config_path, {"source_channel_url": row["url"]})
                            saved_config = PipelineConfig.from_json(config_path)
                            transfers.use_config(saved_config)
                            with state.lock:
                                state.channel_scan = {}
                            self.json_response({"message": "频道已切换", "channel": row})
                            return
                        if path == "/api/channels/rename":
                            name = arguments.get("name")
                            if not isinstance(name, str):
                                raise ValueError("频道名称必须为字符串")
                            row = channels.rename(key, name)
                            self.json_response({"message": "频道名称已更新", "channel": row})
                            return
                        row = channels.remove(key, config.source_channel_url)
                        self.json_response({"message": "频道已从列表移除", "channel": row})
                    except (ValueError, RuntimeError, OSError) as exc:
                        self.json_response({"message": str(exc)}, 400)
            elif path in {"/api/open-downloads", "/api/open-task"}:
                try:
                    directory = (transfers.local_file(str(arguments.get("id", ""))).parent if path == "/api/open-task"
                                 else PipelineConfig.from_json(config_path).inbox_dir.resolve())
                    directory.mkdir(parents=True, exist_ok=True)
                    os.startfile(str(directory))
                    self.json_response({"message": "已打开下载目录"})
                except (ValueError, OSError, AttributeError) as exc:
                    self.json_response({"message": f"打开目录失败: {exc}"}, 400)
            elif path == "/api/stop":
                if not state.running:
                    self.json_response({"message": "当前没有运行中的任务"}, 409); return
                state.stop_event.set()
                with state.lock:
                    state.channel_requests_open = False
                    if state.action == "channel-scan":
                        state.channel_scan["stop_requested"] = True
                logging.info("已请求停止；Telegram API 任务将中断")
                self.json_response({"message": "已请求停止"})
            elif path in {"/api/run", "/api/download", "/api/retry", "/api/upload", "/api/channel-scan",
                          "/api/names/generate", "/api/names/apply",
                          "/api/telegram-channels-scan",
                          "/api/channel-download", "/api/channel-cover", "/api/quark-login", "/api/quark-cli-login",
                          "/api/quark-cli-check", "/api/quark-api-check",
                          "/api/telegram-api-login", "/api/browser-check"}:
                with state.control_lock:
                    with state.lock:
                        busy = state.running
                        can_enqueue = (state.channel_requests_open and not state.stop_event.is_set()
                                       and path in {"/api/channel-download", "/api/channel-cover"})
                    if busy and not can_enqueue:
                        self.json_response({"message": "已有任务运行中"}, 409)
                        return
                    try:
                        config = PipelineConfig.from_json(config_path)
                        if path in {"/api/names/generate", "/api/names/apply"}:
                            identities = arguments.get("ids")
                            if (not isinstance(identities, list) or not 1 <= len(identities) <= 100
                                    or any(not isinstance(identity, str) for identity in identities)
                                    or len(set(identities)) != len(identities)):
                                raise ValueError("请选择 1 到 100 条不重复的素材")
                            chosen = [transfers.get(identity) for identity in identities]
                            for task in chosen:
                                if task["download_status"] != "completed":
                                    raise ValueError("请先下载素材，再生成或应用中文名")
                                if path == "/api/names/apply":
                                    validate_name(task.get("chinese_filename", ""), task["filename"])
                                    if task["upload_status"] in {"completed", "link_pending"}:
                                        if task.get("upload_provider") != "quark_api":
                                            raise ValueError("已上传文件的改名仅支持夸克 API")
                                        if not task.get("upload_remote_fid") and not task.get("upload_task_id"):
                                            raise ValueError("缺少原上传文件 ID 或任务 ID，请先补取链接")
                                    if config.upload_provider != "quark_api":
                                        raise ValueError("请先将上传方式设置为夸克 API")
                            if path == "/api/names/apply":
                                names = [row["chinese_filename"].casefold() for row in chosen]
                                if len(names) != len(set(names)):
                                    raise ValueError("选中的中文名称重复，请编辑后再应用")
                                validate_quark_api_settings({}, config)
                            validate_naming_settings({}, config)
                        elif path == "/api/upload":
                            identities = arguments.get("ids")
                            if not isinstance(identities, list) or not identities or len(identities) > 100 or not all(isinstance(item, str) for item in identities):
                                raise ValueError("请选择 1 到 100 个已下载素材")
                            if len(set(identities)) != len(identities):
                                raise ValueError("上传列表包含重复任务")
                            if config.upload_provider == "quark_api":
                                validate_upload_batch_names([transfers.get(identity) for identity in identities])
                            for identity in identities:
                                task = transfers.get(identity)
                                if (task["upload_status"] in {"completed", "uploading"}
                                        or task["upload_status"] == "local"
                                        and config.upload_provider not in {"quark_browser", "quark_cli", "quark_api"}):
                                    raise ValueError("选中任务已上传或正在上传")
                                if task["upload_status"] == "link_pending":
                                    if task.get("upload_target") != config.cloud_upload_target():
                                        raise ValueError("请恢复该任务原上传目标后再获取链接，不能改目录重复上传")
                                    if (config.upload_provider not in {"quark_browser", "quark_cli", "quark_api"}
                                            or task.get("upload_provider") != config.upload_provider):
                                        raise ValueError("请切回原夸克上传方式核对已有文件")
                                    transfers.upload_name(identity)
                                else:
                                    transfers.local_file(identity, completed=True)
                            if config.upload_provider == "quark_browser":
                                validate_browser_path(config.browser_executable_path)
                            cloud_adapter(config)
                        elif path in {"/api/browser-check", "/api/quark-login", "/api/quark-cli-login", "/api/quark-cli-check", "/api/quark-api-check"}:
                            if path == "/api/quark-api-check":
                                if config.upload_provider != "quark_api":
                                    raise ValueError("当前上传方式不是夸克 API")
                                validate_quark_api_settings({}, config)
                            elif path == "/api/quark-login" and config.upload_provider != "quark_browser":
                                raise ValueError("当前上传方式不是夸克网页上传")
                            if path in {"/api/quark-cli-login", "/api/quark-cli-check"} and config.upload_provider != "quark_cli":
                                raise ValueError("当前上传方式不是夸克 CLI / WSL")
                            if path in {"/api/browser-check", "/api/quark-login"}:
                                validate_browser_settings(config_payload(config))
                            elif config.upload_provider == "quark_cli":
                                validate_quark_cli_settings(config_payload(config), config)
                            elif path != "/api/quark-api-check":
                                raise ValueError("当前上传方式与该操作不匹配")
                        else:
                            if config.telegram_access_mode != "api":
                                raise ValueError("旧配置使用 Telegram Web，请在设置中保存 API 配置后重试")
                            if path in {"/api/telegram-channels-scan", "/api/telegram-api-login"}:
                                validate_telegram_settings(config_payload(config), config)
                            else:
                                validate_source_settings(config_payload(config), config)
                            if path in {"/api/channel-scan", "/api/channel-download", "/api/channel-cover"}:
                                require_selected_channel(config)
                                TelethonTelegramAdapter.normalize_channel(config.source_channel_url)
                            if path in {"/api/download", "/api/run"} and config.source_channel_url:
                                require_selected_channel(config)
                            if path == "/api/channel-scan":
                                if type(arguments.get("older", False)) is not bool:
                                    raise ValueError("扫描范围无效")
                                if arguments.get("older"):
                                    listing = catalog.snapshot(config.source_channel_url,
                                                               exclude_forwarded_posts=config.exclude_forwarded_posts)
                                    if not listing["has_more"] or not listing["oldest_scanned_id"]:
                                        raise ValueError("没有可继续读取的更早消息")
                            if path == "/api/telegram-channels-scan" and arguments:
                                raise ValueError("已加入频道扫描不接受请求参数")
                            if path in {"/api/channel-download", "/api/channel-cover"}:
                                ids = arguments.get("ids") if path == "/api/channel-download" else [arguments.get("id")]
                                if (not isinstance(ids, list) or not 1 <= len(ids) <= 20
                                        or any(type(mid) is not int or mid <= 0 for mid in ids)
                                        or len(set(ids)) != len(ids)):
                                    raise ValueError("请选择 1 到 20 条不重复的频道消息")
                                messages = {row["message_id"]: row for row in
                                            catalog.snapshot(config.source_channel_url,
                                                             exclude_forwarded_posts=config.exclude_forwarded_posts)["messages"]}
                                for mid in ids:
                                    if mid not in messages:
                                        raise ValueError(f"消息 {mid} 不在当前频道列表中，请先重新读取")
                                    if path == "/api/channel-download" and transfers.has_completed_message(config.source_channel_url, mid):
                                        raise ValueError(f"消息 {mid} 已下载，请在下载任务中查看")
                                if (path == "/api/channel-cover" and messages[ids[0]].get("full_cover_url")
                                        and (not arguments.get("original", False) or messages[ids[0]].get("full_quality") in {"full", "document_thumbnail"})):
                                    self.json_response({"message": "高清图已缓存",
                                                        "url": messages[ids[0]]["full_cover_url"]})
                                    return
                            if path == "/api/retry":
                                task = transfers.get(str(arguments.get("id", "")))
                                if not task.get("message_id") or not task.get("chat") or task["download_status"] not in {"failed", "cancelled", "interrupted"}:
                                    raise ValueError("仅支持重新下载失败或中断的 Telegram 任务")
                    except (ValueError, RuntimeError, OSError) as exc:
                        self.json_response({"message": str(exc)}, 400)
                        return
                    registration_error, prepared = "", []
                    with state.lock:
                        if path in {"/api/channel-download", "/api/channel-cover"}:
                            kind = "download" if path == "/api/channel-download" else "cover"
                            keys = [(kind, mid) for mid in ids]
                            if (busy and (not state.channel_requests_open or state.stop_event.is_set())
                                    or len(state.channel_request_keys) + len(keys) > 40
                                    or any(key in state.channel_request_keys for key in keys)):
                                self.json_response({"message": "任务已提交或队列暂不可用，请刷新后重试"}, 409)
                                return
                            try:
                                for mid, key in zip(ids, keys):
                                    row = messages[mid]
                                    prepared.append({"kind": kind, "message_id": mid, "filename": row["filename"],
                                                     "original": arguments.get("original") is True,
                                                     "cover_message_id": row.get("cover_message_id", 0)})
                                    if kind == "download":
                                        transfers.event({"chat": row["chat"], "message_id": mid,
                                                         "filename": row["filename"], "title": row.get("text") or row["filename"],
                                                         "source_url": row.get("source_url", ""), "size": row.get("size") or 0,
                                                         "download_status": "queued"})
                                    else:
                                        catalog.update_cover(config.source_channel_url, mid, full_status="loading", full_error="")
                                state.channel_request_keys.update(keys)
                                if kind == "cover" and not arguments.get("prefetch", False):
                                    state.channel_requests.extendleft(reversed(prepared))
                                else:
                                    state.channel_requests.extend(prepared)
                            except (ValueError, OSError) as exc:
                                registration_error = str(exc)
                        if not busy and not registration_error:
                            state.stop_event.clear()
                            state.running = True
                            state.action = path.removeprefix("/api/")
                            state.error = ""
                            state.channel_requests_open = path in {
                                "/api/channel-scan", "/api/channel-cover", "/api/channel-download"}
                        if path == "/api/channel-scan":
                            state.channel_scan_started = time.monotonic()
                            state.channel_scan = {"stage": "connecting", "target": config.channel_message_limit,
                                                  "found": 0, "scanned": 0, "processed": 0, "total": 0,
                                                  "ready": 0, "failed": 0, "no_cover": 0, "cached": 0}
                        if path == "/api/telegram-channels-scan":
                            state.telegram_channels = {"stage": "connecting", "scanned_at": "",
                                                       "error": "", "found": 0, "added": 0, "updated": 0}
                if registration_error:
                    for request in prepared:
                        try:
                            media_job_result(config, request, {}, registration_error)
                        except OSError:
                            logging.warning("任务提交失败，状态保存未完成", exc_info=True)
                    self.json_response({"message": f"任务提交失败: {registration_error}"}, 400)
                    return
                if not busy:
                    threading.Thread(target=worker, args=(path, arguments, config), daemon=True).start()
                self.json_response({"message": "任务已加入下载/图片队列" if busy else "任务已启动"})
            else:
                self.send_error(404)

        def log_message(self, format: str, *args) -> None:
            if args and isinstance(args[0], str) and any(route in args[0] for route in ("/api/logs", "/api/workbench", "/api/preview", "/static/")):
                return
            logging.info("Web %s", format % args)

    def upload_tasks(identities: list[str], config: PipelineConfig) -> None:
        if config.upload_provider == "quark_api":
            validate_upload_batch_names([transfers.get(identity) for identity in identities])

        def upload_one(identity: str) -> None:
            if state.stop_event.is_set():
                return
            task = transfers.get(identity)
            recovering = task["upload_status"] == "link_pending"
            try:
                def stage(value: str) -> None:
                    if value != "completed":
                        transfers.update(identity, upload_status=value)

                def progress(current: int, total: int, speed: float, eta: float | None) -> None:
                    transfers.update(identity, upload_received=max(0, int(current)), upload_size=max(0, int(total)),
                                     upload_speed=max(0, float(speed)), upload_eta=eta)

                cloud = cloud_adapter(
                    config, on_stage=stage, on_progress=progress,
                    on_remote=lambda values: transfers.update(identity, **values),
                    on_phase=lambda value: transfers.update(identity, upload_phase=value),
                )
                if recovering:
                    source = Path(transfers.upload_name(identity))
                    if not isinstance(cloud, (QuarkBrowserStorageAdapter, QuarkCLIStorageAdapter, QuarkAPIStorageAdapter)):
                        raise ValueError("请切回原夸克上传方式，核对已有文件分享链接")
                    transfers.update(identity, error="")
                    if isinstance(cloud, QuarkAPIStorageAdapter):
                        naming_options = {}
                        if task.get("remote_filename"):
                            naming_options["remote_name"] = task["remote_filename"]
                        if task.get("rename_target"):
                            naming_options["rename_to"] = task["rename_target"]
                        if task.get("rename_pending_name"):
                            naming_options["pending_name"] = task["rename_pending_name"]
                        result = cloud.share_existing(
                            source, task["upload_target"], remote_fid=task.get("upload_remote_fid", ""),
                            size=task.get("upload_size"), md5=task.get("upload_md5", ""),
                            sha1=task.get("upload_sha1", ""), upload_task_id=task.get("upload_task_id", ""),
                            **naming_options,
                        )
                    else:
                        result = cloud.share_existing(source, task["upload_target"])
                else:
                    source = transfers.local_file(identity, completed=True)
                    if source.suffix.lower() == ".zip":
                        with zipfile.ZipFile(source) as archive:
                            if archive.testzip():
                                raise ValueError("ZIP 完整性校验失败，不能上传")
                    else:
                        validate_rar(source, config.archive_test_executable_path, config.archive_test_timeout_seconds)
                    transfers.update(identity, validation="passed", error="", share_url="", upload_status="uploading",
                                     upload_provider=config.upload_provider, upload_filename=source.name,
                                     upload_local_path=str(source.resolve()), upload_target=config.cloud_upload_target(),
                                     upload_received=0, upload_size=source.stat().st_size, upload_speed=0, upload_eta=None,
                                     upload_phase="", upload_remote_fid="", upload_task_id="", upload_md5="", upload_sha1="")
                    new_name = task.get("rename_target", "") if config.upload_provider == "quark_api" else ""
                    result = cloud.upload(source, config.cloud_upload_target(), **({"new_name": new_name} if new_name else {}))
                final_status = "local" if config.upload_provider == "local" else "completed"
                final_size = task.get("upload_size", 0) if recovering else source.stat().st_size
                transfers.update(identity, upload_status=final_status, share_url=result, error="",
                                 upload_received=final_size, upload_size=final_size,
                                 upload_speed=0, upload_eta=None, upload_phase="completed")
                logging.info("素材上传结束: %s (%s)", source.name, config.upload_provider)
            except Exception as exc:
                current = transfers.get(identity)
                if current.get("naming_status") == "applying":
                    transfers.update(identity, naming_status="failed", naming_error=str(exc))
                status = "link_pending" if recovering or current["upload_status"] == "link_pending" else "failed"
                transfers.update(identity, upload_status=status, error=str(exc), upload_speed=0, upload_eta=None)
                if isinstance(exc, DownloadCancelled):
                    logging.info("素材上传已停止: %s", task["filename"])
                else:
                    with state.lock:
                        state.error = "部分素材上传未完成，请查看任务错误信息"
                    logging.exception("素材上传未完成: %s", task["filename"])

        concurrency = config.quark_api_file_concurrency if config.upload_provider == "quark_api" else 1
        logging.info("开始批量上传：%d 个素材，同时上传数=%d", len(identities), concurrency)
        if concurrency == 1:
            for identity in identities:
                upload_one(identity)
        else:
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="QuarkUpload") as pool:
                list(pool.map(upload_one, identities))

    def validated_name_entries(arguments):
        rows = arguments.get("entries")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
            raise ValueError("请提交 1 到 100 条名称")
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in seen:
                raise ValueError("名称记录无效或重复")
            task = transfers.get(row["id"])
            result.append((row["id"], validate_name(row.get("name"), task["filename"])))
            seen.add(row["id"])
        return result

    def naming_tasks(action, arguments, config):
        translator = NameTranslator(config, state.stop_event)
        for identity in arguments["ids"]:
            if state.stop_event.is_set():
                break
            task = transfers.get(identity)
            try:
                if action == "/api/names/generate":
                    transfers.update(identity, naming_status="generating", naming_error="")
                    suggestion = translator.suggest(task["filename"])
                    transfers.update(identity, **suggestion)
                else:
                    candidate = validate_name(task["chinese_filename"], task["filename"])
                    transfers.update(identity, rename_target=candidate, naming_status="approved", naming_error="")
                    if task["upload_status"] in {"completed", "link_pending"}:
                        transfers.update(identity, naming_status="applying")
                        cloud = cloud_adapter(config, on_remote=lambda values: transfers.update(identity, **values))
                        result = cloud.share_existing(
                            Path(task["upload_filename"]), task["upload_target"],
                            remote_fid=task.get("upload_remote_fid", ""), upload_task_id=task.get("upload_task_id", ""),
                            size=task.get("upload_size"), md5=task.get("upload_md5", ""), sha1=task.get("upload_sha1", ""),
                            remote_name=task.get("remote_filename", ""), rename_to=candidate,
                            pending_name=task.get("rename_pending_name", ""),
                        )
                        transfers.update(identity, share_url=result, upload_status="completed", error="")
                    translator.remember(task["filename"], candidate)
                    transfers.update(identity, naming_quality="confirmed", naming_untranslated=[],
                                     naming_note="已人工确认名称")
                    logging.info("中文名称已应用：%s → %s", task["filename"], candidate)
            except Exception as exc:
                transfers.update(identity, naming_status="failed", naming_error=str(exc))
                if isinstance(exc, DownloadCancelled):
                    break
                with state.lock:
                    state.error = "部分中文名称未完成，请查看命名结果"
                logging.warning("中文命名未完成：%s，%s", task["filename"], exc)

    def worker(action: str, arguments: dict, config: PipelineConfig) -> None:
        auto_upload_candidates.clear()
        try:
            logging.info("开始任务，上传方式=%s", config.upload_provider)
            if action in {"/api/names/generate", "/api/names/apply"}:
                naming_tasks(action, arguments, config)
                return
            if action == "/api/upload":
                upload_tasks(arguments["ids"], config)
                return
            if action == "/api/quark-api-check":
                cloud_adapter(config).check()
                return
            if action == "/api/browser-check":
                check_browser(config.quark_browser_profile, config.browser_executable_path)
                return
            if action == "/api/quark-login":
                if config.upload_provider != "quark_browser":
                    raise ValueError("当前上传方式不是夸克网页上传")
                from .quark_browser import login_and_inspect
                login_and_inspect(config.quark_folder_url, config.quark_browser_profile, config.browser_executable_path)
                logging.info("夸克登录流程结束")
                return
            if action == "/api/quark-cli-login":
                QuarkCLIStorageAdapter(
                    mode=config.quark_cli_mode,
                    executable_path=config.quark_cli_executable_path,
                    wsl_distribution=config.quark_cli_wsl_distribution,
                    node_command=config.quark_cli_node_command,
                    script_path=config.quark_cli_script_path,
                    parent_fid=config.quark_cli_parent_fid,
                    timeout_seconds=config.quark_cli_timeout_seconds,
                    share_expired_type=config.quark_cli_share_expired_type,
                    share_url_type=config.quark_cli_share_url_type,
                    stop_event=state.stop_event,
                ).login()
                logging.info("夸克 CLI 登录流程结束")
                return
            if action == "/api/quark-cli-check":
                QuarkCLIStorageAdapter(
                    mode=config.quark_cli_mode,
                    executable_path=config.quark_cli_executable_path,
                    wsl_distribution=config.quark_cli_wsl_distribution,
                    node_command=config.quark_cli_node_command,
                    script_path=config.quark_cli_script_path,
                    parent_fid=config.quark_cli_parent_fid,
                    timeout_seconds=config.quark_cli_timeout_seconds,
                    share_expired_type=config.quark_cli_share_expired_type,
                    share_url_type=config.quark_cli_share_url_type,
                    stop_event=state.stop_event,
                ).check()
                logging.info("夸克 CLI 检查流程结束")
                return
            if action == "/api/telegram-api-login":
                command = ([sys.executable] if getattr(sys, "frozen", False) else
                           [sys.executable, str(Path(__file__).resolve().parents[1] / "run_pipeline.py")])
                command += ["--config", str(config_path), "--telegram-api-login"]
                logging.info("正在打开 Telegram API 交互登录终端")
                process = subprocess.Popen(command, cwd=config_path.parent,
                                           creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0)
                if process.wait() != 0:
                    raise RuntimeError("Telegram API 登录未完成，请查看交互终端中的提示")
                logging.info("Telegram API 登录成功")
                return
            if action == "/api/telegram-channels-scan":
                rows = api_adapter(config).list_joined_channels(
                    avatar_dir=config.output_dir / ".channel-avatars")
                if state.stop_event.is_set():
                    raise DownloadCancelled("已请求停止已加入频道扫描")
                merged = channels.merge_joined(rows)
                with state.lock:
                    state.telegram_channels = {"stage": "completed", "found": len(rows), **merged,
                                               "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                               "error": ""}
                logging.info("账号频道已保存：获取=%d，新增=%d，更新=%d，列表总数=%d",
                             len(rows), merged["added"], merged["updated"], merged["total"])
                return
            if action in {"/api/channel-scan", "/api/channel-cover", "/api/channel-download"}:
                listing = catalog.snapshot(config.source_channel_url,
                                           exclude_forwarded_posts=config.exclude_forwarded_posts)
                older = arguments.get("older", False)
                advance_cursor = older or not listing["oldest_scanned_id"]
                preview_dir = catalog.preview_root / listing["channel"]
                def publish(page: dict) -> None:
                    catalog.merge(config.source_channel_url, page, older=advance_cursor)

                page = api_adapter(config).list_archives(
                    config.source_channel_url,
                    before_id=listing["oldest_scanned_id"] if older else 0,
                    limit=config.channel_message_limit,
                    preview_dir=preview_dir, known_covers=catalog.covers(config.source_channel_url),
                    on_page=publish if action == "/api/channel-scan" else None,
                    on_progress=state.scan_progress if action == "/api/channel-scan" else None,
                    take_request=state.take_channel_request, close_requests=state.close_channel_requests,
                    on_request_done=lambda request, result, error: media_job_result(config, request, result, error),
                    media_only=action != "/api/channel-scan")
                if action == "/api/channel-scan":
                    publish(page)
                    state.scan_progress({"stage": "completed", "found": len(page["messages"])})
                    logging.info("频道消息列表已更新，新增读取压缩包=%d", len(page["messages"]))
                else:
                    logging.info("频道媒体队列已处理完成")
                return
            if config.source_channel_url or config.source_chat:
                telegram = api_adapter(config)
                logging.info("使用 Telegram API 实际抓取")
            else:
                telegram = LocalTelegramAdapter(config.inbox_dir)
                logging.info("使用本地素材模拟抓取")
            if action in {"/api/download", "/api/retry", "/api/channel-download"}:
                if action == "/api/retry":
                    task = transfers.get(arguments["id"])
                    messages = telegram.fetch_messages(task["chat"], [task["message_id"]], task["chat"], "message_ids", 1)
                elif action == "/api/channel-download":
                    ids = arguments["ids"]
                    messages = telegram.fetch_messages(config.source_chat, ids, config.source_channel_url,
                                                       "message_ids", len(ids))
                else:
                    messages = telegram.fetch_messages(config.source_chat, [], config.source_channel_url, "recent", config.recent_message_limit)
                for message in messages:
                    transfers.accept_message(message)
                    for row in transfers.snapshot()["tasks"]:
                        if (row["message_id"] == message.message_id and channel_key(row["chat"]) == channel_key(message.chat)
                                and row["download_status"] == "completed"):
                            auto_upload_candidates.add(row["id"])
                logging.info("下载任务结束，返回素材消息=%d", len(messages))
                return
            cloud = cloud_adapter(config)
            candidates = run_pipeline(config, telegram, cloud)
            logging.info("任务结束，候选数=%d", len(candidates))
            for item in candidates:
                logging.info("%s status=%s share=%s warnings=%s", item.candidate_id, item.status, item.share_url, item.warnings)
        except DownloadCancelled:
            if action == "/api/channel-scan":
                state.scan_progress({"stage": "cancelled"})
                logging.info("频道加载已停止，已读取的消息保留")
            elif action == "/api/telegram-channels-scan":
                with state.lock:
                    state.telegram_channels["stage"] = "cancelled"
                logging.info("已加入频道扫描已停止")
            else:
                logging.info("任务已停止")
        except Exception as exc:
            if action == "/api/channel-scan":
                state.scan_progress({"stage": "failed"})
            elif action == "/api/telegram-channels-scan":
                with state.lock:
                    state.telegram_channels.update({"stage": "failed", "error": str(exc)})
            with state.lock:
                state.error = str(exc)
            logging.exception("任务失败")
        finally:
            try:
                if (config.auto_upload_after_download and not state.stop_event.is_set()
                        and action in {"/api/download", "/api/retry", "/api/channel-download", "/api/channel-scan"}):
                    if config.upload_provider not in {"quark_browser", "quark_cli", "quark_api"}:
                        logging.warning("自动上传已跳过：请配置真实夸克上传方式")
                    else:
                        ready = []
                        for identity in sorted(auto_upload_candidates):
                            task = transfers.get(identity)
                            if task["download_status"] != "completed" or task["upload_status"] != "waiting":
                                continue
                            if re.search(r"(?:[._\- ]?part)[._\- ]?\d+(?=\.(?:zip|rar)$)", task["filename"], re.I):
                                logging.warning("分卷素材需整组核对，跳过自动上传：%s", task["filename"])
                                continue
                            ready.append(identity)
                        if ready:
                            logging.info("下载阶段结束，开始自动上传 %d 个素材包", len(ready))
                            upload_tasks(ready, config)
                auto_upload_candidates.clear()
                with state.lock:
                    abandoned = list(state.channel_requests)
                    state.channel_requests.clear()
                    state.channel_request_keys.clear()
                    state.channel_requests_open = False
                for request in abandoned:
                    if request["kind"] == "cover":
                        catalog.update_cover(config.source_channel_url, request["message_id"],
                                             full_status="cancelled" if state.stop_event.is_set() else "failed",
                                             full_error=state.error or "任务未完成，可重新加载")
                transfers.finish(state.error, stopped=state.stop_event.is_set())
            except OSError:
                logging.exception("任务状态保存失败，请检查磁盘空间或文件权限")
            finally:
                with state.lock:
                    state.running = False

    server = ThreadingHTTPServer((host, port), Handler)
    if open_browser:
        webbrowser.open(f"http://{host}:{port}/")
    logging.info("界面已启动：http://%s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
