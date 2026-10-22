"""Offline visual check for the channel-message view; no account or network scan."""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "outputs" / "ui-redesign-v2" / "assets"
BROWSER = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


def check_loading(page, address: str, result_dir: Path, unique: str, label: str,
                  original: dict) -> None:
    base = address.split("/?")[0]
    with urlopen(f"{base}/api/workbench", timeout=5) as response:
        workbench = json.load(response)
    catalog = {**original, "messages": [], "oldest_scanned_id": 0, "has_more": False, "scanned_at": ""}
    progress = {"stage": "connecting", "target": 100, "found": 0, "scanned": 0,
                "processed": 0, "total": 0, "ready": 0, "failed": 0, "cached": 0,
                "no_cover": 0, "elapsed_seconds": 0}
    workbench.update(running=False, action="", error="", channel_scan={})
    page.route("**/api/workbench", lambda route: route.fulfill(json=workbench))
    page.route("**/api/channel-messages", lambda route: route.fulfill(json=catalog))
    page.route("**/api/logs", lambda route: route.fulfill(json={"logs": []}))

    def start(route):
        progress.update(stage="connecting", found=0, scanned=0, processed=0,
                        total=0, ready=0, failed=0, elapsed_seconds=0)
        progress.pop("stop_requested", None)
        workbench.update(running=True, action="channel-scan", error="", channel_scan=progress,
                         channel_requests_open=True, task_id="fixture-message-scan", stop_requested=False)
        route.fulfill(json={"message": "fixture scan started"})

    def stop(route):
        assert route.request.post_data_json == {
            "task_id": workbench["task_id"], "action": workbench["action"]}
        progress["stop_requested"] = True
        workbench["stop_requested"] = True
        route.fulfill(json={"message": "fixture stopping"})

    page.route("**/api/channel-scan", start)
    page.route("**/api/stop", stop)
    full_requests, file_requests = [], []

    def full_request(route):
        mid = route.request.post_data_json["id"]
        full_requests.append(mid)
        message = next(row for row in catalog["messages"] if row["message_id"] == mid)
        message.update(full_status="loading", full_error="")
        route.fulfill(json={"message": "fixture full image queued"})

    def file_request(route):
        file_requests.extend(route.request.post_data_json["ids"])
        route.fulfill(json={"message": "fixture files queued"})

    page.route("**/api/channel-cover", full_request)
    page.route("**/api/channel-download", file_request)
    page.reload()
    page.locator("#scan-channel").wait_for(state="visible")
    page.wait_for_function("!document.getElementById('scan-channel').disabled")
    assert page.locator("#channel-loading").is_hidden()
    page.locator("#scan-channel").click()
    page.wait_for_function("document.getElementById('channel-loading').dataset.stage === 'connecting'")
    assert page.locator("#channel-messages-section").get_attribute("aria-busy") == "true"
    assert page.locator("#scan-channel").is_disabled()
    assert page.locator("#channel-loading-icon .loading-spinner").count() == 1
    assert page.locator("#channel-loading-progress").get_attribute("value") is None

    def refresh(stage):
        page.evaluate("document.getElementById('refresh').click()")
        page.wait_for_function(
            "(stage) => document.getElementById('channel-loading').dataset.stage === stage", arg=stage)

    progress.update(stage="scanning", found=2, scanned=30, elapsed_seconds=2)
    catalog.update(messages=[{**row, "cover_url": "", "cover_status": "pending"}
                             for row in original["messages"][:2]],
                   oldest_scanned_id=30217, has_more=True, scanned_at=original["scanned_at"])
    refresh("scanning")
    page.wait_for_function("document.querySelectorAll('.channel-message').length === 2")
    assert "2/100" in page.locator("#channel-loading-title").inner_text()
    assert "30" in page.locator("#channel-loading-detail").inner_text()
    page.locator(".channel-checkbox").first.check()
    assert page.locator("#channel-download-selected").is_enabled()
    page.locator("[data-message-id='30217'] [data-channel-action='download']").click()
    page.wait_for_function("!document.getElementById('channel-download-selected').disabled")
    assert file_requests == [30217], "Files must be submit-able while scanning is active"

    progress.update(stage="covers", found=3, scanned=42, total=3, processed=1, ready=1, elapsed_seconds=4)
    catalog["messages"] = [{**row, "cover_status": "ready" if index == 0 else "pending",
                            "cover_url": row["cover_url"] if index == 0 else "",
                            "cover_quality": "preview", "full_cover_url": ""}
                           for index, row in enumerate(original["messages"])]
    refresh("covers")
    page.locator(".channel-message-cover img").first.wait_for(state="visible")
    page.wait_for_function(
        "document.querySelector('.channel-message-cover img').naturalWidth > 0")
    assert page.locator(".channel-checkbox").first.is_checked()
    assert page.locator("#channel-loading-progress").get_attribute("max") == "3"
    assert page.locator("#channel-loading-progress").get_attribute("value") == "1"
    loading_screenshot = result_dir / f"channel-loading-{unique}-{label}.png"
    page.screenshot(path=str(loading_screenshot), full_page=True)
    page.locator("button.channel-message-cover").first.click()
    page.wait_for_function(
        "document.getElementById('channel-preview-status-text').textContent === '正在加载高清图'")
    assert full_requests == [30218]
    catalog["messages"][0].update(full_status="failed", full_error="fixture full-image timeout")
    refresh("covers")
    page.wait_for_function("!document.getElementById('channel-preview-retry').hidden")
    page.locator("#channel-preview-retry").click()
    page.wait_for_function(
        "document.getElementById('channel-preview-status-text').textContent === '正在加载高清图'")
    assert full_requests == [30218, 30218]
    catalog["messages"][0].update(full_status="ready", full_error="", full_quality="full",
                                  full_cover_url=original["messages"][0]["full_cover_url"])
    refresh("covers")
    page.wait_for_function("document.getElementById('channel-preview-status-text').textContent === '最大尺寸图'")
    page.wait_for_function("document.getElementById('channel-preview-image').naturalWidth > 0")
    assert page.locator("#channel-preview-image").get_attribute("src").startswith(base + "/api/channel-full-preview/")
    full_screenshot = result_dir / f"channel-full-{unique}-{label}.png"
    page.screenshot(path=str(full_screenshot))
    page.keyboard.press("Escape")

    page.evaluate("""() => {
        window.fixtureRow = document.querySelector('.channel-message');
        window.fixtureImage = window.fixtureRow.querySelector('img');
        window.fixtureCheckbox = window.fixtureRow.querySelector('input');
        window.scrollTo(0, 150);
        window.fixtureScroll = window.scrollY;
    }""")
    progress.update(processed=2, ready=2, elapsed_seconds=5)
    catalog["messages"][1].update(cover_url=original["messages"][1]["cover_url"], cover_status="ready")
    refresh("covers")
    page.wait_for_function("document.getElementById('channel-loading-progress').value === 2")
    assert page.evaluate("""() =>
        window.fixtureRow === document.querySelector('.channel-message')
        && window.fixtureImage === window.fixtureRow.querySelector('img')
        && window.fixtureCheckbox === window.fixtureRow.querySelector('input')
        && window.fixtureCheckbox.checked && window.scrollY === window.fixtureScroll
    """), "Polling replaced rows/images or lost selection/scroll"
    page.locator("#channel-search").fill("30217")
    progress.update(elapsed_seconds=6)
    refresh("covers")
    assert page.locator("#channel-search").input_value() == "30217"
    assert page.locator(".channel-message").count() == 1
    page.locator("#channel-search").fill("")
    assert page.locator(".channel-checkbox").first.is_checked()

    progress.update(stage="completed", processed=3, ready=2, failed=1, elapsed_seconds=7)
    catalog["messages"][2]["cover_status"] = "failed"
    workbench.update(running=False, channel_requests_open=False)
    refresh("completed")
    assert "加载完成" in page.locator("#channel-loading-title").inner_text()
    assert "1 张失败" in page.locator("#channel-loading-detail").inner_text()
    assert page.locator("#channel-loading-time").inner_text() == "耗时 7 秒"
    assert page.locator("#channel-loading-progress").is_hidden()
    assert page.locator("#channel-loading-icon .loading-spinner").count() == 0
    assert page.locator("#channel-download-selected").is_enabled()
    assert page.locator("#channel-messages-section").get_attribute("aria-busy") == "false"
    page.evaluate("window.scrollTo(0, 0)")
    completed_screenshot = result_dir / f"channel-completed-{unique}-{label}.png"
    page.screenshot(path=str(completed_screenshot), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    page.locator("#scan-channel").click()
    page.wait_for_function("document.getElementById('channel-loading').dataset.stage === 'connecting'")
    progress.update(stage="covers", found=3, total=3, processed=2, ready=2, elapsed_seconds=3)
    refresh("covers")
    page.locator("#stop").click()
    page.wait_for_function("document.getElementById('channel-loading-title').textContent === '正在停止加载'")
    progress.update(stage="cancelled", elapsed_seconds=4)
    workbench["running"] = False
    refresh("cancelled")
    assert page.locator("#channel-loading-title").inner_text() == "加载已停止"
    assert page.locator(".channel-message").count() == 3
    assert page.locator("#stop").is_hidden()
    assert not page.locator("#errors").inner_text()

    page.locator("#scan-channel").click()
    page.wait_for_function("document.getElementById('channel-loading').dataset.stage === 'connecting'")
    progress.update(stage="failed", elapsed_seconds=2)
    workbench.update(running=False, error="fixture connection lost")
    refresh("failed")
    assert page.locator("#channel-loading-title").inner_text() == "加载失败"
    assert "fixture connection lost" in page.locator("#channel-loading-detail").inner_text()
    assert page.locator(".channel-message").count() == 3
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    print(loading_screenshot.resolve())
    print(completed_screenshot.resolve())
    print(full_screenshot.resolve())


def check_transfers(page, address: str, result_dir: Path, unique: str, label: str,
                    original: dict) -> None:
    base = address.split("/?")[0]
    with urlopen(f"{base}/api/workbench", timeout=5) as response:
        workbench = json.load(response)
    workbench.update(running=False, action="", error="", channel_scan={}, tasks=[])
    catalog = {**original, "messages": [
        {**original["messages"][index % len(original["messages"])], "message_id": 400 + index,
         "filename": f"model-{index}.rar", "series": False}
        for index in range(9)
    ]}

    def task(index, **values):
        return {"id": f"transfer-{index}", "message_id": 400 + index, "chat": "demo_channel",
                "filename": f"model-{index}.rar", "title": f"Model {index}", "size": 100,
                "received": 100, "download_status": "completed", "validation": "passed",
                "upload_status": "waiting", "images": [], **values}

    workbench["tasks"] = [
        task(0),
        task(1, remote_device=True, upload_status="completed", upload_provider="quark_api",
             share_url="https://pan.quark.cn/s/fixture"),
        task(2, upload_status="local", upload_provider="local"),
        task(3, upload_status="link_pending", upload_provider="quark_api",
             upload_remote_fid="unconfirmed", share_url="https://pan.quark.cn/s/unconfirmed"),
        task(4, upload_status="failed", upload_provider="quark_cli"),
        task(5, chat="unrelated_channel", upload_status="completed", upload_provider="quark_api"),
        task(6, id="transfer-6-failed", download_status="failed", upload_status="failed"),
        task(6, id="transfer-6-success", remote_device=True, upload_status="completed",
             upload_provider="quark_browser", remote_filename="translated.rar"),
        task(7, chat="-1000000001001", remote_device=True),
        task(8, download_status="downloading", upload_status="waiting"),
    ]
    submitted = []
    page.unroute("**/api/workbench")
    page.unroute("**/api/channel-messages")
    page.unroute("**/api/channel-download")
    page.route("**/api/workbench", lambda route: route.fulfill(json=workbench))
    page.route("**/api/channel-messages", lambda route: route.fulfill(json=catalog))

    def download(route):
        submitted.append(route.request.post_data_json["ids"])
        route.fulfill(json={"message": "fixture accepted"})

    page.route("**/api/channel-download", download)
    page.reload()
    page.wait_for_function("document.querySelectorAll('.channel-message').length === 9")
    page.wait_for_function(
        "document.querySelector('[data-message-id=\"401\"]').textContent.includes('已转存夸克')")

    def row(index):
        return page.locator(f"[data-message-id='{400 + index}']")

    def badges(index):
        return row(index).locator(".channel-message-badges").inner_text()

    assert "已下载" in badges(0) and "已转存夸克" not in badges(0)
    assert row(0).locator("[data-channel-action='download']").is_disabled()
    assert "已下载" in badges(1) and "其他设备" in badges(1) and "已转存夸克" in badges(1)
    assert row(1).locator(".channel-checkbox").is_disabled()
    assert "下载到本机" in row(1).locator("[data-channel-action='download']").inner_text()
    assert row(1).get_by_role("link", name="查看夸克").get_attribute("href") == "https://pan.quark.cn/s/fixture"
    assert "已转存夸克" not in badges(2)
    assert "转存待确认" in badges(3) and "已转存夸克" not in badges(3)
    assert row(3).get_by_role("link", name="查看夸克").count() == 0
    assert "转存失败" in badges(4) and "已转存夸克" not in badges(4)
    assert badges(5) == "未下载", "A different channel's message ID or filename matched"
    assert "已转存夸克" in badges(6) and "下载失败" in badges(6)
    assert "其他设备" in badges(7), "Numeric channel ID did not match its saved username"
    assert "下载中" in badges(8) and "已下载" not in badges(8), "Full byte count was treated as success"
    assert "已下载 7" in page.locator("#channel-result-count").inner_text()
    assert "已转存 2" in page.locator("#channel-result-count").inner_text()

    status_filter = page.locator("#channel-transfer-filter")
    for status, expected in (("transferred", 2), ("downloaded", 7), ("new", 2), ("all", 9)):
        status_filter.select_option(status)
        assert page.locator(".channel-message").count() == expected
    status_filter.select_option("transferred")
    assert page.locator("#channel-select-all").is_disabled()
    assert not page.locator("#channel-select-all").is_checked()
    page.locator("#channel-search").fill("model-1")
    assert page.locator(".channel-message").count() == 1
    page.locator("#channel-search").fill("")
    status_filter.select_option("all")
    page.locator("#channel-select-all").check()
    assert page.locator(".channel-checkbox:checked").count() == 1
    assert row(5).locator(".channel-checkbox").is_checked()
    page.locator("#channel-download-selected").click()
    page.wait_for_function("!document.getElementById('channel-download-selected').disabled")
    assert submitted == [[405]], "Bulk selection repeated a processed file"
    row(1).locator("[data-channel-action='download']").click()
    page.wait_for_function("!document.querySelector('[data-message-id=\"401\"] [data-channel-action=\"download\"]').disabled")
    assert submitted == [[405], [401]], "Explicit local download of a remote file was blocked"

    workbench["tasks"][0].update(upload_status="completed", upload_provider="quark_api",
                                  share_url="https://pan.quark.cn/s/new")
    page.locator("#refresh").click()
    page.wait_for_function(
        "document.querySelector('[data-message-id=\"400\"]').textContent.includes('已转存夸克')")
    assert "已转存 3" in page.locator("#channel-result-count").inner_text()
    row(1).locator("[data-channel-action='task']").click()
    page.wait_for_function("document.getElementById('page-title').textContent === '下载任务'")
    assert page.locator("#detail-path").inner_text() == "文件位于其他设备"
    page.keyboard.press("Escape")
    page.locator(".nav-item[data-view='messages']").click()
    page.wait_for_function("document.querySelectorAll('.channel-message').length === 9")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator("#channel-transfer-filter").is_visible()
    screenshot = result_dir / f"channel-transfers-{unique}-{label}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    print(screenshot.resolve())


def check_cloud_delete(page, address: str, result_dir: Path, unique: str, label: str,
                       original: dict) -> None:
    base = address.split("/?")[0]
    with urlopen(f"{base}/api/workbench", timeout=5) as response:
        workbench = json.load(response)
    with urlopen(f"{base}/api/config", timeout=5) as response:
        config = json.load(response)
    config.update(upload_provider="quark_api", quark_cookie_configured=True)
    task = {
        "id": "delete-fixture", "message_id": 30218, "chat": "demo_channel",
        "filename": "model.rar", "title": "Model", "remote_filename": "中文模型.rar",
        "download_status": "completed", "upload_status": "completed", "validation": "passed",
        "upload_provider": "quark_api", "upload_remote_fid": "original-fid",
        "upload_target": "original-folder", "upload_filename": "model.rar", "upload_size": 100,
        "size": 100, "received": 100, "local_path": "C:/fixture/model.rar", "images": [],
        "share_url": "https://pan.quark.cn/s/fixture", "cloud_delete_status": "",
    }
    workbench.update(running=False, action="", error="", cloud_delete_supported=True, record_remove_supported=True,
                     tasks=[task, {**task, "id": "other-device", "message_id": 10, "remote_device": True},
                            {**task, "id": "cli-upload", "message_id": 11, "upload_provider": "quark_cli"},
                            {**task, "id": "cloud-only", "message_id": 12, "upload_provider": "quark_cli",
                             "download_status": "failed", "local_path": ""},
                            {**task, "id": "failed-only", "message_id": 13,
                             "download_status": "failed", "upload_status": "failed"}])
    for route in ("**/api/workbench", "**/api/channel-messages"):
        page.unroute(route)
    page.route("**/api/config", lambda route: route.fulfill(json=config))
    page.route("**/api/workbench", lambda route: route.fulfill(json=workbench))
    page.route("**/api/channel-messages", lambda route: route.fulfill(json=original))
    requests, confirmations = [], []
    outcomes = iter(["failed", "pending", "completed"])

    def delete(route):
        payload = route.request.post_data_json
        assert payload == {"id": "delete-fixture", "remote_fid": "original-fid", "confirm": True}
        requests.append(payload)
        outcome = next(outcomes)
        task.update(cloud_delete_status=outcome,
                    cloud_delete_error="fixture remote file changed" if outcome == "failed" else "")
        if outcome == "completed":
            task.update(upload_status="deleted", share_url="", cloud_deleted_at="2026-10-22T08:00:00Z")
        route.fulfill(json={"message": "fixture accepted"})

    def confirm(dialog, accept):
        confirmations.append(dialog.message)
        dialog.accept() if accept else dialog.dismiss()

    page.route("**/api/cloud-delete", delete)
    for view in ("download", "upload", "history"):
        page.goto(base + f"/?view={view}")
        page.wait_for_function("document.getElementById('nav-library').textContent === '4'")
        assert page.locator(".row-delete-cloud:visible, .row-remove-record:visible").count() == 0
        assert page.locator("#delete-cloud-task").is_hidden()
        assert page.locator("#remove-record-task").is_hidden()
    page.goto(base + "/?view=library")
    page.wait_for_function("document.querySelector('.row-delete-cloud:not([hidden])') && !document.querySelector('.row-delete-cloud:not([hidden])').disabled")
    assert page.locator(".row-delete-cloud:visible").count() == 1
    assert page.locator("#task-list .task:visible").count() == 4
    assert page.locator("[data-task-id='cloud-only']").is_visible()
    assert page.locator("[data-task-id='cloud-only'] .badge").first.inner_text() == "已完成"
    assert page.locator("[data-task-id='failed-only']").count() == 0
    assert page.locator(".row-remove-record:visible").count() == 3
    assert page.locator("[data-task-id='other-device'] .row-remove-record").is_hidden()
    row = page.locator("[data-task-id='delete-fixture']")
    page.once("dialog", lambda dialog: confirm(dialog, False))
    row.locator(".row-delete-cloud").click()
    assert not requests
    assert "中文模型.rar" in confirmations[-1] and "本地文件和任务记录将保留" in confirmations[-1]
    row.locator(".task-title").click()
    assert page.locator("#delete-cloud-task").is_visible()
    for status in ("failed", "pending", "completed"):
        page.once("dialog", lambda dialog: confirm(dialog, True))
        page.locator("#delete-cloud-task").click()
        expected = {"failed": "云端删除失败", "pending": "云端删除待确认", "completed": "云端已删除"}[status]
        page.wait_for_function("(text) => document.getElementById('detail-upload').textContent === text", arg=expected)
        if status == "failed":
            assert "fixture remote file changed" in page.locator("#detail-error").inner_text()
            assert page.locator("#share-result").is_visible()
        elif status == "pending":
            assert page.locator("#share-result").is_hidden()
            assert "重新确认" in page.locator("#delete-cloud-task").inner_text()
            assert page.locator("#remove-record-task").is_disabled()
    assert len(requests) == 3
    assert page.locator("#delete-cloud-task").is_hidden()
    assert page.locator("#share-result").is_hidden()
    assert page.locator("#detail-download").inner_text() == "已下载"
    assert page.locator("#open-task").is_visible()
    assert page.locator("#remove-record-task").is_enabled()
    screenshot = result_dir / f"library-cloud-deleted-{unique}-{label}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    page.keyboard.press("Escape")
    row.locator(".task-checkbox").check()
    assert page.locator("#upload").is_enabled(), "A deleted cloud file should be eligible for a new upload"
    page.locator(".nav-item[data-view='messages']").click()
    page.wait_for_function(
        "document.querySelector('[data-message-id=\"30218\"]')?.textContent.includes('云端已删除')")
    badges = page.locator("[data-message-id='30218'] .channel-message-badges").inner_text()
    assert "已下载" in badges and "已转存夸克" not in badges
    assert page.locator("[data-message-id='30218']").get_by_role("link", name="查看夸克").count() == 0
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator(".nav-item[data-view='library']").click()
    row.wait_for(state="visible")
    removed = []

    def remove(route):
        assert route.request.post_data_json == {"id": task["id"], "confirm": True}
        removed.append(task["id"])
        workbench["tasks"] = [item for item in workbench["tasks"] if item["id"] != task["id"]]
        route.fulfill(json={"message": "fixture record removed"})

    page.route("**/api/library/remove", remove)
    page.once("dialog", lambda dialog: confirm(dialog, False))
    row.locator(".row-remove-record").click()
    assert not removed and row.is_visible()
    assert "本地文件、图片和夸克文件保留" in confirmations[-1]
    row.locator(".task-title").click()
    page.once("dialog", lambda dialog: confirm(dialog, True))
    page.locator("#remove-record-task").click()
    page.wait_for_function("!document.querySelector('[data-task-id=\"delete-fixture\"]')")
    assert removed == [task["id"]]
    assert page.locator("#nav-library").inner_text() == "3"
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    removed_screenshot = result_dir / f"library-record-removed-{unique}-{label}.png"
    page.screenshot(path=str(removed_screenshot), full_page=True)
    page.reload()
    page.wait_for_function("document.getElementById('nav-library').textContent === '3'")
    assert page.locator("[data-task-id='delete-fixture']").count() == 0
    page.locator(".nav-item[data-view='messages']").click()
    page.wait_for_function(
        "document.querySelector('[data-message-id=\"30218\"] .channel-message-badges')?.textContent === '未下载'")
    print(removed_screenshot.resolve())
    print(screenshot.resolve())


def check(executable: Path | None = None) -> None:
    from playwright.sync_api import sync_playwright

    if not BROWSER.is_file():
        raise RuntimeError(f"Browser missing: {BROWSER}")
    with tempfile.TemporaryDirectory(prefix="channel-ui-", ignore_cleanup_errors=True) as temp:
        base = Path(temp)
        output = base / "output"
        preview = output / ".channel-previews" / "demo_channel"
        preview.mkdir(parents=True)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            unused_proxy_port = listener.getsockname()[1]
        config = base / "config.json"
        config.write_text(json.dumps({
            "source_channel_url": "https://t.me/demo_channel",
            "telegram_access_mode": "api", "telegram_api_id": 12345, "telegram_api_hash": "a" * 32,
            "telegram_api_proxy_url": f"socks5://127.0.0.1:{unused_proxy_port}",
            "inbox_dir": str(base / "inbox"), "work_dir": str(base / "work"),
            "output_dir": str(output), "telegram_session": str(base / "telegram.session"),
        }), encoding="utf-8")
        examples = [(30218, "Architectural Forms Vol. 04.rar", "arch.png",
                     "Facade studies, modular walls and exterior details.", 136_800_000),
                    (30217, "Sculptural Sofa Collection.zip", "sofa.png",
                     "Seating set with three finishes and production-ready geometry.", 78_600_000),
                    (30214, "Ceramic Vases for Interiors.rar", "vase.png",
                     "Contemporary ceramic pieces for interior scenes.", 42_300_000)]
        rows = []
        images = {}
        for mid, name, asset, caption, size in examples:
            target = preview / f"{mid}.png"
            target.write_bytes((ASSETS / asset).read_bytes())
            images[mid] = target.read_bytes()
            rows.append({"message_id": mid, "chat": "demo_channel", "filename": name, "size": size,
                         "text": caption, "date": "2026-10-02T08:00:00+00:00",
                         "source_url": f"https://t.me/demo_channel/{mid}",
                         "cover_path": str(target), "cover_message_id": mid - 1, "cover_quality": "full"})
        (output / "channel-catalog.json").write_text(json.dumps({
            "channel": "demo_channel", "messages": rows, "oldest_scanned_id": 30214,
            "has_more": True, "scanned_at": "2026-10-02T08:10:00+00:00",
        }), encoding="utf-8")
        original = {
            "channel": "demo_channel", "oldest_scanned_id": 30214,
            "has_more": True, "scanned_at": "2026-10-02T08:10:00+00:00",
            "messages": [{**row, "cover_url": f"/api/channel-preview/{row['message_id']}",
                          "full_cover_url": f"/api/channel-full-preview/{row['message_id']}",
                          "cover_status": "ready", "full_status": "ready", "full_quality": "full"}
                         for row in rows],
        }
        saved = {
            "active": "demo_channel",
            "channels": [{"key": "demo_channel", "target": "demo_channel", "id": 1001,
                          "name": "Demo channel", "title": "Demo channel", "username": "demo_channel",
                          "url": "https://t.me/demo_channel", "public": True, "avatar_url": ""}],
        }
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        code = "from xianyu_pipeline.web_ui import start_web_ui; import sys; start_web_ui(sys.argv[1], port=int(sys.argv[2]), open_browser=False)"
        command = ([str(executable.resolve()), "--config", str(config), "--web-ui",
                    "--web-port", str(port), "--no-open-browser"] if executable else
                   [sys.executable, "-c", code, str(config), str(port)])
        server = subprocess.Popen(command, cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        address = f"http://127.0.0.1:{port}/?view=messages"
        try:
            for _ in range(100):
                try:
                    with urlopen(f"http://127.0.0.1:{port}/api/channel-messages", timeout=0.5):
                        break
                except (URLError, OSError):
                    if server.poll() is not None:
                        raise AssertionError("Channel UI server exited")
                    time.sleep(0.1)
            else:
                raise AssertionError("Channel UI server did not start")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(BROWSER), headless=True)
                try:
                    result_dir = ROOT / "outputs"
                    unique = uuid4().hex[:8]
                    for label, width, height in (("desktop", 1360, 880), ("mobile", 390, 844)):
                        page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.route("**/api/channels", lambda route: route.fulfill(json=saved))
                        page.route("**/api/channel-messages", lambda route: route.fulfill(json=original))

                        def preview_image(route):
                            mid = int(route.request.url.split("?", 1)[0].rsplit("/", 1)[1])
                            route.fulfill(content_type="image/png", body=images[mid])

                        page.route("**/api/channel-preview/**", preview_image)
                        page.route("**/api/channel-full-preview/**", preview_image)
                        page.goto(address)
                        page.locator("#channel-select").wait_for(state="visible")
                        page.wait_for_function("!document.getElementById('scan-channel').disabled")
                        assert "Demo channel" in page.locator("#channel-select").inner_text()
                        page.locator(".channel-message").first.wait_for(timeout=10000)
                        assert page.locator(".channel-message").count() == 3
                        page.locator(".channel-message-cover img").first.wait_for(state="visible")
                        assert page.locator(".channel-message-cover img").first.evaluate("(image) => image.naturalWidth > 0")
                        cover = page.locator("button.channel-message-cover").first
                        cover.click()
                        assert page.locator("#channel-preview-dialog").is_visible()
                        page.wait_for_function("document.getElementById('channel-preview-image').naturalWidth > 0")
                        page.locator("#channel-zoom-in").click()
                        assert page.locator("#channel-zoom-level").inner_text() == "150%"
                        zoom_screenshot = result_dir / f"channel-zoom-{unique}-{label}.png"
                        page.screenshot(path=str(zoom_screenshot))
                        page.keyboard.press("Escape")
                        assert not page.locator("#channel-preview-dialog").is_visible()
                        assert cover.evaluate("(button) => document.activeElement === button")
                        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "horizontal overflow"
                        page.locator("#channel-search").fill("30217")
                        assert page.locator(".channel-message").count() == 1
                        page.locator("#channel-search").fill("")
                        page.locator(".channel-checkbox").first.check()
                        assert page.locator("#channel-download-selected").is_enabled()
                        page.locator(".channel-checkbox").first.uncheck()
                        screenshot = result_dir / f"channel-catalog-{unique}-{label}.png"
                        page.screenshot(path=str(screenshot), full_page=True)
                        check_loading(page, address, result_dir, unique, label, original)
                        check_transfers(page, address, result_dir, unique, label, original)
                        check_cloud_delete(page, address, result_dir, unique, label, original)
                        assert not errors, errors
                        print(screenshot.resolve())
                        print(zoom_screenshot.resolve())
                        page.close()
                finally:
                    browser.close()
            if executable:
                request = Request(f"http://127.0.0.1:{port}/api/telegram-channels-scan", data=b"{}",
                                  method="POST", headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=5) as response:
                    assert response.status == 200
                for _ in range(100):
                    with urlopen(f"http://127.0.0.1:{port}/api/logs", timeout=5) as response:
                        state = json.load(response)
                    if not state["running"]:
                        break
                    time.sleep(0.3)
                else:
                    raise AssertionError("Packaged API scan did not return")
                assert "未监听" in state["error"], state["error"]
                print("Packaged Telegram API import and local proxy diagnostic: PASS")
        finally:
            server.terminate()
            server.wait(timeout=10)
            time.sleep(0.3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, help="Check a packaged executable instead of source")
    args = parser.parse_args()
    check(args.executable)
