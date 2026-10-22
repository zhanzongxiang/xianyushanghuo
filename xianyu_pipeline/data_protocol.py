"""Allowlisted business records shared by the desktop and data service."""
from __future__ import annotations

import json
import math
import re
from urllib.parse import urlsplit
from .telegram_sources import source_scope, split_scope

PROTOCOL_VERSION = 1
MAX_JSON = 8 * 1024 * 1024
MAX_ASSET = 12 * 1024 * 1024
NAME = r"[a-z0-9_+\-]{1,128}"
SCOPE = NAME + r"(?:~[1-9][0-9]{0,9})?"
DIGEST = r"[a-f0-9]{64}"
FIELDS = {
    "channels": set("key url name id title username public verified scam fake name_custom created_at updated_at avatar_digest kind forum topics_scanned_at".split()),
    "topics": set("key channel topic_id title closed hidden top_message_id".split()),
    "messages": set("message_id chat filename size text date source_url forwarded cover_message_id cover_quality full_cover_message_id full_quality cover_digest full_cover_digest topic_id source_kind".split()),
    "cursors": set("channel chat topic_id oldest_scanned_id has_more scanned_at".split()),
    "tasks": set("""id device_id message_id chat topic_id source_kind filename title source_url size received speed eta
        download_status upload_status validation share_url error image_source image_message_ids image_group_id
        upload_provider upload_filename upload_target upload_received upload_size upload_speed upload_eta
        upload_remote_fid upload_task_id upload_md5 upload_sha1 upload_phase
        cloud_delete_status cloud_delete_error cloud_delete_task_id cloud_deleted_at
        record_removed_at
        chinese_filename naming_source naming_note naming_error naming_status naming_quality naming_untranslated
        remote_filename rename_target rename_pending_name created_at updated_at image_digests""".split()),
}


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_exchange(payload: dict):
    if not isinstance(payload, dict):
        raise ValueError("同步请求必须为对象")
    workspace, device = payload.get("workspace"), payload.get("device_id")
    cursor, operations = payload.get("cursor"), payload.get("operations")
    if (not isinstance(workspace, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", workspace)
            or not isinstance(device, str) or not re.fullmatch(r"[a-f0-9]{32}", device)
            or type(cursor) is not int or cursor < 0
            or not isinstance(operations, list) or len(operations) > 50):
        raise ValueError("同步请求格式无效")
    for operation in operations:
        if (not isinstance(operation, dict) or set(operation) != {"id", "kind", "key", "base_revision", "value"}
                or not isinstance(operation["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", operation["id"])
                or type(operation["base_revision"]) is not int or operation["base_revision"] < 0):
            raise ValueError("同步操作格式无效")
        validate_record(operation["kind"], operation["key"], operation["value"], device)
    return workspace, cursor, operations


def validate_endpoint(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("数据服务地址必须为字符串")
    if not value:
        return ""
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("数据服务端口无效") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("请填写数据服务根地址，例如 https://data.example.com")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("远程数据服务必须使用 HTTPS；SSH 隧道可填写 http://127.0.0.1:8780")
    return value.rstrip("/")


def validate_record(kind: str, key: str, value, device_id: str | None = None):
    if kind not in FIELDS or not isinstance(key, str) or len(key) > 256:
        raise ValueError("数据类型或记录标识无效")
    pattern = {"channels": NAME, "topics": NAME + r"~[1-9][0-9]{0,9}",
               "cursors": SCOPE, "messages": SCOPE + r":[1-9][0-9]*",
               "tasks": r"[a-f0-9]{32}:[a-f0-9]{24}"}[kind]
    if not re.fullmatch(pattern, key):
        raise ValueError("记录标识格式无效")
    if kind == "tasks" and device_id is not None and not key.startswith(device_id + ":"):
        raise ValueError("不能修改其他设备的任务")
    if value is None:
        if kind not in {"channels", "topics"}:
            raise ValueError("该类型暂不支持删除")
        return None
    if not isinstance(value, dict) or set(value) - FIELDS[kind]:
        raise ValueError("记录包含未允许的字段")
    if len(encoded(value).encode()) > 128 * 1024:
        raise ValueError("单条记录过大")
    boolean_fields = {"public", "verified", "scam", "fake", "name_custom", "forwarded", "has_more", "forum", "closed", "hidden"}
    integer_fields = {"id", "message_id", "size", "received", "cover_message_id", "full_cover_message_id",
                      "oldest_scanned_id", "upload_received", "upload_size", "topic_id", "top_message_id"}
    list_fields = {"image_digests", "image_message_ids", "naming_untranslated"}
    for field, item in value.items():
        if field in boolean_fields:
            valid = type(item) is bool
        elif field in integer_fields and not (field == "id" and kind == "tasks"):
            valid = type(item) is int and item >= 0
        elif field in {"speed", "eta", "upload_speed", "upload_eta"}:
            valid = item is None or (type(item) in {int, float} and math.isfinite(item) and item >= 0)
        elif field in list_fields:
            valid = isinstance(item, list) and len(item) <= 100
            if valid:
                valid = all(type(entry) is int and entry > 0 for entry in item) if field == "image_message_ids" else all(isinstance(entry, str) for entry in item)
        else:
            valid = isinstance(item, str)
        if not valid:
            raise ValueError("记录字段格式无效")
    if value.get("kind", "channel") not in {"channel", "supergroup", "forum"} or value.get("source_kind", "channel") not in {"channel", "supergroup", "forum"}:
        raise ValueError("来源类型无效")
    if "topic_id" in value and not 0 <= value["topic_id"] <= 2147483647:
        raise ValueError("话题 ID 无效")
    if kind == "channels":
        if value.get("key") != key or not isinstance(value.get("name"), str):
            raise ValueError("频道记录无效")
        if len(value["name"]) > 80 or any(ord(char) < 32 for char in value["name"]):
            raise ValueError("频道名称无效")
        if value.get("url") != (key if key.startswith("-") else f"https://t.me/{key}"):
            raise ValueError("频道地址无效")
    if kind == "messages":
        chat, mid = key.rsplit(":", 1)
        if (source_scope(value.get("chat", ""), value.get("topic_id", 0)) != chat
                or type(value.get("message_id")) is not int or value["message_id"] != int(mid)):
            raise ValueError("消息记录无效")
    if kind == "topics":
        if (not value.get("topic_id") or source_scope(value.get("channel", ""), value["topic_id"]) != key
                or value.get("key") != key or not isinstance(value.get("title"), str)
                or not 1 <= len(value["title"]) <= 256 or any(ord(c) < 32 for c in value["title"])):
            raise ValueError("话题记录无效")
    if kind == "cursors":
        peer, topic_id = split_scope(key)
        if (value.get("channel") != key or type(value.get("oldest_scanned_id")) is not int
                or value["oldest_scanned_id"] < 0 or type(value.get("has_more")) is not bool
                or value.get("topic_id", topic_id) != topic_id or value.get("chat", peer) != peer):
            raise ValueError("频道游标无效")
    if kind == "tasks":
        if (not isinstance(value.get("device_id"), str) or not isinstance(value.get("id"), str)
                or value["device_id"] + ":" + value["id"] != key):
            raise ValueError("任务设备标识无效")
    for field in ("avatar_digest", "cover_digest", "full_cover_digest"):
        if value.get(field) and (not isinstance(value[field], str) or not re.fullmatch(DIGEST, value[field])):
            raise ValueError("图片标识无效")
    if "image_digests" in value:
        digests = value["image_digests"]
        if (not isinstance(digests, list) or len(digests) > 10
                or any(not isinstance(item, str) or not re.fullmatch(DIGEST, item) for item in digests)):
            raise ValueError("任务图片标识无效")
    return value
