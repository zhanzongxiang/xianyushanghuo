"""Offline folder navigation, config persistence and responsive layout checks."""
from __future__ import annotations

import argparse
from pathlib import Path

import check_channel_picker_ui as harness


def check_view(page, fixture, origin, label, unique):
    errors, requests, held = [], [], []
    fixture.config.update(upload_provider="quark_api", quark_cookie_configured=True,
                          quark_api_parent_fid="old", quark_api_folder_path="/原目录")
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", fixture.route)
    behavior = {"hold": True, "fail": False}
    long_name = "模型素材" * 25 + "<script>window.unsafeFolder=true</script>"

    def listing(parent="0", number=1):
        rows = ([{"fid": "models", "name": "模型资源"}, {"fid": "images", "name": "预览图片"}]
                if parent == "0" and number == 1 else [{"fid": "long", "name": long_name}]
                if parent == "0" else [{"fid": "stl", "name": "打印模型"}] if parent == "models" else [])
        return {"parent_fid": parent, "page": number, "folders": rows,
                "next_page": 2 if parent == "0" and number == 1 else None,
                "has_more": parent == "0" and number == 1}

    def folders(route):
        payload = route.request.post_data_json
        requests.append(payload)
        if behavior["hold"]:
            held.append(route)
        elif behavior["fail"]:
            route.fulfill(status=502, json={"message": "夸克登录失效，请更新 Cookie"})
        else:
            route.fulfill(json=listing(payload["parent_fid"], payload["page"]))

    page.route("**/api/quark-folders", folders)
    page.goto(origin + "/?view=settings")
    page.wait_for_function("!document.getElementById('quark-folder-browse').disabled")
    assert not requests, "Opening settings must not contact Quark"
    page.locator("#quark-folder-browse").click()
    page.wait_for_function("document.getElementById('quark-folder-list').getAttribute('aria-busy') === 'true'")
    page.wait_for_timeout(50)
    assert held and page.locator("#quark-folder-use").is_disabled()
    assert "正在读取" in page.locator("#quark-folder-status").inner_text()
    assert not fixture.posts and not fixture.workbench["running"]
    behavior["hold"] = False
    held.pop().fulfill(json=listing())
    page.locator(".quark-folder-row").first.wait_for()
    assert requests[-1] == {"parent_fid": "0", "page": 1}, "Saved Cookie must stay on server"
    page.locator("#quark-folder-more").click()
    page.wait_for_function("document.querySelectorAll('.quark-folder-row').length === 3")
    assert page.locator("#quark-folder-more").is_hidden()
    page.screenshot(path=str(harness.ARTIFACTS / f"quark-folders-{label}-{unique}.png"))
    assert page.evaluate("window.unsafeFolder !== true")
    assert page.locator("#quark-folder-dialog").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("button", name="进入 模型资源", exact=True).click()
    page.get_by_role("button", name="进入 打印模型", exact=True).click()
    page.wait_for_function("!document.getElementById('quark-folder-use').disabled")
    assert "没有子目录" in page.locator("#quark-folder-status").inner_text()
    page.locator("#quark-folder-up").click()
    page.get_by_role("button", name="进入 打印模型", exact=True).wait_for()
    page.locator("#quark-folder-cancel").click()
    assert page.locator("#quark_api_parent_fid").input_value() == "old"
    assert not fixture.posts

    page.locator("#quark-folder-browse").click()
    page.get_by_role("button", name="进入 模型资源", exact=True).click()
    page.get_by_role("button", name="进入 打印模型", exact=True).click()
    page.wait_for_function("!document.getElementById('quark-folder-use').disabled")
    page.locator("#quark-folder-use").click()
    assert page.locator("#quark_api_parent_fid").input_value() == "stl"
    assert page.locator("#quark_api_folder_path").input_value() == "/模型资源/打印模型"
    assert not fixture.posts, "Selection should not save unrelated edits"
    page.locator("#save-config").click()
    page.wait_for_function("document.getElementById('config-dirty').textContent === ''")
    assert fixture.config["quark_api_parent_fid"] == "stl"
    assert fixture.config["quark_api_folder_path"] == "/模型资源/打印模型"
    page.reload()
    page.wait_for_function("!document.getElementById('quark-folder-browse').disabled")
    assert page.locator("#quark_api_folder_path").input_value() == "/模型资源/打印模型"

    behavior["fail"] = True
    page.locator("#quark-folder-browse").click()
    page.locator("#quark-folder-error").wait_for()
    assert page.locator("#quark-folder-use").is_disabled()
    assert "登录失效" in page.locator("#quark-folder-error").inner_text()
    behavior["fail"] = False
    page.locator("#quark-folder-retry").click()
    page.locator(".quark-folder-row").first.wait_for()
    # A late pagination response must not replace a later navigation response.
    behavior["hold"] = True
    page.locator("#quark-folder-more").click()
    page.wait_for_timeout(50)
    assert held
    behavior["hold"] = False
    page.locator('[data-folder-level="0"]').click()
    page.wait_for_function("!document.getElementById('quark-folder-use').disabled")
    held.pop().fulfill(json=listing(number=2))
    page.wait_for_timeout(100)
    assert page.locator(".quark-folder-row").count() == 2
    page.locator("#quark-folder-use").click()
    assert page.locator("#quark_api_parent_fid").input_value() == "0"
    assert page.locator("#quark_api_folder_path").input_value() == "/"
    page.locator("#quark_api_parent_fid").fill("manual")
    assert page.locator("#quark_api_folder_path").input_value() == ""
    page.locator("#quark_cookie").fill("__uid=temporary-fixture")
    page.locator("#quark-folder-browse").click()
    page.locator(".quark-folder-row").first.wait_for()
    assert requests[-1]["quark_cookie"] == "__uid=temporary-fixture"
    page.keyboard.press("Escape")
    assert not page.locator("#quark-folder-dialog").evaluate("e=>e.open")
    assert all(path == "/api/config" for path, _ in fixture.posts)
    assert not errors, errors
    print(f"{label}: folder navigation, paging, cancel, save/reload, retry, stale responses and escaping PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", type=Path, required=True)
    args = parser.parse_args()
    harness.BROWSER = args.browser
    harness.check(check_callback=check_view)
