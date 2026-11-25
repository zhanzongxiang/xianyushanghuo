"""Acquire API credentials through Quark's visible web login window."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from threading import Event

from .browser_download import DownloadCancelled
from .browser_profiles import launch_browser_context
from .quark_api import API_URL, QuarkAPIError, QuarkAPIStorageAdapter, validate_cookie


LOGIN_URL = "https://pan.quark.cn/list"
LOGIN_TIMEOUT = 300


class QuarkLoginError(RuntimeError):
    pass


def api_cookie_header(cookies: list[dict]) -> str:
    # BrowserContext.cookies filters by the API URL, including HttpOnly cookies.
    # Prefer the most specific path/domain if the browser has duplicate names.
    values = {}
    for cookie in sorted(cookies, key=lambda row: (
            -len(row.get("path", "/")), -len(row.get("domain", "").lstrip(".")))):
        domain = cookie.get("domain", "").lstrip(".").lower()
        if domain not in {"quark.cn", "drive-pc.quark.cn"}:
            continue
        values.setdefault(cookie["name"], cookie["value"])
    return validate_cookie("; ".join(f"{name}={value}" for name, value in values.items()), required=False)


def show_login_window(context, page) -> None:
    session = None
    try:
        session = context.new_cdp_session(page)
        window = session.send("Browser.getWindowForTarget")
        if window["bounds"].get("windowState") == "minimized":
            session.send("Browser.setWindowBounds", {
                "windowId": window["windowId"], "bounds": {"windowState": "normal"},
            })
    except Exception as exc:
        logging.warning("恢复夸克登录窗口失败，错误类型=%s", type(exc).__name__)
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass
    page.bring_to_front()


def login_and_capture_cookie(profile_dir: Path, browser_executable_path: str, *,
                             stop_event: Event | None = None, on_status=None,
                             timeout_seconds: float = LOGIN_TIMEOUT) -> str:
    stop = stop_event if stop_event is not None else Event()

    def check_stop():
        if stop.is_set():
            raise DownloadCancelled("夸克登录已取消，原 Cookie 已保留")

    def report(stage, message):
        if on_status is not None:
            on_status({"stage": stage, "message": message})

    check_stop()
    report("opening", "正在打开夸克登录窗口")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise QuarkLoginError("缺少 Playwright，请安装 requirements.txt 后重试") from None

    deadline = time.monotonic() + timeout_seconds
    last_cookie, next_check = "", 0.0
    try:
        with sync_playwright() as playwright:
            context = launch_browser_context(playwright, profile_dir, browser_executable_path,
                                             headless=False, accept_downloads=False)
            try:
                check_stop()
                page = context.pages[0] if context.pages else context.new_page()
                show_login_window(context, page)
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                report("waiting", "请在打开的夸克窗口使用夸克 App 扫码登录")
                while time.monotonic() < deadline:
                    check_stop()
                    if page.is_closed():
                        raise DownloadCancelled("夸克登录窗口已关闭，原 Cookie 已保留")
                    cookie = api_cookie_header(context.cookies([API_URL + "file/sort"]))
                    if cookie and (cookie != last_cookie or time.monotonic() >= next_check):
                        last_cookie = cookie
                        report("verifying", "正在验证夸克登录及目录读取权限")
                        api = QuarkAPIStorageAdapter(cookie, timeout_seconds=10, stop_event=stop)
                        try:
                            api.check()
                        except QuarkAPIError:
                            next_check = time.monotonic() + 10
                            report("waiting", "等待扫码确认；若已登录，请检查夸克网络连接")
                        else:
                            check_stop()
                            return validate_cookie(api.cookie)
                    page.wait_for_timeout(500)
                raise QuarkLoginError("夸克登录等待超时或 API 验证未通过，请重新扫码；原 Cookie 已保留")
            finally:
                context.close()
    except (DownloadCancelled, QuarkLoginError):
        raise
    except Exception as exc:
        # Browser/HTTP errors can carry headers or tokens; never expose their text.
        logging.warning("夸克扫码登录失败，错误类型=%s", type(exc).__name__)
        if stop.is_set() or ("page" in locals() and page.is_closed()):
            raise DownloadCancelled("夸克登录已取消，原 Cookie 已保留") from None
        raise QuarkLoginError("夸克登录窗口异常，请检查浏览器路径和网络后重试；原 Cookie 已保留") from None
