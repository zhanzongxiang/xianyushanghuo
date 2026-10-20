"""基于网页控件的夸克上传；不调用未公开的网盘接口。"""
from __future__ import annotations

import re
import logging
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from .browser_profiles import launch_browser_context

class QuarkBrowserError(RuntimeError):
    pass


class QuarkBrowserStorageAdapter:
    """使用独立持久化浏览器配置；首次交互登录，后续可无头运行。

    网页控件选择器必须由用户在目标账号页面实测后配置，不猜测 DOM。
    link_pending 表示“上传结果/分享链接待确认”，不能据此再次上传。
    """

    def __init__(self, *, folder_url: str, profile_dir: Path, headless: bool,
                 upload_input_selector: str, file_row_selector: str,
                 share_button_selector: str, share_link_selector: str,
                 timeout_seconds: int = 180, browser_executable_path: str = "",
                 share_create_button_selector: str = "", folder_ready_selector: str = "",
                 share_expire_days: int | None = None,
                 on_stage: Callable[[str], None] | None = None,
                 on_progress: Callable[[int, int, float, float | None], None] | None = None):
        parsed = urlparse(folder_url)
        if (parsed.scheme != "https" or parsed.netloc.lower() not in {"pan.quark.cn", "drive.quark.cn"}
                or re.search(r"[\s\\]", folder_url)):
            raise ValueError("quark_folder_url 必须是夸克网盘的 HTTPS 文件夹地址")
        required = (upload_input_selector, file_row_selector, share_button_selector, share_link_selector)
        if not all(required):
            raise ValueError("网页上传需要配置经实际页面确认的上传控件、文件行、分享按钮及链接选择器")
        if share_expire_days is not None and (type(share_expire_days) is not int or share_expire_days not in {0, 1, 7, 30}):
            raise ValueError("分享有效期仅支持1、7、30天或0（永久）")
        if share_expire_days is not None and not share_create_button_selector:
            raise ValueError("指定有效期必须配置创建分享按钮，不能依赖默认分享")
        self.share_expire_days = share_expire_days
        self.folder_url = folder_url
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.upload_input_selector = upload_input_selector
        self.file_row_selector = file_row_selector
        self.share_button_selector = share_button_selector
        self.share_link_selector = share_link_selector
        self.share_create_button_selector = share_create_button_selector
        self.folder_ready_selector = folder_ready_selector
        self.timeout_ms = timeout_seconds * 1000
        self.browser_executable_path = browser_executable_path
        self.on_stage = on_stage
        self.on_progress = on_progress

    def _report_stage(self, stage: str) -> None:
        if callable(self.on_stage):
            try:
                self.on_stage(stage)
            except Exception:
                logging.warning("夸克阶段回调失败：%s", stage, exc_info=True)

    def upload(self, local_path: Path, remote_folder: str) -> str:
        if not local_path.is_file():
            raise FileNotFoundError(local_path)
        return self._run(local_path, remote_folder, upload=True)

    def share_existing(self, local_path: Path, remote_folder: str) -> str:
        """仅按文件名确认已有文件的分享链接；本地文件可以已不存在。"""
        self._report_stage("link_pending")
        return self._run(local_path, remote_folder, upload=False)

    def _run(self, local_path: Path, remote_folder: str, *, upload: bool) -> str:
        if remote_folder and remote_folder != self.folder_url:
            # 文件夹地址是唯一上传目标；标签与 URL 不等时避免错误目录上传。
            raise QuarkBrowserError("cloud_folder 与 quark_folder_url 不匹配，请只保留实际文件夹 URL 或清空 cloud_folder")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise QuarkBrowserError("缺少 playwright 依赖，请重新构建便携包") from exc
        with sync_playwright() as playwright:
            context = launch_browser_context(playwright, self.profile_dir, self.browser_executable_path,
                                              headless=self.headless, accept_downloads=False)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(self.timeout_ms)
                page.goto(self.folder_url, wait_until="domcontentloaded")
                if urlparse(page.url) != urlparse(self.folder_url):
                    raise QuarkBrowserError("未进入目标文件夹；请先运行可见窗口登录并复制实际文件夹地址")
                if self.folder_ready_selector:
                    page.locator(self.folder_ready_selector).wait_for(state="visible", timeout=self.timeout_ms)
                if upload:
                    upload_input = page.locator(self.upload_input_selector)
                    if upload_input.count() != 1:
                        raise QuarkBrowserError("上传文件控件不存在或不唯一；请检查登录状态与页面选择器")
                    # 在上传前检查同名文件，禁止误把旧文件的分享链接当作本次结果。
                    rows = page.locator(self.file_row_selector).filter(has_text=local_path.name)
                    if rows.count():
                        raise QuarkBrowserError(f"目标目录已有同名文件，请人工核对后处理：{local_path.name}")
                    resolved_path = str(local_path.resolve())
                    self._report_stage("uploading")
                    try:
                        upload_input.set_input_files(resolved_path)
                    finally:
                        # 控件即使抛错也可能已发送文件；后续只能确认已有文件，不能重传。
                        self._report_stage("link_pending")
                result = self._share_file(page, local_path.name)
            finally:
                context.close()
        self._report_stage("completed")
        return result

    def _share_file(self, page, filename: str) -> str:
        rows = page.locator(self.file_row_selector).filter(has_text=filename)
        if rows.count() > 1:
            raise QuarkBrowserError("目标目录同名文件行不唯一，无法确认分享文件")
        rows.first.wait_for(state="visible", timeout=self.timeout_ms)
        if rows.count() != 1:
            raise QuarkBrowserError("目标目录同名文件行不存在或不唯一，无法确认分享文件")
        row = rows
        # has_text 是子串匹配；只接受整行或行内唯一的完整文件名文本。
        if (row.inner_text().strip() != filename
                and row.get_by_text(filename, exact=True).count() != 1):
            raise QuarkBrowserError("目标目录文件名无法精确确认，请人工核对同名文件")
        share_button = row.locator(self.share_button_selector)
        if share_button.count() != 1:
            raise QuarkBrowserError("文件行中找不到唯一的分享按钮，请人工检查上传状态")
        if self.share_create_button_selector:
            row.hover()
        share_button.click()
        if self.share_create_button_selector:
            create_button = page.locator(self.share_create_button_selector)
            create_button.wait_for(state="visible", timeout=self.timeout_ms)
            if create_button.count() != 1:
                raise QuarkBrowserError("创建分享按钮不存在或不唯一，未继续创建分享")
            if self.share_expire_days is not None:
                label = "永久有效" if self.share_expire_days == 0 else f"{self.share_expire_days}天"
                option = page.locator(".ant-modal:visible label.ant-radio-button-wrapper").filter(
                    has_text=re.compile(rf"^{re.escape(label)}$"))
                if option.count() != 1:
                    raise QuarkBrowserError("有效期选项不存在或不唯一，未创建分享")
                option.click()
                if not option.locator("input[type=radio]").is_checked():
                    raise QuarkBrowserError("有效期选择未生效，未创建分享")
            create_button.click()
        link = page.locator(self.share_link_selector)
        if link.count() > 1:
            raise QuarkBrowserError("分享链接控件不唯一")
        link.wait_for(state="visible", timeout=self.timeout_ms)
        if link.count() != 1:
            raise QuarkBrowserError("分享链接控件不存在或不唯一")
        result = link.input_value() if link.evaluate("el => 'value' in el") else link.inner_text()
        url = self._share_url(result)
        if self.share_expire_days is not None:
            expected = "永久有效" if self.share_expire_days == 0 else f"{self.share_expire_days}天后失效"
            if expected not in result:
                raise QuarkBrowserError("分享已创建但有效期未确认，保持链接待核对；不要重新上传文件")
        return url

    @staticmethod
    def _share_url(value: str) -> str:
        # 校验整个 URL，避免从恶意域名、嵌套 URL 或畸形路径中截取“有效”前缀。
        urls = re.findall(r"""(?<![\w:/?=&%#@.+-])https?://[^\s<>"']+""", value)
        if len(urls) == 1:
            candidate = urls[0].rstrip(".,;!?)。，；！？）")
            try:
                parsed = urlparse(candidate)
            except ValueError:
                parsed = None
            if (parsed is not None and parsed.scheme == "https"
                    and parsed.netloc.lower() == "pan.quark.cn"
                    and re.fullmatch(r"/s/[a-zA-Z0-9_-]+", parsed.path)
                    and not parsed.params):
                return f"https://pan.quark.cn{parsed.path}"
        raise QuarkBrowserError("页面未返回唯一有效的夸克分享链接；没有记录未经核验的 URL")


def login_and_inspect(folder_url: str, profile_dir: Path, browser_executable_path: str = "") -> None:
    """只用于首次人工登录和观察控件；不上传文件。"""
    parsed = urlparse(folder_url)
    if parsed.scheme != "https" or parsed.hostname not in {"pan.quark.cn", "drive.quark.cn"}:
        raise ValueError("请提供真实夸克网盘 HTTPS 文件夹地址")
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import Error as PlaywrightError
    with sync_playwright() as playwright:
        context = launch_browser_context(playwright, profile_dir, browser_executable_path,
                                          headless=False, accept_downloads=False)
        page = None
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(folder_url, wait_until="domcontentloaded")
            logging.info("请在打开的夸克窗口登录并进入目标文件夹，完成后关闭该窗口；最长等待 5 分钟。")
            logging.info("实际夸克登录目录：%s", context._pipeline_profile_dir)
            wait_for_login_window(page)
            logging.info("夸克登录窗口已结束；此操作不验证登录结果，上传前仍需确认登录状态和页面选择器。")
        except PlaywrightError:
            if page is None or not page.is_closed():
                raise
            logging.info("夸克登录窗口已关闭")
        finally:
            context.close()


def wait_for_login_window(page, timeout_seconds: int = 300) -> None:
    from playwright.sync_api import Error as PlaywrightError
    deadline = time.monotonic() + timeout_seconds
    while not page.is_closed() and time.monotonic() < deadline:
        try:
            page.wait_for_timeout(500)
        except PlaywrightError:
            if page.is_closed():
                return
            raise
    if not page.is_closed():
        logging.warning("夸克登录等待超时，即将关闭窗口；可重新点击首次登录")
