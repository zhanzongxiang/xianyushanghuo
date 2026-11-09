"""Offline browser checks for TDLib engine selection and authorization."""
from __future__ import annotations

import json

from check_channel_picker_ui import ARTIFACTS, avatar_png, assert_no_overflow, check, refresh


def check_tdlib(page, fixture, origin, label, unique):
    errors, posts = [], []
    status = {"available": True, "version": "1.8.69", "state": "idle",
              "authorized": False, "error": "", "auth_pending": False}
    fixture.config["telegram_download_engine"] = "telethon"
    fail_login, fail_save, fail_status = True, True, False
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", fixture.route)

    def route(request):
        path = request.request.url.split("?", 1)[0].split(origin)[-1]
        if path == "/api/tdlib/status":
            if fail_status:
                request.fulfill(status=503, json={"message": "fixture status unavailable"})
                return
            request.fulfill(json=status)
        elif path == "/api/tdlib/qr":
            request.fulfill(content_type="image/png", body=avatar_png())
        elif path == "/api/tdlib/login":
            payload = request.request.post_data_json
            posts.append((path, payload))
            if fail_login:
                request.fulfill(status=400, json={"message": "fixture proxy port unavailable"})
                return
            status.update(state="qr" if payload["method"] == "qr" else "phone",
                          qr_url="/api/tdlib/qr?v=fixture", error="")
            request.fulfill(json=status)
        elif path == "/api/tdlib/auth":
            payload = request.request.post_data_json
            posts.append((path, payload))
            if payload["field"] == "phone":
                status.update(state="code")
            elif payload["field"] == "code":
                status.update(state="password", hint="fixture hint")
            else:
                status.update(auth_pending=True)
            request.fulfill(json=status)
        else:
            request.fallback()

    page.route("**/api/tdlib/**", route)

    def config_route(request):
        if request.request.method == "POST" and fail_save:
            request.fulfill(status=400, json={"message": "fixture settings could not be saved"})
        else:
            request.fallback()

    page.route("**/api/config", config_route)
    page.goto(origin + "/?view=settings")
    engine = page.locator("#telegram_download_engine")
    page.wait_for_function("!document.getElementById('config-fields').disabled")
    assert engine.input_value() == "telethon"
    assert page.locator("#download_workers").is_visible()
    assert page.locator("#tdlib-controls").is_hidden()
    engine.select_option("tdlib")
    assert page.locator("#download_workers").is_hidden()
    assert page.locator("#download-crypto-info").is_hidden()
    assert page.locator("#tdlib-controls").is_visible()
    page.locator("#tdlib-login").click()
    assert page.locator("#tdlib-dialog").is_visible()
    assert "未保存" in page.locator("#tdlib-auth-error").inner_text()
    assert page.locator("#tdlib-save-config").is_visible()
    assert page.locator("#tdlib-login-methods").is_hidden()
    assert posts == [], "Unsaved settings started authorization"
    assert fixture.config["telegram_download_engine"] == "telethon", "Opening login saved unrelated settings"
    error = page.locator("#tdlib-auth-error")
    assert error.evaluate("(el) => { const box = el.getBoundingClientRect(); return box.top >= 0 && box.bottom <= innerHeight; }")
    assert_no_overflow(page, label + ": unsaved settings feedback overflow")
    page.screenshot(path=str(ARTIFACTS / f"tdlib-unsaved-{unique}-{label}.png"))
    page.locator("#tdlib-save-config").click()
    page.wait_for_function("document.getElementById('tdlib-auth-error').textContent.includes('could not be saved')")
    assert page.locator("#tdlib-save-config").is_visible() and not posts
    fail_save = False
    page.locator("#tdlib-save-config").click()
    page.wait_for_function("document.getElementById('config-dirty').textContent === ''")
    assert fixture.config["telegram_download_engine"] == "tdlib"
    page.locator("#tdlib-qr-login").wait_for(state="visible")
    page.locator("#tdlib-qr-login").click()
    page.wait_for_function("document.getElementById('tdlib-auth-error').textContent.includes('proxy port unavailable')")
    page.wait_for_timeout(2000)
    assert "proxy port unavailable" in error.inner_text() and error.is_visible(), "Status polling erased the login error"
    fail_login = False
    page.locator("#tdlib-qr-login").click()
    image = page.locator("#tdlib-qr-image")
    page.wait_for_function("document.getElementById('tdlib-qr-image').naturalWidth > 0")
    assert image.is_visible()
    assert_no_overflow(page, label + ": QR dialog overflow")
    page.screenshot(path=str(ARTIFACTS / f"tdlib-login-{unique}-{label}.png"), full_page=True)
    status.update(state="qr_pending", error="fixture proxy unavailable")
    page.wait_for_function("document.getElementById('tdlib-auth-error').textContent.includes('proxy unavailable')")
    assert page.locator("#tdlib-qr-login").is_visible(), "Failed QR request has no retry"
    page.locator("#tdlib-phone-login").click()
    field = page.locator("#tdlib-auth-value")
    field.wait_for(state="visible")
    assert field.get_attribute("type") == "tel"
    field.fill("+8613800000000")
    page.locator("#tdlib-auth-submit").click()
    page.wait_for_function("document.getElementById('tdlib-auth-label').textContent === '登录验证码'")
    field.fill("12345")
    page.locator("#tdlib-auth-submit").click()
    page.wait_for_function("document.getElementById('tdlib-auth-value').type === 'password'")
    field.fill("PRIVATE-PASSWORD")
    page.locator("#tdlib-auth-submit").click()
    page.wait_for_function("document.getElementById('tdlib-auth-submit').disabled")
    assert field.input_value() == ""
    assert "PRIVATE-PASSWORD" not in page.locator("body").inner_text()
    assert posts[-1] == ("/api/tdlib/auth", {"field": "password", "value": "PRIVATE-PASSWORD"})
    assert_no_overflow(page, label + ": password dialog overflow")
    status.update(auth_pending=False, error="fixture verification failed")
    page.wait_for_function("document.getElementById('tdlib-auth-error').textContent.includes('verification failed')")
    page.wait_for_function("!document.getElementById('tdlib-auth-submit').disabled")
    field.fill("UNSUBMITTED-SECRET")
    page.keyboard.press("Escape")
    page.wait_for_function("""!document.getElementById('tdlib-dialog').open
        && document.getElementById('tdlib-auth-value').value === ''
        && !document.getElementById('tdlib-qr-image').hasAttribute('src')""")
    assert field.input_value() == "" and image.get_attribute("src") is None
    page.locator("#tdlib-login").click()
    status.update(state="ready", authorized=True, error="")
    page.locator("#tdlib-dialog").wait_for(state="hidden")
    assert "已登录" in page.locator("#tdlib-status").inner_text()
    assert "PRIVATE" not in json.dumps(fixture.config)
    assert not any(path in {"/api/channel-download", "/api/channel-scan", "/api/run"}
                   for path, _ in fixture.posts)
    status.update(state="idle", authorized=False)
    fail_status = True
    page.locator("#tdlib-login").click()
    page.wait_for_function("document.getElementById('tdlib-auth-error').textContent.includes('status unavailable')")
    fail_status = False
    page.locator("#tdlib-auth-error").wait_for(state="hidden")
    page.locator("#tdlib-close").click()
    page.locator("#tdlib-dialog").wait_for(state="hidden")
    fixture.workbench.update(running=True, action="channel-download", task_id="other-download")
    refresh(page, fixture)
    before = len(posts)
    page.locator("#tdlib-login").click()
    page.wait_for_function("document.getElementById('tdlib-auth-error').textContent.includes('当前任务结束')")
    assert page.locator("#tdlib-login-methods").is_hidden() and len(posts) == before
    assert page.locator("#tdlib-save-config").is_hidden()
    page.locator("#tdlib-close").click()
    page.locator("#tdlib-dialog").wait_for(state="hidden")
    fixture.workbench.update(running=False, action="")
    refresh(page, fixture)
    engine.select_option("telethon")
    assert page.locator("#download_workers").is_visible()
    assert page.locator("#tdlib-controls").is_hidden()
    assert_no_overflow(page, label + ": settings overflow")
    assert not errors, errors


if __name__ == "__main__":
    check(check_callback=check_tdlib)
