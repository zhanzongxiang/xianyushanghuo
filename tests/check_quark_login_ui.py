"""Offline browser checks for Quark cookie acquisition and login status."""
from __future__ import annotations

from check_channel_picker_ui import ARTIFACTS, BROWSER, assert_no_overflow, check, refresh


def check_login(page, fixture, origin, label, unique):
    errors, starts = [], []
    fixture.config.update(upload_provider="quark_api", quark_cookie_configured=False,
                          browser_executable_path="")
    fixture.workbench["quark_login"] = {"stage": "idle", "message": "", "saved_at": ""}
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", fixture.route)
    page.route("**/api/drafts", lambda route: route.fulfill(json={"drafts": [], "template": {}, "running": False}))

    def start(route):
        starts.append(route.request.post_data_json)
        fixture.workbench.update(running=True, action="quark-login", error="",
                                 task_id=f"fixture-quark-login-{len(starts)}", stop_requested=False)
        fixture.workbench["quark_login"] = {"stage": "waiting", "message": "请在夸克窗口扫码登录", "saved_at": ""}
        route.fulfill(json={"message": "任务已启动"})

    page.route("**/api/quark-login", start)
    page.goto(origin + "/?view=settings")
    page.wait_for_function("!document.getElementById('quark-api-login').disabled")
    assert not starts and not fixture.posts
    assert page.locator("#browser-path-field").is_visible()
    assert page.locator("#quark-login").is_hidden()
    assert not page.locator("#quark_cookie").get_attribute("required")
    page.locator("#quark-api-login").click()
    assert "浏览器程序路径" in page.locator("#form-error").inner_text()
    assert not starts
    page.locator("#browser_executable_path").fill(str(BROWSER))
    page.locator("#save-config").click()
    page.wait_for_function("document.getElementById('config-dirty').textContent === ''")
    assert fixture.posts[-1][0] == "/api/config"
    assert "quark_cookie" not in fixture.posts[-1][1]
    page.locator("#quark-api-login").click()
    page.wait_for_function("document.getElementById('quark-api-login-status').textContent.includes('扫码登录')")
    assert starts == [{}]
    assert page.locator("#quark-api-login").is_disabled()
    assert page.locator("#save-config").is_disabled()
    assert page.locator("#stop").is_visible()
    assert page.locator("#quark-api-login-status").get_attribute("aria-busy") == "true"
    assert_no_overflow(page, label + ": Quark waiting status overflow")
    page.locator("#quark-api-login").scroll_into_view_if_needed()
    page.screenshot(path=str(ARTIFACTS / f"quark-login-waiting-{unique}-{label}.png"))

    fixture.workbench["quark_login"].update(stage="verifying", message="正在验证夸克登录及目录读取权限")
    refresh(page, fixture)
    assert "正在验证" in page.locator("#quark-api-login-status").inner_text()
    # Typing an unrelated setting while waiting must not be overwritten by success.
    page.locator("#quark_api_parent_fid").fill("unsaved-target")
    fixture.config["quark_cookie_configured"] = True
    fixture.workbench.update(running=False)
    fixture.workbench["quark_login"].update(stage="completed", message="扫码登录成功，Cookie 已验证并保存到本机",
                                           saved_at="2026-11-25T01:00:00Z")
    refresh(page, fixture)
    page.wait_for_function("document.getElementById('quark-cookie-status').textContent === '已保存'")
    assert page.locator("#quark_cookie").input_value() == "********"
    assert page.locator("#quark_api_parent_fid").input_value() == "unsaved-target"
    assert page.locator("#config-dirty").inner_text()
    assert page.locator("#quark-api-login").is_enabled()
    assert page.locator("#quark-api-login-status").get_attribute("aria-busy") == "false"
    assert_no_overflow(page, label + ": Quark success status overflow")
    page.locator("#quark-api-login").scroll_into_view_if_needed()
    page.screenshot(path=str(ARTIFACTS / f"quark-login-success-{unique}-{label}.png"))

    page.locator("#quark-api-login").click()
    page.wait_for_function("document.getElementById('quark-api-login-status').textContent.includes('扫码登录')")
    page.locator("#stop").click()
    fixture.workbench["quark_login"].update(stage="cancelled", message="夸克登录已取消，原 Cookie 已保留")
    refresh(page, fixture)
    assert "已取消" in page.locator("#quark-api-login-status").inner_text()
    assert page.locator("#quark_cookie").input_value() == "********"
    assert page.locator("#quark-api-login").is_enabled()
    fixture.workbench["quark_login"].update(stage="failed", message="夸克登录未完成，请重试；原 Cookie 已保留")
    refresh(page, fixture)
    assert "重试" in page.locator("#quark-api-login-status").inner_text()
    assert all(path in {"/api/config", "/api/stop"} for path, _ in fixture.posts)
    assert not errors, errors
    print(f"{label}: first login, browser path, status, masking, unsaved edits, cancel and retry PASS")


if __name__ == "__main__":
    check(check_callback=check_login)
