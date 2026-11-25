"""Offline browser checks for Telethon QR and phone authorization."""
from __future__ import annotations

import io
import json

import qrcode

from check_channel_picker_ui import ARTIFACTS, assert_no_overflow, check, refresh


def check_login(page, fixture, origin, label, unique):
    errors, posts = [], []
    state = {"state": "idle", "active": False, "authorized": False, "auth_pending": False,
             "error": "", "ticket": "", "method": "qr"}
    failed = True
    generation = 0
    output = io.BytesIO()
    qrcode.make("tg://login?token=offline-ui-fixture").save(output, format="PNG")
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", fixture.route)
    page.route("**/api/drafts", lambda route: route.fulfill(json={"drafts": [], "template": {}, "running": False}))

    def route(request):
        nonlocal generation
        path = request.request.url.split("?", 1)[0].split(origin)[-1]
        if path == "/api/telegram-auth/status":
            request.fulfill(json=state)
        elif path == "/api/telegram-auth/qr":
            request.fulfill(content_type="image/png", body=output.getvalue(), headers={"Cache-Control": "no-store"})
        elif path == "/api/telegram-api-login":
            payload = request.request.post_data_json
            posts.append((path, payload))
            if failed:
                request.fulfill(status=400, json={"message": "fixture proxy unavailable"})
                return
            generation += 1
            state.update(state="qr" if payload["method"] == "qr" else "phone", active=True,
                         authorized=False, method=payload["method"], error="", auth_pending=False,
                         ticket=f"fixture-{generation}", qr_url=f"/api/telegram-auth/qr?ticket=fixture-{generation}&v=1", expires_in=25)
            request.fulfill(json=state)
        elif path == "/api/telegram-auth/auth":
            payload = request.request.post_data_json
            posts.append((path, payload))
            if payload["field"] == "phone":
                state.update(state="code", error="")
            elif payload["field"] == "code":
                state.update(state="password", error="")
            elif payload["value"] == "WRONG-PRIVATE":
                state.update(state="password", error="两步验证密码不正确，请重新输入")
            else:
                state.update(state="ready", active=False, authorized=True, error="")
            request.fulfill(json=state)
        elif path == "/api/telegram-auth/cancel":
            posts.append((path, request.request.post_data_json))
            state.update(state="cancelled", active=False, auth_pending=False, authorized=False, error="")
            request.fulfill(json=state)
        else:
            request.fallback()

    page.route("**/api/telegram-auth/**", route)
    page.route("**/api/telegram-api-login", route)
    page.goto(origin + "/?view=settings")
    page.wait_for_function("!document.getElementById('config-fields').disabled")
    assert posts == []
    page.locator("#telegram-login").click()
    page.locator("#telegram-auth-dialog").wait_for(state="visible")
    page.wait_for_function("document.getElementById('telegram-auth-error').textContent.includes('proxy unavailable')")
    page.wait_for_timeout(1300)
    assert page.locator("#telegram-auth-error").is_visible()
    assert "proxy unavailable" in page.locator("#telegram-auth-error").inner_text()
    failed = False
    page.locator("#telegram-auth-retry").click()
    image = page.locator("#telegram-auth-image")
    page.wait_for_function("document.getElementById('telegram-auth-image').naturalWidth > 0")
    assert image.is_visible() and not page.locator("#telegram-auth-field").is_visible()
    assert posts[-1] == ("/api/telegram-api-login", {"method": "qr"})
    assert "25" in page.locator("#telegram-auth-expiry").inner_text()
    assert_no_overflow(page, label + ": Telegram QR dialog overflow")
    page.screenshot(path=str(ARTIFACTS / f"telegram-login-qr-{unique}-{label}.png"))
    state.update(qr_url=f"/api/telegram-auth/qr?ticket=fixture-{generation}&v=2", expires_in=30)
    page.wait_for_function("document.getElementById('telegram-auth-image').getAttribute('src').endsWith('v=2')")
    page.locator("#telegram-auth-phone").click()
    field = page.locator("#telegram-auth-value")
    field.wait_for(state="visible")
    assert field.get_attribute("type") == "tel"
    assert posts[-2][0] == "/api/telegram-auth/cancel"
    field.fill("+8613800000000")
    page.locator("#telegram-auth-submit").click()
    page.wait_for_function("document.getElementById('telegram-auth-label').textContent === '登录验证码'")
    field.fill("12345")
    page.locator("#telegram-auth-submit").click()
    page.wait_for_function("document.getElementById('telegram-auth-value').type === 'password'")
    field.fill("WRONG-PRIVATE")
    page.locator("#telegram-auth-submit").click()
    page.wait_for_function("document.getElementById('telegram-auth-error').textContent.includes('不正确')")
    assert field.input_value() == "" and not page.locator("#telegram-auth-submit").is_disabled()
    assert "WRONG-PRIVATE" not in page.locator("body").inner_text()
    assert_no_overflow(page, label + ": Telegram password dialog overflow")
    page.screenshot(path=str(ARTIFACTS / f"telegram-login-password-{unique}-{label}.png"))
    field.fill("PRIVATE-PASSWORD")
    page.locator("#telegram-auth-submit").click()
    page.locator("#telegram-auth-dialog").wait_for(state="hidden")
    assert "登录成功" in page.locator("#notice").inner_text()
    assert field.input_value() == "" and image.get_attribute("src") is None
    assert posts[-1][0] == "/api/telegram-auth/auth", "Successful login was cancelled"
    assert "PRIVATE" not in json.dumps(fixture.config)
    count = len(posts)
    page.locator("#telegram-login").click()
    image.wait_for(state="visible")
    assert len(posts) == count + 1 and posts[-1][0] == "/api/telegram-api-login", "Cached success skipped session validation"
    page.keyboard.press("Escape")
    page.wait_for_function("!document.getElementById('telegram-auth-dialog').open")
    page.wait_for_timeout(200)

    # A page refresh must let the user reopen and finish an ongoing login.
    state.update(state="qr", active=True, authorized=False, auth_pending=False, error="")
    fixture.workbench["telegram_login"] = dict(state)
    refresh(page, fixture)
    count = len(posts)
    assert page.locator("#telegram-login").is_enabled()
    page.locator("#telegram-login").click()
    image.wait_for(state="visible")
    assert len(posts) == count, "Reopening created a second login attempt"
    page.keyboard.press("Escape")
    page.wait_for_function("!document.getElementById('telegram-auth-dialog').open")
    page.wait_for_timeout(200)
    assert posts[-1][0] == "/api/telegram-auth/cancel"
    assert field.input_value() == "" and image.get_attribute("src") is None

    fixture.workbench["telegram_login"] = dict(state)
    refresh(page, fixture)
    page.locator("#telegram-login").click()
    image.wait_for(state="visible")
    state.update(state="password", qr_url="")
    page.wait_for_function("document.getElementById('telegram-auth-value').type === 'password'")
    field.fill("UNSUBMITTED-SECRET")
    page.locator("#telegram-auth-close").click()
    page.wait_for_function("document.getElementById('telegram-auth-value').value === ''")
    assert field.input_value() == ""
    assert not any(value == "UNSUBMITTED-SECRET" for _, payload in posts for value in payload.values())
    assert not any(path in {"/api/channel-download", "/api/channel-scan", "/api/run"} for path, _ in fixture.posts)
    assert not errors, errors


if __name__ == "__main__":
    check(check_callback=check_login)
