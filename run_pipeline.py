from __future__ import annotations

import argparse
from pathlib import Path

from xianyu_pipeline.adapters import (
    LocalCloudStorageAdapter,
    LocalTelegramAdapter,
    QuarkCloudStorageAdapter,
    TelegramBrowserAdapter,
    TelethonTelegramAdapter,
)
from xianyu_pipeline.quark_browser import QuarkBrowserStorageAdapter
from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.pipeline import run_pipeline
from xianyu_pipeline.runtime import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="生成闲鱼数字商品待审核素材包")
    parser.add_argument("--config", default="config/pipeline.example.json")
    parser.add_argument("--quark-login", action="store_true", help="打开可见浏览器完成夸克登录，不上传文件")
    parser.add_argument("--telegram-login", action="store_true", help="打开可见 Telegram Web 完成首次登录，不下载文件")
    parser.add_argument("--browser-check", action="store_true", help="启动并关闭配置的浏览器，仅检查启动和专用目录")
    parser.add_argument("--web-ui", action="store_true", help="启动本地参数和日志界面")
    parser.add_argument("--web-port", type=int, default=8765, help="本地界面端口")
    parser.add_argument("--no-open-browser", action="store_true", help="启动界面服务但不自动打开浏览器")
    args = parser.parse_args()

    if args.web_ui:
        from xianyu_pipeline.web_ui import start_web_ui
        start_web_ui(args.config, port=args.web_port, open_browser=not args.no_open_browser)
        return

    config = PipelineConfig.from_json(args.config)
    configure_logging(config.output_dir / "logs")
    if args.browser_check:
        from xianyu_pipeline.browser_profiles import check_browser
        check_browser(config.telegram_browser_profile, config.browser_executable_path,
                      headless=config.telegram_browser_headless)
        return
    if args.quark_login:
        from xianyu_pipeline.quark_browser import login_and_inspect
        if not config.quark_folder_url:
            raise SystemExit("请先在配置中填写 quark_folder_url")
        login_and_inspect(config.quark_folder_url, config.quark_browser_profile, config.browser_executable_path)
        return
    if args.telegram_login:
        from xianyu_pipeline.adapters import telegram_browser_login
        telegram_browser_login(config.telegram_browser_profile, config.source_channel_url, config.browser_executable_path)
        return
    if config.source_chat or config.source_channel_url:
        if config.telegram_access_mode == "browser":
            telegram = TelegramBrowserAdapter(
                config.telegram_browser_profile,
                config.inbox_dir,
                config.telegram_browser_headless,
                config.telegram_browser_timeout_seconds,
                config.telegram_message_selector,
                config.telegram_attachment_selector,
                watch_mode=config.telegram_run_mode == "watch",
                scan_limit=config.telegram_scan_limit,
                **config.browser_download_options(),
            )
        else:
            if not config.telegram_api_id or not config.telegram_api_hash:
                raise SystemExit("API 模式需要 Telegram API ID 和 API Hash；或将 telegram_access_mode 改为 browser")
            telegram = TelethonTelegramAdapter(
                config.telegram_api_id,
                config.telegram_api_hash,
                config.telegram_session,
                config.inbox_dir,
            )
    else:
        telegram = LocalTelegramAdapter(config.inbox_dir)

    if config.upload_provider == "quark_browser":
        cloud = QuarkBrowserStorageAdapter(
            folder_url=config.quark_folder_url,
            profile_dir=config.quark_browser_profile,
            headless=config.quark_headless,
            upload_input_selector=config.quark_upload_input_selector,
            file_row_selector=config.quark_file_row_selector,
            share_button_selector=config.quark_share_button_selector,
            share_link_selector=config.quark_share_link_selector,
            timeout_seconds=config.quark_timeout_seconds,
            browser_executable_path=config.browser_executable_path,
        )
    elif config.upload_provider == "quark":
        # 兼容旧配置，但不会调用未经验证的接口。
        cloud = QuarkCloudStorageAdapter(config.quark_cookie)
    else:
        cloud = LocalCloudStorageAdapter(config.output_dir / "cloud_fixture")
    if isinstance(telegram, TelegramBrowserAdapter) and telegram.watch_mode:
        from xianyu_pipeline.pipeline import process_messages
        telegram.on_message = lambda message: process_messages(config, [message], cloud)
        telegram.fetch_messages(config.source_chat, config.message_ids, config.source_channel_url,
                                config.fetch_mode, config.recent_message_limit)
        return
    candidates = run_pipeline(config, telegram, cloud)
    for candidate in candidates:
        print(f"{candidate.candidate_id}: {candidate.status} | {candidate.title} | {candidate.share_url}")
        for warning in candidate.warnings:
            print(f"  - {warning}")


if __name__ == "__main__":
    main()
