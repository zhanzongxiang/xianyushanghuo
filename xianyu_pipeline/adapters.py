from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import os
import re
import shutil
import sys
import ctypes
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from .archive_validation import ArchiveValidationError, validate_rar
from .browser_download import BrowserDownloadMonitor, DownloadCancelled
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


def parse_telegram_api_proxy(value: str) -> dict | None:
    if not isinstance(value, str):
        raise ValueError("Telegram API 代理地址必须为字符串")
    if not value:
        return None
    if re.search(r"\s", value):
        raise ValueError("Telegram API 代理地址不能包含空白字符")
    parsed = urlparse(value)
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Telegram API 代理主机或端口无效") from exc
    if (parsed.scheme not in {"socks5", "http"} or not host or not port or "@" in parsed.netloc
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("Telegram API 代理须为 socks5://主机:端口 或 http://主机:端口，不包含账号密码")
    return {"proxy_type": parsed.scheme, "addr": host, "port": port, "rdns": True}


def parse_telegram_dc2_server(value: str) -> tuple[str, int] | None:
    if value == "":
        return None
    if value != "149.154.167.50:443":
        raise ValueError("仅支持图中的 DC 2 生产地址 149.154.167.50:443，不使用测试服务器")
    return "149.154.167.50", 443


class _TransferEvents:
    """One attachment's snapshots; byte counts and rates never come from a timer."""

    def __init__(self, callback, message_id: int, chat: str, name: str, title: str, size=0):
        self.callback = callback
        self.data = dict(message_id=message_id, chat=chat, filename=name,
                         title=re.sub(r"\s+", " ", title).strip()[:180] or name,
                         size=size or 0, received=0, speed=0, eta=None, image_paths=[],
                         image_source="", image_message_ids=[], image_group_id="")
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", chat) and chat != "joinchat" and message_id > 0:
            self.data["source_url"] = f"https://t.me/{chat}/{message_id}"
        self.sample_time = None
        self.sample_bytes = 0
        self.terminal = False

    def emit(self, status: str, **fields) -> None:
        if self.terminal:
            return
        self.data.update(fields, download_status=status)
        if status != "downloading":
            self.data.update(speed=0, eta=None)
        self.terminal = status in {"completed", "failed", "cancelled"}
        if self.callback is not None:
            try:
                self.callback({**self.data, "image_paths": list(self.data["image_paths"]),
                               "image_message_ids": list(self.data["image_message_ids"])})
            except (Exception, asyncio.CancelledError):
                logging.exception("下载事件回调失败，继续处理附件")

    def start(self) -> None:
        self.sample_time = time.monotonic()
        self.sample_bytes = 0
        self.emit("downloading")

    def progress(self, received: int, size: int | None) -> None:
        now = time.monotonic()
        elapsed = now - self.sample_time if self.sample_time is not None else 0
        speed = max(0, received - self.sample_bytes) / elapsed if elapsed > 0 else 0
        self.sample_time, self.sample_bytes = now, received
        total = size if size is not None else self.data["size"]
        eta = max(0, total - received) / speed if total > 0 and speed > 0 else None
        self.emit("downloading", received=received, size=total, speed=speed, eta=eta)

    def fail(self, error: BaseException, local_path: Path | None = None) -> None:
        cause = error
        while cause is not None:
            if getattr(cause, "local_path", None):
                local_path = Path(cause.local_path)
                break
            cause = cause.__cause__
        fields = {"error": f"{type(error).__name__}: {error}"}
        if local_path is not None:
            fields["local_path"] = str(local_path.resolve())
        status = "cancelled" if isinstance(error, (DownloadCancelled, asyncio.CancelledError, KeyboardInterrupt)) else "failed"
        self.emit(status, **fields)


_PREVIEW_TIMEOUT_SECONDS = 3.0
_PHOTO_LOOKUP_TIMEOUT_SECONDS = 5.0
_PHOTO_TIMEOUT_SECONDS = 8.0
_ALBUM_TIMEOUT_SECONDS = 25.0


class TelethonTelegramAdapter:
    """Telegram 实际适配器；需要额外安装 telethon 并完成首次登录。

    on_event receives synchronous dict snapshots (bytes, bytes/second, seconds).
    Without a callback, preview downloads are disabled and CLI output is unchanged.
    """

    def __init__(self, api_id: int, api_hash: str, session_path: str | Path, download_dir: Path,
                 scan_limit: int = 200, archive_test_executable_path: str = "",
                 archive_test_timeout_seconds: int = 600, proxy_url: str = "",
                 dc2_server: str = "", download_start_timeout_seconds: int = 180,
                 download_transfer_timeout_seconds: int = 3600, stop_event: Event | None = None,
                 on_event: Callable[[dict], None] | None = None):
        if not api_id or not api_hash:
            raise ValueError("Telegram 适配器需要 api_id 和 api_hash")
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = str(session_path)
        self.download_dir = Path(download_dir)
        self.scan_limit = scan_limit
        self.archive_test_executable_path = archive_test_executable_path
        self.archive_test_timeout_seconds = archive_test_timeout_seconds
        self.proxy = parse_telegram_api_proxy(proxy_url)
        self.dc2_server = parse_telegram_dc2_server(dc2_server)
        self.download_start_timeout_seconds = download_start_timeout_seconds
        self.download_transfer_timeout_seconds = download_transfer_timeout_seconds
        self.stop_event = stop_event
        self.on_event = on_event

    def _client(self):
        from telethon.sync import TelegramClient
        if self.proxy:
            try:
                import python_socks  # noqa: F401
            except ImportError as exc:
                raise RuntimeError("Telegram API 代理依赖缺失，请重新安装 requirements.txt 或更新便携包") from exc
        client_type = TelegramClient
        if self.dc2_server:
            dc2_server = self.dc2_server

            # Telethon resolves media DCs independently of the session's initial DC.
            class Dc2TelegramClient(TelegramClient):
                async def _get_dc(self, dc_id, cdn=False):
                    dc = await super()._get_dc(dc_id, cdn=cdn)
                    if dc_id == 2 and not cdn:
                        selected = copy.copy(dc)
                        selected.ip_address, selected.port = dc2_server
                        logging.info("Telegram API 跨 DC 文件连接使用 DC 2 生产节点: %s:%d",
                                     *dc2_server)
                        return selected
                    return dc

            client_type = Dc2TelegramClient
        options = {"proxy": self.proxy} if self.proxy else {}
        client = client_type(self.session_path, self.api_id, self.api_hash, **options)
        if self.dc2_server:
            session = client.session
            if session.dc_id in (0, 2) and session.auth_key is None:
                session.set_dc(2, *self.dc2_server)
                session.save()
                logging.info("Telegram API 使用可选的 DC 2 生产节点: %s:%d", *self.dc2_server)
            elif session.server_address != self.dc2_server[0]:
                logging.info("Telegram API 已有会话密钥或其他 DC，保留会话原节点: %s:%s",
                             session.server_address, session.port)
        if self.proxy:
            logging.info("Telegram API 使用代理: %s://%s:%d",
                         self.proxy["proxy_type"], self.proxy["addr"], self.proxy["port"])
        return client

    def login(self) -> None:
        Path(self.session_path).parent.mkdir(parents=True, exist_ok=True)
        client = self._client()
        try:
            try:
                client.start()
            except (ConnectionError, OSError) as exc:
                raise RuntimeError("Telegram API 无法连接，请检查网络或配置的代理") from exc
            if not client.is_user_authorized():
                raise RuntimeError("Telegram API 授权未完成")
        finally:
            client.disconnect()
        logging.info("Telegram API 登录成功，会话已保存在本机")

    @staticmethod
    def normalize_channel(channel: str) -> str:
        value = channel.strip()
        if value.startswith("https://t.me/") or value.startswith("http://t.me/"):
            parsed = urlparse(value)
            if parsed.hostname != "t.me":
                raise ValueError("频道链接必须是 t.me 地址")
            parts = [part for part in parsed.path.split("/") if part]
            if not parts or parts[0] == "c":
                raise ValueError("暂不支持仅凭私有频道链接推断频道，请填写可访问的频道用户名或聊天 ID")
            value = parts[0]
        value = value.removeprefix("@").strip()
        if not value or not re.fullmatch(r"[A-Za-z0-9_+\-]+", value):
            raise ValueError("频道链接或用户名格式不正确")
        return value

    @staticmethod
    def _archive_name(message) -> str | None:
        name = getattr(getattr(message, "file", None), "name", None)
        if isinstance(name, str) and re.fullmatch(r'[^\\/:*?"<>|\x00-\x1f]+\.(?:zip|rar)', name, re.I):
            return name
        return None

    async def _download_preview(self, client, media, message_id: int, timeout: float) -> str:
        preview = self.download_dir.resolve() / ".previews" / f"{int(message_id)}-{uuid4().hex}.jpg"
        saved = False
        try:
            preview.parent.mkdir(parents=True, exist_ok=True)

            async def transfer():
                result = client.download_media(media, file=str(preview), thumb=-1)
                return await result if inspect.isawaitable(result) else result

            returned = await asyncio.wait_for(transfer(), timeout=timeout)
            if not returned or Path(returned).resolve() != preview or not preview.is_file():
                raise RuntimeError("预览图未生成预期文件")
            if preview.stat().st_size > 12 * 1024 * 1024:
                raise RuntimeError("预览图超过 12 MiB 限制")
            with preview.open("rb") as stream:
                header = stream.read(12)
            # Telegram can return a video thumbnail. Only publish bitmap previews.
            if header.startswith(b"\xff\xd8\xff"):
                suffix = ".jpg"
            elif header.startswith(b"\x89PNG\r\n\x1a\n"):
                suffix = ".png"
            elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
                suffix = ".webp"
            else:
                raise RuntimeError("预览内容不是支持的图片")
            target = preview.with_suffix(suffix)
            if target != preview:
                preview.replace(target)
            saved = True
            return str(target)
        finally:
            # Failed or timed-out thumbnail writes must never become image_paths.
            if preview.exists() and not saved:
                try:
                    preview.unlink()
                except OSError:
                    logging.warning("缩略图临时文件清理失败: %s", preview)

    def _download_thumbnail(self, client, message) -> list[str]:
        document = getattr(message, "document", None)
        if self.on_event is None or not getattr(document, "thumbs", None):
            return []
        try:
            coroutine = self._download_preview(client, document, message.id, _PREVIEW_TIMEOUT_SECONDS)
            loop = getattr(client, "loop", None)
            return [loop.run_until_complete(coroutine) if loop is not None else asyncio.run(coroutine)]
        except (Exception, asyncio.CancelledError) as exc:
            logging.warning("消息 %s 的 document 缩略图不可用，附件继续传输: %s: %s",
                            message.id, type(exc).__name__, exc)
            return []

    @staticmethod
    def _preceding_photo_group(history, archive_id: int) -> list:
        from telethon.tl.types import MessageMediaPhoto

        photos = []
        group_id = None
        previous_id = archive_id
        for item in history:
            # Unfiltered history preserves intervening text/files as hard boundaries.
            # message.photo alone also includes webpage and service-message photos.
            if (not item or not isinstance(getattr(item, "media", None), MessageMediaPhoto)
                    or not getattr(item, "photo", None) or not 0 < item.id < previous_id):
                break
            current_group = getattr(item, "grouped_id", None)
            if photos and (group_id is None or current_group != group_id):
                break
            group_id = current_group
            photos.append(item)
            previous_id = item.id
            if len(photos) == 5:
                break
        return list(reversed(photos))

    def _download_preceding_photos(self, client, message, chat: str) -> dict:
        if self.on_event is None:
            return {}
        paths, message_ids = [], []
        group_id = ""

        async def retrieve():
            nonlocal group_id

            async def lookup():
                result = client.get_messages(chat, limit=6, offset_id=message.id)
                return await result if inspect.isawaitable(result) else result

            history = await asyncio.wait_for(lookup(), timeout=_PHOTO_LOOKUP_TIMEOUT_SECONDS)
            photos = self._preceding_photo_group(history, message.id)
            if not photos:
                return
            group_id = str(getattr(photos[0], "grouped_id", None) or "")
            logging.info("Telegram API 前置配图: 压缩包消息=%s, 图片消息=%s（按相邻位置自动关联）",
                         message.id, [photo.id for photo in photos])
            for photo in photos:
                if self.stop_event and self.stop_event.is_set():
                    raise DownloadCancelled("已请求停止 Telegram API 下载")
                try:
                    path = await self._download_preview(client, photo.photo, photo.id, _PHOTO_TIMEOUT_SECONDS)
                except Exception as exc:
                    logging.warning("配图消息 %s 下载失败，继续处理附件: %s: %s",
                                    photo.id, type(exc).__name__, exc)
                    continue
                paths.append(path)
                message_ids.append(photo.id)

        async def guarded():
            task = asyncio.create_task(asyncio.wait_for(retrieve(), timeout=_ALBUM_TIMEOUT_SECONDS))
            try:
                while True:
                    if self.stop_event and self.stop_event.is_set():
                        raise DownloadCancelled("已请求停止 Telegram API 下载")
                    done, _ = await asyncio.wait((task,), timeout=0.2)
                    if task in done:
                        return await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        try:
            loop = getattr(client, "loop", None)
            if loop is None:
                asyncio.run(guarded())
            else:
                loop.run_until_complete(guarded())
        except DownloadCancelled:
            for path in paths:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    logging.warning("已取消任务的配图清理失败: %s", path)
            raise
        except (Exception, asyncio.CancelledError) as exc:
            logging.warning("消息 %s 的前置配图获取未完成，保留 %d 张，附件继续传输: %s: %s",
                            message.id, len(paths), type(exc).__name__, exc)
        return {"image_paths": paths, "image_source": "preceding_album" if group_id else "preceding_photo",
                "image_message_ids": message_ids, "image_group_id": group_id} if paths else {}

    def _download_archive(self, client, message, name: str, transfer: _TransferEvents | None = None) -> Path:
        staging = self.download_dir / ".telegram-api-downloads" / uuid4().hex
        source = staging / name
        try:
            staging.mkdir(parents=True, exist_ok=True)
            if self.stop_event and self.stop_event.is_set():
                raise DownloadCancelled("已请求停止 Telegram API 下载")
            if transfer is not None:
                previews = self._download_preceding_photos(client, message, transfer.data["chat"])
                if self.stop_event and self.stop_event.is_set():
                    raise DownloadCancelled("已请求停止 Telegram API 下载")
                if not previews:
                    image_paths = self._download_thumbnail(client, message)
                    if image_paths:
                        previews = dict(image_paths=image_paths, image_source="document_thumbnail",
                                        image_message_ids=[message.id], image_group_id="")
                if previews:
                    transfer.emit("queued", **previews)
            # Preview latency must not count towards the attachment's watchdog or speed.
            started = last_progress = last_report = time.monotonic()
            received = 0

            def on_progress(current: int, total: int | None) -> None:
                nonlocal last_progress, last_report, received
                now = time.monotonic()
                received = current
                last_progress = now
                if transfer is not None:
                    transfer.progress(current, total)
                if current and (current == total or now - last_report >= 15):
                    logging.info("Telegram API 下载进度: 消息=%d, 已接收=%d/%s 字节",
                                 message.id, current, total if total is not None else "?")
                    last_report = now

            async def guarded_download():
                nonlocal last_report

                async def transfer():
                    result = client.download_media(message, file=str(source), progress_callback=on_progress)
                    return await result if inspect.isawaitable(result) else result

                task = asyncio.create_task(transfer())
                try:
                    while True:
                        done, _ = await asyncio.wait((task,), timeout=1)
                        if task in done:
                            return await task
                        now = time.monotonic()
                        if self.stop_event and self.stop_event.is_set():
                            raise DownloadCancelled("已请求停止 Telegram API 下载")
                        if now - last_progress >= self.download_start_timeout_seconds:
                            raise RuntimeError(
                                f"Telegram API 下载连续 {self.download_start_timeout_seconds} 秒无进展")
                        if now - started >= self.download_transfer_timeout_seconds:
                            raise RuntimeError(
                                f"Telegram API 下载超过 {self.download_transfer_timeout_seconds} 秒总时限")
                        if now - last_report >= 30:
                            logging.info("Telegram API 下载等待: 消息=%d, 已接收=%d 字节, "
                                         "距上次进展=%d 秒", message.id, received, int(now - last_progress))
                            last_report = now
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

            if self.stop_event and self.stop_event.is_set():
                raise DownloadCancelled("已请求停止 Telegram API 下载")
            if transfer is not None:
                transfer.start()
            logging.info("Telegram API 开始下载: 消息=%d, 文件=%s, 预期=%s 字节",
                         message.id, name, getattr(message.file, "size", "?"))
            loop = getattr(client, "loop", None)
            returned = (loop.run_until_complete(guarded_download()) if loop is not None
                        else asyncio.run(guarded_download()))
            if not returned or Path(returned).resolve() != source.resolve() or not source.is_file():
                raise RuntimeError("Telegram API 下载未生成预期的压缩包文件")
            if self.stop_event and self.stop_event.is_set():
                raise DownloadCancelled("已请求停止 Telegram API 下载")
            if transfer is not None:
                transfer.emit("validating", received=source.stat().st_size)
            size = getattr(message.file, "size", None)
            if not source.stat().st_size or (size is not None and source.stat().st_size != size):
                raise RuntimeError("Telegram API 下载文件大小与消息元数据不一致")
            if source.suffix.lower() == ".zip":
                with zipfile.ZipFile(source) as archive:
                    bad = archive.testzip()
                    if bad:
                        raise RuntimeError(f"ZIP 内容校验失败: {bad}")
            else:
                validate_rar(source, self.archive_test_executable_path, self.archive_test_timeout_seconds)
            target = self.download_dir / name
            index = 2
            while target.exists():
                target = self.download_dir / f"{Path(name).stem}-{index}{Path(name).suffix}"
                index += 1
            source.replace(target)
            logging.info("Telegram API 附件已校验保存: 消息=%d, 文件=%s, 字节=%d",
                         message.id, target, target.stat().st_size)
            if transfer is not None:
                transfer.emit("completed", local_path=str(target.resolve()), received=target.stat().st_size,
                              size=target.stat().st_size)
            return target
        except (Exception, asyncio.CancelledError, KeyboardInterrupt) as exc:
            recovery = None
            detail = f"消息 {message.id} 的 {name} 下载失败: {exc}"
            if source.is_file():
                size = source.stat().st_size
                if not size:
                    try:
                        source.unlink()
                    except OSError:
                        recovery = source
                        logging.exception("零字节 API 暂存文件清理失败: %s", source)
                    detail = f"消息 {message.id} 的 {name} 下载失败，未收到文件字节: {exc}"
                else:
                    recovery = self.download_dir / ".unverified" / uuid4().hex / name
                    try:
                        recovery.parent.mkdir(parents=True, exist_ok=True)
                        source.replace(recovery)
                    except OSError:
                        recovery = source
                        logging.exception("API 下载隔离失败，保留原暂存文件: %s", source)
                    detail = (f"消息 {message.id} 的 {name} 未通过下载/校验，已接收 {size} 字节，"
                              f"文件保留待检查: {recovery}; 原因: {exc}")
                if transfer is not None:
                    transfer.data["received"] = size
            if transfer is not None:
                transfer.fail(exc, recovery)
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                raise
            raise RuntimeError(detail) from exc
        finally:
            if staging.is_dir() and not any(staging.iterdir()):
                staging.rmdir()

    def fetch_messages(self, chat: str, message_ids: list[int], channel_url: str = "",
                       mode: str = "message_ids", limit: int = 20) -> list[SourceMessage]:
        target = self.normalize_channel(channel_url or chat)
        if mode == "message_ids" and not message_ids:
            raise ValueError("指定消息模式需要 message_ids；或将 fetch_mode 改为 recent")
        if mode not in {"message_ids", "recent"} or not 1 <= limit <= 100 or not 1 <= self.scan_limit <= 1000:
            raise ValueError("Telegram API 抓取模式、数量或扫描上限无效")
        self.download_dir.mkdir(parents=True, exist_ok=True)
        Path(self.session_path).parent.mkdir(parents=True, exist_ok=True)
        client = self._client()
        try:
            try:
                client.connect()
            except (ConnectionError, OSError) as exc:
                raise RuntimeError("Telegram API 无法连接，请检查网络或配置的代理") from exc
            if not client.is_user_authorized():
                raise RuntimeError("Telegram API 尚未登录，请先在界面点击“首次登录 Telegram”完成账号授权")
            iterator = (client.iter_messages(target, limit=self.scan_limit) if mode == "recent"
                        else (client.get_messages(target, ids=mid) for mid in message_ids))
            chosen = []
            scanned = 0
            for message in iterator:
                scanned += 1
                if message and (name := self._archive_name(message)):
                    chosen.append((message, name))
                    if mode == "recent" and len(chosen) >= limit:
                        break
            logging.info("Telegram API 消息扫描: 检查=%d, ZIP/RAR 消息=%d, 目标=%d",
                         scanned, len(chosen), limit)
            messages = []
            jobs = [(message, name, _TransferEvents(self.on_event, message.id, target, name,
                                                   message.message or "", getattr(message.file, "size", 0)))
                    for message, name in (reversed(chosen) if mode == "recent" else chosen)]
            for _, _, transfer in jobs:
                transfer.emit("queued")
            try:
                for message, name, transfer in jobs:
                    path = self._download_archive(client, message, name, transfer)
                    messages.append(SourceMessage(message_id=message.id, chat=target,
                                                  text=message.message or "", attachment_paths=[path]))
            except (Exception, asyncio.CancelledError, KeyboardInterrupt) as exc:
                for _, _, transfer in jobs:
                    if not transfer.terminal:
                        transfer.emit("cancelled", error=f"本批次已中止，未继续下载: {type(exc).__name__}: {exc}")
                raise
            return messages
        finally:
            client.disconnect()


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


ARCHIVE_NAME_SELECTOR = "[class*='document-name'], [class*='file-name'], [class*='document-title']"
DOCUMENT_CARD_SELECTOR = "[class*='document-container']:not(:has([class*='document-container']))"
ARCHIVE_CARDS_JS = r"""(message, customSelector) => {
    const nameSelector = '[class*="document-name"], [class*="file-name"], [class*="document-title"]';
    const archiveName = value => typeof value === 'string' &&
        /^[^\\/:*?"<>|\r\n]+\.(zip|rar)$/i.test(value.trim());
    const results = [];
    if (customSelector) {
        for (const card of message.querySelectorAll(customSelector)) {
            const named = Array.from(card.querySelectorAll(nameSelector), el => el.textContent.trim());
            const lines = (card.innerText || '').split(/\r?\n/).map(line => line.trim());
            const matches = [...named, ...lines].filter(archiveName);
            if (new Set(matches).size === 1) results.push({name: matches[0], source: 'custom'});
        }
        return results;
    }
    for (const card of message.querySelectorAll('[class*="document-container"]')) {
        if (card.querySelector('[class*="document-container"]')) continue;
        const names = Array.from(card.querySelectorAll(nameSelector), el => el.textContent.trim())
            .filter(archiveName);
        if (names.length === 1) results.push({name: names[0], source: 'document'});
    }
    for (const nameNode of message.querySelectorAll(nameSelector)) {
        if (nameNode.closest('[class*="document-container"], a[download]')) continue;
        const name = nameNode.textContent.trim();
        if (archiveName(name)) results.push({name, source: 'name'});
    }
    for (const link of message.querySelectorAll('a[download]')) {
        if (link.closest('[class*="document-container"]')) continue;
        const name = link.getAttribute('download');
        if (archiveName(name)) results.push({name: name.trim(), source: 'link'});
    }
    return results;
}"""


@dataclass(frozen=True)
class ArchiveCard:
    name: str
    source: str


@dataclass(frozen=True)
class MessageSnapshot:
    message_id: int
    attribute: str
    value: str
    text: str
    cards: tuple[ArchiveCard, ...]


class TelegramBrowserAdapter:
    """通过 Telegram Web 的持久化浏览器会话抓取附件。

    首次使用时以可见窗口登录；登录状态保存在 profile_dir，后续可选择后台运行。
    页面选择器允许配置，但默认只使用公开的稳定属性和文本，不调用 Telegram 内部 API。
    on_event is a synchronous dict observer; browser previews remain unavailable.
    """

    def __init__(self, profile_dir: Path, download_dir: Path, headless: bool = False,
                 timeout_seconds: int = 180, message_selector: str = "",
                 attachment_selector: str = "", watch_mode: bool = False,
                 stop_event: Event | None = None, scan_limit: int = 200, on_message=None,
                 browser_executable_path: str = "", start_timeout_seconds: int = 180,
                 start_grace_seconds: int = 30, transfer_timeout_seconds: int = 3600,
                 archive_test_executable_path: str = "", archive_test_timeout_seconds: int = 600,
                 on_event: Callable[[dict], None] | None = None):
        self.on_message = on_message
        self.on_event = on_event
        self._scanned_snapshots: dict[int, MessageSnapshot] = {}
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
    def _parse_archive_cards(raw_cards) -> tuple[ArchiveCard, ...]:
        cards = []
        for raw in raw_cards or []:
            if not isinstance(raw, dict):
                continue
            name, source = raw.get("name"), raw.get("source")
            if (isinstance(name, str) and isinstance(source, str)
                    and source in {"document", "name", "link", "custom"}
                    and re.fullmatch(r'[^\\/:*?"<>|\r\n]+\.(?:zip|rar)', name, re.I)):
                cards.append(ArchiveCard(name, source))
        return tuple(cards)

    def _unique_message_nodes(self, nodes, limit: int):
        """Capture one DOM state so a virtual-list update cannot invalidate nth(index)."""
        raw_nodes = nodes.evaluate_all("""(elements, selector) => elements.map(element => ({
            message_id: element.getAttribute('data-message-id'),
            mid: element.getAttribute('data-mid'),
            text: element.innerText,
            cards: (""" + ARCHIVE_CARDS_JS + """)(element, selector)
        }))""", self.attachment_selector)
        by_id: dict[int, MessageSnapshot] = {}
        order: list[int] = []
        for raw_node in raw_nodes:
            for attribute, key in (("data-message-id", "message_id"), ("data-mid", "mid")):
                value = raw_node.get(key)
                match = re.search(r"(\d+)$", value) if isinstance(value, str) else None
                if match:
                    message_id = int(match.group(1))
                    if message_id not in by_id:
                        by_id[message_id] = MessageSnapshot(
                            message_id, attribute, value, raw_node.get("text") or "",
                            self._parse_archive_cards(raw_node.get("cards")))
                        order.append(message_id)
                    break
        return [by_id[message_id] for message_id in order[-limit:]]

    def _message_locator(self, page, snapshot: MessageSnapshot):
        selector = f'[{snapshot.attribute}={json.dumps(snapshot.value)}]'
        return page.locator(self.message_selector).and_(page.locator(selector)).first

    def _archive_cards(self, message) -> tuple[ArchiveCard, ...]:
        return self._parse_archive_cards(message.evaluate(ARCHIVE_CARDS_JS, self.attachment_selector))

    def _card_locator(self, message, card: ArchiveCard):
        exact_name = re.compile(rf"^\s*{re.escape(card.name)}\s*$")
        if card.source == "document":
            return message.locator(DOCUMENT_CARD_SELECTOR).filter(
                has_text=re.compile(rf"^\s*{re.escape(card.name)}\s*$", re.M))
        if card.source == "name":
            return message.locator(ARCHIVE_NAME_SELECTOR).filter(has_text=exact_name)
        if card.source == "link":
            return message.locator(f'a[download={json.dumps(card.name)}]')
        return message.locator(self.attachment_selector).filter(
            has_text=re.compile(rf"^\s*{re.escape(card.name)}\s*$", re.M))

    @staticmethod
    def _card_still_matches(button, card: ArchiveCard) -> bool:
        return button.evaluate("""(element, card) => {
            const selector = '[class*="document-name"], [class*="file-name"], [class*="document-title"]';
            if (card.source === 'link') return element.getAttribute('download') === card.name;
            if (card.source === 'name') return element.textContent.trim() === card.name;
            const names = Array.from(element.querySelectorAll(selector), el => el.textContent.trim());
            if (card.source === 'document') return names.length === 1 && names[0] === card.name;
            const lines = (element.innerText || '').split(/\\r?\\n/).map(line => line.trim());
            return names.includes(card.name) || lines.includes(card.name);
        }""", {"name": card.name, "source": card.source})

    @staticmethod
    def _missing_messages_reason(body: str) -> str:
        if "Waiting for network" in body or "等待网络" in body:
            return ("Telegram Web 正在等待网络，频道消息尚未加载。"
                    "请确认该浏览器能正常访问 Telegram Web，并检查当前网络或代理后重试。")
        return ("Telegram Web 未读取到频道消息：若出现二维码，请确认抓取与首次登录使用"
                "同一浏览器登录态目录；否则请检查频道访问权限或页面结构。")

    def _open_channel_messages(self, page, channel_url: str):
        page.set_default_timeout(self.timeout_ms)
        logging.info("等待 Telegram Web 页面及频道消息，单步最多 %d 秒", self.timeout_ms // 1000)
        page.goto("https://web.telegram.org/k/", wait_until="domcontentloaded", timeout=self.timeout_ms)
        logging.info("Telegram Web 登录检查: URL=%s, 标题=%s", page.url, page.title())
        target_url = self.channel_web_url(channel_url)
        logging.info("打开 Telegram Web 频道聊天: %s", target_url)
        page.goto(target_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        if not page.url.startswith("https://web.telegram.org/"):
            raise RuntimeError(f"未进入 Telegram Web 聊天页面，当前 URL: {page.url}")
        message_nodes = page.locator(self.message_selector)
        try:
            message_nodes.first.wait_for(timeout=self.timeout_ms)
        except Exception as exc:
            try:
                body = page.locator("body").inner_text(timeout=1500)
            except Exception:
                body = ""
            logging.error("频道诊断: URL=%s, 标题=%s, 消息节点数=%d, 等待网络=%s",
                          page.url, page.title(), message_nodes.count(), "Waiting for network" in body)
            raise RuntimeError(self._missing_messages_reason(body)) from exc
        return message_nodes

    def _scan_archive_ids(self, page, nodes, limit: int) -> list[int]:
        """Only count archive-bearing document cards or named download links."""
        self._scanned_snapshots = {}
        found: list[int] = []
        found_ids: set[int] = set()
        seen: set[int] = set()
        deadline = time.monotonic() + min(self.timeout_ms / 1000, 90)
        stagnant = 0
        scroll_failures = 0
        while len(seen) < self.scan_limit and time.monotonic() < deadline and not self.stop_event.is_set():
            unique = self._unique_message_nodes(nodes, self.scan_limit)
            before = len(seen)
            for snapshot in reversed(unique):
                mid = snapshot.message_id
                if mid not in seen and len(seen) >= self.scan_limit:
                    continue
                seen.add(mid)
                if snapshot.cards and mid not in found_ids:
                    found.append(mid)
                    found_ids.add(mid)
                    self._scanned_snapshots[mid] = snapshot
            if len(found) >= limit:
                break
            if not unique:
                page.wait_for_timeout(700)
                continue
            try:
                self._message_locator(page, unique[0]).evaluate(
                    "el => el.scrollIntoView({block: 'start'})", timeout=1000)
                page.mouse.wheel(0, -800)
            except Exception as exc:
                logging.warning("向前加载历史消息失败: %s", str(exc).splitlines()[0])
                scroll_failures += 1
                if scroll_failures >= 3:
                    break
                page.wait_for_timeout(500)
                continue
            scroll_failures = 0
            page.wait_for_timeout(1200)
            stagnant = stagnant + 1 if before == len(seen) else 0
            if stagnant >= 4:
                break
        logging.info("压缩包消息扫描: 已检查=%d, ZIP/RAR消息=%d, 目标=%d, 扫描上限=%d", len(seen), len(found), limit, self.scan_limit)
        return list(reversed(found[:limit]))

    def _locate_message(self, page, nodes, message_id: int):
        for snapshot in self._unique_message_nodes(nodes, self.scan_limit):
            if snapshot.message_id == message_id:
                return self._message_locator(page, snapshot)
        # 虚拟列表滚动时向下找回节点；找不到则明确失败，不猜 Web K 私有深链。
        stagnant = 0
        previous_ids: tuple[int, ...] = ()
        for _ in range(self.scan_limit):
            page.mouse.wheel(0, 800)
            page.wait_for_timeout(350)
            snapshots = self._unique_message_nodes(nodes, self.scan_limit)
            for snapshot in snapshots:
                if snapshot.message_id == message_id:
                    return self._message_locator(page, snapshot)
            visible_ids = tuple(snapshot.message_id for snapshot in snapshots)
            stagnant = stagnant + 1 if visible_ids == previous_ids else 0
            if stagnant >= 4:
                break
            previous_ids = visible_ids
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
            ids = tuple(snapshot.message_id for snapshot in unique)
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
                    self._message_locator(page, unique[0]).evaluate(
                        "el => el.scrollIntoView({block: 'start'})", timeout=1000)
                    page.mouse.wheel(0, -750)
                    scrolls += 1
                except Exception as exc:
                    logging.warning("加载更早消息失败: %s", str(exc).splitlines()[0])
                    break
            page.wait_for_timeout(500)
        return self._unique_message_nodes(nodes, limit)

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
                        error = RuntimeError(f"永久保存复制失败；完整下载已转存待检查: {recovery}; 原因: {exc}")
                        error.local_path = str(recovery.resolve())
                        raise error from exc
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
                    error = ArchiveValidationError(exc.category, f"{exc}；文件已保留: {quarantine}")
                    error.local_path = str(quarantine.resolve())
                    raise error from exc
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

    def _download_card(self, monitor, button, visible_name: str, transfer: _TransferEvents,
                       page, context) -> Path | None:
        download = None
        transfer.start()
        monitor.on_progress = lambda entry: transfer.progress(
            int(entry.get("receivedBytes") or 0), int(entry.get("totalBytes") or 0))
        try:
            download = monitor.start(button, self.start_timeout_seconds,
                                     self.start_grace_seconds, self.stop_event)
            suggested_ext = Path(download.suggested_filename).suffix.lower()
            if suggested_ext and suggested_ext not in {".zip", ".rar"}:
                download.cancel()
                transfer.emit("cancelled", error=f"浏览器下载不是 ZIP/RAR: {download.suggested_filename}")
                return None
            logging.info("下载已开始，浏览器名称=%s，卡片名称=%s", download.suggested_filename, visible_name)
            resources = download_resource_status(self.download_dir)
            logging.info("采用浏览器本地下载路径复制；下载完成前不复制、不关闭浏览器")
            logging.info("下载前资源: %s", resources)
            if isinstance(resources.get("memory_available"), int) and resources["memory_available"] < 1024 * 1024 * 1024:
                logging.warning("物理内存可用不足 1 GiB，大文件可能导致 Chromium 意外退出；建议关闭占内存程序后重试")
            if isinstance(resources.get("disk_free"), int) and resources["disk_free"] < 512 * 1024 * 1024:
                download.cancel()
                raise RuntimeError("下载目录剩余空间不足 512 MiB，请释放空间后重试")
            monitor.wait_complete(download, self.transfer_timeout_seconds, self.stop_event)
            transfer.emit("validating", received=Path(download.path()).stat().st_size)
            target = self._verified_download(
                download, self.download_dir, visible_name, page=page, context=context,
                resource_status=resources, archive_test_executable_path=self.archive_test_executable_path,
                archive_test_timeout_seconds=self.archive_test_timeout_seconds)
            monitor.release(download)
            transfer.emit("completed", local_path=str(target.resolve()),
                          received=target.stat().st_size, size=target.stat().st_size)
            return target
        except (Exception, asyncio.CancelledError, KeyboardInterrupt) as exc:
            logging.error("下载阶段失败: 暂存目录=%s; 下载状态=%s; 当前资源=%s",
                          monitor.staging_dir, monitor.entries, download_resource_status(self.download_dir))
            retained = None
            if download is not None:
                for path in (monitor.staging_dir / download.guid,
                             monitor.staging_dir / f"{download.guid}.crdownload"):
                    if path.is_file():
                        retained = path
                        transfer.data["received"] = path.stat().st_size
                        break
            transfer.fail(exc, retained)
            if isinstance(exc, ArchiveValidationError) and download is not None:
                monitor.release(download)
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                raise
            stage = "压缩包校验失败（已保留文件）" if isinstance(exc, ArchiveValidationError) else "传输/永久保存失败"
            raise RuntimeError(f"消息 {transfer.data['message_id']} 附件 {visible_name} {stage}: {type(exc).__name__}: {exc}") from exc
        finally:
            monitor.on_progress = None

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
            transfers: dict[tuple[int, str], _TransferEvents] = {}
            abort_reason = "本次抓取已结束，附件未传输"
            context.on("close", lambda _: (close_events.append("上下文关闭"), logging.warning("浏览器事件: Telegram 上下文已关闭")))
            try:
                page = context.pages[0] if context.pages else context.new_page()
                monitor = BrowserDownloadMonitor(page, context, staging_dir)
                page.on("close", lambda _: (close_events.append("页面关闭"), logging.warning("浏览器事件: Telegram 页面已关闭")))
                page.on("crash", lambda _: (close_events.append("页面崩溃"), logging.error("浏览器事件: Telegram 页面已崩溃")))
                message_nodes = self._open_channel_messages(page, channel_url)
                logging.info("频道消息已出现，寻找最新 %d 条 ZIP/RAR 消息", limit)
                if self.watch_mode:
                    self._wait_for_messages(page, message_nodes, min(2, limit))
                baseline = max((item.message_id for item in self._unique_message_nodes(
                    message_nodes, self.scan_limit)), default=0)
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
                        snapshots = self._unique_message_nodes(message_nodes, self.scan_limit)
                        self._scanned_snapshots = {item.message_id: item for item in snapshots}
                        ids = [item.message_id for item in snapshots
                               if item.message_id > baseline and item.message_id not in processed and item.cards]
                    else:
                        ids = self._scan_archive_ids(page, message_nodes, limit)
                        if len(ids) < limit:
                            logging.warning("扫描范围内仅找到 %d/%d 条 ZIP/RAR 消息，按实际找到数量处理", len(ids), limit)
                    logging.info("频道诊断: URL=%s, DOM 节点=%d, 本轮 ZIP/RAR 候选消息=%d", page.url, message_nodes.count(), len(ids))
                    for message_id in ids:
                        snapshot = self._scanned_snapshots.get(message_id)
                        if snapshot is not None and message_id not in processed:
                            for card in snapshot.cards:
                                transfer_key = (message_id, card.name)
                                if transfer_key not in transfers or transfers[transfer_key].terminal:
                                    transfer = _TransferEvents(self.on_event, message_id, key, card.name, snapshot.text)
                                    transfers[transfer_key] = transfer
                                    transfer.emit("queued")
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
                            cards = self._archive_cards(message)
                        except Exception as exc:
                            raise RuntimeError(f"读取消息 {message_id} 失败，页面可能刷新或浏览器已关闭: {exc}") from exc
                        logging.info("检查第 %d/%d 条候选消息: message_id=%d, 文件卡片=%s, 内容摘要=%s",
                                     ordinal, len(ids), message_id, [card.name for card in cards],
                                     re.sub(r"\s+", " ", text)[:180])
                        if not cards:
                            logging.warning("消息 %d 的 ZIP/RAR 文件卡片已消失或未渲染，跳过且不计为已处理", message_id)
                            continue
                        paths: list[Path] = []
                        saved_names: set[str] = set()
                        for attachment_index, card in enumerate(cards, 1):
                            transfer_key = (message_id, card.name)
                            if transfer_key not in transfers:
                                transfers[transfer_key] = _TransferEvents(self.on_event, message_id, key, card.name, text)
                                transfers[transfer_key].emit("queued")
                            transfer = transfers[transfer_key]
                            button = self._card_locator(message, card)
                            if (sum(item.name == card.name for item in cards) != 1 or button.count() != 1
                                    or self._archive_cards(message).count(card) != 1
                                    or not self._card_still_matches(button, card)):
                                logging.warning("消息 %d: 文件卡片不唯一或已变化，跳过 %s，不点击下载",
                                                message_id, card.name)
                                transfer.emit("cancelled", error="文件卡片不唯一或已变化，未点击下载")
                                continue
                            visible_name = card.name
                            logging.info("消息 %d: 点击已确认文件卡片 %d/%d（%s），等待下载事件",
                                         message_id, attachment_index, len(cards), visible_name)
                            file_key = visible_name
                            if file_key in saved_names:
                                transfer.emit("cancelled", error="本条消息的同名附件已保存，未重复下载")
                                continue
                            target = self._download_card(monitor, button, visible_name, transfer, page, context)
                            if target is None:
                                continue
                            paths.append(target)
                            saved_names.add(file_key)
                            files_downloaded += 1
                            logging.info("消息 %d: 已保存 %s，大小=%d 字节", message_id, target.resolve(), target.stat().st_size)
                        if not paths:
                            logging.warning("消息 %d 没有可安全点击的 ZIP/RAR 卡片，未计为已处理", message_id)
                            continue
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
                    for transfer in transfers.values():
                        if not transfer.terminal:
                            transfer.emit("cancelled", error="扫描后的附件卡片不可用，未下载")
                    if not self.watch_mode:
                        logging.info("本次实际新增处理 ZIP/RAR 消息=%d，筛选目标=%d", completed, limit)
                        break
                    logging.info("监测中：只处理启动后新出现的 ZIP/RAR 消息；已处理=%d", len(processed))
                logging.info("抓取汇总: 有效附件=%d, 含附件消息=%d", files_downloaded, len(messages))
            except (Exception, asyncio.CancelledError, KeyboardInterrupt) as exc:
                abort_reason = f"本批次已中止，未继续下载: {type(exc).__name__}: {exc}"
                raise
            finally:
                for transfer in transfers.values():
                    if not transfer.terminal:
                        transfer.emit("cancelled", error=abort_reason)
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
