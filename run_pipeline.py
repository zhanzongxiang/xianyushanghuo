from __future__ import annotations

import argparse
import json
from pathlib import Path

from xianyu_pipeline.adapters import (
    LocalCloudStorageAdapter,
    LocalTelegramAdapter,
    QuarkCloudStorageAdapter,
    TelethonTelegramAdapter,
)
from xianyu_pipeline.quark_browser import QuarkBrowserStorageAdapter
from xianyu_pipeline.quark_cli import QuarkCLIStorageAdapter
from xianyu_pipeline.quark_api import QuarkAPIStorageAdapter
from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.pipeline import run_pipeline
from xianyu_pipeline.runtime import configure_logging, check_download_acceleration
from xianyu_pipeline.local_settings import restore_telegram_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="生成闲鱼数字商品待审核素材包")
    parser.add_argument("--config", default="config/pipeline.example.json")
    parser.add_argument("--quark-login", action="store_true", help="打开可见浏览器完成夸克登录，不上传文件")
    parser.add_argument("--quark-api-check", action="store_true", help="检查夸克 API Cookie 和目标目录，不上传文件")
    parser.add_argument("--telegram-api-login", action="store_true", help="在当前终端交互登录 Telegram API，不下载文件")
    parser.add_argument("--browser-check", action="store_true", help="检查夸克上传浏览器的启动和专用目录")
    parser.add_argument("--web-ui", action="store_true", help="启动本地参数和日志界面")
    parser.add_argument("--web-port", type=int, default=8765, help="本地界面端口")
    parser.add_argument("--no-open-browser", action="store_true", help="启动界面服务但不自动打开浏览器")
    parser.add_argument("--download-check", action="store_true", help="检查下载解密加速组件，不连接 Telegram")
    parser.add_argument("--analyze-downloads", type=Path, metavar="LOG_DIR",
                        help="分析下载诊断日志目录，不连接 Telegram")
    args = parser.parse_args()

    if args.download_check:
        print(json.dumps(check_download_acceleration(), ensure_ascii=False))
        return

    if args.analyze_downloads is not None:
        from xianyu_pipeline.download_diagnostics import analyze_files, diagnostic_files, render_report
        if not args.analyze_downloads.is_dir():
            raise SystemExit("下载诊断日志目录不存在")
        print(render_report(analyze_files(diagnostic_files(args.analyze_downloads))))
        return

    if args.web_ui:
        from xianyu_pipeline.web_ui import start_web_ui
        start_web_ui(args.config, port=args.web_port, open_browser=not args.no_open_browser)
        return

    restore_telegram_settings(args.config)
    config = PipelineConfig.from_json(args.config)
    configure_logging(config.output_dir / "logs")
    if args.quark_api_check:
        QuarkAPIStorageAdapter(config.quark_cookie, parent_fid=config.quark_api_parent_fid,
                               timeout_seconds=config.quark_api_timeout_seconds).check()
        return
    if args.browser_check:
        from xianyu_pipeline.browser_profiles import check_browser
        check_browser(config.quark_browser_profile, config.browser_executable_path)
        return
    if args.quark_login:
        if config.upload_provider == "quark_api":
            raise SystemExit("夸克 API 请在设置中填写 Cookie，使用 --quark-api-check 检查")
        if config.upload_provider == "quark_cli":
            QuarkCLIStorageAdapter(
                mode=config.quark_cli_mode,
                executable_path=config.quark_cli_executable_path,
                wsl_distribution=config.quark_cli_wsl_distribution,
                node_command=config.quark_cli_node_command,
                script_path=config.quark_cli_script_path,
                parent_fid=config.quark_cli_parent_fid,
                timeout_seconds=config.quark_cli_timeout_seconds,
                share_expired_type=config.quark_cli_share_expired_type,
                share_url_type=config.quark_cli_share_url_type,
            ).login()
            return
        from xianyu_pipeline.quark_browser import login_and_inspect
        if not config.quark_folder_url:
            raise SystemExit("请先在配置中填写 quark_folder_url")
        login_and_inspect(config.quark_folder_url, config.quark_browser_profile, config.browser_executable_path)
        return
    if config.telegram_access_mode != "api":
        raise SystemExit("旧配置使用 Telegram Web；请在设置中保存 Telegram API 配置后重试")
    if config.telegram_run_mode != "once":
        raise SystemExit("Telegram API 仅支持抓取一次")
    if args.telegram_api_login:
        telegram = TelethonTelegramAdapter(config.telegram_api_id, config.telegram_api_hash,
                                           config.telegram_session, config.inbox_dir,
                                           exclude_forwarded_posts=config.exclude_forwarded_posts,
                                           proxy_url=config.telegram_api_proxy_url,
                                           dc2_server=config.telegram_api_dc2_server,
                                           download_start_timeout_seconds=config.download_start_timeout_seconds,
                                           download_transfer_timeout_seconds=config.download_transfer_timeout_seconds,
                                           download_workers=config.download_workers,
                                           download_file_concurrency=config.download_file_concurrency)
        telegram.login()
        return
    if config.source_chat or config.source_channel_url:
        if not config.telegram_api_id or not config.telegram_api_hash:
            raise SystemExit("请先在本机配置 Telegram API ID 和 API Hash")
        telegram = TelethonTelegramAdapter(
            config.telegram_api_id,
            config.telegram_api_hash,
            config.telegram_session,
            config.inbox_dir,
            topic_id=config.source_topic_id,
            exclude_forwarded_posts=config.exclude_forwarded_posts,
            scan_limit=config.telegram_scan_limit,
            archive_test_executable_path=config.archive_test_executable_path,
            archive_test_timeout_seconds=config.archive_test_timeout_seconds,
            proxy_url=config.telegram_api_proxy_url,
            dc2_server=config.telegram_api_dc2_server,
            download_start_timeout_seconds=config.download_start_timeout_seconds,
            download_transfer_timeout_seconds=config.download_transfer_timeout_seconds,
            download_workers=config.download_workers,
            download_file_concurrency=config.download_file_concurrency,
        )
    else:
        telegram = LocalTelegramAdapter(config.inbox_dir)

    if config.upload_provider == "quark_api":
        cloud = QuarkAPIStorageAdapter(
            config.quark_cookie, parent_fid=config.quark_api_parent_fid,
            timeout_seconds=config.quark_api_timeout_seconds,
            share_expire_days=config.quark_share_expire_days,
        )
    elif config.upload_provider == "quark_browser":
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
    elif config.upload_provider == "quark_cli":
        cloud = QuarkCLIStorageAdapter(
            mode=config.quark_cli_mode,
            executable_path=config.quark_cli_executable_path,
            wsl_distribution=config.quark_cli_wsl_distribution,
            node_command=config.quark_cli_node_command,
            script_path=config.quark_cli_script_path,
            parent_fid=config.quark_cli_parent_fid,
            timeout_seconds=config.quark_cli_timeout_seconds,
            share_expired_type=config.quark_cli_share_expired_type,
            share_url_type=config.quark_cli_share_url_type,
        )
    elif config.upload_provider == "quark":
        # 兼容旧配置，但不会调用未经验证的接口。
        cloud = QuarkCloudStorageAdapter(config.quark_cookie)
    else:
        cloud = LocalCloudStorageAdapter(config.output_dir / "cloud_fixture")
    candidates = run_pipeline(config, telegram, cloud)
    for candidate in candidates:
        print(f"{candidate.candidate_id}: {candidate.status} | {candidate.title} | {candidate.share_url}")
        for warning in candidate.warnings:
            print(f"  - {warning}")


if __name__ == "__main__":
    main()
