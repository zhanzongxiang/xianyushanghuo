"""Adapter for the observed Taobao v2 copy-product form, saved off shelf."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urljoin, urlsplit

from .browser_download import DownloadCancelled

PUBLISH_HOST = "item.upload.taobao.com"
PUBLISH_PATH = "/sell/v2/publish.htm"
PICKER = '.next-overlay-wrapper.opened iframe[src*="/app/crs-qn/sucai-selector-ng/"]:visible'
MAIN = "#sell-field-mainImagesGroup"
DETAIL = "#sell-field-descRepublicOfSell"
SUBMIT_PATH = "/sell/v2/submit.htm"


def warehouse_receipt(payload, identity):
    if not isinstance(payload, dict):
        return None
    models = payload.get('models')
    if not isinstance(models, dict):
        return None
    message = models.get('globalMessage', {})
    if not isinstance(message, dict) or message.get('type') != 'success':
        return None
    value = message.get('successUrl')
    if not isinstance(value, str):
        return None
    parsed = urlsplit(urljoin(f'https://{PUBLISH_HOST}{SUBMIT_PATH}', value))
    query = parse_qs(parsed.query)
    if (parsed.scheme != 'https' or parsed.netloc != PUBLISH_HOST or parsed.path != '/sell/v2/success.htm'
            or query.get('primaryId') != [identity] or query.get('isEdit') != ['false']):
        return None
    return {'remote_url': f'https://upload.taobao.com/auction/publish/edit.htm?item_num_id={identity}&auto=false',
            'confirmed_by': 'warehouse_receipt'}


def warehouse_submission(post_data, source_url, row):
    """Check the actual browser payload before it can leave this process."""
    validate_publish_url(source_url)
    form = parse_qs(post_data or "", keep_blank_values=True)
    source = parse_qs(urlsplit(source_url).query).get('itemId')
    if form.get('copyItem') != ['true'] or form.get('itemId') != source:
        raise ValueError("提交请求不是复制新商品，已拦截")
    new_ids = form.get('copyItemNewId', [])
    if len(new_ids) != 1 or not re.fullmatch(r'[1-9][0-9]+', new_ids[0]) or new_ids == source:
        raise ValueError("无法确认新商品 ID，已拦截")
    if len(form.get('jsonBody', [])) != 1:
        raise ValueError("商品提交数据格式变化，已拦截")
    body = json.loads(form['jsonBody'][0])
    if not isinstance(body, dict):
        raise ValueError("商品提交数据格式变化，已拦截")
    if body.get('startTime') != {'type': 2, 'shelfTime': None}:
        raise ValueError("提交请求不是放入仓库，已拦截上架操作")
    if (body.get('title') != row['title'] or Decimal(str(body.get('price'))) != Decimal(row['price'])
            or str(body.get('quantity')) != str(row['stock'])):
        raise ValueError("提交请求中的标题、价格或库存与资料不一致，已拦截")
    images = body.get('mainImagesGroup', {}).get('images', [])
    if len(images) != len(row['image_indices']):
        raise ValueError("提交请求中的主图数量不一致，已拦截")
    return new_ids[0]


def validate_publish_url(url):
    value = urlsplit(url)
    query = parse_qs(value.query)
    if (value.scheme != "https" or value.netloc != PUBLISH_HOST or value.path != PUBLISH_PATH
            or query.get("copyItem") != ["true"] or len(query.get("itemId", [])) != 1
            or not re.fullmatch(r"[1-9][0-9]+", query["itemId"][0])):
        raise ValueError("仓库适配需要淘宝 v2 复制商品链接（copyItem=true 和 itemId），不能使用编辑原商品链接")


def square_images(images, directory):
    from PIL import Image, ImageOps
    result = []
    for index, source in enumerate(images):
        with Image.open(source) as original:
            rgba = ImageOps.exif_transpose(original).convert("RGBA")
            rgba.thumbnail((1440, 1440))
            square = Image.new("RGB", (1440, 1440), "white")
            square.paste(rgba, ((1440-rgba.width)//2, (1440-rgba.height)//2), rgba)
            digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()[:16]
            target = Path(directory) / f"1_material_{index + 1}_{digest}.jpg"
            square.save(target, quality=92)
            result.append(target)
    return result


class TaobaoWarehouseAdapter:
    def __init__(self, config, stop_event):
        self.config, self.stop_event = config, stop_event

    def check_stop(self):
        if self.stop_event.is_set():
            raise DownloadCancelled("千牛操作已停止")

    def wait(self, page, predicate, message, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check_stop()
            if predicate():
                return
            page.wait_for_timeout(250)
        raise ValueError(message)

    def assert_shop(self, page, row):
        shop = page.locator('[class*="UserArea_accountWrap__"] [class*="UserArea_shopName__"]:visible')
        shop.wait_for(timeout=30000)
        if shop.count() != 1 or shop.inner_text().strip() != row["shop_name"]:
            raise ValueError("发布页店铺与商品资料不一致")

    def warehouse_radio(self, page):
        return page.locator('#sell-field-startTime label').filter(
            has=page.get_by_text("放入仓库", exact=True)).locator('input[type=radio]')

    def assert_off_shelf(self, page):
        radio = self.warehouse_radio(page)
        if radio.count() != 1 or not radio.is_checked():
            raise ValueError("未确认选择放入仓库，已停止提交")
        selected = page.locator('#sell-field-startTime input[type=radio]:checked')
        if selected.count() != 1:
            raise ValueError("上架状态不明确，已停止提交")

    def fill(self, locator, value, *, numeric=False):
        self.check_stop()
        if locator.count() != 1:
            raise ValueError("发布页字段缺失或不唯一")
        locator.fill(str(value), timeout=10000)
        locator.press("Tab")
        actual = locator.input_value().strip()
        equal = Decimal(actual) == Decimal(str(value)) if numeric and actual else actual == str(value).strip()
        if not equal:
            raise ValueError("发布页字段回读不一致")

    def clear_sales(self, page):
        # Copying a product also copies its SKU pictures and pricing. Remove those
        # rows through the form before setting the new single-item price/stock.
        root = page.locator('#sell-field-saleProp')
        for _ in range(100):
            self.check_stop()
            rows = root.locator('li').filter(has=page.locator('input'))
            populated = next((row for row in rows.all() if any(
                text.strip() for text in row.locator('input').evaluate_all('els => els.map(e => e.value)'))), None)
            if populated is None:
                break
            populated.hover()
            populated.locator('button.color-delete-icon').click(timeout=10000)
            page.wait_for_timeout(200)
        if any(value.strip() for value in root.locator('input').evaluate_all('els => els.map(e => e.value)')):
            raise ValueError("复制商品的旧规格未清空，请人工核对")
        if page.locator('#sell-field-sku tbody tr:visible').count():
            raise ValueError("复制商品仍有旧 SKU，已停止保存")

    def clear_image_group(self, page, selector):
        group = page.locator(selector)
        for _ in range(10):
            picture = group.locator('.sell-component-image-v2').filter(has=page.locator('img.image-item')).first
            if not picture.count():
                return
            picture.hover()
            delete = page.locator('.next-menu-item [title="删除"]:visible')
            delete.wait_for(timeout=5000)
            if delete.count() != 1:
                raise ValueError("旧图片删除入口无法确认，请清空复制模板图片后重试")
            delete.click()
            page.wait_for_timeout(300)
        raise ValueError("复制模板图片未清空")

    def upload_images(self, page, images):
        self.clear_image_group(page, MAIN)
        for selector in ('#sell-field-threeToFourImages', '#sell-field-yinHeWhiteBgImage'):
            self.clear_image_group(page, selector)
        if page.locator('#sell-field-auctionVideos video, #sell-field-auctionVideos .solid').count():
            raise ValueError("复制商品含有旧视频，请先移除模板视频")
        with TemporaryDirectory(prefix="qianniu-images-") as folder:
            paths = square_images(images, folder)
            page.locator(MAIN + ' .image-empty').first.click()
            picker = page.frame_locator(PICKER)
            picker.get_by_role('button', name='本地上传', exact=True).wait_for(timeout=30000)
            page.wait_for_timeout(1000)
            missing = [path for path in paths if not picker.get_by_text(path.name, exact=True).count()]
            if missing:
                picker.get_by_role('button', name='本地上传', exact=True).click()
                picker.locator('input[type=file]').set_input_files([str(path) for path in missing])
                self.wait(page, lambda: picker.get_by_text(f'{len(missing)} 个文件上传成功', exact=True).is_visible(),
                          "主图上传未全部成功", seconds=180)
                picker.get_by_role('button', name='完成', exact=True).click()
            for index, path in enumerate(paths):
                self.check_stop()
                if not page.locator(PICKER).count():
                    page.locator(MAIN + ' .image-empty').first.click()
                picker = page.frame_locator(PICKER)
                card = picker.locator('[class*=PicList_pic_background__]').filter(
                    has=picker.get_by_text(path.name, exact=True))
                if card.count() > 1:
                    raise ValueError("图片空间存在重名预览，请核对后重试")
                card.hover(timeout=20000)
                card.locator('input[type=checkbox]').check(timeout=10000)
                self.wait(page, lambda: page.locator(MAIN + ' img.image-item').count() == index + 1,
                          "上传图片未选入商品主图")
                page.keyboard.press('Escape')
                if page.locator(PICKER).count():
                    page.get_by_text('当前类目', exact=True).click()
                self.wait(page, lambda: not page.locator(PICKER).count(), "图片选择窗口未关闭")
        logging.info("千牛主图已上传并选入，共 %d 张", len(images))

    def fill_description(self, page, text):
        detail = page.locator(DETAIL)
        detail.get_by_role('button', name='清空', exact=True).click()
        dialog = page.locator('.next-dialog:visible').filter(has_text='确定要清空所有模块吗')
        dialog.get_by_role('button', name='确定', exact=True).click()
        detail.locator('[data-type=text]').click()
        detail.locator('textarea[readonly]').click()
        editor = page.locator('textarea[placeholder="文本模块,此处可以输入商品详情的描述信息"]:not([readonly]):visible')
        self.fill(editor, text)
        page.get_by_role('button', name='确定', exact=True).click()
        saved = detail.locator('.viewGroup')
        self.wait(page, lambda: saved.count() == 1 and saved.inner_text().strip() == text.strip(),
                  "商品详情回读失败")
        if saved.count() != 1:
            raise ValueError("商品详情回读失败")
        if detail.locator('img.image-item').count():
            raise ValueError("复制商品仍残留旧详情图")

    def prepare(self, page, row, images):
        validate_publish_url(page.url)
        self.assert_shop(page, row)
        category = page.locator('#sell-field-category').inner_text()
        expected = row['category'].strip()
        paths = [text.strip() for text in category.splitlines() if '>>' in text]
        if len(paths) != 1 or expected not in {paths[0], paths[0].split('>>')[-1]}:
            raise ValueError("商品类目与复制页面不一致，请填写页面实际类目：" + category.replace('\n', ' '))
        if sum(1 if ord(char) < 128 else 2 for char in row['title']) > 60:
            raise ValueError("淘宝标题最多 60 字符，中文按 2 字符计算，请缩短标题")
        self.warehouse_radio(page).check()
        self.assert_off_shelf(page)
        self.fill(page.locator('#sell-field-title input'), row['title'])
        self.clear_sales(page)
        self.fill(page.locator('#sell-field-price input'), row['price'], numeric=True)
        self.fill(page.locator('#struct-quantity input'), row['stock'], numeric=True)
        purchase = page.locator('#sell-field-purchaseTips input')
        if purchase.count() == 1:
            self.fill(purchase, '')
        discount = page.locator('#sell-field-multiDiscountPromotion input[type=checkbox]')
        if discount.count() == 1 and discount.is_checked():
            discount.uncheck()
        self.upload_images(page, images)
        self.fill_description(page, row['description'])
        self.assert_shop(page, row)
        self.assert_off_shelf(page)
        if Decimal(page.locator('#sell-field-price input').input_value()) != Decimal(row['price']):
            raise ValueError("商品价格在填写期间发生变化")
        if int(page.locator('#struct-quantity input').input_value()) != row['stock']:
            raise ValueError("商品库存在填写期间发生变化")

    def save(self, page, row, images):
        self.prepare(page, row, images)
        return self.submit(page, row)

    def submit(self, page, row):
        from .qianniu_browser import QianniuSavePending
        self.check_stop()
        validate_publish_url(page.url)
        self.assert_shop(page, row)
        self.assert_off_shelf(page)
        submitted, blocked, receipts = [], [], []
        source_url = page.url

        def guard(route):
            try:
                self.check_stop()
                request = route.request
                parsed = urlsplit(request.url)
                if request.method != 'POST' or parsed.netloc != PUBLISH_HOST or parsed.path != SUBMIT_PATH:
                    raise ValueError("未知商品提交入口，已拦截")
                if submitted:
                    raise ValueError("本次保存已发送请求，已拦截重复提交")
                identity = warehouse_submission(request.post_data, source_url, row)
                self.assert_off_shelf(page)
            except Exception as exc:
                blocked.append(str(exc))
                route.abort()
                return
            submitted.append(identity)
            route.continue_()

        def completed(request):
            parsed = urlsplit(request.url)
            if parsed.netloc != PUBLISH_HOST or parsed.path != SUBMIT_PATH or not submitted:
                return
            try:
                response = request.response()
                if response and response.status == 200:
                    receipts.append(response.json())
            except Exception:
                logging.info("千牛仓库提交回执暂未解析")

        pattern = '**/sell/**/submit.htm*'
        page.route(pattern, guard)
        page.on('requestfinished', completed)
        try:
            page.get_by_role('button', name='提交宝贝信息', exact=True).click(timeout=15000)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                self.check_stop()
                if blocked:
                    raise ValueError(blocked[0])
                for payload in receipts:
                    receipt = warehouse_receipt(payload, submitted[0])
                    if receipt:
                        logging.info("千牛已确认新商品保存到仓库，商品 ID=%s", submitted[0])
                        return receipt
                errors = page.locator('.sell-component-info-wrapper-msg-content.error:visible').all_text_contents()
                if errors and not submitted:
                    raise ValueError("千牛表单校验未通过：" + "；".join(errors)[:1500])
                page.wait_for_timeout(300)
            raise RuntimeError("未取得仓库商品保存成功回执")
        except Exception as exc:
            if submitted:
                raise QianniuSavePending("仓库保存结果待确认，请先核对千牛，避免重复创建：" + str(exc)) from exc
            raise
        finally:
            try:
                page.unroute(pattern, guard)
                page.remove_listener('requestfinished', completed)
            except Exception:
                logging.info("千牛页面已关闭，提交监听已结束")
