from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import json
import threading

import pytest

from xianyu_pipeline import qianniu_browser
from xianyu_pipeline.browser_download import DownloadCancelled
from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.qianniu_shop import (QIANNIU_HOME_URL, ShopDetector, page_shops,
                                          SHOP_INFO_PATH, page_login_state, payload_shops, seller_page,
                                          shop_info_response, shop_name, shop_response_payload, unique_shops)


@pytest.mark.parametrize("name", ["我的店铺", "进入店铺", "千牛", "", "登录", "x" * 101, "店\n名", None])
def test_generic_labels_and_invalid_names_are_not_shops(name):
    assert shop_name(name) == ""


def test_only_seller_origins_are_read():
    assert seller_page(QIANNIU_HOME_URL)
    for url in ("https://login.taobao.com/", "https://myseller.taobao.com.evil.test/",
                "https://user@myseller.taobao.com/", "http://myseller.taobao.com/",
                "https://shop123.taobao.com/", "https://myseller.taobao.com:8080/"):
        assert not seller_page(url)
        page = SimpleNamespace(url=url, evaluate=lambda *args: pytest.fail("Non-seller page was read"))
        assert page_shops(page) == []


def test_identity_response_ignores_nickname_product_lists_and_unknown_ids():
    data = {"data": {"user": {"nick": "买家昵称", "userId": "123"},
                     "shop": {"shopName": "我的模型店", "shopId": 456},
                     "recommendations": [{"shopName": "其他店铺", "shopId": 789}]}}
    assert payload_shops(data) == [{"name": "我的模型店", "shop_id": "456", "source": "卖家页面店铺信息"}]
    assert not payload_shops({"nick": "我的昵称", "shopName": "未配对名称"})
    assert not payload_shops({"shopName": "我的店铺", "shopId": "123"})
    assert not payload_shops({"shopName": "其他店", "shopId": "../123"})
    assert unique_shops([{"name": "店铺甲"}, {"name": "店铺甲", "shop_id": "123"}]) == [
        {"name": "店铺甲", "shop_id": "123", "source": "页面店铺信息"}]
    assert len(unique_shops([{"name": "同名店", "shop_id": str(i)} for i in (123, 456)])) == 2


def test_response_observer_only_accepts_small_json_from_seller_frames():
    detector = ShopDetector()
    response = SimpleNamespace(
        url=QIANNIU_HOME_URL + "/shopinfo",
        status=200, headers={"content-type": "application/json", "content-length": "100"},
        request=SimpleNamespace(resource_type="xhr", frame=SimpleNamespace(url=QIANNIU_HOME_URL)),
        body=lambda: json.dumps({"data": {"shopName": "识别店铺", "shopId": "123", "token": "PRIVATE"}}).encode(),
    )
    detector.response(response)
    assert detector.responses == [{"name": "识别店铺", "shop_id": "123", "source": "卖家页面店铺信息"}]
    assert "PRIVATE" not in str(detector.responses)
    detector.responses.clear()
    response.headers["content-length"] = str(1024 * 1024)
    detector.response(response)
    assert not detector.responses
    response.headers["content-length"] = "100"
    response.request.frame.url = "https://shop999.taobao.com/"
    detector.response(response)
    assert not detector.responses


def test_completed_json_without_content_length_is_read_but_large_or_invalid_bodies_are_ignored():
    detector = ShopDetector()
    response = SimpleNamespace(
        url=QIANNIU_HOME_URL + "/shopinfo", status=200,
        headers={"content-type": "application/json"},
        request=SimpleNamespace(resource_type="xhr", frame=SimpleNamespace(url=QIANNIU_HOME_URL)),
        body=lambda: b'{"shopName":"shop","shopId":"123"}',
    )
    detector.request_finished(SimpleNamespace(response=lambda: response))
    assert detector.responses[0]["name"] == "shop"
    for body in (b"{}", b"invalid json", b"x" * (256 * 1024 + 1)):
        detector.responses.clear()
        response.body = lambda: body
        detector.request_finished(SimpleNamespace(response=lambda: response))
        assert not detector.responses
    detector.request_finished(SimpleNamespace(response=lambda: None))


def test_current_qianniu_shop_info_jsonp_script_is_observed_from_the_seller_page():
    detector = ShopDetector()
    response = SimpleNamespace(
        url="https://h5api.m.taobao.com" + SHOP_INFO_PATH + "?callback=mtopjsonp12",
        status=200, headers={"content-type": "application/javascript; charset=UTF-8"},
        request=SimpleNamespace(resource_type="script", frame=SimpleNamespace(url=QIANNIU_HOME_URL)),
        body=lambda: ('mtopjsonp12({"data":{"result":{"shopName":"真实结构测试店","shopId":"123456",'
                      '"nick":"不能用作店名的账号昵称"}},"ret":["SUCCESS::调用成功"]})').encode(),
    )
    detector.request_finished(SimpleNamespace(response=lambda: response))
    assert detector.responses == [{"name": "真实结构测试店", "shop_id": "123456", "source": "卖家页面店铺信息"}]
    assert "不能用作店名" not in str(detector.responses)


@pytest.mark.parametrize("url", [
    "http://h5api.m.taobao.com" + SHOP_INFO_PATH,
    "https://h5api.m.taobao.com.evil.test" + SHOP_INFO_PATH,
    "https://h5api.m.taobao.com:8080" + SHOP_INFO_PATH,
    "https://user@h5api.m.taobao.com" + SHOP_INFO_PATH,
    "https://h5api.m.taobao.com/h5/unrelated.recommendations/1.0/",
])
def test_unrelated_mtop_origins_or_endpoints_are_not_used_as_shop_identity(url):
    assert not shop_info_response(url)
    detector = ShopDetector()
    response = SimpleNamespace(
        url=url, status=200, headers={"content-type": "application/json"},
        request=SimpleNamespace(resource_type="script", frame=SimpleNamespace(url=QIANNIU_HOME_URL)),
        body=lambda: pytest.fail("Unrelated response body was read"),
    )
    detector.response(response)
    assert not detector.responses


@pytest.mark.parametrize("body", [
    b'mtopjsonp99({"shopName":"wrong callback","shopId":"123"})',
    b'mtopjsonp12({"shopName":"shop","shopId":"123"});alert(1)',
    b'mtopjsonp12(window.account)',
    b'mtopjsonp12({shopName:"not JSON",shopId:"123"})',
])
def test_jsonp_must_match_the_callback_and_contain_only_json(body):
    with pytest.raises(ValueError):
        shop_response_payload(body, "https://h5api.m.taobao.com" + SHOP_INFO_PATH + "?callback=mtopjsonp12")


def test_shop_info_script_from_an_unrelated_frame_is_ignored():
    detector = ShopDetector()
    response = SimpleNamespace(
        url="https://h5api.m.taobao.com" + SHOP_INFO_PATH + "?callback=mtopjsonp12",
        status=200, headers={"content-type": "application/javascript"},
        request=SimpleNamespace(resource_type="script", frame=SimpleNamespace(url="https://shop999.taobao.com/")),
        body=lambda: pytest.fail("Non-seller frame response was read"),
    )
    detector.response(response)
    assert not detector.responses


def test_login_frames_and_stale_login_tabs_do_not_misrepresent_the_workbench():
    main = object()
    login_frame = SimpleNamespace(
        url="https://havanlogin.taobao.com/mini_login.htm",
        frame_element=lambda: SimpleNamespace(is_visible=lambda: True))
    workbench = SimpleNamespace(url=QIANNIU_HOME_URL, main_frame=main, frames=[main, login_frame],
                                evaluate=lambda script: "recognizing")
    assert page_login_state(workbench) == "waiting"
    login_frame.frame_element = lambda: SimpleNamespace(is_visible=lambda: False)
    assert page_login_state(workbench) == "recognizing"
    login_tab = SimpleNamespace(url="https://loginmyseller.taobao.com/")
    assert ShopDetector().login_state([login_tab]) == "waiting"
    assert ShopDetector().login_state([workbench, login_tab]) == "recognizing"


@pytest.fixture
def shop_waiter(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(qianniu_browser.time, "monotonic", lambda: clock.now)
    stop = threading.Event()
    state = SimpleNamespace(stage="waiting", candidates=[], scans=0, closed=False, after_wait=None)

    def wait(milliseconds):
        clock.now += milliseconds / 1000
        if state.after_wait:
            state.after_wait()

    def scan(pages):
        state.scans += 1
        return state.candidates

    page = SimpleNamespace(is_closed=lambda: state.closed, wait_for_timeout=wait)
    context = SimpleNamespace(pages=[page])
    detector = SimpleNamespace(login_state=lambda pages: state.stage, scan=scan)
    adapter = qianniu_browser.QianniuDraftAdapter(PipelineConfig(), stop)
    return SimpleNamespace(clock=clock, state=state, context=context, detector=detector,
                           adapter=adapter, stop=stop, progress=[])


def test_visible_login_keeps_waiting_without_repeatedly_reading_shop_data(shop_waiter):
    test = shop_waiter
    with pytest.raises(ValueError, match="等待千牛登录超时"):
        test.adapter.wait_for_shop(test.context, test.detector, test.progress.append)
    assert test.clock.now == 300
    assert test.state.scans == 0
    assert {entry["stage"] for entry in test.progress} == {"waiting"}


def test_loaded_workbench_without_shop_stops_after_thirty_seconds(shop_waiter):
    test = shop_waiter
    test.state.stage = "recognizing"
    with pytest.raises(ValueError, match="30 秒内未识别到店铺名称"):
        test.adapter.wait_for_shop(test.context, test.detector, test.progress.append)
    assert test.clock.now == 30
    assert test.progress[0]["stage"] == "recognizing"
    assert test.progress[0]["remaining_seconds"] == 30
    assert test.progress[-1]["remaining_seconds"] <= 1


def test_login_time_does_not_consume_the_shop_recognition_window(shop_waiter):
    test = shop_waiter
    test.state.after_wait = lambda: setattr(test.state, "stage", "recognizing" if test.clock.now >= 90 else "waiting")
    with pytest.raises(ValueError, match="30 秒内未识别到店铺名称"):
        test.adapter.wait_for_shop(test.context, test.detector, test.progress.append)
    assert test.clock.now == 120
    assert test.state.scans < 50
    assert test.progress[0]["stage"] == "waiting"
    assert test.progress[-1]["stage"] == "recognizing"


def test_a_workbench_reload_does_not_reset_the_recognition_timeout(shop_waiter):
    test = shop_waiter
    test.state.stage = "recognizing"
    test.state.after_wait = lambda: setattr(test.state, "stage", "loading")
    with pytest.raises(ValueError, match="30 秒内未识别到店铺名称"):
        test.adapter.wait_for_shop(test.context, test.detector, test.progress.append)
    assert test.clock.now == 30


def test_success_stops_scanning_as_soon_as_shop_identity_stabilizes(shop_waiter):
    test = shop_waiter
    test.state.stage = "recognizing"
    test.state.candidates = [{"name": "识别店铺", "shop_id": "123", "source": "页面店铺信息"}]
    assert test.adapter.wait_for_shop(test.context, test.detector, test.progress.append) == test.state.candidates
    assert test.clock.now == .75 and test.state.scans == 2
    assert test.progress[-1]["stage"] == "detected"


@pytest.mark.parametrize("closed", [True, False])
def test_closed_window_and_stop_button_end_recognition_promptly(shop_waiter, closed):
    test = shop_waiter
    test.state.closed = closed
    if not closed:
        test.state.after_wait = test.stop.set
    with pytest.raises(DownloadCancelled):
        test.adapter.wait_for_shop(test.context, test.detector)
    assert test.clock.now <= .75


def test_login_uses_default_home_and_checks_all_tabs_until_identity_stabilizes(tmp_path, monkeypatch):
    import playwright.sync_api

    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"MZ")
    config = replace(PipelineConfig(), browser_executable_path=str(executable),
                     qianniu_browser_profile=tmp_path / "profile")
    calls, progress = [], []
    page = SimpleNamespace(url=QIANNIU_HOME_URL, frames=[], is_closed=lambda: False,
                           goto=lambda url, **kw: calls.append(("goto", url)),
                           wait_for_timeout=lambda timeout: calls.append(("wait", timeout)))
    context = SimpleNamespace(pages=[page], close=lambda: calls.append(("close",)),
                              on=lambda event, callback: calls.append(("listen", event)))
    manager = SimpleNamespace(__enter__=lambda self: None, __exit__=lambda *args: None)

    class Playwright:
        def __enter__(self):
            return manager

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", Playwright)
    monkeypatch.setattr(qianniu_browser, "launch_browser_context", lambda *args, **kw: context)
    expected = [{"name": "登录后识别店铺", "shop_id": "123", "source": "页面店铺信息"}]
    scans = iter([[], expected, expected])
    monkeypatch.setattr(ShopDetector, "scan", lambda self, pages: next(scans))
    assert qianniu_browser.QianniuDraftAdapter(config, threading.Event()).login(progress.append) == expected
    assert ("goto", QIANNIU_HOME_URL) in calls and calls[-1] == ("close",)
    assert ("listen", "requestfinished") in calls
    assert progress[0]["stage"] == "waiting" and progress[-1]["stage"] == "detected"
