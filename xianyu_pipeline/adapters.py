from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import ctypes
import time
import zipfile
from pathlib import Path
from threading import Event
from typing import Protocol
from urllib.parse import urlparse
from uuid import uuid4

from .archive_validation import ArchiveValidationError, validate_rar
from .browser_download import BrowserDownloadMonitor
from .models import SourceMessage
from .browser_profiles import launch_browser_context


class TelegramAdapter(Protocol):
    def fetch_messages(self, chat: str, message_ids: list[int], channel_url: str = "", mode: str = "message_ids", limit: int = 20) -> list[SourceMessage]: ...


class CloudStorageAdapter(Protocol):
    def upload(self, local_path: Path, remote_folder: str) -> str: ...


class LocalTelegramAdapter:
    """本地验收适配器：从 inbox 读取预置素材，不连接 Telegram。"""

    def __init__(self, inbox_dir: Path):
        self.inbox_dir = inbox_dir

    def fetch_messages(self, chat: str, message_ids: list[int], channel_url: str = "", mode: str = "message_ids", limit: int = 20) -> list[SourceMessage]:
        attachments = sorted(path for path in self.inbox_dir.iterdir() if path.is_file())
        return [
            SourceMessage(
                message_id=message_ids[0] if message_ids else 0,
                chat=chat or "local-inbox",
                text="Local inbox fixture",
                attachment_paths=attachments,
            )
        ]


class TelethonTelegramAdapter:
    """Telegram 实际适配器；需要额外安装 telethon 并完成首次登录。"""

    def __init__(self, api_id: int, api_hash: str, session_path: str | Path, download_dir: Path):
        if not api_id or not api_hash:
            raise ValueError("Telegram 适配器需要 api_id 和 api_hash")
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = str(session_path)
        self.download_dir = download_dir

    @staticmethod
    def normalize_channel(channel: str) -> str:
        value = channel.strip()
        if value.startswith("https://t.me/") or value.startswith("http://t.me/"):
            parsed = urlparse(value)
            parts = [part for part in parsed.path.split("/") if part]
            if not parts or parts[0] == "c":
                raise ValueError("暂不支持仅凭私有频道链接推断频道，请填写可访问的频道用户名或聊天 ID")
            value = parts[0]
        value = value.removeprefix("@").strip()
        if not value or not re.fullmatch(r"[A-Za-z0-9_+\-]+", value):
            raise ValueError("频道链接或用户名格式不正确")
        return value

    def fetch_messages(self, chat: str, message_ids: list[int], channel_url: str = "", mode: str = "message_ids", limit: int = 20) -> list[SourceMessage]:
        try:
            from telethon.sync import TelegramClient
        except ImportError as exc:
            raise RuntimeError("未安装 telethon，请先安装后再启用 Telegram 实际适配器") from exc
        target = self.normalize_channel(channel_url or chat)
        if mode == "message_ids" and not message_ids:
            raise ValueError("指定消息模式需要 message_ids；或将 fetch_mode 改为 recent")
        if mode not in {"message_ids", "recent"}:
            raise ValueError("fetch_mode 只能是 message_ids 或 recent")
        self.download_dir.mkdir(parents=True, exist_ok=True)
        messages: list[SourceMessage] = []
        with TelegramClient(self.session_path, self.api_id, self.api_hash) as client:
            if mode == "recent":
                iterator = client.iter_messages(target, limit=max(1, min(limit, 100)), reverse=False)
            else:
                iterator = (client.get_messages(target, ids=message_id) for message_id in message_ids)
            for message in iterator:
                if not message:
                    continue
                # recent 模式先按频道整体最新消息遍历，不按 sender/user 过滤；
                # 只有最终处理阶段才跳过无附件消息。
                if not message.media:
                    continue
                paths: list[Path] = []
                downloaded = client.download_media(message, file=str(self.download_dir))
                if downloaded:
                    paths.append(Path(downloaded))
                messages.append(SourceMessage(message_id=message.id, chat=target, text=message.message or "", attachment_paths=paths))
        return messages


def download_resource_status(destination: Path) -> dict[str, int | str]:
    """读取真实下载目录磁盘余量与 Windows 物理内存余量；探针失败不阻断下载。"""
    status: dict[str, int | str] = {}
    try:
        status["disk_free"] = shutil.disk_usage(destination).free
    except OSError as exc:
        status["disk_error"] = str(exc).splitlines()[0]
    if sys.platform == "win32":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        ("total_phys", ctypes.c_ulonglong), ("available_phys", ctypes.c_ulonglong),
                        ("total_page", ctypes.c_ulonglong), ("available_page", ctypes.c_ulonglong),
                        ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
                        ("available_extended", ctypes.c_ulonglong)]
        memory = MemoryStatus()
        memory.length = ctypes.sizeof(memory)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            status["memory_available"] = memory.available_phys
            status["page_available"] = memory.available_page
    return status


def launch_telegram_context(playwright, profile_dir: Path, headless: bool,
                            browser_executable_path: str = "", downloads_path: Path | None = None):
    if downloads_path is not None:
        downloads_path.mkdir(parents=True, exist_ok=True)
        logging.info("浏览器暂存目录: %s; 资源=%s", downloads_path.resolve(), download_resource_status(downloads_path))
    return launch_browser_context(playwright, profile_dir, browser_executable_path, headless=headless,
                                  downloads_path=downloads_path)


class TelegramBrowserAdapter:
    """通过 Telegram Web 的持久化浏览器会话抓取附件。

    首次使用时以可见窗口登录；登录状态保存在 profile_dir，后续可选择后台运行。
    页面选择器允许配置，但默认只使用公开的稳定属性和文本，不调用 Telegram 内部 API。
    """

    def __init__(self, profile_dir: Path, download_dir: Path, headless: bool = False,
                 timeout_seconds: int = 180, message_selector: str = "",
                 attachment_selector: str = "", watch_mode: bool = False,
                 stop_event: Event | None = None, scan_limit: int = 200, on_message=None,
                 browser_executable_path: str = "", start_timeout_seconds: int = 180,
                 start_grace_seconds: int = 30, transfer_timeout_seconds: int = 3600,
                 archive_test_executable_path: str = "", archive_test_timeout_seconds: int = 600):
        self.on_message = on_message
        self.watch_mode = watch_mode
        self.stop_event = stop_event or Event()
        self.scan_limit = scan_limit
        self.profile_dir = Path(profile_dir)
        self.download_dir = Path(download_dir)
        self.headless = headless
        self.timeout_ms = timeout_seconds * 1000
        self.message_selector = message_selector.strip() or '[data-message-id], [data-mid], .bubble'
        self.attachment_selector = attachment_selector.strip()
        self.browser_executable_path = browser_executable_path
        for name, value, minimum in (("下载启动超时", start_timeout_seconds, 1),
                                     ("下载启动宽限", start_grace_seconds, 0),
                                     ("下载传输超时", transfer_timeout_seconds, 1),
                                     ("压缩包校验超时", archive_test_timeout_seconds, 1)):
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name}必须为不小于 {minimum} 的整数秒")
        self.start_timeout_seconds = start_timeout_seconds
        self.start_grace_seconds = start_grace_seconds
        self.transfer_timeout_seconds = transfer_timeout_seconds
        self.archive_test_executable_path = archive_test_executable_path
        self.archive_test_timeout_seconds = archive_test_timeout_seconds

    @staticmethod
    def normalize_channel(channel: str) -> str:
        value = channel.strip()
        if not value.startswith(("https://t.me/", "http://t.me/")):
            raise ValueError("浏览器模式需要填写公开 Telegram 频道链接，例如 https://t.me/example_channel")
        parsed = urlparse(value)
        if parsed.hostname not in {"t.me", "telegram.me"}:
            raise ValueError("频道链接必须是 t.me 或 telegram.me 地址")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts or parts[0] in {"c", "joinchat"} or not re.fullmatch(r"[A-Za-z0-9_]+", parts[0]):
            raise ValueError("浏览器模式只支持公开频道用户名链接")
        return parts[0]

    @staticmethod
    def channel_web_url(channel: str) -> str:
        """直接进入 Web K 的频道聊天；绝不导航到 t.me 公开落地页。"""
        return f"https://web.telegram.org/k/#@{TelegramBrowserAdapter.normalize_channel(channel)}"

    @staticmethod
    def _message_id(message) -> int | None:
        # data-mid 可能是复合 ID；同一条消息的父子 bubble 共享尾部消息 ID。
        for attr in ("data-message-id", "data-mid"):
            raw = message.get_attribute(attr, timeout=5000)
            if raw:
                match = re.search(r"(\d+)$", raw)
                if match:
                    return int(match.group(1))
        return None

    @staticmethod
    def _unique_message_nodes(nodes, limit: int):
        """按 DOM 顺序取最新 ID，同时为重复 ID 保留最外层（先出现）节点。"""
        by_id = {}
        order = []
        for index in range(nodes.count()):
            node = nodes.nth(index)
            mid = TelegramBrowserAdapter._message_id(node)
            if mid is not None:
                if mid not in by_id:
                    by_id[mid] = node
                    order.append(mid)
        return [(mid, by_id[mid]) for mid in order[-limit:]]

    @staticmethod
    def _archive_message(node) -> bool:
        text = node.inner_text(timeout=5000)
        return bool(re.search(r"\.(zip|rar)\b", text, re.I))

    def _scan_archive_ids(self, page, nodes, limit: int) -> list[int]:
        """从当前最新消息向前扫描，记录页面曾出现的 ZIP/RAR 候选 ID。"""
        found: list[int] = []
        seen: set[int] = set()
        deadline = time.monotonic() + min(self.timeout_ms / 1000, 90)
        stagnant = 0
        while len(seen) < self.scan_limit and time.monotonic() < deadline and not self.stop_event.is_set():
            unique = self._unique_message_nodes(nodes, self.scan_limit)
            before = len(seen)
            for mid, node in reversed(unique):
                if mid in seen or len(seen) >= self.scan_limit:
                    continue
                seen.add(mid)
                if self._archive_message(node):
                    found.append(mid)
            if len(found) >= limit:
                break
            if not unique:
                page.wait_for_timeout(700)
                continue
            try:
                unique[0][1].evaluate("el => el.scrollIntoView({block: 'start'})")
                page.mouse.wheel(0, -800)
            except Exception as exc:
                logging.warning("向前加载历史消息失败: %s", str(exc).splitlines()[0])
                break
            page.wait_for_timeout(1200)
            stagnant = stagnant + 1 if before == len(seen) else 0
            if stagnant >= 4:
                break
        logging.info("压缩包消息扫描: 已检查=%d, ZIP/RAR消息=%d, 目标=%d, 扫描上限=%d", len(seen), len(found), limit, self.scan_limit)
        return list(reversed(found[:limit]))

    def _locate_message(self, page, nodes, message_id: int):
        for mid, node in self._unique_message_nodes(nodes, self.scan_limit):
            if mid == message_id:
                return node
        # 虚拟列表滚动时向下找回节点；找不到则明确失败，不猜 Web K 私有深链。
        for _ in range(12):
            page.mouse.wheel(0, 800)
            page.wait_for_timeout(350)
            for mid, node in self._unique_message_nodes(nodes, self.scan_limit):
                if mid == message_id:
                    return node
        return None

    def _wait_for_messages(self, page, nodes, limit: int):
        """等待 Web K 异步渲染；不足时向上加载历史，不把首个节点当成完成。"""
        deadline = time.monotonic() + min(self.timeout_ms / 1000, 45)
        last_ids = ()
        stable_since = time.monotonic()
        scrolls = 0
        newest_id = None
        while time.monotonic() < deadline:
            unique = self._unique_message_nodes(nodes, limit)
            ids = tuple(mid for mid, _ in unique)
            if newest_id is None and ids:
                newest_id = ids[-1]
            if ids != last_ids:
                last_ids = ids
                stable_since = time.monotonic()
            if len(unique) >= limit and ids[-1] == newest_id and time.monotonic() - stable_since >= 1.5:
                return unique
            # 先给频道初始异步渲染留时间，再对虚拟列表向上加载。
            if len(unique) < limit and time.monotonic() - stable_since >= 2 and scrolls < 8 and unique:
                try:
                    unique[0][1].evaluate("el => el.scrollIntoView({block: 'start'})")
                    page.mouse.wheel(0, -750)
                    scrolls += 1
                except Exception as exc:
                    logging.warning("加载更早消息失败: %s", str(exc).splitlines()[0])
                    break
            page.wait_for_timeout(500)
        return self._unique_message_nodes(nodes, limit)

    def _attachment_locators(self, message):
        if self.attachment_selector:
            return message.locator(self.attachment_selector)
        # 优先完整文档卡片；避免 .document 与内层子控件同时入选。
        documents = message.locator("[class*='document-container']:not(:has([class*='document-container']))")
        if documents.count():
            return documents
        # Web K 的文件卡片可能使用不同 class；优先命中文件名节点，点击可向上冒泡到卡片。
        file_names = message.locator(
            "[class*='document-name'], [class*='file-name'], [class*='document-title']"
        ).filter(has_text=re.compile(r"\.(zip|7z|rar|pdf|docx?)\b", re.I))
        if file_names.count():
            return file_names
        links = message.locator("a[download], a[href*='download']")
        if links.count():
            return links
        return message.locator("button, [role='button']").filter(
            has_text=re.compile(r"下载|Download|保存|Save|\.(zip|7z|rar|pdf|docx?|png|jpe?g|webp)", re.I)
        )

    @staticmethod
    def _visible_archive_names(button) -> list[str]:
        """仅从所点击文件卡片内读取附件名，不从整条消息正文臆测。"""
        text = button.inner_text(timeout=5000)
        return [line.strip() for line in text.splitlines()
                if re.fullmatch(r"[^\\/:*?\"<>|\r\n]+\.(?:zip|rar)", line.strip(), re.I)]

    @staticmethod
    def _diagnose_download_failure(download, partial: Path, page=None, context=None,
                                   resource_status=None) -> None:
        """在清理 .part 和浏览器前记录状态；诊断本身不能掩盖原始异常。"""
        def probe(label, reader):
            try:
                return reader()
            except Exception as exc:
                return f"不可读取({type(exc).__name__}: {str(exc).splitlines()[0]})"

        page_closed = probe("page", page.is_closed) if page is not None else "未提供"
        page_url = probe("url", lambda: page.url) if page is not None and page_closed is False else "不可用"
        context_pages = probe("context", lambda: len(context.pages)) if context is not None else "未提供"
        partial_size = probe("partial", lambda: partial.stat().st_size if partial.exists() else "不存在")
        # save_as 已抛错，下载通常已终止；若浏览器断开，failure() 可能无法返回，跳过该探针。
        failure = (probe("failure", download.failure)
                   if page_closed is False and isinstance(context_pages, int) else "连接不可用，未查询")
        logging.error(
            "下载失败现场: 浏览器名称=%s; 卡片临时文件=%s; 临时文件大小=%s; "
            "页面已关闭=%s; 页面URL=%s; 上下文页面数=%s; 下载失败原因=%s",
            probe("name", lambda: download.suggested_filename), partial.name, partial_size,
            page_closed, page_url, context_pages, failure,
        )
        logging.error("下载失败资源快照: 下载前=%s; 失败时=%s",
                      resource_status, download_resource_status(partial.parent))

    @staticmethod
    def _verified_download(download, destination: Path, visible_name: str | None = None,
                           page=None, context=None, resource_status=None,
                           archive_test_executable_path: str = "",
                           archive_test_timeout_seconds: int = 600) -> Path:
        """先保存到临时文件，再校验并原子落盘；不覆盖同名已有附件。"""
        suggested = download.suggested_filename
        destination = destination.resolve()
        destination.mkdir(parents=True, exist_ok=True)
        if visible_name and (visible_name in {"", ".", ".."} or Path(visible_name).name != visible_name
                             or re.search(r'[\\/:*?"<>|\x00-\x1f]', visible_name)):
            raise ValueError("页面附件文件名不安全")
        if suggested in {"", ".", ".."} or Path(suggested).name != suggested or re.search(r'[\\/:*?"<>|\x00-\x1f]', suggested):
            raise ValueError("浏览器附件文件名不安全")
        suggested_ext = Path(suggested).suffix.lower()
        # Telegram Web 的 blob 下载可能给 UUID；有其他明确扩展名时不允许冒充压缩包。
        if suggested_ext and suggested_ext not in {".zip", ".rar"}:
            raise ValueError(f"浏览器下载扩展名不是 ZIP/RAR: {suggested}")
        if suggested_ext in {".zip", ".rar"} and visible_name and suggested_ext != Path(visible_name).suffix.lower():
            raise ValueError(f"浏览器文件类型与页面卡片不一致: {suggested} / {visible_name}")
        # 浏览器给出完整压缩包名时优先使用；卡片文字可能被 CSS 缩成省略号。
        name = visible_name if suggested_ext not in {".zip", ".rar"} else suggested
        if not name or Path(name).suffix.lower() not in {".zip", ".rar"}:
            raise ValueError(f"无法确认 ZIP/RAR 文件名: {suggested}")
        if visible_name and name != suggested:
            logging.info("浏览器建议名=%s，使用消息卡片文件名=%s", suggested, name)
        target = destination / name
        if target.exists():
            index = 2
            while (destination / f"{target.stem}-{index}{target.suffix}").exists():
                index += 1
            target = destination / f"{target.stem}-{index}{target.suffix}"
        partial = destination / f".{target.name}.part"
        browser_file = None
        try:
            try:
                # Chromium progress must report completed before the local GUID
                # file is copied; no GB-sized stream is sent through Node/Python.
                browser_file = Path(download.path())
                if not browser_file.is_file():
                    raise RuntimeError("浏览器报告完成，但临时下载文件不存在")
                logging.info("浏览器下载已完成: 临时路径=%s; 大小=%d 字节；永久目标=%s",
                             browser_file, browser_file.stat().st_size, target)
                if shutil.disk_usage(destination).free < browser_file.stat().st_size + 64 * 1024 * 1024:
                    raise RuntimeError("永久保存目录空间不足，无法完整复制下载文件")
                shutil.copyfile(browser_file, partial)
            except Exception as exc:
                TelegramBrowserAdapter._diagnose_download_failure(download, partial, page, context,
                                                                    resource_status)
                if browser_file is not None and browser_file.is_file():
                    recovery = destination / ".unverified" / uuid4().hex / target.name
                    try:
                        recovery.parent.mkdir(parents=True, exist_ok=True)
                        browser_file.replace(recovery)
                    except OSError:
                        logging.exception("完整临时文件转存失败；原位置=%s", browser_file)
                    else:
                        raise RuntimeError(f"永久保存复制失败；完整下载已转存待检查: {recovery}") from exc
                raise
            failure = download.failure()
            if failure:
                raise RuntimeError(f"浏览器下载失败: {failure}")
            if not partial.is_file() or partial.stat().st_size == 0:
                raise RuntimeError("下载后文件为空")
            if partial.stat().st_size != browser_file.stat().st_size:
                raise RuntimeError("本地复制大小与浏览器下载文件不一致")
            if target.suffix.lower() == ".zip":
                with zipfile.ZipFile(partial) as archive:
                    bad = archive.testzip()
                    if bad:
                        raise RuntimeError(f"ZIP 内容校验失败: {bad}")
                    names = archive.namelist()
                    logging.info("ZIP 内容预览: %s，条目数=%d，前 10 项=%s", target.name, len(names), names[:10])
            if target.suffix.lower() == ".rar":
                quarantine = destination / ".unverified" / uuid4().hex / target.name
                quarantine.parent.mkdir(parents=True, exist_ok=True)
                partial.replace(quarantine)
                try:
                    validate_rar(quarantine, archive_test_executable_path, archive_test_timeout_seconds)
                except ArchiveValidationError as exc:
                    logging.error("RAR 已保存但未通过校验: 分类=%s; 保留路径=%s; 原因=%s",
                                  exc.category, quarantine, exc)
                    raise ArchiveValidationError(exc.category, f"{exc}；文件已保留: {quarantine}") from exc
                quarantine.replace(target)
                quarantine.parent.rmdir()
            else:
                partial.replace(target)
            logging.info("附件永久保存并校验通过: %s", target)
            return target
        finally:
            if partial.exists():
                try:
                    partial.unlink()
                except OSError:
                    logging.exception("未完成的 .part 清理失败（不作为成品）: %s", partial)

    def fetch_messages(self, chat: str, message_ids: list[int], channel_url: str = "",
                       mode: str = "message_ids", limit: int = 20) -> list[SourceMessage]:
        if mode != "recent":
            raise ValueError("Telegram 浏览器模式只支持频道最新 N 条发言，请将抓取模式设为 recent")
        if not channel_url:
            raise ValueError("Telegram 浏览器模式需要频道公开链接")
        if not limit or limit < 1 or limit > 100:
            raise ValueError("recent_message_limit 必须在 1 到 100 之间")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("缺少 playwright 依赖，请重新构建便携包") from exc
        self.download_dir.mkdir(parents=True, exist_ok=True)
        messages: list[SourceMessage] = []
        with sync_playwright() as playwright:
            staging_dir = self.download_dir.resolve() / ".browser-downloads" / uuid4().hex
            context = launch_telegram_context(playwright, self.profile_dir, self.headless,
                                              self.browser_executable_path, staging_dir)
            close_events = []
            monitor = None
            context.on("close", lambda _: (close_events.append("上下文关闭"), logging.warning("浏览器事件: Telegram 上下文已关闭")))
            try:
                page = context.pages[0] if context.pages else context.new_page()
                monitor = BrowserDownloadMonitor(page, context, staging_dir)
                page.on("close", lambda _: (close_events.append("页面关闭"), logging.warning("浏览器事件: Telegram 页面已关闭")))
                page.on("crash", lambda _: (close_events.append("页面崩溃"), logging.error("浏览器事件: Telegram 页面已崩溃")))
                page.set_default_timeout(self.timeout_ms)
                page.goto("https://web.telegram.org/k/", wait_until="domcontentloaded")
                logging.info("Telegram Web 登录检查: URL=%s, 标题=%s", page.url, page.title())
                target_url = self.channel_web_url(channel_url)
                logging.info("打开 Telegram Web 频道聊天: %s", target_url)
                page.goto(target_url, wait_until="domcontentloaded")
                if not page.url.startswith("https://web.telegram.org/"):
                    raise RuntimeError(f"未进入 Telegram Web 聊天页面，当前 URL: {page.url}")
                message_nodes = page.locator(self.message_selector)
                try:
                    message_nodes.first.wait_for(timeout=min(self.timeout_ms, 30000))
                except Exception as exc:
                    logging.error("频道诊断: URL=%s, 标题=%s, 消息节点数=0", page.url, page.title())
                    raise RuntimeError("Telegram Web 未读取到频道消息：若出现二维码，请确认抓取与首次登录使用同一浏览器登录态目录；否则请检查频道访问权限或页面结构。") from exc
                logging.info("频道消息已出现，寻找最新 %d 条 ZIP/RAR 消息", limit)
                if self.watch_mode:
                    self._wait_for_messages(page, message_nodes, min(2, limit))
                baseline = max((mid for mid, _ in self._unique_message_nodes(message_nodes, self.scan_limit)), default=0)
                state_path = self.download_dir / ".telegram_archive_processed.json"
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
                except (ValueError, OSError):
                    state = {}
                key = self.normalize_channel(channel_url)
                processed: set[int] = set(state.get(key, []))
                files_downloaded = 0
                logging.info("模式=%s, 启动时最新消息ID=%d, 已处理ID=%d", "持续监测" if self.watch_mode else "抓取一次", baseline, len(processed))
                while not self.stop_event.is_set():
                    if self.watch_mode:
                        if page.is_closed():
                            raise RuntimeError("监测浏览器窗口已关闭")
                        page.wait_for_timeout(2500)
                        ids = [mid for mid, node in self._unique_message_nodes(message_nodes, self.scan_limit)
                               if mid > baseline and mid not in processed and self._archive_message(node)]
                    else:
                        ids = self._scan_archive_ids(page, message_nodes, limit)
                        if len(ids) < limit:
                            logging.warning("扫描范围内仅找到 %d/%d 条 ZIP/RAR 消息，按实际找到数量处理", len(ids), limit)
                    logging.info("频道诊断: URL=%s, DOM 节点=%d, 本轮 ZIP/RAR 候选消息=%d", page.url, message_nodes.count(), len(ids))
                    completed = 0
                    for ordinal, message_id in enumerate(ids, 1):
                        if message_id in processed:
                            continue
                        message = self._locate_message(page, message_nodes, message_id)
                        if message is None:
                            raise RuntimeError(f"ZIP/RAR 消息 {message_id} 被虚拟列表回收，无法重新定位；已停止以避免漏抓")
                        if page.is_closed():
                            raise RuntimeError("Telegram 浏览器窗口已关闭，请在抓取结束前保持窗口打开")
                        try:
                            text = message.inner_text(timeout=5000)
                            attachments = self._attachment_locators(message)
                            attachment_count = attachments.count()
                        except Exception as exc:
                            raise RuntimeError(f"读取消息 {message_id} 失败，页面可能刷新或浏览器已关闭: {exc}") from exc
                        logging.info("检查第 %d/%d 条候选消息: message_id=%d, 附件控件=%d, 内容摘要=%s", ordinal, len(ids), message_id, attachment_count, re.sub(r"\s+", " ", text)[:180])
                        if not attachment_count:
                            logging.error("消息 %d 显示压缩包文件名但无附件控件；节点片段=%s", message_id, message.evaluate("el => el.outerHTML.slice(0, 1200)"))
                            raise RuntimeError(f"消息 {message_id} 找不到 ZIP/RAR 下载控件；请提供日志中的节点片段")
                        paths: list[Path] = []
                        saved_names: set[str] = set()
                        for attachment_index in range(attachment_count):
                            button = attachments.nth(attachment_index)
                            visible_names = self._visible_archive_names(button)
                            logging.info("消息 %d: 点击附件 %d/%d，等待下载事件", message_id, attachment_index + 1, attachment_count)
                            try:
                                download = monitor.start(button, self.start_timeout_seconds,
                                                         self.start_grace_seconds, self.stop_event)
                            except Exception as exc:
                                logging.error("下载阶段失败: 暂存目录=%s; 下载状态=%s; 当前资源=%s",
                                              staging_dir, monitor.entries, download_resource_status(self.download_dir))
                                if page.is_closed():
                                    raise RuntimeError("Telegram 浏览器窗口已关闭，请勿在抓取期间关闭") from exc
                                logging.error("消息 %d 附件 %d 启动失败，未继续点击其他附件: %s",
                                              message_id, attachment_index + 1, str(exc).splitlines()[0])
                                raise
                            suggested_ext = Path(download.suggested_filename).suffix.lower()
                            if suggested_ext and suggested_ext not in {".zip", ".rar"}:
                                logging.info("消息 %d: 跳过非 ZIP/RAR 浏览器下载: %s", message_id, download.suggested_filename)
                                download.cancel()
                                continue
                            if not visible_names and suggested_ext not in {".zip", ".rar"}:
                                logging.info("消息 %d: 跳过无法确认 ZIP/RAR 的附件: %s", message_id, download.suggested_filename)
                                download.cancel()
                                continue
                            if len(visible_names) > 1 and suggested_ext not in {".zip", ".rar"}:
                                download.cancel()
                                raise RuntimeError(f"消息 {message_id} 的卡片包含多个压缩包名，无法将 UUID 下载与文件正确配对")
                            visible_name = visible_names[0] if len(visible_names) == 1 else None
                            file_key = visible_name or download.suggested_filename
                            if file_key in saved_names:
                                download.cancel()
                                continue
                            logging.info("消息 %d: 下载已开始，浏览器名称=%s，卡片名称=%s", message_id, download.suggested_filename, visible_name or "未识别")
                            resources = download_resource_status(self.download_dir)
                            logging.info("采用浏览器本地下载路径复制；下载完成前不复制、不关闭浏览器")
                            logging.info("下载前资源: 磁盘可用=%s 字节, 物理内存可用=%s 字节, 提交额度可用=%s 字节",
                                         resources.get("disk_free", "未知"), resources.get("memory_available", "未知"),
                                         resources.get("page_available", "未知"))
                            # 不凭空猜测附件大小；余量不足时明确终止，避免保存一半后被误认为完整文件。
                            if isinstance(resources.get("memory_available"), int) and resources["memory_available"] < 1024 * 1024 * 1024:
                                logging.warning("物理内存可用不足 1 GiB，大文件可能导致 Chromium 意外退出；建议关闭占内存程序后重试")
                            if isinstance(resources.get("disk_free"), int) and resources["disk_free"] < 512 * 1024 * 1024:
                                download.cancel()
                                raise RuntimeError("下载目录剩余空间不足 512 MiB，请释放空间后重试")
                            try:
                                monitor.wait_complete(download, self.transfer_timeout_seconds)
                                target = self._verified_download(download, self.download_dir, visible_name,
                                                                 page=page, context=context, resource_status=resources,
                                                                 archive_test_executable_path=self.archive_test_executable_path,
                                                                 archive_test_timeout_seconds=self.archive_test_timeout_seconds)
                                monitor.release(download)
                            except Exception as exc:
                                logging.error("下载阶段失败: 暂存目录=%s; 下载状态=%s; 当前资源=%s",
                                              staging_dir, monitor.entries, download_resource_status(self.download_dir))
                                if isinstance(exc, ArchiveValidationError):
                                    monitor.release(download)
                                logging.error("消息 %d 附件保存失败，异常类型=%s；即将进入程序清理流程: %s",
                                              message_id, type(exc).__name__, str(exc).splitlines()[0])
                                stage = "压缩包校验失败（已保留文件）" if isinstance(exc, ArchiveValidationError) else "传输/永久保存失败"
                                raise RuntimeError(f"消息 {message_id} 附件 {download.suggested_filename} {stage}: {type(exc).__name__}: {str(exc).splitlines()[0]}") from exc
                            paths.append(target)
                            saved_names.add(file_key)
                            files_downloaded += 1
                            logging.info("消息 %d: 已保存 %s，大小=%d 字节", message_id, target.resolve(), target.stat().st_size)
                        if not paths:
                            raise RuntimeError(f"消息 {message_id} 没有成功下载 ZIP/RAR 附件，未计为已处理")
                        source = SourceMessage(message_id=message_id, chat=channel_url, text=text, attachment_paths=paths)
                        messages.append(source)
                        if self.on_message is not None:
                            self.on_message(source)
                        processed.add(message_id)
                        completed += 1
                        state[key] = sorted(processed)
                        temporary = state_path.with_suffix(".tmp")
                        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                        temporary.replace(state_path)
                    if not self.watch_mode:
                        logging.info("本次实际新增处理 ZIP/RAR 消息=%d，筛选目标=%d", completed, limit)
                        break
                    logging.info("监测中：只处理启动后新出现的 ZIP/RAR 消息；已处理=%d", len(processed))
                logging.info("抓取汇总: 有效附件=%d, 含附件消息=%d", files_downloaded, len(messages))
            finally:
                if monitor is not None:
                    monitor.cancel_pending()
                logging.info("程序开始关闭 Telegram 浏览器上下文；此前关闭事件=%s",
                             ", ".join(close_events) if close_events else "无")
                try:
                    context.close()
                except Exception as exc:
                    logging.warning("程序关闭 Telegram 浏览器上下文时出错（可能此前已断开）: %s", exc)
                else:
                    logging.info("程序已完成 Telegram 浏览器上下文关闭")
                if staging_dir.exists() and not any(staging_dir.iterdir()):
                    staging_dir.rmdir()
                elif staging_dir.exists():
                    logging.warning("未清理的下载暂存文件保留在: %s（不作为校验通过的附件）", staging_dir)
        return messages


def telegram_browser_login(profile_dir: Path, channel_url: str = "", browser_executable_path: str = "") -> None:
    """打开可见 Telegram Web，供用户人工登录并保存会话。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 playwright 依赖，请重新构建便携包") from exc
    profile_dir = Path(profile_dir)
    if channel_url:
        TelegramBrowserAdapter.normalize_channel(channel_url)
    with sync_playwright() as playwright:
        context = launch_telegram_context(playwright, profile_dir, False, browser_executable_path)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://web.telegram.org/k/", wait_until="domcontentloaded")
            logging.info("Telegram 登录页: URL=%s", page.url)
            logging.info("请在程序打开的 Telegram Web 窗口完成登录；右侧独立桌面客户端的会话不能自动复用。")
            # Web K 登录成功后会从二维码/登录页进入会话列表；不要再强制等待用户关闭窗口。
            logged_in = False
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline and not page.is_closed():
                try:
                    current_url = page.url
                    body_text = page.locator("body").inner_text(timeout=2000)
                    has_login_prompt = bool(re.search(r"扫码|QR|Log in|登录|Phone number", body_text, re.I))
                    has_chat_shell = bool(page.locator("[class*='chat-list'], [class*='dialogs'], [class*='sidebar'], [data-peer-id]").count())
                    if current_url.startswith("https://web.telegram.org/") and has_chat_shell and not has_login_prompt:
                        logged_in = True
                        logging.info("Telegram Web 登录成功，会话已保存到: %s", context._pipeline_profile_dir)
                        break
                except Exception:
                    pass
                page.wait_for_timeout(1000)
            if not logged_in and not page.is_closed():
                logging.warning("等待登录成功超时；请确认页面已进入会话列表")
        finally:
            context.close()


class LocalCloudStorageAdapter:
    """本地验收适配器：模拟固定目录上传并返回 file:// 链接。"""

    def __init__(self, root_dir: Path):
        self.root_dir = root_dir

    def upload(self, local_path: Path, remote_folder: str) -> str:
        destination_dir = self.root_dir / (remote_folder or "default")
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / local_path.name
        shutil.copy2(local_path, destination)
        return destination.resolve().as_uri()


class QuarkCloudStorageAdapter:
    """夸克网盘适配器占位接口。

    夸克个人网盘没有在本项目中确认可长期依赖的公开上传 API，因此不伪造请求。
    使用时注入一个已验证的 SDK/HTTP 实现，或由浏览器自动化层实现 upload_file。
    """

    def __init__(self, cookie: str, upload_file=None):
        self.cookie = cookie or os.getenv("QUARK_COOKIE", "")
        self._upload_file = upload_file

    def upload(self, local_path: Path, remote_folder: str) -> str:
        if not self.cookie:
            raise RuntimeError("缺少夸克登录态：请通过安全的授权方式提供 QUARK_COOKIE")
        if self._upload_file is None:
            raise NotImplementedError("尚未绑定经过验证的夸克上传实现，未执行任何网络请求")
        return str(self._upload_file(local_path, remote_folder, self.cookie))
