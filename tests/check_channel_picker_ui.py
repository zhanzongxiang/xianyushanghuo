"""Offline browser regression for acquiring and choosing Telegram channels.

Run with the project's Python environment:
    python tests/check_channel_picker_ui.py [--executable APP.exe]

All account APIs are mocked; no login, Telegram request, or file download runs.
Screenshots go to tests/artifacts.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4
import zlib


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "tests" / "artifacts"
BROWSER = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
LONG_TITLE = "设计资料与模型素材 · Architectural Forms Collection Extended Edition 2026"


def avatar_png() -> bytes:
    def chunk(kind, content):
        return (struct.pack(">I", len(content)) + kind + content
                + struct.pack(">I", zlib.crc32(kind + content) & 0xFFFFFFFF))

    pixels = b"".join(b"\0" + bytes((81, 152, 124)) * 48 for _ in range(48))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 48, 48, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


class Fixture:
    def __init__(self, origin: str):
        self.origin = origin
        with urlopen(origin + "/api/config", timeout=5) as response:
            self.config = json.load(response)
        with urlopen(origin + "/api/workbench", timeout=5) as response:
            self.workbench = json.load(response)
        self.workbench.update(running=False, action="", error="", channel_scan={})
        self.joined = {"stage": "idle", "found": 0, "added": 0, "updated": 0, "error": ""}
        self.saved = {"active": "fixture_saved", "channels": [
            {"key": "fixture_saved", "name": "Previously saved channel",
             "username": "fixture_saved", "id": 0, "public": True,
             "url": "https://t.me/fixture_saved"}]}
        self.workbench["tasks"] = [
            {"id": "saved-task", "chat": "fixture_saved", "message_id": 21,
             "filename": "saved-model.zip", "title": "Saved model", "size": 100,
             "received": 100, "download_status": "completed", "upload_status": "waiting",
             "images": [], "validation": "passed"},
            {"id": "historical-task", "chat": "history_only", "message_id": 10,
             "filename": "historical.zip", "title": "Historical model", "size": 100,
             "received": 100, "download_status": "completed", "upload_status": "waiting",
             "images": [], "validation": "passed"},
        ]
        self.catalog = self.empty_catalog("fixture_saved")
        self.posts = []
        self.selection_fails = False
        self.joined_gets = 0
        self.rows = [
            {"key": "fixture_public", "target": "fixture_public", "id": 1001,
             "title": LONG_TITLE, "username": "fixture_public",
             "url": "https://t.me/fixture_public", "public": True,
             "avatar_url": "/api/channel-avatar/1001"},
            {"key": "-1001002", "target": "-1001002", "id": 1002,
             "title": "私有素材频道", "username": "", "url": "", "public": False,
             "avatar_url": "/api/channel-avatar/1002"},
            {"key": "fixture_no_avatar", "target": "fixture_no_avatar", "id": 1003,
             "title": "No avatar <script>window.fixtureUnsafe = true</script>",
             "username": "fixture_no_avatar", "url": "https://t.me/fixture_no_avatar",
             "public": True, "avatar_url": ""},
            {"key": "fixture_broken_avatar", "target": "fixture_broken_avatar", "id": 1004,
             "title": "Broken avatar fallback", "username": "fixture_broken_avatar",
             "url": "https://t.me/fixture_broken_avatar", "public": True,
             "avatar_url": "/api/channel-avatar/1004"},
        ]

    @staticmethod
    def empty_catalog(channel: str):
        return {"channel": channel, "messages": [], "oldest_scanned_id": 0,
                "has_more": False, "scanned_at": ""}

    def complete(self, rows=None):
        rows = self.rows if rows is None else rows
        added, updated = 0, 0
        for row in rows:
            previous = next((item for item in self.saved["channels"] if item["key"] == row["key"]), None)
            name = previous["name"] if previous and previous.get("name_custom") else row["title"]
            merged = {**row, "name": name, "url": row["url"] or row["target"]}
            if previous:
                previous.update(merged)
                updated += 1
            else:
                self.saved["channels"].append(merged)
                added += 1
        self.joined.update(stage="completed", found=len(rows), added=added, updated=updated,
                           error="", scanned_at="2026-10-08T01:00:00+00:00")
        self.workbench.update(running=False, action="", error="")

    def route(self, route):
        path = route.request.url.split(self.origin, 1)[-1].split("?", 1)[0]
        if route.request.method == "GET":
            if path.startswith("/api/channel-avatar/"):
                if path.endswith("/1004"):
                    route.fulfill(status=404, body="fixture avatar absent")
                else:
                    route.fulfill(content_type="image/png", body=avatar_png())
                return
            responses = {"/api/config": self.config, "/api/workbench": self.workbench,
                         "/api/channels": self.saved, "/api/channel-messages": self.catalog,
                         "/api/telegram-channels": self.joined, "/api/logs": {"logs": []}}
            if path == "/api/telegram-channels":
                self.joined_gets += 1
            if path in responses:
                route.fulfill(json=responses[path])
                return
        elif route.request.method == "POST":
            payload = route.request.post_data_json or {}
            self.posts.append((path, payload))
            if path == "/api/telegram-channels-scan":
                self.joined.update(stage="connecting", error="")
                self.workbench.update(running=True, action="telegram-channels-scan", error="",
                                      task_id=f"fixture-account-scan-{len(self.posts)}", stop_requested=False)
                route.fulfill(json={"message": "fixture channels requested"})
                return
            if path == "/api/stop":
                assert payload == {"task_id": self.workbench["task_id"], "action": self.workbench["action"]}
                if self.workbench["action"] == "telegram-channels-scan":
                    self.joined.update(stage="cancelled", error="")
                self.workbench.update(running=False, stop_requested=False, channel_requests_open=False)
                route.fulfill(json={"message": "fixture task stopped"})
                return
            if path == "/api/channel-cover":
                assert not payload.get("prefetch") or self.workbench["running"]
                if not self.workbench["running"]:
                    self.workbench.update(running=True, action="channel-cover", error="",
                                          task_id="fixture-full-picture", stop_requested=False,
                                          channel_requests_open=True)
                route.fulfill(json={"message": "fixture image requested"})
                return
            if path == "/api/channels/select":
                if self.selection_fails:
                    route.fulfill(status=400, json={"error": "fixture selection failed"})
                    return
                channel = next(row for row in self.saved["channels"] if row["key"] == payload["key"])
                self.saved["active"] = channel["key"]
                self.config.update(source_channel_url=channel["url"],
                                   source_chat=channel["url"])
                self.catalog = self.empty_catalog(channel["key"])
                route.fulfill(json={"message": "fixture selected", "channel": channel})
                return
            if path == "/api/channels/rename":
                channel = next(row for row in self.saved["channels"] if row["key"] == payload["key"])
                channel.update(name=payload["name"], name_custom=True)
                route.fulfill(json={"message": "fixture renamed", "channel": channel})
                return
            if path == "/api/channels/remove":
                self.saved["channels"] = [row for row in self.saved["channels"] if row["key"] != payload["key"]]
                route.fulfill(json={"message": "fixture removed"})
                return
            if path == "/api/config":
                if payload.pop("data_server_token", ""):
                    payload["data_server_token_configured"] = True
                if payload.pop("data_server_db_password", ""):
                    payload["data_server_db_password_configured"] = True
                self.config.update(payload)
                if self.config.get("data_server_enabled"):
                    self.workbench["data_sync"] = {"enabled": True, "stage": "connected", "pending": 0,
                                                   "conflicts": 0, "error": "", "last_sync": "2026-10-20T01:00:00Z"}
                channel = next(row for row in self.saved["channels"]
                               if row["url"] == self.config["source_channel_url"])
                self.saved["active"] = channel["key"]
                self.catalog = self.empty_catalog(channel["key"])
                route.fulfill(json={"message": "fixture saved"})
                return
            if path == "/api/data-check":
                route.fulfill(json={"message": "数据库连接成功"})
                return
            if path == "/api/data-sync":
                if payload.get("resolve"):
                    self.workbench["data_sync"].update(conflicts=0, pending=0, stage="connected", error="")
                route.fulfill(json={"message": "fixture sync requested"})
                return
            if path == "/api/channel-scan":
                channel = next(row for row in self.saved["channels"] if row["key"] == self.saved["active"])
                self.catalog.update(
                    channel=channel["key"], scanned_at="2026-10-08T01:05:00+00:00",
                    messages=[{"message_id": 42, "chat": channel["key"],
                               "filename": "fixture-model.zip", "size": 1240000,
                               "text": "Offline main channel post",
                               "date": "2026-10-08T01:00:00+00:00", "source_url": "",
                               "cover_url": "", "cover_status": "no_cover"}])
                self.workbench.update(running=False, action="channel-scan", error="",
                                      channel_scan={"stage": "completed", "found": 1,
                                                    "processed": 1, "total": 1, "no_cover": 1,
                                                    "elapsed_seconds": 1})
                route.fulfill(json={"message": "fixture messages requested"})
                return
        raise AssertionError(f"Unexpected account or action request: {route.request.method} {path}")


def check_task_scoping(page, fixture: Fixture, label: str, unique: str) -> None:
    fixture.catalog["messages"][0].update(cover_url="/api/channel-preview/42", full_cover_url="",
                                          full_status="", cover_status="ready")
    page.route("**/api/channel-preview/**",
               lambda route: route.fulfill(content_type="image/png", body=avatar_png()))
    refresh(page, fixture)
    page.locator(".channel-message-cover img").wait_for(state="visible")
    before = len(fixture.posts)
    page.wait_for_timeout(2300)
    assert not any(path == "/api/channel-cover" for path, _ in fixture.posts[before:]), (
        "Viewing cached messages started an independent image task")
    page.locator("button.channel-message-cover").click()
    page.wait_for_function("document.getElementById('stop').hidden === false")
    page.keyboard.press("Escape")
    assert fixture.workbench["action"] == "channel-cover"

    page.locator(".nav-item[data-view='channels']").click()
    assert page.locator("#stop").is_hidden()
    assert "后台正在加载高清图" in page.locator("#connection").get_attribute("aria-label")
    assert page.locator("#add-channel").is_enabled()
    assert page.locator("#scan-joined-channels").is_disabled()
    assert "加载高清图" in page.locator("#scan-joined-channels").get_attribute("title")
    editor = page.locator("[data-channel-management='edit'][data-channel-key='fixture_saved']")
    assert editor.is_enabled()
    editor.click()
    page.locator("#channel-editor-name").fill("后台任务期间编辑频道")
    page.locator("#channel-editor-form button[type='submit']").click()
    page.locator("#channel-editor-dialog").wait_for(state="hidden")
    assert fixture.workbench["running"] and fixture.workbench["task_id"] == "fixture-full-picture"
    assert not any(path == "/api/stop" for path, _ in fixture.posts[before:])
    assert_no_overflow(page, f"{label}: background image status overflow")
    screenshot = ARTIFACTS / f"channel-background-task-{unique}-{label}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    print(screenshot.resolve())

    page.locator(".nav-item[data-view='messages']").click()
    assert page.locator("#stop").is_visible()
    assert "停止加载高清图" in page.locator("#stop").inner_text()
    page.locator("#stop").click()
    page.locator("#stop").wait_for(state="hidden")
    assert fixture.posts[-1] == ("/api/stop", {"task_id": "fixture-full-picture", "action": "channel-cover"})

    page.locator(".nav-item[data-view='channels']").click()
    page.locator("#scan-joined-channels").click()
    page.locator("#stop").wait_for(state="visible")
    assert "停止获取账号频道" in page.locator("#stop").inner_text()
    assert page.locator("#add-channel").is_disabled()
    page.locator(".nav-item[data-view='messages']").click()
    assert page.locator("#stop").is_hidden(), "Messages page can stop an unrelated account scan"
    page.locator(".nav-item[data-view='channels']").click()
    page.locator("#stop").click()
    page.locator("#stop").wait_for(state="hidden")
    assert fixture.joined["stage"] == "cancelled"

    page.locator(".nav-item[data-view='messages']").click()
    fixture.workbench.update(running=True, action="channel-scan", task_id="fixture-reused-scan",
                             channel_requests_open=True, stop_requested=False)
    refresh(page, fixture)
    before = len(fixture.posts)
    deadline = time.monotonic() + 6
    while not any(path == "/api/channel-cover" for path, _ in fixture.posts[before:]):
        assert time.monotonic() < deadline, "Active scan did not prefetch a preview"
        page.wait_for_timeout(50)
    assert fixture.posts[-1] == ("/api/channel-cover", {"id": 42, "prefetch": True})
    assert fixture.workbench["task_id"] == "fixture-reused-scan"
    page.locator("#stop").click()
    page.locator("#stop").wait_for(state="hidden")


def refresh(page, fixture: Fixture) -> None:
    previous = fixture.joined_gets
    page.locator("#refresh").click()
    page.wait_for_function("document.getElementById('refresh').disabled === false")
    deadline = time.monotonic() + 5
    while fixture.joined_gets <= previous:
        assert time.monotonic() < deadline, "Refresh did not request joined channels"
        page.wait_for_timeout(20)
    page.wait_for_timeout(50)


def assert_no_overflow(page, label: str) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), label


def check_view(page, fixture: Fixture, origin: str, label: str, unique: str) -> None:
    errors = []
    page.on("pageerror", lambda error: (errors.append(str(error)), print(f"Browser error: {error}")))
    page.route("**/api/**", fixture.route)
    page.goto(origin + "/?view=messages")
    page.wait_for_function("!document.getElementById('scan-channel').disabled")
    assert page.locator(".heading-actions #scan-joined-channels").count() == 0
    assert page.locator("#channels-section #scan-joined-channels").count() == 1
    assert page.locator("#scan-joined-channels").is_hidden()
    assert page.locator("#channel-select").is_enabled(), "Persisted channel requires a new account scan"
    assert "Previously saved channel" in page.locator("#channel-select").inner_text()
    assert page.locator("#channel-picker-panel").is_hidden()
    assert_no_overflow(page, f"{label}: initial overflow")

    page.locator("button[data-view='channels']").click()
    page.locator("#channels-section").wait_for(state="visible")
    assert page.locator("#saved-channel-count").inner_text() == "1"
    page.locator("#scan-joined-channels").click()
    page.wait_for_function("document.getElementById('scan-joined-channels').disabled")
    assert fixture.posts == [("/api/telegram-channels-scan", {})]
    assert page.locator("#saved-channel-list .channel-row").count() == 1
    fixture.complete()
    refresh(page, fixture)
    page.wait_for_function("!document.getElementById('scan-joined-channels').disabled")
    assert fixture.saved["active"] == "fixture_saved", "Acquiring channels silently selected another one"
    assert page.locator("#saved-channel-list .channel-row").count() == 5
    assert page.locator("#saved-channel-count").inner_text() == "5"
    assert str(len(fixture.rows)) in page.locator("#joined-channel-status").inner_text()
    assert page.locator("#channel-list").count() == 0, "Historical tasks created a separate directory"
    assert_no_overflow(page, f"{label}: source directory overflow")
    screenshot = ARTIFACTS / f"channel-sources-{unique}-{label}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    print(screenshot.resolve())
    page.locator(".nav-item[data-view='messages']").click()

    picker = page.locator("#channel-select")
    assert picker.get_attribute("aria-haspopup") == "listbox"
    picker.click()
    assert picker.get_attribute("aria-expanded") == "true"
    page.locator("#channel-picker-search").wait_for(state="visible")
    options = page.locator("#channel-options [data-channel-option]")
    assert options.count() == len(fixture.saved["channels"])
    for key in ("fixture_public", "-1001002"):
        option = page.locator(f"#channel-options [data-channel-key='{key}']")
        assert option.get_attribute("role") == "option"
        image = option.locator(".channel-avatar img")
        image.wait_for(state="visible")
        page.wait_for_function(
            "(key) => document.querySelector(`#channel-options [data-channel-key='${key}'] img`).naturalWidth > 0",
            arg=key)
    private_option = page.locator("#channel-options [data-channel-key='-1001002']")
    assert "私有" in private_option.inner_text()
    missing_avatar = page.locator("#channel-options [data-channel-key='fixture_no_avatar'] .channel-avatar")
    assert missing_avatar.is_visible()
    assert missing_avatar.locator("img").count() == 0
    page.wait_for_function("""() => {
        const avatar = document.querySelector(
            "#channel-options [data-channel-key='fixture_broken_avatar'] .channel-avatar");
        return avatar && !avatar.querySelector("img");
    }""")
    assert page.evaluate("typeof window.fixtureUnsafe === 'undefined'"), "Channel title was interpreted as markup"
    assert_no_overflow(page, f"{label}: dropdown overflow")
    screenshot = ARTIFACTS / f"channel-picker-{unique}-{label}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    print(screenshot.resolve())

    page.locator("#channel-picker-search").fill(LONG_TITLE)
    assert options.count() == 1, "Search must match the complete channel title"
    assert options.first.get_attribute("data-channel-key") == "fixture_public"
    page.evaluate("""() => {
        window.fixtureOption = document.querySelector("#channel-options [data-channel-option]");
        window.fixtureSearch = document.getElementById("channel-picker-search");
        window.fixtureSearch.focus();
    }""")
    previous_gets = fixture.joined_gets
    page.wait_for_timeout(2200)
    assert fixture.joined_gets > previous_gets, "Polling did not refresh channel state"
    assert page.evaluate("""() =>
        window.fixtureOption === document.querySelector("#channel-options [data-channel-option]")
        && window.fixtureSearch === document.activeElement
        && window.fixtureSearch.value.length > 10
    """), "Polling replaced picker rows or lost search focus"
    page.keyboard.press("Escape")
    assert page.locator("#channel-picker-panel").is_hidden()
    assert picker.get_attribute("aria-expanded") == "false"
    assert picker.evaluate("(button) => button === document.activeElement")
    picker.click()
    page.locator("#page-title").click()
    assert page.locator("#channel-picker-panel").is_hidden(), "Click outside did not dismiss picker"

    picker.click()
    page.locator("#channel-picker-search").fill("私有素材频道")
    assert options.count() == 1
    fixture.selection_fails = True
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    page.wait_for_function("document.getElementById('errors').textContent.includes('fixture selection failed')")
    assert page.locator("#scan-channel").is_enabled()
    assert fixture.saved["active"] == "fixture_saved", "Failed switch lost the active saved source"
    fixture.selection_fails = False
    if page.locator("#channel-picker-panel").is_hidden():
        picker.click()
    page.locator("#channel-picker-search").fill("私有素材频道")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    page.wait_for_function("!document.getElementById('scan-channel').disabled")
    page.wait_for_function("document.getElementById('channel-select').textContent.includes('私有素材频道')")
    assert fixture.saved["active"] == "-1001002"
    assert "私有素材频道" in picker.inner_text()
    selected_avatar = page.locator("#channel-selected-avatar img")
    selected_avatar.wait_for(state="visible")
    page.wait_for_function("document.querySelector('#channel-selected-avatar img').naturalWidth > 0")
    assert page.locator("#channel-picker-panel").is_hidden()
    assert not any(path in {"/api/channel-download", "/api/channel-cover", "/api/channel-scan"}
                   for path, _ in fixture.posts), "Acquisition or selection started media actions"

    page.locator("#scan-channel").click()
    page.locator(".channel-message").wait_for(state="visible")
    assert page.locator(".channel-message").count() == 1
    assert [path for path, _ in fixture.posts].count("/api/channel-scan") == 1
    assert not any(path in {"/api/channel-download", "/api/channel-cover"}
                   for path, _ in fixture.posts)
    assert_no_overflow(page, f"{label}: selected message overflow")
    selected_screenshot = ARTIFACTS / f"channel-picker-selected-{unique}-{label}.png"
    page.screenshot(path=str(selected_screenshot), full_page=True)
    print(selected_screenshot.resolve())

    page.locator("button[data-view='channels']").click()
    page.locator("#scan-joined-channels").click()
    page.wait_for_function("document.getElementById('scan-joined-channels').disabled")
    assert fixture.saved["active"] == "-1001002"
    assert len(fixture.catalog["messages"]) == 1
    fixture.complete([])
    refresh(page, fixture)
    page.wait_for_function("!document.getElementById('scan-joined-channels').disabled")
    assert page.locator("#saved-channel-list .channel-row").count() == 5
    assert page.locator("#scan-channel").is_enabled()
    assert page.locator("#channel-select").is_enabled()
    assert "0" in page.locator("#joined-channel-status").inner_text()

    for stage in ("failed", "cancelled"):
        fixture.joined.update(stage=stage, error="fixture proxy unavailable")
        refresh(page, fixture)
        assert page.locator("#saved-channel-list .channel-row").count() == 5
        assert page.locator("#scan-channel").is_enabled()
        assert page.locator("#channel-select").is_enabled()
        assert page.locator("#scan-joined-channels").is_enabled()
        if stage == "failed":
            assert "fixture proxy unavailable" in page.locator("#joined-channel-status").inner_text()
    assert_no_overflow(page, f"{label}: failure overflow")

    # Rename the canonical record, then verify every consumer uses that label.
    page.locator("[data-channel-management='edit'][data-channel-key='fixture_saved']").click()
    page.locator("#channel-editor-name").fill("自定义模型频道")
    page.locator("#channel-editor-form button[type='submit']").click()
    page.locator("#channel-editor-dialog").wait_for(state="hidden")
    fixture.complete()
    refresh(page, fixture)
    assert page.locator("#saved-channel-list .channel-row").count() == 5, "Rescan duplicated rows"
    page.locator(".nav-item[data-view='settings']").click()
    source = page.locator("#source_channel_url")
    assert source.locator("option").count() == 6
    assert source.locator("option[value='https://t.me/fixture_saved']").inner_text() == "自定义模型频道"
    assert source.input_value() == "-1001002"
    source.select_option("https://t.me/fixture_saved")
    refresh(page, fixture)
    assert source.input_value() == "https://t.me/fixture_saved", "Polling discarded the settings draft"
    page.locator("#save-config").click()
    page.wait_for_function("document.getElementById('config-dirty').textContent === ''")
    assert fixture.saved["active"] == "fixture_saved"

    page.locator(".nav-item[data-view='download']").click()
    filter_input = page.locator("#channel-filter")
    assert filter_input.locator("option").count() == 6
    assert filter_input.locator("option[value='history_only']").count() == 0
    assert filter_input.locator("option[value='fixture_saved']").inner_text() == "自定义模型频道"
    assert "自定义模型频道" in page.locator("[data-task-id='saved-task'] .task-sub").inner_text()
    filter_input.select_option("fixture_saved")
    assert page.locator("#task-list .task:visible").count() == 1
    page.locator(".nav-item[data-view='upload']").click()
    assert filter_input.input_value() == ""
    page.locator(".nav-item[data-view='download']").click()
    assert filter_input.input_value() == "fixture_saved", "Per-view filter lost the saved selection"

    page.locator(".nav-item[data-view='channels']").click()
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("[data-channel-management='remove'][data-channel-key='fixture_broken_avatar']").click()
    page.wait_for_function("document.getElementById('saved-channel-count').textContent === '4'")
    page.locator(".nav-item[data-view='messages']").click()
    picker.click()
    assert options.count() == 4 and "自定义模型频道" in picker.inner_text()
    assert page.locator("#channel-options [data-channel-key='fixture_broken_avatar']").count() == 0
    page.keyboard.press("Escape")

    # A new page load has only persisted directory data; no scan selection state exists.
    page.reload()
    page.wait_for_function("!document.getElementById('scan-channel').disabled")
    assert "自定义模型频道" in picker.inner_text()
    assert_no_overflow(page, f"{label}: final overflow")

    page.locator(".nav-item[data-view='settings']").click()
    page.locator("#data_server_enabled").check()
    page.locator("#data_server_backend").select_option("http")
    page.locator("#data_server_url").fill("http://47.113.126.29:8780")
    page.locator("#data_server_token").fill("offline-sync-token-" + "a" * 32)
    page.locator("#save-config").click()
    assert page.locator("#data_server_url").get_attribute("aria-invalid") == "true"
    page.locator("#data_server_url").fill("http://127.0.0.1:8780")
    page.locator("#save-config").click()
    page.wait_for_function("document.getElementById('config-dirty').textContent === ''")
    assert page.locator("#data_server_token").input_value() == "********"
    assert "data_server_token" not in fixture.config
    assert page.locator("#data-sync-now").is_enabled()
    fixture.workbench["data_sync"].update(stage="offline", pending=3, error="无法连接数据服务")
    refresh(page, fixture)
    assert "待同步 3 条" in page.locator("#data-sync-status").inner_text()
    assert "无法连接数据服务" in page.locator("#data-sync-status").inner_text()
    page.locator("#data-sync-now").click()
    page.wait_for_function("!document.getElementById('data-sync-now').disabled")
    assert any(path == "/api/data-sync" and payload == {} for path, payload in fixture.posts)
    for choice in ("server", "local"):
        fixture.workbench["data_sync"].update(stage="connected", conflicts=2)
        refresh(page, fixture)
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#data-sync-" + choice).click()
        page.locator("#data-sync-" + choice).wait_for(state="hidden")
        assert any(path == "/api/data-sync" and payload == {"resolve": choice} for path, payload in fixture.posts)
    page.locator("#data_server_backend").select_option("mysql")
    assert page.locator("#data_server_db_port").input_value() == "3306"
    assert page.locator("#data-db-fields").is_visible()
    assert page.locator("#data-http-fields").is_hidden()
    page.locator("#data_server_backend").select_option("postgresql")
    assert page.locator("#data_server_db_port").input_value() == "5432"
    page.locator("#data_server_db_host").fill("47.113.126.29")
    page.locator("#data_server_db_password").fill("offline-database-password")
    page.locator("#data_server_db_tls").select_option("disable")
    page.locator("#save-config").click()
    assert page.locator("#data_server_db_tls").get_attribute("aria-invalid") == "true"
    page.locator("#data_server_db_tls").select_option("require")
    page.locator("#save-config").click()
    page.wait_for_function("document.getElementById('config-dirty').textContent === ''")
    assert page.locator("#data_server_db_password").input_value() == "********"
    assert "data_server_db_password" not in fixture.config
    page.locator("#data-db-check").click()
    page.wait_for_function("document.getElementById('data-db-check-result').textContent === '数据库连接成功'")
    refresh(page, fixture)
    assert page.locator("#data-db-check-result").inner_text() == "数据库连接成功"
    assert_no_overflow(page, f"{label}: data settings overflow")
    screenshot = ARTIFACTS / f"data-sync-settings-{unique}-{label}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    print(screenshot.resolve())
    fixture.workbench["tasks"].append({
        "id": "remote-fixture", "device_id": "a" * 32, "remote_device": True,
        "chat": "fixture_saved", "message_id": 42, "filename": "remote-only.zip", "title": "Remote only",
        "download_status": "completed", "upload_status": "link_pending", "size": 100,
        "received": 100, "images": [], "local_path": "", "validation": "passed",
    })
    refresh(page, fixture)
    page.locator(".nav-item[data-view='download']").click()
    remote = page.locator("[data-task-id='remote-fixture']")
    remote.wait_for(state="visible")
    assert remote.locator(".task-checkbox").is_disabled()
    assert remote.locator(".row-link").is_hidden()
    remote.locator(".task-title").click()
    assert page.locator("#detail-path").inner_text() == "文件位于其他设备"
    assert page.locator("#open-task").is_hidden() and page.locator("#retry-link").is_hidden()
    page.keyboard.press("Escape")
    # A different computer's completed file must not block a new local download.
    fixture.catalog.update(channel="fixture_saved", messages=[{
        "message_id": 42, "chat": "fixture_saved", "filename": "remote-only.zip",
        "size": 100, "cover_url": "", "cover_status": "no_cover", "date": "", "text": "",
    }])
    page.locator(".nav-item[data-view='messages']").click()
    page.locator("[data-message-id='42'] [data-channel-action='download']").wait_for(state="visible")
    assert page.locator("[data-message-id='42'] [data-channel-action='download']").is_enabled()
    check_task_scoping(page, fixture, label, unique)
    assert not errors, errors


def check(executable: Path | None = None, *, check_callback=None) -> None:
    from playwright.sync_api import sync_playwright

    if not BROWSER.is_file():
        raise RuntimeError(f"Browser missing: {BROWSER}")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="channel-picker-", ignore_cleanup_errors=True) as temp:
        base = Path(temp)
        config = base / "config.json"
        config.write_text(json.dumps({
            "source_channel_url": "https://t.me/fixture_saved",
            "telegram_access_mode": "api", "telegram_api_id": 12345,
            "telegram_api_hash": "a" * 32, "upload_provider": "local",
            "inbox_dir": str(base / "inbox"), "work_dir": str(base / "work"),
            "output_dir": str(base / "output"), "telegram_session": str(base / "telegram.session"),
        }), encoding="utf-8")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        code = ("from xianyu_pipeline.web_ui import start_web_ui; import sys; "
                "start_web_ui(sys.argv[1], port=int(sys.argv[2]), open_browser=False)")
        command = ([str(executable.resolve()), "--config", str(config), "--web-ui",
                    "--web-port", str(port), "--no-open-browser"] if executable else
                   [sys.executable, "-c", code, str(config), str(port)])
        server = subprocess.Popen(
            command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        origin = f"http://127.0.0.1:{port}"
        try:
            for _ in range(100):
                try:
                    with urlopen(origin + "/api/config", timeout=0.5):
                        break
                except (URLError, OSError):
                    if server.poll() is not None:
                        raise AssertionError("Channel picker UI server exited")
                    time.sleep(0.1)
            else:
                raise AssertionError("Channel picker UI server did not start")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(BROWSER), headless=True)
                try:
                    unique = uuid4().hex[:8]
                    for label, width, height in (("desktop", 1360, 880), ("mobile", 390, 844),
                                                 ("tiny", 320, 720)):
                        page = browser.new_page(viewport={"width": width, "height": height})
                        page.set_default_timeout(5000)
                        try:
                            (check_callback or check_view)(page, Fixture(origin), origin, label, unique)
                        except Exception:
                            print(page.locator("#errors").inner_text())
                            print(page.evaluate("document.getElementById('config-fields').disabled"))
                            raise
                        finally:
                            page.close()
                finally:
                    browser.close()
        finally:
            server.terminate()
            server.wait(timeout=10)
    print("Offline channel picker checks: PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, help="Check a packaged executable instead of source")
    arguments = parser.parse_args()
    check(arguments.executable)
