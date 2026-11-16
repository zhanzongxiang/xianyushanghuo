"""Offline product-draft UI and browser-adapter acceptance; no account is used."""
from __future__ import annotations

from dataclasses import replace
import io
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from check_workbench_ui import make_fixtures, isolated_environment, stop_process, LocalAPI, Budget, DEFAULT_BROWSER
from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.workbench import TransferStore
from xianyu_pipeline.product_drafts import ProductDraftStore
from xianyu_pipeline.qianniu_browser import QianniuDraftAdapter, QianniuSavePending
from xianyu_pipeline.qianniu_shop import SHOP_INFO_PATH, ShopDetector, page_login_state, page_shops


def check_adapter(browser, config, image):
    """Run the actual fill/save adapter against a fully intercepted publisher page."""
    url = "https://item.publish.taobao.com/offline-draft-fixture"
    selectors = {key: "#" + key for key in ("shop", "title", "price", "stock", "category", "description", "images", "save", "success")}
    config = replace(config, qianniu_shop_name="离线测试店铺", qianniu_publish_url=url, qianniu_form_selectors=selectors)
    html = """<!doctype html><meta charset="utf-8"><div id="shop">离线测试店铺</div>
    <input id="title"><input id="price"><input id="stock"><select id="category"><option>店铺实际类目</option></select>
    <textarea id="description"></textarea><input id="images" type="file" multiple>
    <button id="save" onclick="document.querySelector('#success').hidden=false">保存草稿</button>
    <div id="success" hidden>商品草稿保存成功</div>"""
    context = browser.new_context()
    context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html))
    page = context.new_page()
    page.goto(url)
    row = dict(shop_name="离线测试店铺", title="模型标题", price="9.90", stock=10, category="店铺实际类目", description="详情", image_indices=[0])
    adapter = QianniuDraftAdapter(config, threading.Event())
    result = adapter.fill_and_save(page, row, [image])
    assert result["confirmed_by"] == "receipt"
    assert page.locator("#title").input_value() == row["title"]
    assert page.locator("#images").evaluate("(el) => el.files.length") == 1
    for text in ("发布", "提交", "放入仓库"):
        page.goto(url)
        page.locator("#save").evaluate("(el, text) => el.textContent=text", text)
        try:
            adapter.fill_and_save(page, row, [image])
        except ValueError:
            pass
        else:
            raise AssertionError("Non-draft button was clicked")
        assert page.locator("#success").is_hidden()
    page.goto(url)
    page.locator("#save").evaluate("(el) => el.onclick = () => { document.querySelector('#success').hidden=false; document.querySelector('#success').textContent='商品草稿保存成功'; }")
    page.locator("#shop").evaluate("(el) => el.textContent='另一家店铺'")
    try:
        adapter.fill_and_save(page, row, [image])
    except ValueError:
        pass
    else:
        raise AssertionError("A different shop was accepted")
    page.goto(url)
    stop = threading.Event()
    page.locator("#save").evaluate("(el) => el.onclick = () => window.notifyClicked()")
    page.expose_function("notifyClicked", lambda: stop.set())
    try:
        QianniuDraftAdapter(config, stop).fill_and_save(page, row, [image])
    except QianniuSavePending:
        pass
    else:
        raise AssertionError("An interrupted click did not become pending")
    page.goto(url)
    assert page_shops(page, "#shop")[0]["name"] == "离线测试店铺"
    page.set_content("""<header><span>账号昵称并不是店铺名</span>
      <a href="https://shop123.taobao.com/">我的店铺</a></header>
      <main><a href="https://shop999.taobao.com/">推荐的其他店铺</a></main>""")
    assert not page_shops(page)
    page.locator("header a").evaluate("(el) => { el.textContent='自动识别的模型店'; }")
    assert page_shops(page) == [{"name": "自动识别的模型店", "shop_id": "123", "source": "页面店铺信息"}]
    page.set_content("""<nav><a href="https://shop123.taobao.com/" title="店铺甲">进入店铺</a></nav>
      <div data-shop-name="店铺甲"></div><div data-shop-name="不可见店铺" hidden></div>""")
    assert page_shops(page) == [{"name": "店铺甲", "shop_id": "123", "source": "页面店铺信息"}]
    page.set_content("<header><div>店铺名称：本机测试店</div></header>")
    assert page_shops(page)[0]["name"] == "本机测试店"
    assert page_login_state(page) == "recognizing"
    page.set_content('<main>扫码登录更便捷</main><input type="password">')
    assert page_login_state(page) == "waiting"
    page.set_content("<main>加载中...</main>")
    assert page_login_state(page) == "loading"
    page.set_content('<main>千牛工作台</main><iframe src="https://item.publish.taobao.com/offline-login-fixture"></iframe>')
    page.frames[1].set_content('<input type="password">')
    assert page_login_state(page) == "waiting"
    page.locator("iframe").evaluate("(el) => el.hidden=true")
    assert page_login_state(page) == "recognizing"
    detector = ShopDetector()
    context.on("requestfinished", detector.request_finished)
    context.route("**/shopinfo", lambda route: route.fulfill(
        content_type="application/json", body='{"shopName":"offline network shop","shopId":"456"}'))
    page.evaluate("fetch('/shopinfo').then(r => r.json())")
    page.wait_for_timeout(100)
    assert detector.responses == [{"name": "offline network shop", "shop_id": "456", "source": "卖家页面店铺信息"}]
    detector.responses.clear()
    page.set_content("""<div id="qn-worbench-container"><div id="qn-workbench-inner-container">
      <main><div class="rightSide--fixture"><div class="ShopInfo_topTitle__fixture">
        <div class="ShopInfo_name__fixture">工作台结构测试店</div></div></div></main>
      <div class="ShopInfo_name__hidden" hidden>不可见店铺</div></div></div>
      <aside><div class="ShopInfo_name__outside">组件范围之外的店铺</div></aside>""")
    assert page_shops(page) == [{"name": "工作台结构测试店", "shop_id": "", "source": "页面店铺信息"}]
    mtop_url = "https://h5api.m.taobao.com" + SHOP_INFO_PATH + "?callback=mtopjsonp12"
    context.route(mtop_url, lambda route: route.fulfill(
        content_type="application/javascript",
        body='mtopjsonp12({"data":{"result":{"shopName":"工作台结构测试店","shopId":"123456"}}})'))
    page.evaluate("""url => new Promise((resolve, reject) => {
      const script = document.createElement("script");
      window.mtopjsonp12 = () => {};
      script.onload = resolve; script.onerror = reject; script.src = url;
      document.head.appendChild(script);
    })""", mtop_url)
    page.wait_for_timeout(100)
    assert detector.scan([page]) == [
        {"name": "工作台结构测试店", "shop_id": "123456", "source": "卖家页面店铺信息"}]
    context.close()


def check_shop_discovery_ui(page, expect, origin, artifact_dir):
    """UI responses are explicitly offline; real HTTP confirmation is covered by pytest."""
    detection = {"stage": "idle", "ticket": "offline-ui", "candidates": [], "error": ""}
    calls = []

    def drafts(route):
        response = route.fetch()
        body = response.json()
        body["qianniu_shop"] = detection
        route.fulfill(response=response, json=body)

    def login(route):
        calls.append(route.request.post_data_json)
        detection.update(stage="waiting", candidates=[])
        route.fulfill(json={"message": "离线测试：开始识别"})

    def confirm(route):
        assert route.request.post_data_json == {"ticket": "offline-ui", "index": 1}
        detection.update(stage="confirmed")
        route.fulfill(json={"message": "离线测试：店铺已确认", "shop_name": "离线测试店铺", "bound_drafts": 0})

    page.route(origin + "/api/drafts", drafts)
    page.route(origin + "/api/qianniu/login", login)
    page.route(origin + "/api/qianniu/shop/confirm", confirm)
    try:
        page.locator("#draft-open-template").click()
        assert not page.locator("#dt-shop").get_attribute("required")
        page.locator("#dt-price").fill("16.80")
        page.locator("#dt-shop-detect").click()
        expect(page.locator("#dt-shop-status")).to_have_text("等待在千牛窗口中完成登录")
        expect(page.locator("#dt-shop-result")).not_to_be_visible()
        detection.update(stage="recognizing", remaining_seconds=30)
        expect(page.locator("#dt-shop-status")).to_have_text("正在识别店铺（剩余 30 秒）", timeout=10000)
        detection.update(remaining_seconds=10)
        expect(page.locator("#dt-shop-status")).to_have_text("正在识别店铺（剩余 10 秒）", timeout=10000)
        detection.update(stage="failed", error="千牛页面已打开，但 30 秒内未识别到店铺名称。请手动填写店铺名称")
        expect(page.locator("#dt-shop-status")).to_contain_text("请手动填写店铺名称", timeout=10000)
        expect(page.locator("#dt-shop")).to_be_editable()
        expect(page.locator("#dt-save")).to_be_enabled()
        detection.update(stage="detected", candidates=[
            {"name": "另一个离线店铺", "shop_id": "456", "source": "页面店铺信息"},
            {"name": "离线测试店铺", "shop_id": "123", "source": "页面店铺信息"}])
        expect(page.locator("#dt-shop-result")).to_be_visible(timeout=10000)
        expect(page.locator("#dt-shop")).to_have_value("离线测试店铺")
        page.locator("#dt-shop-candidate").select_option("1")
        for width, height, name in ((1440, 1000, "shop-discovery-desktop"), (390, 844, "shop-discovery-mobile")):
            page.set_viewport_size({"width": width, "height": height})
            page.screenshot(path=str(artifact_dir / (name + ".png")), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        page.locator("#dt-shop-use").click()
        expect(page.locator("#dt-shop-status")).to_have_text("店铺已确认并保存")
        expect(page.locator("#dt-price")).to_have_value("16.80")
        expect(page.locator("#dt-shop")).to_have_value("离线测试店铺")
        expect(page.locator("#dt-shop-result")).not_to_be_visible()
        assert len(calls) == 1 and calls[0]["url"] == ""
        detection.update(stage="failed", candidates=[], error="离线测试：未找到店铺")
        expect(page.locator("#dt-shop-status")).to_have_text("离线测试：未找到店铺", timeout=10000)
        page.locator("#draft-template-dialog [data-draft-close]").click()
        page.set_viewport_size({"width": 1600, "height": 1100})
    finally:
        page.unroute(origin + "/api/drafts", drafts)
        page.unroute(origin + "/api/qianniu/login", login)
        page.unroute(origin + "/api/qianniu/shop/confirm", confirm)


def run(root):
    from playwright.sync_api import sync_playwright, expect
    config_path, settings = make_fixtures(root, DEFAULT_BROWSER)
    settings.update(qianniu_shop_name="离线测试店铺", qianniu_default_price="9.90",
                    qianniu_default_stock=99, qianniu_default_category="店铺实际类目",
                    qianniu_default_format="STL", qianniu_default_variant="标准版")
    config_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    config = PipelineConfig.from_json(config_path)
    transfers = TransferStore(config)
    for index, task in enumerate(transfers.snapshot()["tasks"]):
        if task["download_status"] == "completed" and task["filename"] != "04-damaged-archive.zip":
            transfers.update(task["id"], upload_status="completed", upload_provider="quark_api",
                             share_url=f"https://pan.quark.cn/s/offline-{index}")
            transfers.enrich(task["id"])
    drafts = ProductDraftStore(config.output_dir)
    task_rows = [transfers.get(t["id"]) for t in transfers.snapshot()["tasks"]
                 if t["download_status"] == "completed" and transfers.get(t["id"]).get("image_paths")]
    for task in task_rows[:2]:
        drafts.create(task, config)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    api = LocalAPI(origin, Budget(110))
    artifact_dir = REPO / "tests/artifacts/product-drafts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    server = None
    try:
        with (root / "server.log").open("wb") as log:
            server = subprocess.Popen([sys.executable, str(REPO / "run_pipeline.py"), "--config", str(config_path),
                                       "--web-ui", "--no-open-browser", "--web-port", str(port)],
                                      cwd=root, env=isolated_environment(root), stdin=subprocess.DEVNULL,
                                      stdout=log, stderr=subprocess.STDOUT,
                                      creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            for _ in range(100):
                try:
                    api.json("/api/drafts")
                    break
                except OSError:
                    assert server.poll() is None
                    time.sleep(.1)
            errors, external = [], []
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(DEFAULT_BROWSER), headless=True)
                check_adapter(browser, config, Path(task_rows[0]["image_paths"][0]))
                context = browser.new_context(viewport={"width": 1600, "height": 1100})

                def route(request):
                    if request.request.url.startswith(origin + "/"):
                        request.continue_()
                    else:
                        external.append(request.request.url)
                        request.abort()

                context.route("**/*", route)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(origin + "/?view=drafts")
                expect(page.locator("#draft-title")).to_be_visible()
                expect(page.locator("#draft-list .draft-row")).to_have_count(2)
                expect(page.locator("#draft-hero-image")).to_be_visible()
                page.wait_for_function("document.querySelector('#draft-hero-image').naturalWidth > 0")
                page.locator("#draft-title").fill("人工修改标题，轮询不能覆盖")
                page.wait_for_timeout(2300)
                expect(page.locator("#draft-title")).to_have_value("人工修改标题，轮询不能覆盖")
                page.locator("#draft-save-local").click()
                expect(page.locator("#draft-save-note")).to_have_text("资料已保存至工作台")
                expect(page.locator("#draft-title")).to_have_value("人工修改标题，轮询不能覆盖")
                record = api.json("/api/drafts")["drafts"][0]
                assert any(r["title"] == "人工修改标题，轮询不能覆盖" for r in api.json("/api/drafts")["drafts"])
                page.locator("#draft-set-cover").is_visible()
                page.locator("#draft-hero").click()
                expect(page.locator("#draft-large-image")).to_be_visible()
                page.locator("#draft-image-dialog [data-draft-close]").click()
                with page.expect_download() as download:
                    page.locator("#draft-export").click()
                archive = zipfile.ZipFile(download.value.path())
                assert "product.json" in archive.namelist() and "delivery.txt" in archive.namelist()
                assert "pan.quark.cn" not in archive.read("product.json").decode("utf-8")
                page.locator("#draft-open-template").click()
                expect(page.locator("#draft-template-dialog")).to_be_visible()
                page.locator("#dt-price").fill("12.50")
                page.locator("#dt-save").click()
                expect(page.locator("#draft-template-dialog")).not_to_be_visible()
                assert api.json("/api/drafts")["template"]["qianniu_default_price"] == "12.50"
                assert record["price"] == "9.90"
                check_shop_discovery_ui(page, expect, origin, artifact_dir)
                with page.expect_response(lambda response: response.url == origin + "/api/drafts/template"
                                          and response.request.method == "POST") as auto_create:
                    page.locator("#draft-auto-create").check()
                assert auto_create.value.status == 200, auto_create.value.text()
                page.wait_for_function("document.querySelector('#draft-auto-create').checked && !document.querySelector('#draft-auto-create').disabled")
                assert api.json("/api/drafts")["template"]["qianniu_auto_create_draft"]
                page.locator("#draft-import").click()
                expect(page.locator("#draft-import-dialog")).to_be_visible()
                page.locator("#draft-import-list input").first.check()
                page.locator("#draft-import-confirm").click()
                expect(page.locator("#draft-import-dialog")).not_to_be_visible()
                assert len(api.json("/api/drafts")["drafts"]) == 2
                page.locator("#draft-publish").click()
                expect(page.locator("#draft-error")).to_contain_text("千牛")
                assert not api.json("/api/workbench")["running"]
                page.reload()
                expect(page.locator("#draft-title")).to_be_visible()
                for width, height, name in ((1600, 1100, "desktop"), (1440, 1000, "laptop"), (390, 844, "mobile")):
                    page.set_viewport_size({"width": width, "height": height})
                    page.evaluate("document.activeElement?.blur(); window.scrollTo(0, 0)")
                    page.screenshot(path=str(artifact_dir / (name + ".png")), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), f"Overflow at {name}"
                assert not errors, errors
                assert not external, external
                browser.close()
            print("PASS: draft editor, templates, import deduplication, preserved edits, real images, export, 3 responsive layouts; shop identity DOM, detection progress/confirmation/failure UI; adapter receipt/wrong-shop/publish-button/interruption guards")
    finally:
        stop_process(server)
        logging.shutdown()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="product-drafts-ui-", ignore_cleanup_errors=True) as directory:
        run(Path(directory))
