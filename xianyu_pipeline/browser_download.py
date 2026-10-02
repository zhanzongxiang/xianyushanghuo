from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable
from uuid import UUID


class DownloadStartTimeout(RuntimeError):
    pass


class DownloadCancelled(RuntimeError):
    pass


class ChromiumDownload:
    def __init__(self, monitor: "BrowserDownloadMonitor", guid: str):
        self.monitor = monitor
        self.guid = str(UUID(guid))
        self.entry = monitor.entries[guid]
        self.url = self.entry["url"]
        self.suggested_filename = self.entry["suggestedFilename"]

    def path(self) -> Path:
        if self.entry.get("state") != "completed":
            raise RuntimeError("浏览器尚未完成传输，禁止读取临时文件作为成品")
        return self.monitor.staging_dir / self.guid

    def failure(self) -> str | None:
        state = self.entry.get("state")
        if state == "completed":
            return None
        return f"Chromium download state: {state}"

    def cancel(self) -> None:
        if self.entry.get("state") == "inProgress":
            self.monitor.session.send("Browser.cancelDownload", {"guid": self.guid})


class BrowserDownloadMonitor:
    """Observe Chromium download events without blocking Playwright's event loop.

    on_progress receives copies of raw CDP entries for the unambiguous active GUID.
    CDP completion is transfer completion only, not archive validation success.
    """

    def __init__(self, page, context, staging_dir: Path,
                 on_progress: Callable[[dict], None] | None = None):
        self.page = page
        self.on_progress = on_progress
        self.staging_dir = staging_dir.resolve()
        self.session = context.browser.new_browser_cdp_session()
        self.entries: dict[str, dict] = {}
        self.session.on("Browser.downloadWillBegin", self._begin)
        self.session.on("Browser.downloadProgress", self._progress)
        # Browser-domain events require a browser-level CDP session.
        # Track GUID files explicitly so transfer deadlines don't block on path().
        self.session.send("Browser.setDownloadBehavior", {
            "behavior": "allowAndName", "downloadPath": str(staging_dir.resolve()), "eventsEnabled": True,
        })

    def _begin(self, event: dict) -> None:
        self.entries[event["guid"]] = {**event, "state": "inProgress"}
        logging.info("浏览器下载开始: guid=%s; 文件名=%s", event["guid"], event.get("suggestedFilename"))
        self._notify(event["guid"])

    def _progress(self, event: dict) -> None:
        self.entries.setdefault(event["guid"], {}).update(event)
        self._notify(event["guid"])

    def _notify(self, guid: str) -> None:
        # Ignore old GUIDs and ambiguous clicks; never associate another file's bytes.
        active = getattr(self, "active_guids", None)
        if self.on_progress is None or active is None or set(self.entries).difference(active) != {guid}:
            return
        try:
            self.on_progress(dict(self.entries[guid]))
        except (Exception, asyncio.CancelledError):
            logging.exception("浏览器下载进度回调失败，继续传输")

    def _tick(self) -> None:
        if self.page.is_closed():
            raise RuntimeError("浏览器页面已关闭，下载未完成")
        self.page.wait_for_timeout(250)

    def cancel_pending(self) -> None:
        for guid, entry in list(self.entries.items()):
            if entry.get("state") == "inProgress":
                try:
                    self.session.send("Browser.cancelDownload", {"guid": guid})
                except Exception:
                    logging.exception("取消未完成下载失败: %s", guid)

    def start(self, button, timeout_seconds: int, grace_seconds: int, stop_event=None):
        self.active_guids = set(self.entries)
        if stop_event is not None and stop_event.is_set():
            raise DownloadCancelled("已停止等待下载开始")
        started = time.monotonic()
        next_log = started + 10
        warned = False
        button.click(timeout=5000)
        while not set(self.entries).difference(self.active_guids):
            now = time.monotonic()
            if stop_event is not None and stop_event.is_set():
                self.cancel_pending()
                raise DownloadCancelled("已停止等待下载开始")
            if now - started >= timeout_seconds and not warned:
                warned = True
                logging.warning("下载事件尚未出现；继续观察 %s 秒，不重复点击、不切换附件", grace_seconds)
            if now - started >= timeout_seconds + grace_seconds:
                self.cancel_pending()
                raise DownloadStartTimeout(
                    f"下载启动超时（已等待 {timeout_seconds + grace_seconds} 秒）；"
                    "未重复点击或处理下一个附件，请检查网页下载状态及运行日志"
                )
            if now >= next_log:
                logging.info("等待网页准备下载: 已等待 %.0f 秒", now - started)
                next_log = now + 10
            self._tick()
        guids = set(self.entries).difference(self.active_guids)
        if len(guids) != 1:
            self.cancel_pending()
            raise RuntimeError("单次点击触发多个下载，停止以避免附件错配")
        return ChromiumDownload(self, guids.pop())

    def release(self, download: ChromiumDownload) -> None:
        """Remove only the exact completed GUID after a durable copy is verified."""
        try:
            download.path().unlink(missing_ok=True)
        except OSError:
            logging.exception("永久文件已保留，但浏览器暂存文件清理失败: %s", download.path())

    def wait_complete(self, download, timeout_seconds: int, stop_event=None) -> None:
        deadline = time.monotonic() + timeout_seconds
        next_log = 0
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                self.cancel_pending()
                raise DownloadCancelled("已请求停止浏览器下载")
            entries = [entry for guid, entry in self.entries.items() if guid not in self.active_guids]
            if len(entries) > 1:
                self.cancel_pending()
                raise RuntimeError("出现多个并发下载，无法确认附件对应关系")
            if entries:
                entry = entries[0]
                if entry.get("url") and entry["url"] != download.url:
                    self.cancel_pending()
                    raise RuntimeError("浏览器下载事件与附件 URL 不一致")
                state = entry.get("state")
                if state == "completed":
                    logging.info("传输完成: 已接收=%s 字节; 浏览器总大小=%s 字节",
                                 entry.get("receivedBytes"), entry.get("totalBytes"))
                    return
                if state == "canceled":
                    raise DownloadCancelled(f"浏览器传输已取消: {download.failure()}")
                if time.monotonic() >= next_log:
                    logging.info("传输中: 已接收=%s 字节; 总大小=%s 字节",
                                 entry.get("receivedBytes", 0), entry.get("totalBytes", "未知"))
                    next_log = time.monotonic() + 10
            self._tick()
        self.cancel_pending()
        raise RuntimeError(f"下载传输超时（{timeout_seconds} 秒），已取消当前传输，未标记成功")
