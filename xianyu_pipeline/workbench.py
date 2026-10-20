"""Persistent, account-free transfer state for the local workbench."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from .config import PipelineConfig
from .models import utc_now


IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif"}
ACTIVE_DOWNLOADS = {"queued", "downloading", "validating"}


def channel_key(chat: str) -> str:
    value = str(chat).strip().rstrip("/")
    if value.startswith(("https://t.me/", "http://t.me/")):
        value = urlparse(value).path.strip("/").split("/")[0]
    return value.removeprefix("@").lower()


def task_key(chat: str, message_id: int, filename: str) -> str:
    return hashlib.sha256(f"{channel_key(chat)}:{message_id}:{filename}".encode()).hexdigest()[:24]


def image_type(path: Path) -> str | None:
    if path.suffix.lower() not in IMAGE_TYPES or not path.is_file() or path.stat().st_size > 12 * 1024 * 1024:
        return None
    with path.open("rb") as stream:
        head = stream.read(16)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return None


class TransferStore:
    def __init__(self, config: PipelineConfig):
        self.config = config
        config.ensure_directories()
        self.path = config.output_dir / "workbench-tasks.json"
        self.lock = threading.RLock()
        self.tasks: dict[str, dict] = {}
        self.last_flush = 0.0
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    raise ValueError("invalid task list")
                self.tasks = {row["id"]: row for row in data if isinstance(row, dict) and isinstance(row.get("id"), str)}
            except (OSError, ValueError, KeyError):
                logging.exception("工作台任务记录无法读取，原文件已保留")
                backup = self.path.with_name(f"workbench-tasks-unreadable-{time.time_ns()}.json")
                shutil.copy2(self.path, backup)
        for task in self.tasks.values():
            self._naming_defaults(task)
            # Older records bound uploads implicitly to the then-current local file.
            if "upload_filename" not in task and task.get("upload_status") in {"uploading", "link_pending", "completed", "local"}:
                local_path = task.get("local_path", "")
                task["upload_filename"] = Path(local_path).name if local_path else ""
                task.setdefault("upload_local_path", local_path)
            task.setdefault("upload_filename", "")
            task.setdefault("upload_local_path", "")
            task.setdefault("upload_target", "")
            task.setdefault("upload_received", 0)
            task.setdefault("upload_size", 0)
            task.setdefault("upload_speed", 0)
            task.setdefault("upload_eta", None)
            for field in ("upload_remote_fid", "upload_task_id", "upload_md5", "upload_sha1", "upload_phase"):
                task.setdefault(field, "")
            task.setdefault("image_source", "")
            task.setdefault("image_message_ids", [])
            task.setdefault("image_group_id", "")
            task.setdefault("storage_root", str(config.inbox_dir.resolve()))
            if task.get("download_status") in ACTIVE_DOWNLOADS:
                task.update(download_status="interrupted", error="上次运行中断，可重新下载", speed=0, eta=None)
            if task.get("upload_status") == "uploading":
                task.update(upload_status="link_pending", error="上次上传中断，请先核对网盘文件或重新获取链接")
            if task["naming_status"] in {"generating", "applying"}:
                task.update(naming_status="failed", naming_error="上次命名操作中断，可重试")
        self.import_existing()
        self.flush()

    def flush(self) -> None:
        with self.lock:
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(list(self.tasks.values()), ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
            self.last_flush = time.monotonic()

    def use_config(self, config: PipelineConfig) -> None:
        with self.lock:
            config.ensure_directories()
            changed = config.inbox_dir.resolve() != self.config.inbox_dir.resolve()
            self.config = config
            if changed:
                self.import_existing()
                self.flush()

    @staticmethod
    def _naming_defaults(task):
        for field in ("chinese_filename", "naming_source", "naming_note", "naming_error",
                      "naming_quality", "remote_filename", "rename_target", "rename_pending_name"):
            task.setdefault(field, "")
        task.setdefault("naming_status", "idle")
        task.setdefault("naming_untranslated", [])

    def event(self, event: dict) -> str:
        with self.lock:
            filename = str(event.get("filename", ""))
            mid = int(event.get("message_id", 0))
            chat = str(event.get("chat", ""))
            identity = task_key(chat, mid, filename)
            is_new = identity not in self.tasks
            if is_new:
                self.tasks[identity] = {
                    "id": identity, "message_id": mid, "chat": chat, "filename": filename,
                    "title": Path(filename).stem, "source_url": "", "size": 0, "received": 0,
                    "speed": 0, "eta": None, "download_status": "queued", "upload_status": "waiting",
                    "local_path": "", "image_paths": [], "share_url": "", "error": "",
                    "image_source": "", "image_message_ids": [], "image_group_id": "",
                    "storage_root": str(self.config.inbox_dir.resolve()),
                    "upload_provider": "", "upload_filename": "", "upload_local_path": "", "upload_target": "",
                    "upload_received": 0, "upload_size": 0, "upload_speed": 0, "upload_eta": None,
                    "upload_remote_fid": "", "upload_task_id": "", "upload_md5": "", "upload_sha1": "", "upload_phase": "",
                    "created_at": utc_now(), "validation": "pending",
                }
            task = self.tasks[identity]
            self._naming_defaults(task)
            previous_status = task["download_status"]
            allowed = set(task) - {"id", "created_at", "share_url", "storage_root", "chinese_filename",
                                  "naming_source", "naming_note", "naming_error", "naming_status",
                                  "naming_quality", "naming_untranslated",
                                  "remote_filename", "rename_target", "rename_pending_name"} - {
                key for key in task if key.startswith("upload_")}
            task.update({key: value for key, value in event.items() if key in allowed})
            if event.get("download_status") == "queued" or event.get("local_path"):
                task["storage_root"] = str(self.config.inbox_dir.resolve())
            task["updated_at"] = utc_now()
            if event.get("download_status") == "queued":
                task.update(error="", received=0, speed=0, eta=None)
            if event.get("download_status") == "completed":
                task.update(validation="passed", speed=0, eta=None, error="")
            if event.get("download_status") in {"failed", "cancelled", "interrupted"}:
                task.update(speed=0, eta=None)
            if (is_new or task["download_status"] != previous_status or time.monotonic() - self.last_flush >= 1
                    or event.get("download_status") != "downloading"):
                self.flush()
            return identity

    def get(self, identity: str) -> dict:
        with self.lock:
            if identity not in self.tasks:
                raise ValueError("任务不存在，请刷新列表")
            return copy.deepcopy(self.tasks[identity])

    def has_completed_message(self, chat: str, message_id: int) -> bool:
        with self.lock:
            return any(channel_key(row.get("chat", "")) == channel_key(chat)
                       and row.get("message_id") == message_id
                       and row.get("download_status") == "completed"
                       for row in self.tasks.values())

    def update(self, identity: str, **values) -> None:
        with self.lock:
            if identity not in self.tasks:
                raise ValueError("任务不存在")
            self.tasks[identity].update(values, updated_at=utc_now())
            self.flush()

    def finish(self, error: str = "", stopped: bool = False) -> None:
        with self.lock:
            for task in self.tasks.values():
                if task["download_status"] in ACTIVE_DOWNLOADS:
                    task.update(download_status="cancelled" if stopped else "interrupted", speed=0, eta=None,
                                error=error or "任务未完成，可重新下载", updated_at=utc_now())
                if task["upload_status"] == "uploading":
                    task.update(upload_status="link_pending", error=error or "上传结果待确认")
            self.flush()

    def local_file(self, identity: str, *, completed: bool = False) -> Path:
        task = self.get(identity)
        if completed and task["download_status"] != "completed":
            raise ValueError("文件尚未下载完成")
        if not task["local_path"]:
            raise ValueError("任务尚未生成本地文件")
        path = Path(task["local_path"]).resolve()
        root = Path(task["storage_root"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("本地文件不存在或不在下载目录内")
        if completed and any(part.startswith(".") for part in path.relative_to(root).parts):
            raise ValueError("暂存或待检查文件不可上传")
        if completed and path.suffix.lower() not in {".zip", ".rar"}:
            raise ValueError("工作台仅上传 ZIP/RAR 素材包")
        return path

    def upload_name(self, identity: str) -> str:
        name = self.get(identity).get("upload_filename", "")
        if not isinstance(name, str) or not re.fullmatch(r'[^\\/:*?"<>|\x00-\x1f]+\.(?:zip|rar)', name, re.I):
            raise ValueError("原上传文件名缺失或无效，请人工核对网盘文件")
        return name

    def preview(self, identity: str, index: int) -> tuple[Path, str]:
        task = self.get(identity)
        paths = task.get("image_paths", [])
        if index < 0 or index >= len(paths):
            raise ValueError("展示图不存在")
        path = Path(paths[index]).resolve()
        if not path.is_relative_to(Path(task["storage_root"]).resolve()):
            raise ValueError("展示图不在素材目录内")
        mime = image_type(path)
        if not mime:
            raise ValueError("展示图格式不支持或已丢失")
        return path, mime

    def enrich(self, identity: str) -> None:
        """Read bounded raster previews only; never extract archive paths verbatim."""
        task = self.get(identity)
        if task.get("image_paths"):
            return
        source = self.local_file(identity, completed=True)
        if source.suffix.lower() != ".zip":
            return
        previews = []
        folder = Path(task["storage_root"]) / ".previews" / identity
        try:
            with zipfile.ZipFile(source) as archive:
                for member in archive.infolist():
                    extension = Path(member.filename).suffix.lower()
                    if (extension not in IMAGE_TYPES or member.is_dir() or member.flag_bits & 1
                            or member.file_size > 8 * 1024 * 1024
                            or member.file_size > max(1, member.compress_size) * 200):
                        continue
                    folder.mkdir(parents=True, exist_ok=True)
                    target = folder / f"{len(previews)}{extension}"
                    with archive.open(member) as stream:
                        content = stream.read(8 * 1024 * 1024 + 1)
                    if len(content) > 8 * 1024 * 1024:
                        continue
                    target.write_bytes(content)
                    if image_type(target):
                        previews.append(str(target.resolve()))
                    else:
                        target.unlink()
                    if len(previews) == 3:
                        break
            if previews:
                self.update(identity, image_paths=previews, image_source="zip",
                            image_message_ids=[], image_group_id="")
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile):
            logging.warning("无法读取压缩包展示图: %s", source.name, exc_info=True)

    def accept_message(self, message) -> None:
        for path in message.attachment_paths:
            path = Path(path)
            if path.suffix.lower() not in {".zip", ".rar"}:
                continue
            with self.lock:
                existing = next((row["id"] for row in self.tasks.values()
                                 if any(row.get(key) and Path(row[key]).resolve() == path.resolve()
                                        for key in ("local_path", "upload_local_path"))), None)
            if existing:
                identity = existing
            else:
                identity = self.event({"message_id": message.message_id, "chat": message.chat,
                                       "filename": path.name, "title": path.stem,
                                       "local_path": str(path.resolve()), "download_status": "completed",
                                       "received": path.stat().st_size, "size": path.stat().st_size})
                if message.chat == "local-inbox":
                    self.update(identity, validation="pending")
            try:
                self.enrich(identity)
            except (OSError, ValueError):
                logging.warning("素材预览未生成: %s", path.name)

    def import_existing(self) -> None:
        existing_paths = {str(Path(row[key]).resolve()) for row in self.tasks.values()
                          for key in ("local_path", "upload_local_path") if row.get(key)}
        for path in self.config.inbox_dir.iterdir():
            if not path.is_file() or path.suffix.lower() not in {".zip", ".rar"} or str(path.resolve()) in existing_paths:
                continue
            identity = task_key("local-inbox", 0, path.name)
            if identity in self.tasks and self.tasks[identity].get("local_path") != str(path.resolve()):
                logging.warning("新下载目录中的同名本地素材与旧任务冲突，保留旧任务: %s", path.name)
                continue
            identity = self.event({"message_id": 0, "chat": "local-inbox", "filename": path.name,
                                   "local_path": str(path.resolve()), "download_status": "completed",
                                   "size": path.stat().st_size, "received": path.stat().st_size})
            self.update(identity, validation="pending")
            self.enrich(identity)

    def snapshot(self) -> dict:
        with self.lock:
            rows = copy.deepcopy(list(self.tasks.values()))
        for row in rows:
            row.pop("storage_root", None)
            row["images"] = [{"url": f"/api/preview/{row['id']}/{index}", "name": Path(path).name}
                             for index, path in enumerate(row.pop("image_paths", []))]
            # Stale progress must never keep displaying a moving rate.
            if row["download_status"] == "downloading":
                from datetime import datetime, timezone
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(row["updated_at"])).total_seconds()
                if elapsed > 5:
                    row.update(speed=0, eta=None)
        disk = shutil.disk_usage(self.config.inbox_dir)
        if self.config.upload_provider == "quark_browser":
            upload_target = self.config.quark_folder_url
        elif self.config.upload_provider == "quark_cli":
            upload_target = f"夸克 CLI 目录 FID: {self.config.quark_cli_parent_fid}" if self.config.quark_cli_parent_fid else "夸克 CLI 默认目录"
        elif self.config.upload_provider == "quark_api":
            upload_target = f"夸克 API 目录 FID: {self.config.quark_api_parent_fid}"
        else:
            upload_target = "本地模拟"
        return {"tasks": rows, "disk": {"free": disk.free, "total": disk.total},
                "download_dir": str(self.config.inbox_dir.resolve()),
                "upload_target": upload_target}
