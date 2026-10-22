"""Telegram peer and forum-topic identities shared by local caches and sync."""
from __future__ import annotations

import re
from urllib.parse import urlparse


def channel_key(chat: str) -> str:
    value = str(chat).strip().rstrip("/")
    if value.startswith(("https://t.me/", "http://t.me/")):
        value = urlparse(value).path.strip("/").split("/")[0]
    return value.removeprefix("@").lower()


def source_scope(chat: str, topic_id: int = 0) -> str:
    if type(topic_id) is not int or not 0 <= topic_id <= 2147483647:
        raise ValueError("话题 ID 必须为有效的非负整数")
    key = channel_key(chat)
    if not re.fullmatch(r"[a-z0-9_+\-]{1,128}", key):
        raise ValueError("来源标识无效")
    return f"{key}~{topic_id}" if topic_id else key


def split_scope(value: str) -> tuple[str, int]:
    key, separator, topic = value.partition("~")
    topic_id = int(topic) if separator and topic.isascii() and topic.isdecimal() else 0
    if source_scope(key, topic_id) != value:
        raise ValueError("话题来源标识无效")
    return key, topic_id


def message_topic_id(message) -> int:
    """General contains unthreaded posts and ordinary replies without forum flags."""
    if type(getattr(message, "action", None)).__name__ == "MessageActionTopicCreate":
        return int(message.id)
    reply = getattr(message, "reply_to", None)
    if getattr(reply, "forum_topic", False):
        return int(getattr(reply, "reply_to_top_id", None)
                   or getattr(reply, "reply_to_msg_id", None) or 1)
    return 1
