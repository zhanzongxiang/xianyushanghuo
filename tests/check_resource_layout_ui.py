"""Offline interactions for resource galleries, source topics and download modes."""
from __future__ import annotations

import argparse
from pathlib import Path

import check_channel_picker_ui as harness
from check_channel_ui import ASSETS


class ResourceFixture(harness.Fixture):
    def __init__(self, origin):
        super().__init__(origin)
        self.saved["channels"].extend([
            {"key": "fixture_other", "name": "Other models", "username": "fixture_other",
             "url": "https://t.me/fixture_other", "public": True},
            {"key": "-1001234567890", "name": "Model forum", "url": "-1001234567890",
             "forum": True, "kind": "supergroup", "topics": [
                 {"topic_id": 11, "title": "Miniatures"}, {"topic_id": 22, "title": "Buildings"}]}])
        self.config.update(source_channel_url="https://t.me/fixture_saved", source_topic_id=0)
        self.catalogs = {}
        for scope in ("fixture_saved", "fixture_other", "-1001234567890~11", "-1001234567890~22"):
            self.catalogs[scope] = {"channel": scope, "oldest_scanned_id": 10, "has_more": True,
                "scanned_at": "2026-10-30T01:00:00Z", "messages": [
                    {"message_id": mid, "chat": scope.split("~")[0], "filename": name,
                     "size": size, "text": "Caption <script>window.unsafeFixture = true</script>",
                     "date": "2026-10-30T01:00:00Z", "source_url": f"https://t.me/fixture_saved/{mid}",
                     "cover_message_id": mid - 1, "cover_status": "ready", "cover_quality": "preview",
                     "cover_url": f"/api/channel-preview/{mid}?source={scope}",
                     "full_cover_url": f"/api/channel-full-preview/{mid}?source={scope}",
                     "full_status": "ready", "full_quality": "full"}
                    for mid, name, size in ((40, f"@Author {scope} Zeta.zip", 4000),
                        (30, "Alpha-long-model-" + "details" * 25 + ".rar", 1000),
                        (21, "Local-model.zip", 2000), (10, "Cloud-model.rar", 3000))]}
        self.catalog = self.catalogs["fixture_saved"]
        for catalog in self.catalogs.values():
            for row in catalog["messages"]:
                row["full_images"] = [{"message_id": row["cover_message_id"], "full_cover_url": row["full_cover_url"],
                                       "full_status": "ready", "full_quality": "full"}]
        self.workbench["tasks"].append({"id": "cloud-task", "chat": "fixture_saved", "message_id": 10,
            "filename": "Cloud-model.rar", "download_status": "completed", "upload_status": "completed",
            "upload_provider": "quark_api", "share_url": "https://pan.quark.cn/s/fixture", "images": []})

    def route(self, route):
        path = route.request.url.split(self.origin, 1)[-1].split("?", 1)[0]
        if path.startswith(("/api/channel-preview/", "/api/channel-full-preview/")):
            route.fulfill(content_type="image/png", body=(ASSETS / "arch.png").read_bytes())
            return
        if route.request.method == "POST":
            data = route.request.post_data_json
            if path == "/api/channel-download":
                self.posts.append((path, data))
                route.fulfill(json={"message": "fixture queued"})
                return
            if path == "/api/channels/select":
                self.posts.append((path, data))
                row = next(row for row in self.saved["channels"] if row["key"] == data["key"])
                topic = data.get("topic_id", 0)
                self.saved.update(active=row["key"], active_topic_id=topic)
                self.config.update(source_channel_url=row["url"], source_topic_id=topic)
                scope = row["key"] + (f"~{topic}" if topic else "")
                self.catalog = self.catalogs.get(scope, self.empty_catalog(scope))
                route.fulfill(json={"channel": row, "message": "selected"})
                return
        super().route(route)


def check_view(page, _fixture, origin, label, unique):
    fixture = ResourceFixture(origin)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", fixture.route)
    page.goto(origin + "/?view=messages")
    page.wait_for_function("document.querySelectorAll('.channel-message').length === 4")
    page.wait_for_function("!document.getElementById('scan-channel').disabled")
    assert not fixture.posts
    assert page.locator("#channel-grid-mode").get_attribute("aria-pressed") == "true"
    assert page.locator(".channel-message").first.get_attribute("data-message-id") == "40"
    page.locator("#channel-format").select_option("rar")
    assert page.locator(".channel-message").count() == 2
    page.locator("#channel-sort").select_option("size")
    assert page.locator(".channel-message").first.get_attribute("data-message-id") == "30"
    page.locator("#channel-format").select_option("all")
    page.locator('[data-catalog-filter="transferred"]').click()
    assert page.locator(".channel-message").count() == 1
    assert page.locator(".channel-message").get_attribute("data-message-id") == "10"
    page.locator('[data-catalog-filter="new"]').click()
    assert page.locator(".channel-message").count() == 2
    page.locator("#channel-select-all").check()
    assert "2" in page.locator("#channel-selected-count").inner_text()
    page.locator('[data-message-id="30"] [data-channel-action="inspect"]').click()
    assert page.locator("#resource-inspector").is_visible()
    assert page.locator("#resource-mid").inner_text() == "#30"
    assert page.locator("#resource-photo-id").inner_text() == "#29"
    assert "details" in page.locator("#resource-filename").inner_text()
    assert page.locator(".channel-checkbox:checked").count() == 2
    page.locator("#resource-picture").click()
    page.wait_for_function("""() => {const img=document.getElementById('channel-preview-image');return img.complete && img.naturalWidth>0;}""")
    assert page.locator("#channel-preview-dialog").evaluate("e=>e.open")
    assert not fixture.posts, "Viewing cached images must not start any job"
    page.locator("#channel-preview-close").click()
    assert page.locator("#resource-inspector").is_visible()
    page.locator("#resource-download-upload").click()
    page.wait_for_function("!document.getElementById('resource-download-upload').disabled")
    assert fixture.posts[-1] == ("/api/channel-download", {"ids": [30], "auto_upload_after_download": True})
    page.locator("#resource-close").click()
    page.locator("#channel-download-selected").click()
    page.wait_for_function("!document.getElementById('channel-download-selected').disabled")
    assert fixture.posts[-1] == ("/api/channel-download", {"ids": [40, 30], "auto_upload_after_download": False})
    page.locator("#channel-list-mode").click()
    assert page.locator("#channel-messages-section").get_attribute("data-mode") == "list"
    assert page.locator(".channel-checkbox:checked").count() == 2
    page.locator("#channel-clear-selection").click()
    assert page.locator("#channel-download-selected").is_disabled()

    # Preserve selections and filters per source; message IDs may overlap.
    page.locator('[data-message-id="40"] .channel-checkbox').check()
    page.locator("#channel-search").fill("Zeta")
    if label == "desktop":
        page.locator('[data-source-select="fixture_other"]').click()
    else:
        page.locator("#channel-select").click()
        page.locator('[data-channel-option][data-channel-key="fixture_other"]').click()
    page.wait_for_function("document.getElementById('channel-selected-title').textContent === 'Other models'")
    page.wait_for_function("document.querySelectorAll('.channel-message').length === 4")
    assert page.locator("#channel-search").input_value() == ""
    assert page.locator(".channel-checkbox:checked").count() == 0
    page.locator("#channel-select").click()
    page.locator('[data-channel-option][data-channel-key="fixture_saved"]').click()
    page.wait_for_function("document.getElementById('channel-search').value === 'Zeta'")
    assert page.locator(".channel-message").count() == 1
    assert page.locator(".channel-checkbox:checked").count() == 1

    if label == "desktop":
        page.locator('[data-source-expand="-1001234567890"]').click()
        page.locator('[data-source-topic="11"]').click()
    else:
        page.locator("#channel-select").click()
        page.locator('[data-channel-option][data-channel-key="-1001234567890"]').click()
        page.wait_for_function("!document.getElementById('channel-topic-select').disabled")
        page.locator("#channel-topic-select").select_option("11")
    page.wait_for_function("document.getElementById('channel-topic-select').value === '11'")
    page.wait_for_function("document.querySelectorAll('.channel-message').length === 4")
    assert fixture.posts[-1] == ("/api/channels/select", {"key": "-1001234567890", "topic_id": 11})
    assert page.locator(".channel-checkbox:checked").count() == 0
    page.locator("#channel-grid-mode").click()
    page.locator('[data-message-id="30"] [data-channel-action="inspect"]').click()
    page.screenshot(path=str(harness.ARTIFACTS / f"resource-layout-{label}-{unique}.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.evaluate("window.unsafeFixture !== true")
    page.keyboard.press("Escape")
    assert page.locator("#resource-inspector").is_hidden()
    assert not errors, errors
    print(f"{label}: filters, sorting, details, zoom, batch actions, per-source selection and forum topics PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", type=Path, required=True)
    args = parser.parse_args()
    harness.BROWSER = args.browser
    harness.check(check_callback=check_view)
