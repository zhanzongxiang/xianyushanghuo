import json
from pathlib import Path

import pytest

from xianyu_pipeline.channel_catalog import ChannelCatalog


def row(message_id: int, cover_path: str = "") -> dict:
    return {"message_id": message_id, "chat": "example_channel", "filename": f"{message_id}.rar",
            "size": 100, "text": "caption", "date": "", "source_url": f"https://t.me/example_channel/{message_id}",
            "cover_path": cover_path, "cover_message_id": message_id - 1, "cover_quality": "full",
            "forwarded": False}


def test_catalog_preserves_pages_and_limits_preview_to_its_own_directory(tmp_path):
    catalog = ChannelCatalog(tmp_path / "output")
    preview = catalog.preview_root / "example_channel" / "9.jpg"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"\xff\xd8\xffthumbnail")
    catalog.merge("https://t.me/example_channel", {"messages": [row(10, str(preview))],
                  "oldest_scanned_id": 10, "has_more": True})
    listing = catalog.snapshot("example_channel")
    assert listing["messages"][0]["cover_url"].startswith("/api/channel-preview/10?v=")
    assert "cover_path" not in listing["messages"][0]
    assert catalog.preview("example_channel", 10)[0] == preview
    assert catalog.contains("example_channel", 10)
    assert catalog.covers("example_channel")[10]["cover_quality"] == "full"
    assert catalog.snapshot("different_channel")["messages"] == []

    catalog.merge("example_channel", {"messages": [row(8)], "oldest_scanned_id": 8,
                  "has_more": False}, older=True)
    assert [item["message_id"] for item in catalog.snapshot("example_channel")["messages"]] == [10, 8]
    assert catalog.snapshot("example_channel")["oldest_scanned_id"] == 8
    assert not catalog.snapshot("example_channel")["has_more"]
    assert ChannelCatalog(tmp_path / "output").snapshot("example_channel")["messages"][0]["cover_url"]

    outside = tmp_path / "private.jpg"
    outside.write_bytes(b"\xff\xd8\xffprivate")
    catalog.merge("example_channel", {"messages": [row(7, str(outside))],
                  "oldest_scanned_id": 7, "has_more": False})
    assert not catalog.snapshot("example_channel")["messages"][-1]["cover_url"]
    with pytest.raises(ValueError, match="不存在"):
        catalog.preview("example_channel", 7)


def test_legacy_thumbnail_is_not_reused_as_full_preview(tmp_path):
    catalog = ChannelCatalog(tmp_path / "output")
    preview = catalog.preview_root / "example_channel" / "legacy.jpg"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"\xff\xd8\xffthumbnail")
    legacy = row(10, str(preview))
    legacy.pop("cover_quality")
    catalog.merge("example_channel", {"messages": [legacy], "oldest_scanned_id": 10, "has_more": False})
    assert catalog.covers("example_channel") == {}
    assert catalog.snapshot("example_channel")["messages"][0]["cover_url"].startswith("/api/channel-preview/10?v=")
    catalog.merge("example_channel", {"messages": [row(10)], "oldest_scanned_id": 10, "has_more": False})
    assert catalog.covers("example_channel") == {}


def test_catalog_refresh_does_not_reset_older_page_cursor(tmp_path):
    catalog = ChannelCatalog(tmp_path)
    catalog.merge("example_channel", {"messages": [row(10)], "oldest_scanned_id": 10,
                  "has_more": True})
    catalog.merge("example_channel", {"messages": [row(8)], "oldest_scanned_id": 8,
                  "has_more": True}, older=True)
    catalog.merge("example_channel", {"messages": [row(12)], "oldest_scanned_id": 12,
                  "has_more": False})
    listing = catalog.snapshot("example_channel")
    assert [item["message_id"] for item in listing["messages"]] == [12, 10, 8]
    assert listing["oldest_scanned_id"] == 8 and listing["has_more"]


def test_catalog_can_hide_forwarded_posts_and_preserve_legacy_rows(tmp_path):
    catalog = ChannelCatalog(tmp_path / "output")
    forwarded = row(11)
    forwarded["forwarded"] = True
    legacy = row(10)
    legacy.pop("forwarded")
    catalog.merge("example_channel", {"messages": [forwarded, legacy],
                                      "oldest_scanned_id": 10, "has_more": False})

    filtered = catalog.snapshot("example_channel", exclude_forwarded_posts=True)
    assert [item["message_id"] for item in filtered["messages"]] == [10]
    assert catalog.contains("example_channel", 10, exclude_forwarded_posts=True)
    assert not catalog.contains("example_channel", 11, exclude_forwarded_posts=True)


def test_streamed_metadata_does_not_mutate_source_rows_and_keeps_existing_cover(tmp_path):
    catalog = ChannelCatalog(tmp_path / "output")
    preview = catalog.preview_root / "example_channel" / "cover.jpg"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"\xff\xd8\xfffixture")
    saved = {**row(10, str(preview)), "cover_status": "ready"}
    catalog.merge("example_channel", {"messages": [saved], "oldest_scanned_id": 10, "has_more": True})
    pending = {**row(10), "cover_status": "pending", "cover_quality": ""}
    catalog.merge("example_channel", {"messages": [pending], "oldest_scanned_id": 10, "has_more": True})
    assert pending["cover_path"] == "" and pending["cover_status"] == "pending"
    restored = catalog.snapshot("example_channel")["messages"][0]
    assert restored["cover_url"] and restored["cover_status"] == "ready"


def test_medium_cache_full_upgrade_and_versioned_urls_keep_cursor_and_list_image(tmp_path):
    catalog = ChannelCatalog(tmp_path / "output")
    folder = catalog.preview_root / "example_channel"
    folder.mkdir(parents=True)
    medium, full = folder / "medium.jpg", folder / "full.jpg"
    medium.write_bytes(b"\xff\xd8\xffmedium")
    full.write_bytes(b"\xff\xd8\xfffull")
    catalog.merge("example_channel", {"messages": [{**row(10, str(medium)), "cover_quality": "preview"}],
                                      "oldest_scanned_id": 8, "has_more": True})
    before = catalog.snapshot("example_channel")["messages"][0]
    assert 10 in catalog.covers("example_channel") and not before["full_cover_url"]
    catalog.update_cover("example_channel", 10, full_cover_path=str(full), full_status="ready",
                         full_cover_message_id=9, full_quality="full")
    listing = catalog.snapshot("example_channel")
    upgraded = listing["messages"][0]
    assert upgraded["cover_url"] == before["cover_url"]
    assert upgraded["full_cover_url"].startswith("/api/channel-full-preview/10?v=")
    assert "full_cover_path" not in upgraded
    assert listing["oldest_scanned_id"] == 8 and listing["has_more"]
    assert catalog.preview("example_channel", 10, full=True)[0] == full
    catalog.merge("example_channel", {"messages": [{**row(10), "cover_quality": ""}],
                                      "oldest_scanned_id": 10, "has_more": False})
    assert catalog.snapshot("example_channel")["messages"][0]["full_cover_url"] == upgraded["full_cover_url"]


def test_full_preview_must_remain_inside_catalog_directory(tmp_path):
    catalog = ChannelCatalog(tmp_path / "output")
    outside = tmp_path / "private.jpg"
    outside.write_bytes(b"\xff\xd8\xffprivate")
    catalog.merge("example_channel", {"messages": [row(10)], "oldest_scanned_id": 10, "has_more": False})
    catalog.update_cover("example_channel", 10, full_cover_path=str(outside), full_status="ready")
    assert not catalog.snapshot("example_channel")["messages"][0]["full_cover_url"]
    with pytest.raises(ValueError):
        catalog.preview("example_channel", 10, full=True)


def test_catalog_isolated_per_channel_and_migrates_legacy_active_file(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    legacy = output / "channel-catalog.json"
    legacy.write_text(json.dumps({"channel": "legacy_channel", "messages": [row(7)],
                                  "oldest_scanned_id": 7, "has_more": False,
                                  "scanned_at": ""}), encoding="utf-8")
    catalog = ChannelCatalog(output)
    assert [item["message_id"] for item in catalog.snapshot("legacy_channel")["messages"]] == [7]
    catalog.merge("legacy_channel", {"messages": [row(8)], "oldest_scanned_id": 8, "has_more": False})
    catalog.merge("other_channel", {"messages": [row(9)], "oldest_scanned_id": 9, "has_more": False})
    assert [item["message_id"] for item in catalog.snapshot("legacy_channel")["messages"]] == [8, 7]
    assert [item["message_id"] for item in catalog.snapshot("other_channel")["messages"]] == [9]
    assert (output / ".channel-catalog" / "legacy_channel.json").is_file()
    assert (output / ".channel-catalog" / "other_channel.json").is_file()
