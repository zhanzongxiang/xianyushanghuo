"""Persistent saved Telegram channel sources for the local workbench."""
from __future__ import annotations

import copy
import json
import re
import threading
from pathlib import Path
from urllib.parse import urlparse

from .models import utc_now
from .workbench import channel_key


def normalize_public_channel(value: str) -> str:
    """Normalize a public t.me URL or username without contacting Telegram."""
    value = str(value or "").strip()
    if value.startswith(("https://t.me/", "http://t.me/")):
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
    return value.lower()


class ChannelDirectory:
    """Keep channel presets separate from the active pipeline configuration."""

    def __init__(self, output_dir: Path):
        self.path = output_dir / "channels.json"
        self.lock = threading.RLock()
        self.data = {"channels": []}
        self._load()

    @staticmethod
    def _empty() -> dict:
        return {"channels": []}

    @staticmethod
    def _valid_name(value: str) -> str:
        value = str(value or "").strip()
        if len(value) > 80 or any(ord(char) < 32 for char in value):
            raise ValueError("频道名称不能超过 80 个字符或包含控制字符")
        return value

    @staticmethod
    def _record(key: str, url: str, name: str = "", *,
                created_at: str | None = None, updated_at: str | None = None,
                metadata: dict | None = None) -> dict:
        now = utc_now()
        metadata = metadata or {}
        identity = metadata.get("id", 0)
        identity = identity if type(identity) is int and identity > 0 else 0
        username = key if not key.startswith("-") else ""
        avatar = metadata.get("avatar_url", "")
        if not isinstance(avatar, str) or not re.fullmatch(
                rf"/api/channel-avatar/{identity}(?:\?v=[0-9]+)?", avatar):
            avatar = ""
        return {
            "key": key,
            "url": url,
            "name": name or key,
            "id": identity,
            "title": str(metadata.get("title") or name or key),
            "username": username,
            "public": bool(username),
            "avatar_url": avatar,
            "verified": metadata.get("verified") is True,
            "scam": metadata.get("scam") is True,
            "fake": metadata.get("fake") is True,
            "name_custom": metadata.get("name_custom", bool(name and name != key)) is True,
            "created_at": created_at or now,
            "updated_at": updated_at or now,
        }

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            rows = stored.get("channels") if isinstance(stored, dict) else None
            if not isinstance(rows, list):
                raise ValueError("频道列表格式无效")
            valid = []
            seen = set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = row.get("key")
                url = row.get("url")
                name = row.get("name")
                if (not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_+\-]+", key)
                        or not isinstance(url, str) or not isinstance(name, str)
                        or key in seen):
                    continue
                seen.add(key)
                valid.append(self._record(
                    key, url, self._valid_name(name),
                    created_at=row.get("created_at") if isinstance(row.get("created_at"), str) else None,
                    updated_at=row.get("updated_at") if isinstance(row.get("updated_at"), str) else None,
                    metadata=row,
                ))
            self.data = {"channels": valid}
        except (OSError, ValueError, TypeError):
            # Keep the unreadable file for diagnosis; a clean in-memory list is safer.
            self.data = self._empty()

    def _flush(self, data: dict | None = None) -> None:
        data = self.data if data is None else data
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        self.data = data

    def merge_joined(self, rows: list[dict]) -> dict:
        """Commit one completed scan; preserve presets, custom labels and absent channels."""
        with self.lock:
            merged = copy.deepcopy(self.data["channels"])
            by_key = {row["key"]: row for row in merged}
            added, updated = 0, 0
            seen = set()
            for item in rows:
                key = channel_key(item.get("target", ""))
                if (not key or not re.fullmatch(r"[a-z0-9_+\-]+", key)
                        or key != item.get("key")):
                    raise ValueError("获取的频道标识无效，原列表已保留")
                if key in seen:
                    continue
                seen.add(key)
                previous = by_key.get(key)
                title = re.sub(r"[\x00-\x1f]", "", str(item.get("title") or key))[:256]
                custom = bool(previous and previous.get("name_custom"))
                name = previous["name"] if custom else title[:80]
                row = self._record(
                    key, self.canonical_url(key), name,
                    created_at=previous["created_at"] if previous else None,
                    metadata={**item, "title": title, "name_custom": custom},
                )
                if previous:
                    previous.update(row)
                    updated += 1
                else:
                    merged.append(row)
                    by_key[key] = row
                    added += 1
            self._flush({"channels": merged})
            return {"added": added, "updated": updated, "total": len(merged)}

    @staticmethod
    def canonical_url(key: str) -> str:
        return key if re.fullmatch(r"-[0-9]+", key) else f"https://t.me/{key}"

    def ensure(self, url: str, name: str = "") -> dict | None:
        """Add a configured channel once, without changing the active channel."""
        value = str(url or "").strip()
        if not value:
            return None
        key = channel_key(value)
        with self.lock:
            for row in self.data["channels"]:
                if row["key"] == key:
                    changed = False
                    if row["url"] != self.canonical_url(key):
                        row["url"] = self.canonical_url(key)
                        changed = True
                    if name and row["name"] == key:
                        row["name"] = self._valid_name(name)
                        row["name_custom"] = True
                        changed = True
                    if changed:
                        row["updated_at"] = utc_now()
                        self._flush()
                    return copy.deepcopy(row)
            row = self._record(key, self.canonical_url(key), self._valid_name(name))
            self.data["channels"].append(row)
            self._flush()
            return copy.deepcopy(row)

    def list(self, active_url: str = "") -> dict:
        active = channel_key(active_url) if active_url else ""
        with self.lock:
            rows = [copy.deepcopy(row) for row in self.data["channels"]]
        rows.sort(key=lambda row: (row["key"] != active, row["name"].lower(), row["key"]))
        return {"active": active, "channels": rows}

    def get(self, key: str) -> dict:
        value = channel_key(key)
        with self.lock:
            row = next((row for row in self.data["channels"] if row["key"] == value), None)
            if row is None:
                raise ValueError("频道不在已保存列表中")
            return copy.deepcopy(row)

    def add(self, url: str, name: str = "") -> dict:
        value = str(url or "").strip()
        key = channel_key(value)
        label = self._valid_name(name)
        with self.lock:
            if any(row["key"] == key for row in self.data["channels"]):
                raise ValueError("该频道已经保存")
            row = self._record(key, self.canonical_url(key), label)
            self.data["channels"].append(row)
            self._flush()
            return copy.deepcopy(row)

    def rename(self, key: str, name: str) -> dict:
        label = self._valid_name(name)
        if not label:
            raise ValueError("频道名称不能为空")
        value = channel_key(key)
        with self.lock:
            row = next((row for row in self.data["channels"] if row["key"] == value), None)
            if row is None:
                raise ValueError("频道不在已保存列表中")
            row["name"] = label
            row["name_custom"] = True
            row["updated_at"] = utc_now()
            self._flush()
            return copy.deepcopy(row)

    def remove(self, key: str, active_url: str = "") -> dict:
        value = channel_key(key)
        active = channel_key(active_url) if active_url else ""
        if value == active:
            raise ValueError("当前频道不能删除，请先切换到其他频道")
        with self.lock:
            index = next((index for index, row in enumerate(self.data["channels"])
                          if row["key"] == value), None)
            if index is None:
                raise ValueError("频道不在已保存列表中")
            row = self.data["channels"].pop(index)
            self._flush()
            return copy.deepcopy(row)
