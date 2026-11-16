"""Visible, explicitly mapped Qianniu draft saving with receipt verification."""
from __future__ import annotations

import re
import logging
import math
import time
from pathlib import Path

from .browser_download import DownloadCancelled
from .browser_profiles import launch_browser_context
from .product_drafts import missing_fields, validate_qianniu_url
from .qianniu_shop import QIANNIU_HOME_URL, ShopDetector
from .runtime import validate_browser_path

LOGIN_WAIT_SECONDS = 300
SHOP_DETECTION_SECONDS = 30
SHOP_SCAN_INTERVAL_MS = 750


class QianniuSavePending(RuntimeError):
    """The save click may have reached the platform; do not create again."""


def validate_adapter(config):
    if not config.qianniu_shop_name.strip():
        raise ValueError("请先登录千牛识别并确认目标店铺，或在发布模板中填写店铺名称")
    validate_browser_path(config.browser_executable_path)
    validate_qianniu_url(config.qianniu_publish_url, required=True)
    required = {"shop", "title", "price", "stock", "category", "description", "images", "save", "success"}
    if required - set(config.qianniu_form_selectors):
        raise ValueError("千牛发布页面尚未适配：请在设置中配置并核验页面控件，再执行自动保存")


class QianniuDraftAdapter:
    def __init__(self, config, stop_event):
        self.config, self.stop_event = config, stop_event

    def check_stop(self):
        if self.stop_event.is_set():
            raise DownloadCancelled("千牛操作已停止")

    def login(self, on_status=None):
        self.check_stop()
        validate_browser_path(self.config.browser_executable_path)
        url = self.config.qianniu_publish_url or QIANNIU_HOME_URL
        validate_qianniu_url(url, required=True)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            context = launch_browser_context(playwright, self.config.qianniu_browser_profile,
                                             self.config.browser_executable_path, headless=False,
                                             accept_downloads=False)
            try:
                detector = ShopDetector(self.config.qianniu_form_selectors.get("shop", ""))
                context.on("requestfinished", detector.request_finished)
                page = context.pages[0] if context.pages else context.new_page()
                if on_status:
                    on_status({"stage": "waiting", "candidates": [], "error": ""})
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                return self.wait_for_shop(context, detector, on_status)
            finally:
                try:
                    context.close()
                except Exception:
                    logging.warning("千牛登录窗口关闭未完成", exc_info=True)

    def wait_for_shop(self, context, detector, on_status=None):
        from playwright.sync_api import Error

        started = time.monotonic()
        login_deadline = started + LOGIN_WAIT_SECONDS
        recognition_deadline = None
        previous, last_progress = [], None
        while time.monotonic() < login_deadline:
            self.check_stop()
            pages = [item for item in context.pages if not item.is_closed()]
            if not pages:
                raise DownloadCancelled("千牛登录窗口已关闭，店铺识别已停止")
            stage = detector.login_state(pages)
            candidates = [] if stage == "waiting" else detector.scan(pages)
            now = time.monotonic()
            if stage == "waiting":
                recognition_deadline = None
            elif stage == "recognizing" or candidates:
                if recognition_deadline is None:
                    recognition_deadline = min(now + SHOP_DETECTION_SECONDS, login_deadline)
                    logging.info("千牛页面已打开，开始识别店铺，最多等待 %d 秒", SHOP_DETECTION_SECONDS)
            if candidates and candidates == previous:
                logging.info("千牛店铺识别完成，耗时 %.1f 秒", now - started)
                if on_status:
                    on_status({"stage": "detected", "candidates": candidates, "error": "", "remaining_seconds": 0})
                return candidates
            previous = candidates
            if recognition_deadline is not None:
                stage = "recognizing"
                if now >= recognition_deadline:
                    raise ValueError(
                        f"千牛页面已打开，但 {SHOP_DETECTION_SECONDS} 秒内未识别到店铺名称。"
                        "请手动填写店铺名称，或配置店铺名称控件后重试")
            remaining = math.ceil(max(0, (recognition_deadline or login_deadline) - now))
            progress = (stage, remaining)
            if on_status and progress != last_progress:
                on_status({"stage": stage, "candidates": [], "error": "", "remaining_seconds": remaining})
            if last_progress is None or last_progress[0] != stage:
                if stage == "waiting":
                    logging.info("千牛仍显示登录页面，等待在本次打开的窗口中完成登录")
                elif stage == "loading":
                    logging.info("正在等待千牛页面加载")
            last_progress = progress
            try:
                pages[0].wait_for_timeout(SHOP_SCAN_INTERVAL_MS)
            except Error:
                self.check_stop()
                if not pages[0].is_closed():
                    raise
        raise ValueError("等待千牛登录超时（5 分钟）。请在程序打开的千牛窗口完成登录后重试")

    def save(self, row: dict, images: list[Path]):
        validate_adapter(self.config)
        if missing_fields(row, require_shop=True):
            raise ValueError("请补充：" + "、".join(missing_fields(row, require_shop=True)))
        if row["shop_name"] != self.config.qianniu_shop_name.strip():
            raise ValueError("草稿所属店铺与当前发布模板不一致")
        if len(images) != len(row["image_indices"]) or not all(path.is_file() for path in images):
            raise ValueError("所选商品图片缺失，请重新整理资料")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            context = launch_browser_context(playwright, self.config.qianniu_browser_profile,
                                             self.config.browser_executable_path, headless=False,
                                             accept_downloads=False)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(self.config.qianniu_publish_url, wait_until="domcontentloaded", timeout=45000)
                return self.fill_and_save(page, row, images)
            finally:
                try:
                    context.close()
                except Exception:
                    logging.warning("千牛窗口清理未完成，保留已取得的保存结果", exc_info=True)

    def fill_and_save(self, page, row, images):
        self.check_stop()
        selectors = self.config.qianniu_form_selectors

        def unique(name):
            locator = page.locator(selectors[name])
            locator.wait_for(state="attached", timeout=15000)
            if locator.count() != 1:
                raise ValueError("千牛页面控件不唯一：" + name)
            return locator

        validate_qianniu_url(page.url, required=True)
        shop = unique("shop")
        if shop.inner_text(timeout=5000).strip() != row["shop_name"]:
            raise ValueError("当前千牛店铺与草稿目标不一致，请核对登录账号")
        for name in ("title", "price", "stock", "category", "description"):
            self.check_stop()
            control = unique(name)
            value = str(row[name])
            if control.evaluate("(el) => el.tagName") == "SELECT":
                control.select_option(label=value, timeout=10000)
                actual = control.locator("option:checked").inner_text()
            else:
                control.fill(value, timeout=10000)
                actual = control.evaluate("(el) => el.isContentEditable ? el.innerText : el.value")
            if str(actual).strip() != value.strip():
                raise ValueError("千牛字段回读不一致：" + name)
        image_input = unique("images")
        image_input.set_input_files([str(path) for path in images], timeout=15000)
        button = unique("save")
        if not re.fullmatch(r"(?:存为|保存为|保存)草稿", button.inner_text(timeout=5000).strip()):
            raise ValueError("目标按钮未确认为保存草稿，已停止自动操作")
        receipt = page.locator(selectors["success"])
        if receipt.is_visible():
            raise ValueError("页面已存在保存回执，请核对已有草稿")
        self.check_stop()
        validate_qianniu_url(page.url, required=True)
        try:
            # A failed click can still have reached the server.
            button.click(timeout=15000)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if receipt.is_visible():
                    text = receipt.inner_text(timeout=2000).strip()
                    if re.search(r"草稿.{0,10}(?:保存成功|成功)|(?:保存成功|成功).{0,10}草稿", text):
                        validate_qianniu_url(page.url, required=True)
                        return {"remote_url": page.url, "confirmed_by": "receipt"}
                self.check_stop()
                page.wait_for_timeout(250)
            raise RuntimeError("未取得新的草稿保存成功回执")
        except Exception as exc:
            raise QianniuSavePending("保存结果待确认，请先在千牛核对，避免重复创建：" + str(exc)) from exc
