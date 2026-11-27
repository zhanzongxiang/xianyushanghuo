from __future__ import annotations

import json
from threading import Event
from types import SimpleNamespace

import pytest
import playwright.sync_api

from xianyu_pipeline import quark_login, web_ui
from xianyu_pipeline.browser_download import DownloadCancelled
from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.quark_api import API_URL, QuarkAPIError
from xianyu_pipeline.quark_login import QuarkLoginError, api_cookie_header, login_and_capture_cookie, show_login_window
from test_workbench_actions import app


def cookie(value="PRIVATE-COOKIE", *, domain=".quark.cn", path="/", name="__pus"):
    return {"name": name, "value": value, "domain": domain, "path": path, "httpOnly": True}


def test_api_cookie_header_limits_domains_and_prefers_specific_scope():
    result = api_cookie_header([
        cookie("shared"), cookie("specific", domain="drive-pc.quark.cn"),
        cookie("web-only", domain="pan.quark.cn"), cookie("foreign", domain="other.invalid"),
        cookie("long-path", name="token", path="/1/clouddrive"), cookie("root", name="token"),
    ])
    assert dict(pair.split("=", 1) for pair in result.split("; ")) == {"token": "long-path", "__pus": "specific"}
    assert api_cookie_header([]) == ""


@pytest.mark.parametrize("value", ["bad\nPRIVATE-COOKIE", "bad;PRIVATE-COOKIE"])
def test_cookie_format_errors_do_not_expose_credentials(value):
    with pytest.raises(ValueError) as error:
        api_cookie_header([cookie(value)])
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("window_state", ["normal", "minimized", "maximized"])
def test_login_restores_minimized_window_and_activates_page(window_state):
    calls = []

    class Session:
        def send(self, method, params=None):
            calls.append((method, params))
            return {"windowId": 7, "bounds": {"windowState": window_state}}

        def detach(self):
            calls.append("detach")

    page = SimpleNamespace(bring_to_front=lambda: calls.append("front"))
    context = SimpleNamespace(new_cdp_session=lambda target: Session())
    show_login_window(context, page)
    assert calls[0] == ("Browser.getWindowForTarget", None)
    if window_state == "minimized":
        assert calls[1] == ("Browser.setWindowBounds", {"windowId": 7, "bounds": {"windowState": "normal"}})
    else:
        assert len(calls) == 3
    assert calls[-2:] == ["detach", "front"]


def test_window_restore_failure_does_not_block_login_or_expose_error(caplog):
    calls = []

    def unavailable(page):
        raise RuntimeError("PRIVATE-COOKIE")

    show_login_window(SimpleNamespace(new_cdp_session=unavailable),
                      SimpleNamespace(bring_to_front=lambda: calls.append("front")))
    assert calls == ["front"]
    assert "PRIVATE" not in caplog.text


@pytest.fixture
def browser(monkeypatch, tmp_path):
    state = SimpleNamespace(now=0.0, stages=[], stop=Event(), cookies=[cookie()], checks=[],
                            closed=False, calls=[], wait=None, verify=None, context_closed=False)

    class Page:
        def bring_to_front(self):
            state.calls.append("front")

        def goto(self, url, **options):
            state.calls.append(("goto", url, options))
            if getattr(state, "goto", None):
                state.goto()

        def is_closed(self):
            return state.closed

        def wait_for_timeout(self, milliseconds):
            state.now += milliseconds / 1000
            if state.wait:
                state.wait()

    class Context:
        pages = [Page()]

        def new_cdp_session(self, page):
            return SimpleNamespace(send=lambda method: {"windowId": 7, "bounds": {"windowState": "normal"}},
                                   detach=lambda: None)

        def cookies(self, urls):
            assert urls == [API_URL + "file/sort"]
            return state.cookies

        def close(self):
            state.context_closed = True
            state.closed = True

    context = Context()

    class Playwright:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            state.calls.append("playwright_closed")

    def launch(playwright, profile, executable, **options):
        assert profile == tmp_path and executable == "fixture.exe"
        assert options == {"headless": False, "accept_downloads": False}
        return context

    class API:
        def __init__(self, value, **options):
            assert options == {"timeout_seconds": 10, "stop_event": state.stop}
            self.cookie = value

        def check(self):
            state.checks.append(self.cookie)
            if state.verify:
                state.verify(self)

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", Playwright)
    monkeypatch.setattr(quark_login, "launch_browser_context", launch)
    monkeypatch.setattr(quark_login, "QuarkAPIStorageAdapter", API)
    monkeypatch.setattr(quark_login, "time", SimpleNamespace(monotonic=lambda: state.now))

    def run(timeout=300):
        return login_and_capture_cookie(tmp_path, "fixture.exe", stop_event=state.stop,
                                        on_status=state.stages.append, timeout_seconds=timeout)

    state.run = run
    return state


def test_existing_browser_session_is_verified_and_rotated_cookie_returned(browser, caplog):
    browser.verify = lambda api: setattr(api, "cookie", "__pus=PRIVATE-REFRESHED")
    assert browser.run() == "__pus=PRIVATE-REFRESHED"
    assert browser.checks == ["__pus=PRIVATE-COOKIE"]
    assert browser.context_closed and browser.calls[-1] == "playwright_closed"
    assert browser.calls[0] == "front" and browser.calls[1][0] == "goto"
    assert [row["stage"] for row in browser.stages] == ["opening", "navigating", "waiting", "verifying"]
    assert "PRIVATE" not in caplog.text + json.dumps(browser.stages)


def test_empty_session_waits_for_scan_before_verification(browser):
    browser.cookies = []
    browser.wait = lambda: setattr(browser, "cookies", [cookie()])
    assert browser.run() == "__pus=PRIVATE-COOKIE"
    assert browser.now == .5 and len(browser.checks) == 1


def test_invalid_session_waits_for_new_cookie_and_does_not_leak_rejection(browser, caplog):
    browser.cookies = [cookie("PRIVATE-EXPIRED")]

    def verify(api):
        if "EXPIRED" in api.cookie:
            raise QuarkAPIError("server rejected PRIVATE-EXPIRED")

    browser.verify = verify
    browser.wait = lambda: setattr(browser, "cookies", [cookie("PRIVATE-VALID")])
    assert browser.run() == "__pus=PRIVATE-VALID"
    assert len(browser.checks) == 2
    assert "PRIVATE" not in caplog.text + json.dumps(browser.stages)


def test_unchanged_rejected_cookie_is_throttled_until_timeout(browser):
    def verify(api):
        raise QuarkAPIError("PRIVATE-REJECTION")

    browser.verify = verify
    with pytest.raises(QuarkLoginError, match="超时"):
        browser.run(timeout=2)
    assert len(browser.checks) == 1 and browser.context_closed


@pytest.mark.parametrize("cancel", ["before", "while_waiting", "after_verify", "close_window"])
def test_cancellation_releases_browser_and_returns_no_cookie(browser, cancel):
    if cancel == "before":
        browser.stop.set()
    elif cancel == "after_verify":
        browser.verify = lambda api: browser.stop.set()
    else:
        browser.cookies = []
        browser.wait = browser.stop.set if cancel == "while_waiting" else lambda: setattr(browser, "closed", True)
    with pytest.raises(DownloadCancelled):
        browser.run()
    assert not browser.calls if cancel == "before" else browser.context_closed


def test_browser_error_is_sanitized_and_context_released(browser, caplog):
    def fail(api):
        raise RuntimeError("PRIVATE-COOKIE")

    browser.verify = fail
    with pytest.raises(QuarkLoginError) as error:
        browser.run()
    assert browser.context_closed
    assert "PRIVATE" not in str(error.value) + caplog.text + json.dumps(browser.stages)


@pytest.mark.parametrize("failure", ["timeout", "network", "closed"])
def test_navigation_failure_is_not_cancelled_by_cleanup(browser, caplog, failure):
    def fail_navigation():
        if failure == "closed":
            browser.closed = True
        error = playwright.sync_api.TimeoutError if failure == "timeout" else playwright.sync_api.Error
        raise error("PRIVATE-COOKIE")

    browser.goto = fail_navigation
    with pytest.raises(QuarkLoginError) as error:
        browser.run()
    expected = {"timeout": "加载超时", "network": "加载失败", "closed": "意外关闭"}[failure]
    assert expected in str(error.value)
    assert browser.context_closed and browser.closed
    assert not browser.checks
    assert "阶段=navigating" in caplog.text
    assert "PRIVATE" not in str(error.value) + caplog.text + json.dumps(browser.stages)


def test_launch_failure_reports_executable_problem_without_exposing_error(browser, monkeypatch, caplog):
    def fail_launch(*args, **kwargs):
        raise RuntimeError("PRIVATE-COOKIE")

    monkeypatch.setattr(quark_login, "launch_browser_context", fail_launch)
    with pytest.raises(QuarkLoginError, match="Chrome / Edge 主程序") as error:
        browser.run()
    assert not browser.context_closed
    assert "阶段=opening" in caplog.text
    assert "PRIVATE" not in str(error.value) + caplog.text + json.dumps(browser.stages)


def api_settings(app, **changes):
    return app.post("/api/config", {"upload_provider": "quark_api", "quark_cookie": "",
                                     "quark_api_parent_fid": "0", **changes})


def test_api_config_can_save_before_login_but_account_actions_still_require_cookie(app):
    assert api_settings(app)[0] == 200
    assert not PipelineConfig.from_json(app.config_path).quark_cookie
    assert app.post("/api/quark-api-check")[0] == 400
    assert app.post("/api/quark-folders")[0] == 400
    identity = app.task()
    assert app.post("/api/upload", {"ids": [identity]})[0] == 400
    assert not app.workers


def test_login_starts_without_cookie_and_validated_cookie_is_saved_only_locally(app, monkeypatch, caplog):
    assert api_settings(app)[0] == 200

    def capture(profile, executable, *, stop_event, on_status):
        assert executable == PipelineConfig.from_json(app.config_path).browser_executable_path
        assert stop_event is app.state.stop_event
        assert app.post("/api/config", {"channel_message_limit": 25})[0] == 409
        assert app.post("/api/quark-login")[0] == 409
        assert app.post("/api/telegram-api-login")[0] == 409
        assert app.post("/api/download")[0] == 409
        on_status({"stage": "verifying", "message": "正在验证夸克登录"})
        return "__uid=fixture; __pus=PRIVATE-COOKIE"

    monkeypatch.setattr(web_ui, "login_and_capture_cookie", capture)
    assert app.post("/api/quark-login")[0] == 200
    assert app.get("/api/workbench")[1]["quark_login"]["stage"] == "opening"
    app.run()
    config = PipelineConfig.from_json(app.config_path)
    assert config.quark_cookie == "__uid=fixture; __pus=PRIVATE-COOKIE"
    assert app.store.config.quark_cookie == config.quark_cookie
    result = app.get("/api/workbench")[1]
    assert result["quark_login"]["stage"] == "completed" and result["quark_login"]["saved_at"]
    assert app.get("/api/config")[1]["quark_cookie_configured"]
    assert "PRIVATE" not in json.dumps(result) + json.dumps(app.get("/api/config")) + caplog.text
    assert app.post("/api/config", {"channel_message_limit": 25})[0] == 200


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "stopped_before_save"])
def test_failed_or_cancelled_login_keeps_previous_cookie(app, monkeypatch, outcome):
    assert api_settings(app, quark_cookie="__pus=PRIVATE-OLD")[0] == 200
    before = app.config_path.read_bytes()

    def capture(*args, **options):
        if outcome == "failed":
            raise QuarkLoginError("验证未通过")
        if outcome == "cancelled":
            raise DownloadCancelled("登录已取消")
        app.state.stop_event.set()
        return "__pus=PRIVATE-NEW"

    monkeypatch.setattr(web_ui, "login_and_capture_cookie", capture)
    assert app.post("/api/quark-login")[0] == 200
    app.run()
    assert app.config_path.read_bytes() == before
    assert app.state.quark_login["stage"] == ("failed" if outcome == "failed" else "cancelled")
    if outcome == "failed":
        assert app.state.quark_login["message"] == "验证未通过"


def test_login_checks_browser_and_rejects_unexpected_parameters(app):
    assert api_settings(app, browser_executable_path="")[0] == 200
    assert app.post("/api/quark-login")[0] == 400
    assert app.post("/api/quark-login", {"url": "https://other.invalid/"})[0] == 400
    assert not app.workers


def test_save_failure_keeps_original_cookie_and_reports_failure(app, monkeypatch):
    assert api_settings(app, quark_cookie="__pus=PRIVATE-OLD")[0] == 200
    before = app.config_path.read_bytes()
    monkeypatch.setattr(web_ui, "login_and_capture_cookie", lambda *a, **kw: "__pus=PRIVATE-NEW")

    def fail_save(*args, **kwargs):
        raise PermissionError("配置文件不可写")

    monkeypatch.setattr(web_ui, "write_config", fail_save)
    assert app.post("/api/quark-login")[0] == 200
    app.run()
    assert app.state.quark_login["stage"] == "failed"
    assert app.config_path.read_bytes() == before


def test_web_upload_keeps_original_login_behavior(app, monkeypatch):
    from xianyu_pipeline import quark_browser
    calls = []
    monkeypatch.setattr(quark_browser, "login_and_inspect", lambda *args: calls.append(args))
    monkeypatch.setattr(web_ui, "login_and_capture_cookie", lambda *args, **kw: pytest.fail("API capture in web mode"))
    assert app.post("/api/quark-login")[0] == 200
    app.run()
    assert len(calls) == 1 and not PipelineConfig.from_json(app.config_path).quark_cookie
