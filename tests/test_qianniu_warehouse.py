from dataclasses import replace
import json
import threading
from urllib.parse import urlencode

import pytest
from PIL import Image

from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.qianniu_warehouse import (
    TaobaoWarehouseAdapter, square_images, validate_publish_url, warehouse_receipt, warehouse_submission,
)

URL = 'https://item.upload.taobao.com/sell/v2/publish.htm?copyItem=true&itemId=123456'
ROW = dict(shop_name='离线测试店', title='测试模型', price='1.00', stock=1, image_indices=[0])


def submission(**changes):
    body = dict(startTime={'type': 2, 'shelfTime': None}, title=ROW['title'], price='1', quantity='1',
                mainImagesGroup={'images': [{'name': 'preview.jpg'}]})
    body.update(changes)
    return urlencode(dict(copyItem='true', itemId='123456', copyItemNewId='654321', jsonBody=json.dumps(body)))


def receipt(url='success.htm?primaryId=654321&isEdit=false'):
    return {'models': {'globalMessage': {'type': 'success', 'successUrl': url}}}


@pytest.mark.parametrize('url', [
    URL.replace('copyItem=true', 'copyItem=false'), URL.replace('itemId=123456', 'itemId=123456&itemId=789'),
    URL.replace('https:', 'http:'), URL.replace('item.upload.taobao.com', 'item.upload.taobao.com.evil.test'),
    'https://upload.taobao.com/auction/publish/edit.htm?item_num_id=123456',
])
def test_rejects_edit_or_unverified_publish_pages(url):
    with pytest.raises(ValueError):
        validate_publish_url(url)


def test_guard_checks_new_identity_and_off_shelf_payload():
    assert warehouse_submission(submission(), URL, ROW) == '654321'
    for body in (submission().replace('copyItem=true', 'copyItem=false'),
                 submission().replace('copyItemNewId=654321', 'copyItemNewId=123456')):
        with pytest.raises(ValueError):
            warehouse_submission(body, URL, ROW)


@pytest.mark.parametrize('change', [
    {'startTime': {'type': 0, 'shelfTime': None}}, {'startTime': {'type': 1, 'shelfTime': 'future'}},
    {'startTime': {'type': '2', 'shelfTime': None}}, {'title': 'stale title'}, {'quantity': '99'},
    {'price': '9.90'}, {'mainImagesGroup': {'images': []}},
])
def test_actual_submission_drift_is_blocked(change):
    with pytest.raises(ValueError):
        warehouse_submission(submission(**change), URL, ROW)


@pytest.mark.parametrize('payload', [
    {}, {'models': None}, {'models': []}, {'models': {'globalMessage': {'type': 'error'}}},
    receipt('https://evil.test/success.htm?primaryId=654321&isEdit=false'),
    receipt('success.htm?primaryId=123456&isEdit=false'), receipt('success.htm?primaryId=654321&isEdit=true'),
])
def test_unrelated_receipts_are_not_success(payload):
    assert warehouse_receipt(payload, '654321') is None


def test_receipt_returns_edit_link_for_exact_created_product():
    result = warehouse_receipt(receipt(), '654321')
    assert result['confirmed_by'] == 'warehouse_receipt'
    assert result['remote_url'].endswith('item_num_id=654321&auto=false')


def test_square_previews_keep_full_subject_and_original(tmp_path):
    path = tmp_path/'portrait.png'
    Image.new('RGBA', (600, 1200), (255, 0, 0, 255)).save(path)
    original = path.read_bytes()
    result = square_images([path], tmp_path)
    with Image.open(result[0]) as image:
        assert image.size == (1440, 1440)
        assert image.getpixel((720, 121))[0] > 240
        assert image.getpixel((720, 1318))[0] > 240
        assert all(c > 240 for c in image.getpixel((50, 720)))
    assert path.read_bytes() == original
    assert square_images([path], tmp_path) == result


def test_save_target_requires_explicit_setting():
    config = PipelineConfig()
    assert config.qianniu_save_target == 'draft'
    with pytest.raises(ValueError):
        replace(config, qianniu_save_target='online')


@pytest.fixture(scope='module')
def browser():
    from playwright.sync_api import sync_playwright
    from check_workbench_ui import DEFAULT_BROWSER
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=str(DEFAULT_BROWSER), headless=True)
        yield browser
        browser.close()


@pytest.mark.parametrize('checked,body_change,expected', [
    (True, {}, 'ok'), (False, {}, 'blocked'), (True, {'startTime': {'type': 0, 'shelfTime': None}}, 'blocked'),
    (True, {}, 'pending'),
])
def test_real_browser_submission_guard(browser, monkeypatch, checked, body_change, expected):
    from playwright.sync_api import Route
    payload = submission(**body_change)
    html = '''<meta charset="utf-8"><div class="UserArea_accountWrap__test"><div class="UserArea_shopName__test">离线测试店</div></div>
      <div id="sell-field-startTime"><label><input type="radio" CHECKED><span>放入仓库</span></label></div>
      <button onclick="fetch('/sell/v2/submit.htm',{method:'POST',body:window.payload}).then(r=>r.json()).then(()=>window.afterReceipt()).catch(()=>{})">提交宝贝信息</button>'''.replace('CHECKED', 'checked' if checked else '')
    context = browser.new_context()
    page = context.new_page()
    page.route(URL, lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=html))
    page.goto(URL)
    page.evaluate('(value)=>window.payload=value',payload)
    stop = threading.Event()
    page.expose_function('afterReceipt', lambda: stop.set() if expected == 'pending' else None)
    writes=[]
    def fulfilled(route, **kwargs):
        writes.append(route.request.post_data)
        route.fulfill(content_type='application/json', body=json.dumps({} if expected == 'pending' else receipt()))
    monkeypatch.setattr(Route, 'continue_', fulfilled)
    adapter=TaobaoWarehouseAdapter(PipelineConfig(),stop)
    try:
        if expected=='ok':
            assert adapter.submit(page,ROW)['confirmed_by']=='warehouse_receipt'
            assert len(writes)==1
        elif expected=='pending':
            from xianyu_pipeline.qianniu_browser import QianniuSavePending
            with pytest.raises(QianniuSavePending):
                adapter.submit(page,ROW)
            assert len(writes)==1
        else:
            with pytest.raises(ValueError):
                adapter.submit(page,ROW)
            assert not writes
    finally:
        context.close()
