from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event
from urllib.parse import urlparse

from .adapters import (LocalCloudStorageAdapter, LocalTelegramAdapter, TelegramBrowserAdapter,
                       TelethonTelegramAdapter, parse_telegram_api_proxy, parse_telegram_dc2_server)
from .config import PipelineConfig
from .pipeline import process_messages, run_pipeline
from .quark_browser import QuarkBrowserStorageAdapter
from .runtime import configure_logging, validate_browser_path
from .archive_validation import find_archive_tester, validate_rar
from .browser_profiles import check_browser, resolve_browser_profile, version_text
from .workbench import TransferStore


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
        self.lock = threading.Lock()
        # Serialize configuration saves and validated starts without holding the log lock.
        self.control_lock = threading.Lock()

    def add_log(self, message: str) -> None:
        with self.lock:
            self.logs.append(message)
            self.logs = self.logs[-500:]

    def snapshot(self) -> dict:
        with self.lock:
            return {"running": self.running, "logs": list(self.logs), "action": self.action, "error": self.error}


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
        "telegram_browser_timeout_seconds": config.telegram_browser_timeout_seconds,
        "download_start_timeout_seconds": config.download_start_timeout_seconds,
        "download_transfer_timeout_seconds": config.download_transfer_timeout_seconds,
        "archive_test_executable_path": config.archive_test_executable_path,
        "inbox_dir": str(config.inbox_dir.resolve()),
        "download_dir": str(config.inbox_dir.resolve()),
        "unverified_dir": str((config.inbox_dir / ".unverified").resolve()),
        "log_path": str((config.output_dir / "logs" / "pipeline.log").resolve()),
        "source_channel_url": config.source_channel_url,
        "fetch_mode": "recent",
        "recent_message_limit": config.recent_message_limit,
        "telegram_run_mode": config.telegram_run_mode,
        "message_ids": [],
        "telegram_access_mode": config.telegram_access_mode,
        "telegram_browser_headless": config.telegram_browser_headless,
        "upload_provider": config.upload_provider,
        "quark_folder_url": config.quark_folder_url,
        "quark_headless": config.quark_headless,
    }


def browser_payload(config: PipelineConfig) -> dict:
    try:
        profile = resolve_browser_profile(config.telegram_browser_profile, config.browser_executable_path)
        return {"browser_version": version_text(profile.version), "browser_profile_dir": str(profile.directory),
                "browser_profile_status": profile.status, "browser_profile_error": ""}
    except (ValueError, RuntimeError, OSError) as exc:
        return {"browser_version": "", "browser_profile_dir": "", "browser_profile_status": "",
                "browser_profile_error": str(exc)}


def write_config(path: Path, payload: dict) -> None:
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not payload.get("telegram_api_hash"):
        payload.pop("telegram_api_hash", None)
    existing.update(payload)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


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


def validate_browser_settings(payload: dict, require_browser: bool = True) -> None:
    if not isinstance(payload.get("browser_executable_path", ""), str):
        raise ValueError("浏览器程序路径必须为字符串")
    if require_browser or payload.get("browser_executable_path"):
        validate_browser_path(payload.get("browser_executable_path", ""))
    for key, maximum in (("telegram_browser_timeout_seconds", 600),
                         ("download_start_timeout_seconds", 86400),
                         ("download_transfer_timeout_seconds", 604800)):
        value = payload.get(key)
        if key == "telegram_browser_timeout_seconds" and value is None:
            continue
        minimum = 30 if key == "telegram_browser_timeout_seconds" else 1
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{key} 必须为 {minimum} 到 {maximum} 的整数秒")
    if not isinstance(payload.get("archive_test_executable_path", ""), str):
        raise ValueError("RAR 校验程序路径必须为字符串")
    if payload.get("archive_test_executable_path"):
        find_archive_tester(payload["archive_test_executable_path"])


def validate_source_settings(payload: dict, existing: PipelineConfig) -> None:
    mode = payload.get("telegram_access_mode", existing.telegram_access_mode)
    if mode not in {"browser", "api"}:
        raise ValueError("Telegram 获取方式无效")
    provider = payload.get("upload_provider", existing.upload_provider)
    validate_browser_settings(payload, require_browser=mode == "browser" or provider == "quark_browser")
    if mode == "api":
        parse_telegram_api_proxy(payload.get("telegram_api_proxy_url", existing.telegram_api_proxy_url))
        parse_telegram_dc2_server(payload.get("telegram_api_dc2_server", existing.telegram_api_dc2_server))
        api_id = payload.get("telegram_api_id", existing.telegram_api_id)
        api_hash = payload.get("telegram_api_hash") or existing.telegram_api_hash
        if type(api_id) is not int or not 1 <= api_id <= 2147483647:
            raise ValueError("Telegram API ID 必须为正整数")
        if not isinstance(api_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            raise ValueError("Telegram API Hash 必须为 32 位十六进制字符串")
        if payload.get("telegram_run_mode", existing.telegram_run_mode) == "watch":
            raise ValueError("Telegram API 模式目前仅支持抓取一次")


def start_web_ui(config_path: str | Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    config_path = Path(config_path).resolve()
    state = WebState(config_path)
    configure_logging(PipelineConfig.from_json(config_path).output_dir / "logs", MemoryLogHandler(state))
    transfers = TransferStore(PipelineConfig.from_json(config_path))
    logging.info("v34 可配置下载目录工作台; 配置文件=%s", config_path)

    def transfer_event(event: dict) -> None:
        identity = transfers.event(event)
        if event.get("download_status") == "completed":
            transfers.enrich(identity)

    def cloud_adapter(config: PipelineConfig, on_stage=None):
        if config.upload_provider == "quark_browser":
            return QuarkBrowserStorageAdapter(folder_url=config.quark_folder_url, profile_dir=config.quark_browser_profile,
                headless=config.quark_headless, upload_input_selector=config.quark_upload_input_selector,
                file_row_selector=config.quark_file_row_selector, share_button_selector=config.quark_share_button_selector,
                share_link_selector=config.quark_share_link_selector, timeout_seconds=config.quark_timeout_seconds,
                browser_executable_path=config.browser_executable_path, on_stage=on_stage)
        if config.upload_provider != "local":
            raise ValueError("上传方式必须为本地模拟或夸克网页上传")
        return LocalCloudStorageAdapter(config.output_dir / "cloud_fixture")

    class Handler(BaseHTTPRequestHandler):
        def json_response(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if self.headers.get("Host") not in {f"{host}:{port}", f"localhost:{port}"}:
                self.json_response({"message": "仅允许本机请求"}, 403)
                return
            if path == "/":
                body = HTML.encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            elif path == "/api/config":
                config = PipelineConfig.from_json(config_path)
                self.json_response({**config_payload(config), **browser_payload(config)})
            elif path == "/api/logs":
                self.json_response(state.snapshot())
            elif path == "/api/workbench":
                self.json_response({**transfers.snapshot(), **{key: value for key, value in state.snapshot().items() if key != "logs"}})
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
                            "download_dir", "log_path", "unverified_dir", "telegram_api_configured"
                        }
                        allowed.update({"telegram_scan_limit", "cloud_folder", "telegram_api_hash"})
                        payload = {key: value for key, value in arguments.items() if key in allowed}
                        validate_source_settings({**config_payload(existing), **payload}, existing)
                        if "inbox_dir" in payload:
                            payload["inbox_dir"] = str(prepare_download_directory(payload["inbox_dir"]))
                        payload.update({"fetch_mode": "recent", "source_chat": "", "message_ids": []})
                        write_config(config_path, payload)
                        transfers.use_config(PipelineConfig.from_json(config_path))
                    except (ValueError, RuntimeError, OSError) as exc:
                        self.json_response({"message": str(exc)}, 400)
                        return
                self.json_response({"message": "配置已保存"})
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
                logging.info("已请求停止；Telegram API 下载将中断，浏览器下载将在当前附件结束后停止")
                self.json_response({"message": "已请求停止"})
            elif path in {"/api/run", "/api/download", "/api/retry", "/api/upload", "/api/quark-login", "/api/telegram-login",
                          "/api/telegram-api-login", "/api/browser-check"}:
                with state.control_lock:
                    with state.lock:
                        busy = state.running
                    if busy:
                        self.json_response({"message": "已有任务运行中"}, 409)
                        return
                    try:
                        config = PipelineConfig.from_json(config_path)
                        if path == "/api/upload":
                            identities = arguments.get("ids")
                            if not isinstance(identities, list) or not identities or len(identities) > 100 or not all(isinstance(item, str) for item in identities):
                                raise ValueError("请选择 1 到 100 个已下载素材")
                            if len(set(identities)) != len(identities):
                                raise ValueError("上传列表包含重复任务")
                            for identity in identities:
                                task = transfers.get(identity)
                                if (task["upload_status"] in {"completed", "uploading"}
                                        or task["upload_status"] == "local" and config.upload_provider != "quark_browser"):
                                    raise ValueError("选中任务已上传或正在上传")
                                if task["upload_status"] == "link_pending":
                                    if task.get("upload_target") != config.quark_folder_url:
                                        raise ValueError("请恢复该任务原上传目标后再获取链接，不能改目录重复上传")
                                    if config.upload_provider != "quark_browser":
                                        raise ValueError("请切回夸克网页上传模式核对已有文件")
                                    transfers.upload_name(identity)
                                else:
                                    transfers.local_file(identity, completed=True)
                            if config.upload_provider == "quark_browser":
                                validate_browser_path(config.browser_executable_path)
                            cloud_adapter(config)
                        elif path in {"/api/browser-check", "/api/telegram-login", "/api/quark-login"}:
                            validate_browser_settings(config_payload(config))
                        else:
                            if path == "/api/retry" and config.telegram_access_mode == "browser":
                                raise ValueError("浏览器模式暂不支持单任务重试，请使用 Telegram API 模式")
                            if path == "/api/telegram-api-login" and config.telegram_access_mode != "api":
                                raise ValueError("请先选择 Telegram API 获取方式")
                            validate_source_settings(config_payload(config), config)
                            if path == "/api/retry":
                                task = transfers.get(str(arguments.get("id", "")))
                                if not task.get("message_id") or not task.get("chat") or task["download_status"] not in {"failed", "cancelled", "interrupted"}:
                                    raise ValueError("仅支持重新下载失败或中断的 Telegram 任务")
                                if config.telegram_run_mode == "watch":
                                    raise ValueError("请先将运行方式改为抓取一次，再重试单个文件")
                    except (ValueError, RuntimeError, OSError) as exc:
                        self.json_response({"message": str(exc)}, 400)
                        return
                    with state.lock:
                        state.stop_event.clear()
                        state.running = True
                        state.action = path.removeprefix("/api/")
                        state.error = ""
                threading.Thread(target=worker, args=(path, arguments, config), daemon=True).start()
                self.json_response({"message": "任务已启动"})
            else:
                self.send_error(404)

        def log_message(self, format: str, *args) -> None:
            if args and isinstance(args[0], str) and any(route in args[0] for route in ("/api/logs", "/api/workbench", "/api/preview", "/static/")):
                return
            logging.info("Web %s", format % args)

    def worker(action: str, arguments: dict, config: PipelineConfig) -> None:
        try:
            logging.info("开始任务，上传方式=%s", config.upload_provider)
            if action == "/api/upload":
                for identity in arguments["ids"]:
                    if state.stop_event.is_set():
                        break
                    task = transfers.get(identity)
                    recovering = task["upload_status"] == "link_pending"
                    try:
                        def stage(value: str) -> None:
                            if value != "completed":
                                transfers.update(identity, upload_status=value)
                        cloud = cloud_adapter(config, on_stage=stage)
                        if recovering:
                            source = Path(transfers.upload_name(identity))
                            if not isinstance(cloud, QuarkBrowserStorageAdapter):
                                raise ValueError("请切回夸克网页上传模式，核对已有文件分享链接")
                            transfers.update(identity, error="")
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
                                             upload_local_path=str(source.resolve()),
                                             upload_target=config.quark_folder_url if config.upload_provider == "quark_browser" else config.cloud_folder)
                            result = cloud.upload(source, config.cloud_folder)
                        transfers.update(identity, upload_status="completed" if config.upload_provider == "quark_browser" else "local",
                                         share_url=result, error="")
                        logging.info("素材上传结束: %s (%s)", source.name, config.upload_provider)
                    except Exception as exc:
                        current = transfers.get(identity)
                        status = "link_pending" if recovering or current["upload_status"] == "link_pending" else "failed"
                        transfers.update(identity, upload_status=status, error=str(exc))
                        with state.lock:
                            state.error = "部分素材上传未完成，请查看任务错误信息"
                        logging.exception("素材上传未完成: %s", task["filename"])
                return
            if action == "/api/browser-check":
                check_browser(config.telegram_browser_profile, config.browser_executable_path)
                return
            if action == "/api/quark-login":
                from .quark_browser import login_and_inspect
                login_and_inspect(config.quark_folder_url, config.quark_browser_profile, config.browser_executable_path)
                logging.info("夸克登录流程结束")
                return
            if action == "/api/telegram-login":
                from .adapters import telegram_browser_login
                telegram_browser_login(config.telegram_browser_profile, config.source_channel_url, config.browser_executable_path)
                logging.info("Telegram Web 登录流程结束")
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
            if config.source_channel_url or config.source_chat:
                if config.telegram_access_mode == "browser":
                    if config.telegram_run_mode not in {"once", "watch"} or not 1 <= config.telegram_scan_limit <= 1000:
                        raise ValueError("运行模式或扫描上限无效")
                    telegram = TelegramBrowserAdapter(config.telegram_browser_profile, config.inbox_dir, config.telegram_browser_headless, config.telegram_browser_timeout_seconds, config.telegram_message_selector, config.telegram_attachment_selector, watch_mode=config.telegram_run_mode == "watch", stop_event=state.stop_event, scan_limit=config.telegram_scan_limit, on_event=transfer_event, **config.browser_download_options())
                    logging.info("使用 Telegram Web 浏览器抓取（无需 API ID/API Hash）")
                else:
                    if not config.telegram_api_id or not config.telegram_api_hash:
                        raise ValueError("API 模式需要 Telegram API ID 和 API Hash；或将 telegram_access_mode 改为 browser")
                    telegram = TelethonTelegramAdapter(config.telegram_api_id, config.telegram_api_hash,
                                                       config.telegram_session, config.inbox_dir,
                                                       scan_limit=config.telegram_scan_limit,
                                                       archive_test_executable_path=config.archive_test_executable_path,
                                                       archive_test_timeout_seconds=config.archive_test_timeout_seconds,
                                                       proxy_url=config.telegram_api_proxy_url,
                                                       dc2_server=config.telegram_api_dc2_server,
                                                       download_start_timeout_seconds=config.download_start_timeout_seconds,
                                                       download_transfer_timeout_seconds=config.download_transfer_timeout_seconds,
                                                       stop_event=state.stop_event, on_event=transfer_event)
                    logging.info("使用 Telegram API 实际抓取")
            else:
                telegram = LocalTelegramAdapter(config.inbox_dir)
                logging.info("使用本地素材模拟抓取")
            if action in {"/api/download", "/api/retry"}:
                if isinstance(telegram, TelegramBrowserAdapter):
                    telegram.on_message = transfers.accept_message
                if action == "/api/retry":
                    task = transfers.get(arguments["id"])
                    messages = telegram.fetch_messages(task["chat"], [task["message_id"]], task["chat"], "message_ids", 1)
                else:
                    messages = telegram.fetch_messages(config.source_chat, [], config.source_channel_url, "recent", config.recent_message_limit)
                for message in messages:
                    transfers.accept_message(message)
                logging.info("下载任务结束，返回素材消息=%d", len(messages))
                return
            if config.upload_provider == "quark_browser":
                cloud = QuarkBrowserStorageAdapter(folder_url=config.quark_folder_url, profile_dir=config.quark_browser_profile, headless=config.quark_headless, upload_input_selector=config.quark_upload_input_selector, file_row_selector=config.quark_file_row_selector, share_button_selector=config.quark_share_button_selector, share_link_selector=config.quark_share_link_selector, timeout_seconds=config.quark_timeout_seconds, browser_executable_path=config.browser_executable_path)
            else:
                cloud = LocalCloudStorageAdapter(config.output_dir / "cloud_fixture")
            if config.telegram_run_mode == "watch" and isinstance(telegram, TelegramBrowserAdapter):
                telegram.on_message = lambda message: process_messages(config, [message], cloud)
                telegram.fetch_messages(config.source_chat, config.message_ids, config.source_channel_url, "recent", config.recent_message_limit)
                logging.info("持续监测已停止")
                return
            candidates = run_pipeline(config, telegram, cloud)
            logging.info("任务结束，候选数=%d", len(candidates))
            for item in candidates:
                logging.info("%s status=%s share=%s warnings=%s", item.candidate_id, item.status, item.share_url, item.warnings)
        except Exception as exc:
            with state.lock:
                state.error = str(exc)
            logging.exception("任务失败")
        finally:
            try:
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
