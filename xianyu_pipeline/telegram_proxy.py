"""A stable Telegram SOCKS entry point with browser-first automatic routing."""
from __future__ import annotations

import atexit
import asyncio
import ipaddress
import logging
import threading
import time

from . import ghelper_proxy
from .ghelper_proxy import GhelperBridge, GhelperProxyError, probe_telegram


DEFAULT_LOCAL_PROXY = "socks5://127.0.0.1:7890"
HEALTH_INTERVAL = 15
ROUTE_TIMEOUT = 12
PROBE_AUTHORITY = "149.154.167.51:443"
LABELS = {"browser": "浏览器 Ghelper", "local": "本地代理"}


async def _close(writer):
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 1)
    except (OSError, TimeoutError):
        pass


class TelegramProxyBridge(GhelperBridge):
    def __init__(self, profile="", local_url=""):
        from .adapters import parse_telegram_api_proxy

        self.profile_hint = profile
        self.local_url = local_url or DEFAULT_LOCAL_PROXY
        parse_telegram_api_proxy(self.local_url)
        self.health = {}
        self.checked_at = float("-inf")
        self.health_lock = None
        self.monitor = None
        self.selected = ""
        super().__init__(profile)

    async def _open_route(self, source, authority):
        if source == "browser":
            route = await asyncio.to_thread(ghelper_proxy.discover_route, self.profile_hint)
            self.profile = route.profile
            return await super()._connect(
                authority, connect_timeout=min(4, ROUTE_TIMEOUT / len(route.endpoints)))
        from python_socks.async_.asyncio import Proxy

        host, port = authority.rsplit(":", 1)
        host = str(ipaddress.ip_address(host.strip("[]")))
        sock = await Proxy.from_url(self.local_url).connect(
            dest_host=host, dest_port=int(port), timeout=ROUTE_TIMEOUT)
        try:
            return await asyncio.open_connection(sock=sock)
        except BaseException:
            sock.close()
            raise

    async def _check_route(self, source):
        started = time.monotonic()
        writer = None
        try:
            async with asyncio.timeout(ROUTE_TIMEOUT):
                reader, writer = await self._open_route(source, PROBE_AUTHORITY)
                await probe_telegram(reader, writer)
            return {"connected": True, "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "message": "Telegram 协议握手通过"}
        except Exception:
            # Upstream exceptions can contain account-specific hostnames or proxy credentials.
            return {"connected": False, "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "message": ("未发现可用线路或 Telegram 握手失败" if source == "browser"
                                else "代理未启动或 Telegram 握手失败")}
        finally:
            if writer:
                await _close(writer)

    async def _refresh_health(self, *, force=False):
        if self.health_lock is None:
            self.health_lock = asyncio.Lock()
        async with self.health_lock:
            if not force and time.monotonic() - self.checked_at < HEALTH_INTERVAL:
                return
            browser, local = await asyncio.gather(
                self._check_route("browser"), self._check_route("local"))
            health = {"browser": browser, "local": local}
            before = tuple(self.health.get(key, {}).get("connected") for key in LABELS)
            after = tuple(health[key]["connected"] for key in LABELS)
            self.health = health
            self.checked_at = time.monotonic()
            if before != after:
                logging.info("Telegram 代理检测: 浏览器=%s, 本地=%s",
                             "可用" if after[0] else "不可用", "可用" if after[1] else "不可用")
            if self.monitor is None:
                self.monitor = asyncio.create_task(self._monitor())

    async def _monitor(self):
        while True:
            await asyncio.sleep(HEALTH_INTERVAL)
            await self._refresh_health()

    async def _connect(self, authority):
        await self._refresh_health()
        available = [source for source in LABELS if self.health[source]["connected"]]
        if not available:
            self.error = "Telegram 浏览器与本地代理均不可用，请启用 Ghelper 线路或启动本地代理"
            raise GhelperProxyError(self.error)
        # All media DCs and reconnects enter here; the caller's proxy never changes.
        order = available + [source for source in LABELS if source not in available]
        for source in order:
            try:
                async with asyncio.timeout(ROUTE_TIMEOUT):
                    streams = await self._open_route(source, authority)
            except Exception:
                self.checked_at = float("-inf")
                self.health = {**self.health, source: {
                    "connected": False, "elapsed_ms": 0, "message": "连接失败，等待重新检测"}}
                logging.warning("Telegram %s连接失败，尝试另一代理渠道", LABELS[source])
                continue
            if self.selected != source:
                logging.info("Telegram 新连接使用%s", LABELS[source])
            self.selected = source
            self.error = ""
            return streams
        self.error = "Telegram 两条代理线路连接均失败，请检查浏览器线路或本地代理"
        raise GhelperProxyError(self.error)

    async def _probe(self):
        await self._refresh_health(force=True)
        selected = next((source for source in LABELS if self.health[source]["connected"]), "")
        message = (f"优先使用{LABELS[selected]}" if selected else "浏览器与本地代理均不可用")
        return {"connected": bool(selected), "selected": selected, "message": message,
                "routes": {key: dict(value) for key, value in self.health.items()},
                "interval_seconds": HEALTH_INTERVAL}

    def check(self):
        future = asyncio.run_coroutine_threadsafe(self._probe(), self.loop)
        try:
            return future.result(timeout=ROUTE_TIMEOUT * 2 + 4)
        except TimeoutError:
            future.cancel()
            raise GhelperProxyError("Telegram 代理检测超时，请稍后重试") from None


_bridges = {}
_lock = threading.RLock()


def get_bridge(profile="", local_url="", *, create=True):
    from .adapters import parse_telegram_api_proxy
    from .ghelper_proxy import validate_settings
    validate_settings("auto", profile)
    parse_telegram_api_proxy(local_url)
    key = profile, local_url or DEFAULT_LOCAL_PROXY
    with _lock:
        bridge = _bridges.get(key)
        if bridge and bridge.thread.is_alive() and not bridge.stopping.is_set():
            return bridge
        if not create:
            return None
        if bridge:
            bridge.close()
        bridge = TelegramProxyBridge(*key)
        _bridges[key] = bridge
        logging.info("Telegram 自动代理已启用，本机转接端口=%d，优先浏览器 Ghelper", bridge.port)
        return bridge


def proxy_for_telegram(profile="", local_url="", *, create=True):
    bridge = get_bridge(profile, local_url, create=create)
    return bridge.proxy() if bridge else None


def check_telegram_proxy(profile="", local_url=""):
    bridge = get_bridge(profile, local_url, create=False)
    if bridge:
        return bridge.check()
    # Unsaved settings checks must not leave a separate monitor running for each draft.
    bridge = TelegramProxyBridge(profile, local_url)
    try:
        return bridge.check()
    finally:
        bridge.close()


def close_bridges():
    with _lock:
        bridges = list(_bridges.values())
        _bridges.clear()
    for bridge in bridges:
        bridge.close()


atexit.register(close_bridges)
