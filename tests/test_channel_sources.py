from __future__ import annotations

import json

import pytest

from xianyu_pipeline.channel_sources import ChannelDirectory, normalize_public_channel


def test_normalize_public_channel_accepts_urls_and_usernames():
    assert normalize_public_channel("https://t.me/Free_Models/") == "free_models"
    assert normalize_public_channel("@Free_Models") == "free_models"
    with pytest.raises(ValueError):
        normalize_public_channel("https://t.me/c/12345")
    with pytest.raises(ValueError):
        normalize_public_channel("https://example.com/channel")


def test_directory_persists_names_and_protects_active_channel(tmp_path):
    directory = ChannelDirectory(tmp_path / "output")
    first = directory.add("https://t.me/first_channel", "第一频道")
    second = directory.add("https://t.me/second_channel", "第二频道")
    assert directory.list(first["url"])["active"] == "first_channel"
    assert directory.rename(first["key"], "主模型频道")["name"] == "主模型频道"
    with pytest.raises(ValueError, match="不能删除"):
        directory.remove(first["key"], first["url"])
    removed = directory.remove(second["key"], first["url"])
    assert removed["key"] == "second_channel"

    reloaded = ChannelDirectory(tmp_path / "output")
    saved = reloaded.list(first["url"])
    assert [row["key"] for row in saved["channels"]] == ["first_channel"]
    assert saved["channels"][0]["name"] == "主模型频道"
    assert json.loads((tmp_path / "output" / "channels.json").read_text(encoding="utf-8"))["channels"]


def test_private_channel_preserves_numeric_target_when_saved_and_reloaded(tmp_path):
    directory = ChannelDirectory(tmp_path)
    row = directory.ensure("-100123456789", "Private")
    assert row["key"] == row["url"] == "-100123456789"
    loaded = ChannelDirectory(tmp_path)
    assert loaded.get("-100123456789")["url"] == "-100123456789"
    assert loaded.list("-100123456789")["active"] == "-100123456789"


def scanned(key="models", identity=123, title="Model collection", **fields):
    return {"key": key, "target": key, "id": identity, "title": title, **fields}


def test_account_scan_merges_presets_and_persists_only_display_metadata(tmp_path):
    directory = ChannelDirectory(tmp_path)
    directory.ensure("models")
    directory.add("manual", "Manual source")
    rows = [
        scanned(avatar_url="/api/channel-avatar/123?v=45", verified=True,
                access_hash=999, telegram_api_hash="secret", unknown="private"),
        scanned("-1000000000456", 456, "Private collection"),
        scanned(),
    ]
    assert directory.merge_joined(rows) == {"added": 1, "updated": 1, "total": 3}
    loaded = ChannelDirectory(tmp_path)
    models = loaded.get("models")
    assert models["name"] == models["title"] == "Model collection"
    assert models["username"] == "models" and models["public"] and models["verified"]
    assert models["avatar_url"] == "/api/channel-avatar/123?v=45"
    private = loaded.get("-1000000000456")
    assert private["url"] == "-1000000000456" and not private["public"]
    assert private["id"] == 456 and private["username"] == ""
    assert loaded.get("manual")["name"] == "Manual source"
    stored = directory.path.read_text(encoding="utf-8")
    assert all(field not in stored for field in ("access_hash", "telegram_api_hash", "unknown"))
    assert directory.merge_joined(rows) == {"added": 0, "updated": 2, "total": 3}
    before = directory.list()
    assert directory.merge_joined([]) == {"added": 0, "updated": 0, "total": 3}
    assert directory.list() == before


def test_scan_refreshes_automatic_names_and_preserves_custom_names(tmp_path):
    directory = ChannelDirectory(tmp_path)
    directory.add("custom", "My label")
    directory.merge_joined([scanned(), scanned("custom", 456, "Remote title")])
    original_created = directory.get("models")["created_at"]
    directory.merge_joined([scanned(title="New title"), scanned("custom", 456, "Changed title")])
    assert directory.get("models")["name"] == "New title"
    assert directory.get("models")["created_at"] == original_created
    assert directory.get("custom")["name"] == "My label"
    assert directory.get("custom")["title"] == "Changed title"
    directory.rename("models", "Renamed")
    reloaded = ChannelDirectory(tmp_path)
    reloaded.merge_joined([scanned(title="Third title")])
    assert reloaded.get("models")["name"] == "Renamed"
    assert reloaded.get("models")["title"] == "Third title"


@pytest.mark.parametrize("avatar", [
    "https://example.com/avatar", "/api/channel-avatar/456",
    "/api/channel-avatar/123?secret=abc", "/api/channel-avatar/../123",
])
def test_scan_rejects_unsafe_avatar_locations(tmp_path, avatar):
    directory = ChannelDirectory(tmp_path)
    directory.merge_joined([scanned(avatar_url=avatar)])
    assert directory.get("models")["avatar_url"] == ""


def test_scan_failure_cannot_partially_replace_saved_directory(tmp_path, monkeypatch):
    directory = ChannelDirectory(tmp_path)
    directory.add("manual")
    before = directory.list()
    stored = directory.path.read_bytes()
    with pytest.raises(ValueError, match="频道标识"):
        directory.merge_joined([scanned(), scanned("invalid", target="different")])
    assert directory.list() == before and directory.path.read_bytes() == stored

    def fail_write(*args, **kwargs):
        raise OSError("Fixture disk full")

    monkeypatch.setattr(type(directory.path), "replace", fail_write)
    with pytest.raises(OSError, match="disk full"):
        directory.merge_joined([scanned()])
    assert directory.list() == before and directory.path.read_bytes() == stored
