"""Download an existing channel attachment with TDLib's persistent file engine."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from .archive_validation import validate_rar
from .browser_download import DownloadCancelled
from .download_diagnostics import DownloadTrace
from . import tdlib_client


def message_link(message, transfer=None) -> str:
    chat = str(transfer.data["chat"] if transfer else "").strip()
    if chat.startswith(("https://t.me/", "http://t.me/")):
        chat = urlparse(chat).path.strip("/")
    chat = chat.removeprefix("@")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", chat):
        return f"https://t.me/{chat}/{message.id}"
    peer = getattr(getattr(message, "peer_id", None), "channel_id", None)
    if type(peer) is int and peer > 0:
        return f"https://t.me/c/{peer}/{message.id}"
    if re.fullmatch(r"-100[0-9]+", chat):
        return f"https://t.me/c/{chat[4:]}/{message.id}"
    raise ValueError("TDLib 下载缺少频道消息链接，请重新读取频道消息")


async def resolve_message(service, url, check_stop):
    parts = urlparse(url).path.strip("/").split("/")
    if parts[0] != "c":
        await service.call_async({"@type": "searchPublicChat", "username": parts[0]})
    deadline = time.monotonic() + 60

    async def lookup():
        check_stop()
        try:
            result = await service.call_async({"@type": "getMessageLinkInfo", "url": url}, timeout=15)
        except tdlib_client.TdLibError as exc:
            if parts[0] != "c" or exc.code not in {400, 404}:
                raise
            return None
        return result.get("message")

    message = await lookup()
    if message:
        return message
    # A new TDLib database may not yet know a joined private channel.
    for archived in (False, True):
        for _ in range(5):
            check_stop()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("TDLib 首次读取账号频道超时，请稍后重试")
            try:
                await service.call_async({
                    "@type": "loadChats",
                    "chat_list": {"@type": "chatListArchive" if archived else "chatListMain"},
                    "limit": 100,
                }, timeout=min(15, remaining))
            except tdlib_client.TdLibError as exc:
                if exc.code == 404:
                    break
                raise
            message = await lookup()
            if message:
                return message
    raise RuntimeError("TDLib 找不到该频道消息，请确认账号已加入频道且原消息仍存在")


async def _settle(task):
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def download_archive(adapter, original_client, message, name, transfer):
    service = None
    file_id = None
    reserved = False
    received = resumed = 0
    cache_path = None
    invalid = False
    staging = adapter.download_dir / ".telegram-tdlib-downloads" / uuid4().hex
    source = staging / name
    trace = DownloadTrace(message=message, filename=name, client=original_client, workers=1,
                          file_concurrency=adapter.download_file_concurrency,
                          crypto_backend="TDLib", proxy=adapter.proxy)
    trace.metadata["engine"] = "tdlib"
    trace.metadata["workers_managed_by_engine"] = True
    started = last_progress = time.monotonic()

    def check_stop():
        if adapter.stop_event and adapter.stop_event.is_set():
            raise DownloadCancelled("已请求停止 TDLib 下载")

    async def account_ready():
        deadline = time.monotonic() + 20
        while True:
            check_stop()
            state = service.status()
            if state["authorized"]:
                return
            if state["error"]:
                raise RuntimeError(state["error"])
            if state["state"] != "initializing":
                raise RuntimeError("TDLib 尚未登录，请在设置中点击“登录 TDLib”完成授权")
            if time.monotonic() >= deadline:
                raise TimeoutError("TDLib 初始化超时，请在设置中检查 TDLib 登录和代理")
            await asyncio.sleep(.2)

    async def stop_native():
        if file_id is not None and reserved:
            await service.call_async({"@type": "cancelDownloadFile", "file_id": file_id,
                                      "only_if_pending": False}, timeout=10)

    try:
        adapter._check_proxy()
        check_stop()
        if transfer is not None:
            transfer.emit("downloading", download_engine="tdlib", download_phase="preparing",
                          download_note="正在连接 TDLib 并读取消息")
        service = tdlib_client.get_client(adapter.api_id, adapter.api_hash, adapter.session_path,
                                         adapter.download_dir, adapter.proxy)
        await account_ready()
        account = await service.call_async({"@type": "getMe"})
        if callable(getattr(original_client, "get_me", None)):
            user = original_client.get_me()
            if hasattr(user, "__await__"):
                user = await user
            if getattr(user, "id", None) != account.get("id"):
                raise RuntimeError("TDLib 与频道列表登录的 Telegram 账号不同，请使用同一账号登录")
        td_message = await resolve_message(service, message_link(message, transfer), check_stop)
        content = td_message.get("content") or {}
        document = content.get("document") or {}
        native_file = document.get("document") or {}
        size = getattr(message.file, "size", None)
        if (content.get("@type") != "messageDocument" or document.get("file_name") != name
                or type(size) is not int or size <= 0 or native_file.get("size") != size
                or type(native_file.get("id")) is not int or native_file["id"] <= 0):
            raise RuntimeError("TDLib 消息附件与频道记录不一致或不可访问，请重新读取频道消息")
        file_id = native_file["id"]
        with service.lock:
            if file_id in service.active_files:
                raise RuntimeError("同一文件已经在 TDLib 下载队列中")
            service.active_files.add(file_id)
            service.files[file_id] = native_file
            reserved = True
        resumed = received = min(size, max(0, int(native_file.get("local", {}).get("downloaded_size", 0))))
        if transfer is not None:
            transfer.data.update(resumable=True, download_engine="tdlib")
            transfer.start(received)
        trace.resumed_bytes = resumed
        trace.begin(False, 0)
        logging.info("TDLib 开始下载: 消息=%d, 文件=%s, 已恢复=%d/%d 字节",
                     message.id, name, received, size)
        native_file = await service.call_async({
            "@type": "downloadFile", "file_id": file_id, "priority": 16,
            "offset": 0, "limit": 0, "synchronous": False,
        })
        last_query = started = last_progress = time.monotonic()
        while True:
            check_stop()
            with service.lock:
                current = dict(service.files.get(file_id, native_file))
            local = current.get("local") or {}
            current_bytes = min(size, max(0, int(local.get("downloaded_size", 0))))
            now = time.monotonic()
            if current_bytes > received:
                received, last_progress = current_bytes, now
                trace.progress(max(0, received - resumed))
            if transfer is not None:
                transfer.progress(received, size)
            if local.get("path"):
                candidate = Path(local["path"]).resolve()
                if not candidate.is_relative_to(service.files_dir.resolve()):
                    raise RuntimeError("TDLib 返回的文件路径超出本机缓存目录")
                cache_path = candidate
            if local.get("is_downloading_completed"):
                if received != size or cache_path is None or not cache_path.is_file():
                    raise RuntimeError("TDLib 文件完成状态与文件大小不一致")
                break
            if not local.get("can_be_downloaded", True):
                raise RuntimeError("TDLib 无法下载该附件，消息可能已删除或账号无访问权限")
            if now - last_progress >= adapter.download_start_timeout_seconds:
                raise TimeoutError(f"TDLib 下载连续 {adapter.download_start_timeout_seconds} 秒无进展，断点已保留")
            if now - started >= adapter.download_transfer_timeout_seconds:
                raise TimeoutError(f"TDLib 下载超过 {adapter.download_transfer_timeout_seconds} 秒总时限，断点已保留")
            trace.heartbeat()
            if now - last_query >= 10:
                await service.call_async({"@type": "getFile", "file_id": file_id})
                last_query = now
            await asyncio.sleep(.3)
        trace.transferred()
        if transfer is not None:
            transfer.emit("validating", received=received)
        trace.validating()

        def copy_and_validate():
            nonlocal invalid
            staging.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cache_path, source)
            try:
                if source.stat().st_size != size:
                    raise RuntimeError("TDLib 下载文件大小与消息不一致")
                if source.suffix.lower() == ".zip":
                    with zipfile.ZipFile(source) as archive:
                        bad = archive.testzip()
                        if bad:
                            raise RuntimeError(f"ZIP 内容校验失败: {bad}")
                else:
                    validate_rar(source, adapter.archive_test_executable_path, adapter.archive_test_timeout_seconds)
            except Exception:
                invalid = True
                raise

        await _settle(asyncio.create_task(asyncio.to_thread(copy_and_validate)))
        check_stop()
        target = adapter.download_dir / name
        index = 2
        while target.exists():
            target = adapter.download_dir / f"{Path(name).stem}-{index}{Path(name).suffix}"
            index += 1
        trace.saving()
        source.replace(target)
        if transfer is not None:
            transfer.emit("completed", local_path=str(target.resolve()), received=size, size=size, resumable=False)
        logging.info("TDLib 附件已校验保存: 消息=%d, 字节=%d, 文件=%s", message.id, size, target)
        trace.finish("completed")
        try:
            await service.call_async({"@type": "deleteFile", "file_id": file_id}, timeout=10)
        except (Exception, asyncio.CancelledError):
            logging.warning("TDLib 已完成缓存暂未清理: 消息=%d", message.id)
        return target
    except (Exception, asyncio.CancelledError, KeyboardInterrupt) as exc:
        if service is not None and reserved:
            try:
                await _settle(asyncio.create_task(stop_native()))
                with service.lock:
                    local = service.files.get(file_id, {}).get("local", {})
                    received = max(received, int(local.get("downloaded_size", 0)))
            except Exception:
                logging.warning("TDLib 暂停请求未确认: 消息=%d，已下载数据保留在 TDLib 缓存", message.id)
        recovery = cache_path if cache_path is not None and cache_path.is_file() else None
        if source.is_file():
            recovery = adapter.download_dir / ".unverified" / uuid4().hex / name
            recovery.parent.mkdir(parents=True, exist_ok=True)
            source.replace(recovery)
        if invalid and service is not None and file_id is not None:
            try:
                await service.call_async({"@type": "deleteFile", "file_id": file_id}, timeout=10)
            except Exception:
                logging.warning("TDLib 校验失败缓存暂未清理: 消息=%d", message.id)
        if transfer is not None:
            transfer.data.update(received=received, resumable=reserved and received > 0 and not invalid,
                                 download_engine="tdlib")
            transfer.fail(exc, recovery)
        cancelled = isinstance(exc, (DownloadCancelled, asyncio.CancelledError, KeyboardInterrupt))
        trace.finish("cancelled" if cancelled else "failed", exc)
        if cancelled:
            raise
        raise RuntimeError(
            f"消息 {message.id} 的 {name} TDLib 下载失败，已接收 {received} 字节；"
            f"{'断点已保留；' if reserved and received and not invalid else ''}"
            f"原因: {str(exc) or type(exc).__name__}") from exc
    finally:
        if reserved:
            with service.lock:
                service.active_files.discard(file_id)
        if staging.is_dir() and not any(staging.iterdir()):
            staging.rmdir()
