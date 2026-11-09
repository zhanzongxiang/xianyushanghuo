"""Offline multi-image navigation, streaming, retries and scoped previews."""
from __future__ import annotations

import argparse
from pathlib import Path

import check_channel_picker_ui as harness
from check_resource_layout_ui import ResourceFixture


def check_view(page, _fixture, origin, label, unique):
    fixture = ResourceFixture(origin)
    row = fixture.catalog["messages"][0]
    row.pop("full_images")
    calls, errors = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", fixture.route)

    def photo(mid, status="ready", quality="large"):
        return {"message_id": mid, "full_status": status, "full_quality": quality,
                "full_cover_url": f"/api/channel-full-preview/40?source=fixture_saved&photo={mid}&quality={quality}" if status == "ready" else "",
                "full_error": "图片连接超时" if status == "failed" else ""}

    def load(route):
        data = route.request.post_data_json
        calls.append(data)
        assert data["gallery"] and data["id"] == 40
        if data.get("photo_id"):
            index = next(i for i, image in enumerate(row["full_images"]) if image["message_id"] == data["photo_id"])
            row["full_images"][index] = photo(data["photo_id"], quality="full" if data["original"] else "large")
            row["full_status"], row["full_error"] = "ready", ""
        else:
            row["full_images"] = [photo(37), photo(38, "loading"), photo(39, "loading")]
            row["full_status"] = "loading"
        route.fulfill(json={"message": "queued"})

    page.route("**/api/channel-cover", load)
    page.goto(origin + "/?view=messages")
    page.wait_for_function("document.querySelectorAll('.channel-message').length === 4 && !document.getElementById('scan-channel').disabled")
    page.locator('[data-message-id="40"] .channel-message-cover').click()
    page.wait_for_function("document.querySelectorAll('#channel-preview-strip button').length === 3")
    assert len(calls) == 1, "A legacy cached cover must discover the rest of the album"
    assert page.locator("#channel-preview-count").inner_text() == "3 / 3"
    page.locator('[data-photo-index="0"]').click()
    page.wait_for_function("document.getElementById('channel-preview-image').naturalWidth > 0")
    assert page.locator("#channel-preview-prev").is_disabled()
    page.keyboard.press("ArrowRight")
    assert page.locator("#channel-preview-count").inner_text() == "2 / 3"
    assert page.locator("#channel-preview-image").is_hidden()
    assert "正在加载" in page.locator("#channel-preview-status-text").inner_text()
    row["full_images"] = [photo(37), photo(38, "failed"), photo(39)]
    row["full_status"] = "failed"
    page.locator("#refresh").evaluate("button => button.click()")
    page.wait_for_function("document.getElementById('channel-preview-status').dataset.stage === 'failed'")
    assert "超时" in page.locator("#channel-preview-status-text").inner_text()
    page.locator("#channel-preview-next").click()
    page.wait_for_function("document.getElementById('channel-preview-image').src.includes('photo=39') && document.getElementById('channel-preview-image').naturalWidth > 0")
    assert page.locator("#channel-preview-next").is_disabled()
    page.locator("#channel-zoom-in").click()
    assert page.locator("#channel-zoom-level").inner_text() == "150%"
    page.keyboard.press("ArrowLeft")
    assert page.locator("#channel-zoom-level").inner_text() == "100%"
    page.locator("#channel-preview-retry").click()
    page.wait_for_function("document.getElementById('channel-preview-image').src.includes('photo=38') && document.getElementById('channel-preview-image').naturalWidth > 0")
    assert calls[-1] == {"id": 40, "original": False, "gallery": True, "photo_id": 38}
    page.locator("#channel-preview-original").click()
    page.wait_for_function("document.getElementById('channel-preview-status-text').textContent === '最大尺寸图'")
    assert calls[-1]["photo_id"] == 38 and calls[-1]["original"]
    page.screenshot(path=str(harness.ARTIFACTS / f"channel-gallery-{label}-{unique}.png"))
    dialog = page.locator("#channel-preview-dialog")
    assert dialog.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    assert page.locator("#channel-preview-next").bounding_box()["y"] < page.viewport_size["height"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.keyboard.press("Escape")
    assert not dialog.evaluate("e=>e.open")
    page.locator('[data-message-id="40"] .channel-message-cover').click()
    page.wait_for_function("document.querySelectorAll('#channel-preview-strip button').length === 3")
    assert len(calls) == 3, "Cached galleries must not redownload on reopening"
    page.locator("#channel-preview-close").click()
    page.locator('[data-message-id="30"] .channel-message-cover').click()
    assert page.locator("#channel-preview-gallery").is_hidden()
    page.locator("#channel-preview-close").click()
    page.locator("#channel-select").click()
    page.locator('[data-channel-option][data-channel-key="fixture_other"]').click()
    page.wait_for_function("document.getElementById('channel-selected-title').textContent === 'Other models'")
    page.locator('[data-message-id="40"] .channel-message-cover').click()
    assert page.locator("#channel-preview-gallery").is_hidden()
    assert "source=fixture_other" in page.locator("#channel-preview-image").get_attribute("src")
    assert len(calls) == 3 and not errors, errors
    page.locator("#channel-preview-close").click()
    print(f"{label}: legacy discovery, streaming, navigation, single-image retry, original, cache and scope PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", type=Path, required=True)
    args = parser.parse_args()
    harness.BROWSER = args.browser
    harness.check(check_callback=check_view)
