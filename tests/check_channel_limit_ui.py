"""Offline settings regression for channel batches of up to 1000 messages."""
from __future__ import annotations

import argparse
from pathlib import Path

import check_channel_picker_ui as harness


def check_view(page, fixture, origin, label, unique):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", fixture.route)
    page.goto(origin + "/?view=settings")
    page.wait_for_function("!document.getElementById('save-config').disabled")
    field = page.locator("#channel_message_limit")
    assert field.get_attribute("min") == "1" and field.get_attribute("max") == "1000"
    assert field.input_value() == "100"
    for limit in ("1", "1000"):
        field.fill(limit)
        page.locator("#save-config").click()
        page.wait_for_function("document.getElementById('config-dirty').textContent === ''")
        assert fixture.config["channel_message_limit"] == int(limit)
        page.reload()
        page.wait_for_function("!document.getElementById('save-config').disabled")
        assert field.input_value() == limit
    saved = len(fixture.posts)
    for invalid in ("0", "1001"):
        field.fill(invalid)
        page.locator("#save-config").click()
        assert field.get_attribute("aria-invalid") == "true"
        assert len(fixture.posts) == saved
    field.fill("1000")
    field.scroll_into_view_if_needed()
    page.screenshot(path=str(harness.ARTIFACTS / f"channel-limit-{label}-{unique}.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors, errors
    print(f"{label}: 1-1000 limits, save/reload and invalid-size rejection PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", type=Path, required=True)
    args = parser.parse_args()
    harness.BROWSER = args.browser
    harness.check(check_callback=check_view)
