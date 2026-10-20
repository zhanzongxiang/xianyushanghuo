"""Cached channel archive metadata, separate from download tasks."""
from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path

from .models import utc_now
from .workbench import channel_key, image_type


class ChannelCatalog:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.path = output_dir / "channel-catalog.json"
        self.catalog_root = output_dir / ".channel-catalog"
        self.preview_root = output_dir / ".channel-previews"
        self.lock = threading.RLock()
        self.data = self._empty("")

    @staticmethod
    def _empty(channel: str) -> dict:
        return {"channel": channel, "messages": [], "oldest_scanned_id": 0,
                "has_more": False, "scanned_at": ""}

    def _current(self, channel: str) -> dict:
        key = channel_key(channel)
        path = self._path(key)
        stored = self._read(path)
        if stored is not None:
            return stored
        # v42 and earlier stored the active channel in one shared file. Read it
        # as a migration source, but never let another channel reuse its rows.
        if self.path.exists():
            stored = self._read(self.path)
            if stored is not None and stored["channel"] == key:
                return stored
        return self._empty(key)

    def _path(self, key: str) -> Path:
        return self.catalog_root / f"{key}.json"

    @staticmethod
    def _valid(stored: object) -> dict | None:
        if (not isinstance(stored, dict) or not isinstance(stored.get("channel"), str)
                or not isinstance(stored.get("messages"), list)
                or not all(isinstance(row, dict) and type(row.get("message_id")) is int
                           and row["message_id"] > 0 for row in stored["messages"])
                or type(stored.get("oldest_scanned_id")) is not int
                or type(stored.get("has_more")) is not bool):
            return None
        return stored

    def _read(self, path: Path) -> dict | None:
        try:
            return self._valid(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            if path.exists():
                logging.exception("频道消息缓存无法读取，原文件已保留: %s", path)
            return None

    def _cover(self, row: dict, *, full: bool = False) -> Path | None:
        try:
            value = row.get("full_cover_path") if full else row.get("cover_path")
            if full and not value and row.get("cover_quality") == "full":
                value = row.get("cover_path")
            if not isinstance(value, str) or not value:
                return None
            path = Path(value).resolve()
            if path.is_relative_to(self.preview_root.resolve()) and image_type(path):
                return path
        except (OSError, ValueError):
            pass
        return None

    def covers(self, channel: str) -> dict[int, dict]:
        with self.lock:
            return {row["message_id"]: {key: value for key, value in row.items()
                                       if key.startswith("cover_") or key.startswith("full_")}
                    for row in self._current(channel)["messages"]
                    if isinstance(row, dict) and isinstance(row.get("message_id"), int)
                    and row.get("cover_quality") in {"full", "preview", "document_thumbnail"} and self._cover(row)}

    def snapshot(self, channel: str, *, exclude_forwarded_posts: bool = False) -> dict:
        with self.lock:
            current = self._current(channel)
            rows = copy.deepcopy([row for row in current["messages"]
                                  if not exclude_forwarded_posts or not row.get("forwarded", False)])
            for row in rows:
                cover = self._cover(row)
                full = self._cover(row, full=True)
                row["cover_url"] = (f"/api/channel-preview/{row['message_id']}?v={cover.name}" if cover else "")
                row["full_cover_url"] = (f"/api/channel-full-preview/{row['message_id']}?v={full.name}" if full else "")
                row.pop("cover_path", None)
                row.pop("full_cover_path", None)
            return {**{key: value for key, value in current.items() if key != "messages"},
                    "messages": rows}

    def contains(self, channel: str, message_id: int, *, exclude_forwarded_posts: bool = False) -> bool:
        with self.lock:
            return any(isinstance(row, dict) and row.get("message_id") == message_id
                       and (not exclude_forwarded_posts or not row.get("forwarded", False))
                       for row in self._current(channel)["messages"])

    def preview(self, channel: str, message_id: int, *, exclude_forwarded_posts: bool = False,
                full: bool = False) -> tuple[Path, str]:
        with self.lock:
            row = next((row for row in self._current(channel)["messages"]
                        if isinstance(row, dict) and row.get("message_id") == message_id
                        and (not exclude_forwarded_posts or not row.get("forwarded", False))), None)
            cover = self._cover(row, full=full) if row else None
            if cover is None:
                raise ValueError("频道预览图不存在")
            return cover, image_type(cover)

    def update_cover(self, channel: str, message_id: int, **values) -> None:
        with self.lock:
            current = self._current(channel)
            row = next((row for row in current["messages"] if row["message_id"] == message_id), None)
            if row is None:
                raise ValueError("消息不在当前频道中")
            values = {key: value for key, value in values.items()
                      if key.startswith("cover_") or key.startswith("full_")}
            if values.get("full_cover_path") and not self._cover(row):
                values.update(cover_path=values["full_cover_path"], cover_status="ready",
                              cover_message_id=values.get("full_cover_message_id", 0),
                              cover_quality=values.get("full_quality", "full"))
            self.merge(channel, {"messages": [{**row, **values}],
                                "oldest_scanned_id": current["oldest_scanned_id"],
                                "has_more": current["has_more"]})

    def merge(self, channel: str, page: dict, *, older: bool = False) -> None:
        with self.lock:
            key = channel_key(channel)
            current = self._current(key)
            existing = {row["message_id"]: row for row in current["messages"]
                        if isinstance(row, dict) and isinstance(row.get("message_id"), int)}
            for item in page["messages"]:
                row = dict(item)
                previous = existing.get(row["message_id"], {})
                for field, value in previous.items():
                    if field.startswith("full_") and field not in row:
                        row[field] = value
                if not row.get("cover_path") and previous.get("cover_path"):
                    row["cover_path"] = previous["cover_path"]
                    row["cover_message_id"] = previous.get("cover_message_id", 0)
                    row["cover_quality"] = previous.get("cover_quality", "")
                    if previous.get("cover_status") == "ready":
                        row["cover_status"] = "ready"
                existing[row["message_id"]] = row
            cursor = (page["oldest_scanned_id"] if older or not current["oldest_scanned_id"]
                      else current["oldest_scanned_id"])
            self.data = {"channel": key,
                         "messages": sorted(existing.values(), key=lambda row: row["message_id"], reverse=True)[:300],
                         "oldest_scanned_id": cursor,
                         "has_more": page["has_more"] if older or not current["oldest_scanned_id"] else current["has_more"],
                         "scanned_at": utc_now()}
            self.catalog_root.mkdir(parents=True, exist_ok=True)
            target = self._path(key)
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(target)
