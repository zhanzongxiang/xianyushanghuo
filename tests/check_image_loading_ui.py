"""Offline browser checks for full-batch previews, HTTP cache and slow polling."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen

from check_channel_ui import ASSETS, BROWSER, ROOT

sys.path.insert(0, str(ROOT))
from xianyu_pipeline.channel_catalog import ChannelCatalog


def check_recovery(page, address):
    attempts = []

    def transient(route):
        attempts.append(route.request.url)
        if len(attempts) == 1:
            route.abort("failed")
        else:
            route.continue_()

    page.route("**/api/channel-preview/100?*", transient)
    page.reload()
    page.wait_for_function("""() => {
        const image = document.querySelector('[data-message-id="100"] .channel-message-cover img');
        return image?.complete && image.naturalWidth > 0;
    }""")
    assert len(attempts) == 2, attempts
    page.unroute("**/api/channel-preview/100?*")

    with urlopen(address + "/api/channel-messages") as response:
        listing = json.load(response)
    item = listing["messages"][0]
    item.update(full_cover_url="/api/channel-full-preview/100?test=broken",
                full_status="ready", full_quality="large")
    def known_photo():
        item["full_images"] = [{"message_id": 99, "full_cover_url": item["full_cover_url"],
                                "full_status": item["full_status"], "full_quality": "large", "full_error": item.get("full_error", "")}]
    known_photo()
    page.route("**/api/channel-messages", lambda route: route.fulfill(json=listing))
    full_attempts, posts = [], []

    def full_image(route):
        if "test=broken" in route.request.url:
            full_attempts.append(route.request.url)
            route.fulfill(status=404, body="missing")
        else:
            route.fulfill(content_type="image/png", body=(ASSETS / "arch.png").read_bytes())

    def retry_image(route):
        posts.append(route.request.post_data_json)
        item.update(full_cover_url="/api/channel-full-preview/100?test=repaired",
                    full_status="ready", full_error="")
        known_photo()
        route.fulfill(json={"url": item["full_cover_url"]})

    page.route("**/api/channel-full-preview/100?*", full_image)
    page.route("**/api/channel-cover", retry_image)
    page.reload()
    page.locator('[data-message-id="100"] .channel-message-cover').click()
    page.wait_for_function("document.getElementById('channel-preview-status').dataset.stage === 'failed'")
    assert page.locator("#channel-preview-dialog").evaluate("dialog => dialog.open")
    page.wait_for_function("""() => {
        const image = document.getElementById('channel-preview-image');
        return image.complete && image.naturalWidth > 0 && image.src.includes('/api/channel-preview/100');
    }""")
    assert len(full_attempts) == 3 and not posts
    page.locator("#refresh").evaluate("button => button.click()")
    page.wait_for_timeout(1600)
    assert len(full_attempts) == 3, "Polling restarted the failed image indefinitely"
    page.locator("#channel-preview-retry").click()
    page.wait_for_function("""() => {
        const image = document.getElementById('channel-preview-image');
        return image.complete && image.naturalWidth > 0 && image.src.includes('test=repaired');
    }""")
    assert posts == [{"id": 100, "original": False, "gallery": True, "photo_id": 99, "refresh": True}], posts
    assert page.locator("#channel-preview-retry").is_hidden()
    assert page.locator("#channel-preview-dialog").evaluate("dialog => dialog.open")
    page.locator("#channel-preview-close").click()

    # A failed Telegram request must leave the cached medium image available.
    item.update(full_cover_url="", full_status="cancelled", full_error="previous request interrupted")
    known_photo()

    def offline(route):
        posts.append(route.request.post_data_json)
        item.update(full_status="failed", full_error="API proxy 127.0.0.1:7890 unavailable")
        known_photo()
        route.fulfill(json={"message": "queued"})

    page.unroute("**/api/channel-cover")
    page.route("**/api/channel-cover", offline)
    page.reload()
    page.locator('[data-message-id="100"] .channel-message-cover').click()
    page.wait_for_function("document.getElementById('channel-preview-status-text').textContent.includes('7890')")
    assert page.locator("#channel-preview-retry").is_visible()
    assert page.locator("#channel-preview-image").evaluate("image => image.complete && image.naturalWidth > 0")
    page.locator("#channel-preview-close").click()
    page.wait_for_timeout(100)
    assert page.locator("#channel-preview-image").get_attribute("src") is None
    for pattern in ("**/api/channel-messages", "**/api/channel-full-preview/100?*", "**/api/channel-cover"):
        page.unroute(pattern)


def check():
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="image-loading-", ignore_cleanup_errors=True) as temporary:
        base = Path(temporary)
        output = base / "output"
        catalog = ChannelCatalog(output)
        catalog.preview_root.mkdir(parents=True)
        image = catalog.preview_root / "fixture.png"
        image.write_bytes((ASSETS / "arch.png").read_bytes())
        rows = [{"message_id": mid, "chat": "demo_channel", "filename": f"Model-{mid}.rar",
                 "size": 12345678, "text": "", "date": "", "cover_path": str(image),
                 "cover_status": "ready", "cover_quality": "preview"} for mid in range(100, 0, -1)]
        catalog.merge("demo_channel", {"messages": rows, "oldest_scanned_id": 1, "has_more": False})
        config = base / "config.json"
        config.write_text(json.dumps({
            "source_channel_url": "https://t.me/demo_channel", "telegram_api_id": 12345,
            "telegram_api_hash": "a" * 32, "output_dir": str(output),
            "work_dir": str(base / "work"), "inbox_dir": str(base / "inbox"),
            "telegram_session": str(base / "session"), "data_server_enabled": False,
        }), encoding="utf-8")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        address = f"http://127.0.0.1:{port}"
        code = "from xianyu_pipeline.web_ui import start_web_ui; import sys; start_web_ui(sys.argv[1], port=int(sys.argv[2]), open_browser=False)"
        server = subprocess.Popen([sys.executable, "-c", code, str(config), str(port)], cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            for _ in range(100):
                try:
                    with urlopen(address + "/api/channel-messages", timeout=0.5):
                        break
                except (URLError, OSError):
                    assert server.poll() is None, "Preview server exited"
                    time.sleep(0.05)
            else:
                raise AssertionError("Preview server did not start")

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(BROWSER), headless=True)
                try:
                    for label, width, height in (("desktop", 1360, 880), ("mobile", 390, 844)):
                        page = browser.new_page(viewport={"width": width, "height": height})
                        errors, posts = [], []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on("request", lambda request: posts.append(request.url)
                                if request.method == "POST" else None)
                        page.goto(address + "/?view=messages")
                        ready = """() => {
                            const images = [...document.querySelectorAll('.channel-message-cover img')];
                            return images.length === 100 && images.every(image => image.complete && image.naturalWidth > 0);
                        }"""
                        page.wait_for_function(ready, timeout=30000)
                        assert page.evaluate("window.scrollY === 0")
                        assert page.locator(".channel-message-cover img").last.evaluate(
                            "image => image.getBoundingClientRect().top > innerHeight * 3")
                        assert not any("/api/channel-cover" in url for url in posts)
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        page.reload()
                        page.wait_for_function(ready, timeout=30000)
                        cached = page.evaluate("""() => performance.getEntriesByType('resource')
                            .filter(entry => entry.name.includes('/api/channel-preview/'))
                            .map(entry => ({bytes: entry.transferSize, duration: entry.duration}))""")
                        assert len(cached) == 100 and all(entry["bytes"] == 0 for entry in cached), cached
                        screenshot = ROOT / "outputs" / f"image-loading-{label}.png"
                        page.screenshot(path=str(screenshot))

                        # Hold the log request open: channel images must not wait for it.
                        delayed = []
                        page.route("**/api/logs", lambda route: delayed.append(route))
                        image.unlink()
                        page.reload()
                        try:
                            page.wait_for_function("document.querySelectorAll('.channel-message').length === 100")
                            assert page.locator(".channel-message-cover img").count() == 0
                            image.write_bytes((ASSETS / "arch.png").read_bytes())
                            page.wait_for_function(ready, timeout=10000)
                            assert delayed, "Slow-log fixture was not exercised"
                        finally:
                            for route in delayed:
                                route.fulfill(json={"logs": []})
                            page.unroute("**/api/logs")
                        assert not errors, errors
                        check_recovery(page, address)
                        assert not errors, errors
                        print(f"{label}: 100/100 offscreen previews loaded; 100/100 cache hits; slow-log check PASS")
                        print(f"{label}: transient retry, bounded full-image retry, medium fallback, cache refresh and proxy-error checks PASS")
                        print(screenshot)
                        page.close()
                finally:
                    browser.close()
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    check()
