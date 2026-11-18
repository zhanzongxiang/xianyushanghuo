"""Read shop identity from a seller page without treating account nicknames as shops."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit

QIANNIU_HOME_URL = "https://myseller.taobao.com/home.htm"
SELLER_HOSTS = {"myseller.taobao.com", "qn.taobao.com", "sellercenter.taobao.com",
                "sell.taobao.com", "item.publish.taobao.com", "item.upload.taobao.com"}
LOGIN_HOSTS = {"loginmyseller.taobao.com", "login.taobao.com", "login.tmall.com",
               "havanlogin.taobao.com", "passport.taobao.com"}
GENERIC_NAMES = {"我的店铺", "进入店铺", "查看店铺", "店铺首页", "店铺名称", "店铺",
                 "千牛", "千牛卖家工作台", "卖家中心", "登录", "立即登录", "切换店铺"}
SHOP_INFO_PATH = "/h5/mtop.taobao.jdy.resource.shop.info.get/1.0/"


def seller_page(url: str) -> bool:
    try:
        value = urlsplit(url)
        return (value.scheme == "https" and value.hostname in SELLER_HOSTS
                and not value.username and not value.password and value.port in {None, 443})
    except ValueError:
        return False


def login_page(url: str) -> bool:
    try:
        value = urlsplit(url)
        return value.scheme == "https" and value.hostname in LOGIN_HOSTS
    except ValueError:
        return False


def shop_info_response(url: str) -> bool:
    try:
        value = urlsplit(url)
        return (value.scheme == "https" and value.hostname == "h5api.m.taobao.com"
                and value.path == SHOP_INFO_PATH and not value.username and not value.password
                and value.port in {None, 443})
    except ValueError:
        return False


def shop_response_payload(body: bytes, url: str):
    try:
        return json.loads(body)
    except ValueError:
        if not shop_info_response(url):
            raise
    # MTOP loads this response as a script; decode its JSON argument without executing it.
    match = re.fullmatch(r"\s*([a-zA-Z_$][\w$]*)\s*\((.*)\)\s*;?\s*",
                         body.decode("utf-8-sig"), re.DOTALL)
    callbacks = parse_qs(urlsplit(url).query).get("callback", [])
    if not match or callbacks != [match[1]]:
        raise ValueError("店铺信息 JSONP 回调格式不匹配")
    return json.loads(match[2])


def shop_name(value) -> str:
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        return ""
    value = value.strip()
    return value if 1 <= len(value) <= 100 and value not in GENERIC_NAMES else ""


def unique_shops(candidates: list[dict]) -> list[dict]:
    output = {}
    for row in candidates:
        name = shop_name(row.get("name"))
        if not name:
            continue
        identity = row.get("shop_id", "")
        identity = str(identity) if type(identity) in {int, str} and re.fullmatch(r"[1-9][0-9]{0,23}", str(identity)) else ""
        key = (name, identity)
        output[key] = {"name": name, "shop_id": identity, "source": row.get("source", "页面店铺信息")}
    # DOM often exposes just the name while the seller response also supplies its ID.
    with_id = {name for name, identity in output if identity}
    return [row for (name, identity), row in output.items() if identity or name not in with_id][:20]


def payload_shops(payload) -> list[dict]:
    """Only paired shop ID/name objects, outside lists, count as identity evidence."""
    output, remaining = [], 300

    def visit(value, depth=0):
        nonlocal remaining
        remaining -= 1
        if depth > 6 or remaining < 0 or not isinstance(value, dict):
            return
        for name_key, id_key in (("shopName", "shopId"), ("shop_name", "shop_id"),
                                 ("shopTitle", "shopId"), ("shop_title", "shop_id")):
            if name_key in value and id_key in value:
                output.append({"name": value[name_key], "shop_id": value[id_key],
                               "source": "卖家页面店铺信息"})
        for child in value.values():
            if isinstance(child, dict):
                visit(child, depth + 1)

    visit(payload)
    return [row for row in unique_shops(output) if row["shop_id"]]


DOM_SHOPS = r"""() => {
  const rows = [];
  const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== "hidden"
    && getComputedStyle(el).display !== "none";
  const add = (name, shop_id = "") => { if (rows.length < 40) rows.push({name, shop_id, source: "页面店铺信息"}); };
  for (const el of document.querySelectorAll("[data-shop-name], [data-shopname]")) {
    if (visible(el)) add(el.getAttribute("data-shop-name") || el.getAttribute("data-shopname"),
      el.getAttribute("data-shop-id") || el.getAttribute("data-shopid") || "");
  }
  // The current Qianniu home renders the shop in its right-side profile module.
  const shopNames = document.querySelectorAll(
    '#qn-workbench-inner-container [class*="ShopInfo_name__"], #qn-worbench-container [class*="ShopInfo_name__"]');
  for (const el of shopNames) {
    if (visible(el)) add(el.getAttribute("title") || el.innerText.trim());
  }
  if (location.hostname === 'item.upload.taobao.com') {
    for (const el of document.querySelectorAll('[class*="UserArea_accountWrap__"] [class*="UserArea_shopName__"]')) {
      if (visible(el)) add(el.innerText.trim());
    }
  }
  const headers = document.querySelectorAll("header, nav, [role=banner], #header, #J_Header, .header, .top-header, .seller-header");
  for (const root of headers) {
    for (const el of root.querySelectorAll("a[href]")) {
      if (!visible(el)) continue;
      try {
        const url = new URL(el.href, location.href);
        const match = /^shop([1-9][0-9]*)\.taobao\.com$/.exec(url.hostname);
        if (url.protocol === "https:" && !url.username && !url.password &&
            (match || url.hostname === "store.taobao.com" || url.hostname === "shop.taobao.com")) {
          const id = match?.[1] || url.searchParams.get("shop_id") || "";
          if (el.innerText.trim()) add(el.innerText.trim(), id);
          if (el.title) add(el.title.trim(), id);
        }
      } catch {}
    }
    for (const el of root.querySelectorAll("[class*=shop-name], [class*=shopName], [class*=shop_name]")) {
      if (visible(el) && !el.children.length) add(el.innerText.trim());
    }
    const match = root.innerText.match(/(?:^|\n)\s*店铺名称\s*[:：]\s*([^\n]{1,100})(?:\n|$)/);
    if (match) add(match[1].trim());
  }
  return rows;
}"""

DOM_LOGIN_STATE = r"""() => {
  const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== "hidden";
  const controls = document.querySelectorAll(
    "input[type=password], input[name=TPL_username], #fm-login-id, #J_LoginBox, .login-box");
  if ([...controls].some(visible)) return "waiting";
  const text = (document.body?.innerText || "").trim();
  if (/(?:扫码登录|密码登录|短信登录|欢迎登录做生意上千牛)/.test(text)) return "waiting";
  if (document.readyState === "loading" || !text || /^(?:加载中|正在加载)[.。…\s]*$/.test(text)) return "loading";
  return "recognizing";
}"""


def page_login_state(page) -> str:
    """A loaded seller page starts a bounded search; it does not prove authentication."""
    if login_page(page.url):
        return "waiting"
    if not seller_page(page.url):
        return "loading"
    state = page.evaluate(DOM_LOGIN_STATE)
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            if not frame.frame_element().is_visible():
                continue
            if login_page(frame.url):
                return "waiting"
            if seller_page(frame.url) and frame.evaluate(DOM_LOGIN_STATE) == "waiting":
                return "waiting"
        except Exception:
            continue
    return state


def page_shops(page, selector: str = "") -> list[dict]:
    if not seller_page(page.url):
        return []
    if selector:
        target = page.locator(selector)
        if target.count() == 1 and target.is_visible():
            name = shop_name(target.inner_text(timeout=1000))
            if name:
                return [{"name": name, "shop_id": "", "source": "已配置的店铺控件"}]
    return unique_shops(page.evaluate(DOM_SHOPS))


class ShopDetector:
    def __init__(self, selector=""):
        self.selector = selector
        self.responses = []

    def request_finished(self, request):
        try:
            response = request.response()
            if response is not None:
                self.response(response)
        except Exception:
            return

    def response(self, response):
        # Read only completed responses, so an unfinished body cannot hold up login.
        try:
            mtop_shop = shop_info_response(response.url)
            resource_types = {"fetch", "xhr", "script"} if mtop_shop else {"fetch", "xhr"}
            mime = response.headers.get("content-type", "").lower()
            if ((not seller_page(response.url) and not mtop_shop)
                    or response.request.resource_type not in resource_types
                    or not seller_page(response.request.frame.url)
                    or not 200 <= response.status < 300
                    or not ("json" in mime or mtop_shop and "javascript" in mime)):
                return
            length = response.headers.get("content-length", "")
            if length and (not length.isdigit() or not 0 < int(length) <= 256 * 1024):
                return
            body = response.body()
            if not 0 < len(body) <= 256 * 1024:
                return
            self.responses = unique_shops(self.responses + payload_shops(shop_response_payload(body, response.url)))
        except Exception:
            # An unrelated or unfinished response must not interrupt login.
            return

    def login_state(self, pages) -> str:
        states = []
        for page in pages:
            try:
                states.append(page_login_state(page))
            except Exception:
                continue
        # A stale login tab must not hide a newly opened seller workbench.
        if "recognizing" in states:
            return "recognizing"
        return "waiting" if "waiting" in states else "loading"

    def scan(self, pages) -> list[dict]:
        output = []
        for page in pages:
            for frame in page.frames:
                try:
                    rows = page_shops(frame, self.selector)
                except Exception:
                    continue
                if rows and rows[0]["source"] == "已配置的店铺控件":
                    return rows
                output.extend(rows)
        return unique_shops(output + self.responses)
