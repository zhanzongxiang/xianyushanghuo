"""Offline browser checks for download-only and download-and-upload actions."""
from __future__ import annotations

from check_channel_picker_ui import ARTIFACTS, Fixture, assert_no_overflow, check, refresh


def check_download_modes(page, fixture: Fixture, origin: str, label: str, unique: str):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    fixture.config["auto_upload_after_download"] = True
    fixture.catalog.update(
        channel="fixture_saved", scanned_at="2026-10-23T01:00:00Z",
        messages=[{"message_id": mid, "chat": "fixture_saved",
                   "filename": f"model-{mid}.zip", "size": 1024,
                   "cover_status": "no_cover", "text": ""}
                  for mid in (99, 98)],
    )

    def route(request):
        path = request.request.url.split(origin, 1)[-1].split("?", 1)[0]
        if request.request.method == "POST" and path in {"/api/channel-download", "/api/download"}:
            fixture.posts.append((path, request.request.post_data_json))
            request.fulfill(json={"message": "fixture queued"})
            return
        fixture.route(request)

    page.route("**/api/**", route)
    page.goto(origin + "/?view=messages")
    page.locator("[data-message-id='99'] [data-channel-action='download-upload']").wait_for(state="visible")
    assert page.locator("#channel-download-selected").is_disabled()
    assert page.locator("#channel-download-upload-selected").is_disabled()

    def click_and_expect(selector, path, payload):
        previous = len(fixture.posts)
        with page.expect_response(lambda response: response.url == origin + path
                                  and response.request.method == "POST"):
            page.locator(selector).click()
        page.wait_for_function("!document.getElementById('refresh').disabled")
        assert len(fixture.posts) == previous + 1
        assert fixture.posts[-1] == (path, payload)
        assert fixture.config["auto_upload_after_download"] is True

    click_and_expect("[data-message-id='99'] [data-channel-action='download']",
                     "/api/channel-download", {"ids": [99], "auto_upload_after_download": False})
    click_and_expect("[data-message-id='99'] [data-channel-action='download-upload']",
                     "/api/channel-download", {"ids": [99], "auto_upload_after_download": True})
    page.locator("#channel-select-all").check()
    assert page.locator(".channel-checkbox:checked").count() == 2
    click_and_expect("#channel-download-selected", "/api/channel-download",
                     {"ids": [99, 98], "auto_upload_after_download": False})
    click_and_expect("#channel-download-upload-selected", "/api/channel-download",
                     {"ids": [99, 98], "auto_upload_after_download": True})
    assert_no_overflow(page, f"{label}: download actions")
    if label == "desktop":
        original_viewport = page.viewport_size
        for width, height in ((1920, 1080), (1024, 768)):
            page.set_viewport_size({"width": width, "height": height})
            assert_no_overflow(page, f"{width}: channel layout")
            row = page.locator(".channel-message").first.bounding_box()
            section = page.locator("#channel-messages-section").bounding_box()
            assert section["width"] >= width - 200, "Channel list leaves unused space on the right"
            if width >= 1600:
                next_row = page.locator(".channel-message").nth(1).bounding_box()
                assert abs(row["y"] - next_row["y"]) <= 1, "Wide screens should show two resource columns"
                assert row["x"] + row["width"] < next_row["x"]
                assert abs(next_row["x"] + next_row["width"] - section["x"] - section["width"] + 30) <= 1
            else:
                assert abs(row["x"] + row["width"] - section["x"] - section["width"] + 30) <= 1
            operations = page.locator(".channel-message-actions").first.bounding_box()
            assert operations["x"] + operations["width"] <= row["x"] + row["width"] + 1
            page.screenshot(path=str(ARTIFACTS / f"channel-layout-{unique}-{width}.png"), full_page=True)
        page.set_viewport_size(original_viewport)
    else:
        download = page.locator("#channel-download-selected").bounding_box()
        transfer = page.locator("#channel-download-upload-selected").bounding_box()
        assert abs(download["y"] - transfer["y"]) <= 1, "Batch download buttons split across rows"
        assert download["x"] + download["width"] <= transfer["x"], "Batch download buttons overlap"
    screenshot = ARTIFACTS / f"download-modes-{unique}-{label}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    print(screenshot.resolve())

    fixture.workbench.update(running=True, action="channel-scan", channel_requests_open=True,
                              task_id="fixture-scan", stop_requested=False)
    refresh(page, fixture)
    assert page.locator("#channel-download-selected").is_enabled()
    assert page.locator("#channel-download-upload-selected").is_enabled()
    fixture.workbench.update(channel_requests_open=False)
    refresh(page, fixture)
    assert page.locator("#channel-download-selected").is_disabled()
    assert page.locator("#channel-download-upload-selected").is_disabled()
    fixture.workbench.update(running=False, action="")
    refresh(page, fixture)

    # Saved, incomplete cloud credentials must not prevent a local-only scan.
    fixture.config.update(upload_provider="quark_api", quark_cookie_configured=False)
    page.goto(origin + "/?view=download")
    page.locator("#download-upload").wait_for(state="visible")
    fixture.posts.clear()
    with page.expect_response(lambda response: response.url == origin + "/api/download"
                              and response.request.method == "POST"):
        page.locator("#download").click()
    page.wait_for_function("!document.getElementById('download').disabled")
    assert fixture.posts[-1] == ("/api/download", {"auto_upload_after_download": False})
    with page.expect_response(lambda response: response.url == origin + "/api/download"
                              and response.request.method == "POST"):
        page.locator("#download-upload").click()
    page.wait_for_function("!document.getElementById('download-upload').disabled")
    assert fixture.posts[-1] == ("/api/download", {"auto_upload_after_download": True})
    assert fixture.config["auto_upload_after_download"] is True
    assert_no_overflow(page, f"{label}: scan download actions")
    assert not errors, errors


if __name__ == "__main__":
    check(check_callback=check_download_modes)
