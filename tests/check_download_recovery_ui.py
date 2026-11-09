"""Offline checks for cover previews, speed phases and resumable download controls."""
from __future__ import annotations

from check_channel_picker_ui import ARTIFACTS, Fixture, assert_no_overflow, avatar_png, check, refresh


def check_recovery(page, fixture: Fixture, origin: str, label: str, unique: str):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    task = {
        "id": "resume-fixture", "chat": "fixture_saved", "message_id": 77,
        "filename": "@Pixel_3D_STL Qifrey Witch Hat Atelier.rar", "title": "Qifrey Witch Hat Atelier",
        "size": 936 * 1024 ** 2, "received": 635 * 1024 ** 2,
        "download_status": "downloading", "upload_status": "waiting", "validation": "pending",
        "speed": 2 * 1024 ** 2, "average_speed": 1.5 * 1024 ** 2, "eta": 150,
        "resumable": True, "resumed_bytes": 600 * 1024 ** 2, "download_phase": "transferring",
        "image_source": "channel_cover", "image_message_ids": [76],
        "images": [{"url": "/api/preview/resume-fixture/0", "name": "channel-cover.png"}],
    }
    fixture.workbench["tasks"] = [task]
    fixture.workbench.update(running=True, action="channel-download", task_id="fixture-download")

    def route(request):
        path = request.request.url.split(origin, 1)[-1].split("?", 1)[0]
        if path.startswith("/api/preview/"):
            request.fulfill(content_type="image/png", body=avatar_png())
        elif path == "/api/retry":
            fixture.posts.append((path, request.request.post_data_json))
            request.fulfill(json={"message": "fixture retry"})
        else:
            fixture.route(request)

    page.route("**/api/**", route)
    page.goto(origin + "/?view=download")
    row = page.locator("[data-task-id='resume-fixture']")
    row.wait_for(state="visible")
    page.wait_for_function("document.querySelector('.thumb-button img')?.naturalWidth > 0")
    row.locator(".task-title").click()
    assert "平均 1.5 MB/s" in row.locator(".task-progress").inner_text()
    assert "已恢复 600 MB" in row.locator(".task-progress").inner_text()
    assert page.locator("#detail-image-source").inner_text() == "频道已缓存封面"
    page.wait_for_function("document.querySelector('#preview img')?.naturalWidth > 0")
    page.keyboard.press("Escape")

    retry_note = "正在重建下载连接 1/3，并发降至 4，保留已下载分块"
    for phase, text in (("waiting", "等待 Telegram 数据"), ("retrying", retry_note)):
        task.update(speed=0, eta=None, download_phase=phase,
                    download_note=retry_note if phase == "retrying" else "")
        refresh(page, fixture)
        assert text in row.locator(".task-progress").inner_text()
        assert_no_overflow(page, f"{label}: {phase} progress")

    fixture.workbench.update(running=False, action="")
    task.update(download_status="interrupted", download_note="", error="连接中断，分块断点已保留")
    refresh(page, fixture)
    assert row.locator(".row-retry").inner_text() == "继续下载"
    row.locator(".task-title").click()
    assert page.locator("#retry-task").inner_text() == "继续下载"
    page.keyboard.press("Escape")
    previous = len(fixture.posts)
    row.locator(".row-retry").click()
    page.wait_for_function("!document.getElementById('refresh').disabled")
    assert len(fixture.posts) == previous + 1
    assert fixture.posts[-1] == ("/api/retry", {"id": task["id"], "auto_upload_after_download": False})
    assert_no_overflow(page, f"{label}: resume controls")
    page.evaluate("window.scrollTo(0, 0)")
    screenshot = ARTIFACTS / f"download-recovery-{unique}-{label}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    print(screenshot.resolve())
    assert not errors, errors


if __name__ == "__main__":
    check(check_callback=check_recovery)
