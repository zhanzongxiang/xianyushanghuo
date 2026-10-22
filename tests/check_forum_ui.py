"""Offline forum-topic browser checks using an isolated temporary server."""
from __future__ import annotations

from check_channel_picker_ui import ARTIFACTS, Fixture, avatar_png, assert_no_overflow, check, refresh


def check_forum(page, fixture: Fixture, origin: str, label: str, unique: str):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    parent = {"key": "forum_models", "url": "https://t.me/forum_models", "name": "3D Print Israel.STL",
              "username": "forum_models", "forum": True, "kind": "forum", "topics": [],
              "topics_scanned_at": "", "id": 1001}
    fixture.saved["channels"].append(parent)
    fixture.saved["active_topic_id"] = 0
    fixture.config["source_topic_id"] = 0
    fixture.workbench["telegram_topics"] = {"stage": "idle", "channel": "", "found": 0, "error": ""}
    catalogs = {}

    def route(request):
        path = request.request.url.split(origin, 1)[-1].split("?", 1)[0]
        if path.startswith("/api/channel-preview/"):
            request.fulfill(content_type="image/png", body=avatar_png())
            return
        if request.request.method == "POST":
            payload = request.request.post_data_json or {}
            if path == "/api/channel-topics-scan":
                fixture.posts.append((path, payload))
                fixture.workbench.update(running=True, action="channel-topics-scan",
                                          task_id="fixture-topic-scan", stop_requested=False)
                fixture.workbench["telegram_topics"].update(stage="loading", channel=payload["key"])
                request.fulfill(json={"message": "fixture topic scan started"})
                return
            if path == "/api/channels/select":
                fixture.posts.append((path, payload))
                row = next(item for item in fixture.saved["channels"] if item["key"] == payload["key"])
                topic_id = payload.get("topic_id", 0)
                fixture.saved.update(active=row["key"], active_topic_id=topic_id)
                fixture.config.update(source_channel_url=row["url"], source_topic_id=topic_id)
                scope = f"{row['key']}~{topic_id}" if topic_id else row["key"]
                fixture.catalog = catalogs.get(scope, fixture.empty_catalog(scope))
                request.fulfill(json={"message": "fixture selected", "channel": row})
                return
            if path == "/api/channel-scan":
                fixture.posts.append((path, payload))
                topic_id = fixture.saved["active_topic_id"]
                scope = f"forum_models~{topic_id}"
                fixture.catalog = {**fixture.empty_catalog(scope), "chat": "forum_models",
                                   "topic_id": topic_id, "scanned_at": "2026-10-22T02:00:00Z",
                                   "messages": [{"message_id": 99, "chat": "forum_models", "topic_id": topic_id,
                                                 "filename": f"topic-{topic_id}.rar", "size": 123456,
                                                 "text": "Fixture resource", "cover_status": "ready",
                                                 "cover_url": f"/api/channel-preview/99?source={scope}",
                                                 "full_cover_url": f"/api/channel-preview/99?source={scope}",
                                                 "full_quality": "full", "cover_quality": "preview"}]}
                catalogs[scope] = fixture.catalog
                fixture.workbench.update(running=False, action="", channel_scan={})
                request.fulfill(json={"message": "fixture messages loaded"})
                return
            if path == "/api/channel-download":
                fixture.posts.append((path, payload))
                request.fulfill(json={"message": "fixture download queued"})
                return
        fixture.route(request)

    page.route("**/api/**", route)
    page.goto(origin + "/?view=channels")
    page.locator("[data-channel-management='topics']").wait_for(state="visible")
    assert not fixture.posts, "Viewing the source list must not start a Telegram request"
    page.locator("[data-channel-management='topics']").click()
    page.wait_for_function("!document.getElementById('stop').hidden")
    assert fixture.posts == [("/api/channel-topics-scan", {"key": "forum_models"})]
    assert page.locator("[data-channel-management='refresh-topics']").is_disabled()
    parent["topics"] = [
        {"topic_id": 1, "title": "General", "key": "forum_models~1"},
        {"topic_id": 40, "title": "STL Files for print! Flexi models collection",
         "key": "forum_models~40"},
        {"topic_id": 70, "title": "Movies and Characters", "closed": True, "hidden": True,
         "key": "forum_models~70"},
    ]
    parent["topics_scanned_at"] = "2026-10-22T01:00:00Z"
    fixture.workbench.update(running=False, action="", error="")
    fixture.workbench["telegram_topics"].update(stage="completed", found=3)
    refresh(page, fixture)
    page.locator(".topic-row[data-topic-id='40'] button").wait_for(state="visible")
    assert page.locator(".topic-row").count() == 3
    assert "已关闭" in page.locator(".topic-row[data-topic-id='70']").inner_text()
    assert_no_overflow(page, f"{label}: forum source list")
    source_image = ARTIFACTS / f"forum-sources-{unique}-{label}.png"
    page.screenshot(path=str(source_image), full_page=True)
    print(source_image.resolve())

    page.locator(".topic-row[data-topic-id='40'] button").click()
    page.wait_for_function("!document.getElementById('refresh').disabled")
    page.locator(".nav-item[data-view='messages']").click()
    page.locator("#channel-topic-select").wait_for(state="visible")
    page.wait_for_function("document.getElementById('channel-topic-select').value === '40'")
    assert "STL Files" in page.locator("#page-subtitle").inner_text()
    page.locator("#scan-channel").click()
    page.locator(".channel-message h2").wait_for(state="visible")
    assert page.locator(".channel-message h2").inner_text() == "topic-40.rar"
    page.locator(".channel-message-cover").click()
    page.locator("#channel-preview-dialog").wait_for(state="visible")
    page.wait_for_function("document.getElementById('channel-preview-image').complete && document.getElementById('channel-preview-image').naturalWidth > 0")
    page.keyboard.press("Escape")
    page.locator("#channel-topic-select").select_option("70")
    page.wait_for_function("document.getElementById('channel-topic-select').value === '70'")
    page.wait_for_function("document.querySelectorAll('.channel-message').length === 0")
    page.locator("#scan-channel").click()
    page.locator(".channel-message h2").wait_for(state="visible")
    assert page.locator(".channel-message h2").inner_text() == "topic-70.rar"
    page.locator("#channel-topic-select").select_option("40")
    page.wait_for_function("document.querySelector('.channel-message h2')?.textContent === 'topic-40.rar'")
    page.locator("[data-channel-action='download']").click()
    page.wait_for_function("!document.getElementById('channel-topic-select').disabled")
    assert fixture.posts[-1] == ("/api/channel-download", {"ids": [99]})
    assert_no_overflow(page, f"{label}: forum messages")
    message_image = ARTIFACTS / f"forum-messages-{unique}-{label}.png"
    page.screenshot(path=str(message_image), full_page=True)
    print(message_image.resolve())

    page.locator(".nav-item[data-view='settings']").click()
    page.locator("#source_topic_id").wait_for(state="visible")
    assert page.locator("#source_topic_id").input_value() == "40"
    page.locator("#source_channel_url").select_option("https://t.me/fixture_saved")
    assert page.locator("#source_topic_id").is_hidden()
    assert page.locator("#source_topic_id").input_value() == "0"
    assert not errors, errors


if __name__ == "__main__":
    check(check_callback=check_forum)
