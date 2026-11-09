"""Offline Ghelper settings, independent checking, and responsive layout regression."""
from __future__ import annotations

import argparse
from pathlib import Path

import check_channel_picker_ui as harness


def check_view(page, fixture, origin, label, unique):
    errors, calls, held = [], [], []
    fixture.config.update(telegram_proxy_mode="manual", telegram_ghelper_profile="",
                          telegram_api_proxy_url="socks5://127.0.0.1:7890")
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", fixture.route)

    def check(route):
        calls.append(route.request.post_data_json)
        held.append(route)

    page.route("**/api/telegram-proxy-check", check)
    page.goto(origin + "/?view=settings")
    page.wait_for_function("!document.getElementById('save-config').disabled")
    assert not calls and page.locator("#ghelper-fields").is_hidden()
    page.locator("#telegram_proxy_mode").select_option("ghelper")
    assert page.locator("#telegram-manual-proxy-field").is_hidden()
    page.locator("#ghelper-check").click()
    page.wait_for_timeout(100)
    assert held and page.locator("#ghelper-check").is_disabled()
    assert "正在读取" in page.locator("#ghelper-status").inner_text()
    assert not fixture.posts and not fixture.workbench["running"]
    held.pop().fulfill(status=400, json={"message": "线路无法连接，请在插件中切换线路后重试"})
    page.wait_for_function("!document.getElementById('ghelper-check').disabled")
    assert "切换线路" in page.locator("#ghelper-status").inner_text()
    page.locator("#ghelper-check").click()
    page.wait_for_timeout(100)
    assert held
    held.pop().fulfill(json={"connected": True, "message": "Telegram 协议握手通过", "nodes": 3, "elapsed_ms": 1234})
    page.wait_for_function("!document.getElementById('ghelper-check').disabled")
    assert "握手通过" in page.locator("#ghelper-status").inner_text()
    page.screenshot(path=str(harness.ARTIFACTS / f"ghelper-{label}-{unique}.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator("#ghelper-fields").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    assert calls == [{"telegram_ghelper_profile": ""}] * 2
    page.locator("#save-config").click()
    page.wait_for_function("document.getElementById('config-dirty').textContent === ''")
    assert fixture.config["telegram_proxy_mode"] == "ghelper"
    assert fixture.config["telegram_api_proxy_url"] == "socks5://127.0.0.1:7890"
    page.reload()
    page.wait_for_function("!document.getElementById('save-config').disabled")
    assert page.locator("#telegram_proxy_mode").input_value() == "ghelper"
    assert page.locator("#telegram-manual-proxy-field").is_hidden()
    assert len(calls) == 2
    page.locator("#ghelper-check").click()
    page.wait_for_timeout(100)
    assert held
    page.locator("#telegram_ghelper_profile").fill("C:\\Browser\\Profile 1")
    held.pop().fulfill(json={"connected": True, "message": "STALE", "nodes": 3, "elapsed_ms": 0})
    page.wait_for_timeout(100)
    assert "STALE" not in page.locator("#ghelper-status").inner_text()
    page.locator("#telegram_proxy_mode").select_option("manual")
    assert page.locator("#ghelper-fields").is_hidden()
    assert page.locator("#telegram_api_proxy_url").input_value().endswith(":7890")
    assert all(path == "/api/config" for path, _ in fixture.posts)
    assert not errors, errors
    print(f"{label}: proxy mode, retry, loading, save/reload, stale results and layout PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", type=Path, required=True)
    args = parser.parse_args()
    harness.BROWSER = args.browser
    harness.check(check_callback=check_view)
