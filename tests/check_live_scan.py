"""Read-only diagnostic against a user's already logged-in Telegram profile."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xianyu_pipeline.adapters import TelegramBrowserAdapter, launch_telegram_context
from xianyu_pipeline.config import PipelineConfig


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    config = PipelineConfig.from_json(args.config)
    adapter = TelegramBrowserAdapter(
        config.telegram_browser_profile, config.inbox_dir,
        headless=config.telegram_browser_headless,
        timeout_seconds=config.telegram_browser_timeout_seconds,
        scan_limit=config.telegram_scan_limit,
        browser_executable_path=config.browser_executable_path,
    )
    with sync_playwright() as playwright:
        context = launch_telegram_context(
            playwright, config.telegram_browser_profile, config.telegram_browser_headless,
            config.browser_executable_path,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            nodes = adapter._open_channel_messages(page, config.source_channel_url)
            initial = adapter._unique_message_nodes(nodes, adapter.scan_limit)
            ids = adapter._scan_archive_ids(page, nodes, config.recent_message_limit)
            for mid in ids:
                message = adapter._locate_message(page, nodes, mid)
                if message is None:
                    raise AssertionError(f"Cannot re-locate message {mid}")
                message.inner_text(timeout=3000)
            print(f"PASS: initial message IDs={len(initial)}, archive candidates={len(ids)}, "
                  f"re-located={len(ids)}; no attachment was clicked")
        finally:
            context.close()


if __name__ == "__main__":
    main()
