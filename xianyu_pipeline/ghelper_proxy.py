"""Use Ghelper's saved default HTTPS route for Telegram, without running PAC code."""
from __future__ import annotations

import atexit
import asyncio
import base64
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import ssl
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote


KNOWN_IDS = {"cieikaeocafmceoapfogpffaalkncpkc"}
MAX_PREFERENCES = 16 * 1024 * 1024
MAX_PAC = 2 * 1024 * 1024
CONNECT_TIMEOUT = 12
MAX_CONNECTIONS = 40


class GhelperProxyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Route:
    profile: Path
    endpoints: tuple[tuple[str, int], ...] = field(repr=False)

    def summary(self):
        return {"profile": str(self.profile), "nodes": len(self.endpoints), "protocol": "HTTPS"}


def validate_settings(mode, profile):
    if not isinstance(mode, str) or mode not in {"manual", "ghelper"}:
        raise ValueError("Telegram 代理方式须为手动配置或 Ghelper")
    if (not isinstance(profile, str) or len(profile) > 4096
            or re.search(r"[\x00-\x1f]", profile) or profile and not Path(profile).is_absolute()):
        raise ValueError("Ghelper 配置目录须为空或浏览器用户配置目录的绝对路径")


def _read_json(path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_PREFERENCES + 1)
        if len(raw) > MAX_PREFERENCES:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, OSError):
        raise GhelperProxyError("无法读取 Ghelper 浏览器配置，请确认目录、访问权限并重试") from None


def _is_ghelper(identity, entry, profile):
    if identity in KNOWN_IDS:
        return True
    manifest = entry.get("manifest", {})
    if not isinstance(manifest, dict):
        return False
    name = manifest.get("name", "")
    if isinstance(name, str) and name.startswith("__MSG_") and name.endswith("__"):
        try:
            folder = (profile / "Extensions" / entry["path"]).resolve()
            if not folder.is_relative_to((profile / "Extensions").resolve()):
                return False
            manifest = _read_json(folder / "manifest.json")
            locale = manifest.get("default_locale", "en")
            if not re.fullmatch(r"[a-zA-Z_-]+", locale):
                return False
            name = _read_json(folder / "_locales" / locale / "messages.json").get(name[6:-2], {}).get("message", "")
        except (KeyError, TypeError, AttributeError, GhelperProxyError):
            return False
    return isinstance(name, str) and ("ghelper" in name.lower() or "谷歌上网助手" in name)


def parse_pac(uri):
    if not isinstance(uri, str) or len(uri) > MAX_PAC * 2 or not uri.startswith("data:") or "," not in uri:
        raise GhelperProxyError("Ghelper 未提供内嵌线路规则，请在浏览器中启用代理线路")
    header, content = uri.split(",", 1)
    try:
        pac = (base64.b64decode(content, validate=True).decode("utf-8")
               if header.endswith(";base64") else unquote(content, errors="strict"))
    except (ValueError, UnicodeError):
        raise GhelperProxyError("Ghelper 线路规则无法读取，请重新选择线路") from None
    if len(pac) > MAX_PAC:
        raise GhelperProxyError("Ghelper 线路规则过大")
    # Only read the static default route emitted by Ghelper. Never evaluate arbitrary JS,
    # or pick a server from PAC's unrelated speed-test/custom-domain tables.
    match = re.match(r'\s*(?:var|let|const)\s+proxy\s*=\s*("(?:[^"\\]|\\.)*")\s*;', pac)
    if not match:
        raise GhelperProxyError("暂不支持此 Ghelper 规则格式，请选择提供 HTTPS 默认线路的模式")
    try:
        value = json.loads(match[1])
    except ValueError:
        raise GhelperProxyError("Ghelper 默认线路格式无效") from None
    pieces = value.split(";")
    if not 1 <= len(pieces) <= 8:
        raise GhelperProxyError("Ghelper 默认线路数量无效")
    endpoints = []
    for piece in pieces:
        route = re.fullmatch(r"HTTPS ([a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?):([0-9]{1,5})", piece.strip())
        if not route or not 1 <= int(route[2]) <= 65535:
            raise GhelperProxyError("Ghelper 默认线路必须全部为 HTTPS 代理；不允许回退直连")
        host = route[1].lower()
        if host == "localhost" or host.endswith(".localhost") or ".." in host:
            raise GhelperProxyError("Ghelper HTTPS 节点地址无效")
        endpoint = (host, int(route[2]))
        if endpoint not in endpoints:
            endpoints.append(endpoint)
    return tuple(endpoints)


def _profile_route(profile):
    for name in ("Secure Preferences", "Preferences"):
        file = profile / name
        if not file.is_file():
            continue
        extensions = _read_json(file).get("extensions", {})
        settings = extensions.get("settings", {}) if isinstance(extensions, dict) else {}
        if not isinstance(settings, dict):
            continue
        for identity, entry in settings.items():
            if not isinstance(entry, dict) or not _is_ghelper(identity, entry, profile):
                continue
            if entry.get("disable_reasons") or entry.get("state") == 0:
                raise GhelperProxyError("Ghelper 插件已停用，请先在浏览器中启用")
            preferences = entry.get("preferences", {})
            proxy = preferences.get("proxy", {}) if isinstance(preferences, dict) else {}
            if not isinstance(proxy, dict):
                raise GhelperProxyError("Ghelper 代理配置格式无效")
            if proxy.get("mode") != "pac_script":
                raise GhelperProxyError("Ghelper 尚未启用 PAC 线路，请先在浏览器中选择线路")
            return Route(profile.resolve(), parse_pac(proxy.get("pac_url")))
    raise GhelperProxyError("该浏览器配置目录未找到 Ghelper 线路")


def discover_route(profile=""):
    validate_settings("ghelper", profile)
    if profile:
        return _profile_route(Path(profile))
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise GhelperProxyError("无法自动定位浏览器，请填写包含 Secure Preferences 的用户配置目录")
    routes, errors = [], []
    for relative in ("Google/Chrome/User Data", "Microsoft/Edge/User Data", "Quark/User Data"):
        root = Path(base) / relative
        if not root.is_dir():
            continue
        candidates = [root / "Default", *sorted(root.glob("Profile *"))]
        for candidate in candidates:
            if not candidate.is_dir():
                continue
            try:
                routes.append(_profile_route(candidate))
            except GhelperProxyError as exc:
                errors.append(str(exc))
    if len(routes) == 1:
        return routes[0]
    if len(routes) > 1:
        raise GhelperProxyError("检测到多个 Ghelper 配置，请明确填写要使用的浏览器用户配置目录")
    specific = next((error for error in errors if "未找到" not in error), None)
    raise GhelperProxyError(specific or "未找到可用 Ghelper，请先在浏览器中启用线路或指定用户配置目录")


def _destination(host, port):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise GhelperProxyError("Telegram 转接只接受数据中心 IP 地址") from None
    if not address.is_global or not 1 <= port <= 65535:
        raise GhelperProxyError("Telegram 转接不允许访问本地或私有地址")
    return f"[{address}]:{port}" if address.version == 6 else f"{address}:{port}"


class GhelperBridge:
    def __init__(self, profile):
        self.profile = Path(profile)
        self.username = "telegram"
        self.password = secrets.token_hex(24)
        self.loop = None
        self.server = None
        self.port = None
        self.tasks = set()
        self.started = threading.Event()
        self.stopping = threading.Event()
        self.error = ""
        self.thread = threading.Thread(target=self._run, name="TelegramGhelperProxy", daemon=True)
        self.thread.start()
        if not self.started.wait(5) or not self.port:
            self.close()
            raise GhelperProxyError("Telegram 本机代理转接启动失败")

    def proxy(self):
        return {"proxy_type": "socks5", "addr": "127.0.0.1", "port": self.port,
                "rdns": True, "username": self.username, "password": self.password}

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.server = self.loop.run_until_complete(asyncio.start_server(self._accepted, "127.0.0.1", 0))
            self.port = self.server.sockets[0].getsockname()[1]
            self.started.set()
            if not self.stopping.is_set():
                self.loop.run_forever()
        except Exception:
            self.error = "Telegram 本机转接服务异常"
        finally:
            self.started.set()
            if self.server:
                self.server.close()
                self.loop.run_until_complete(self.server.wait_closed())
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    def _accepted(self, reader, writer):
        if len(self.tasks) >= MAX_CONNECTIONS:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _connect(self, authority):
        route = await asyncio.to_thread(_profile_route, self.profile)
        context = ssl.create_default_context()
        context.set_alpn_protocols(["http/1.1"])
        for index, (host, port) in enumerate(route.endpoints):
            writer = None
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    addresses = await self.loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
                        raise GhelperProxyError("Ghelper 节点解析到了本地或私有地址")
                    # Preserve the original SNI; the hostname can contain account routing data.
                    reader, writer = await asyncio.open_connection(addresses[0][4][0], port,
                        ssl=context, server_hostname=host, ssl_handshake_timeout=CONNECT_TIMEOUT,
                        ssl_shutdown_timeout=1, limit=16384)
                    writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("ascii"))
                    await writer.drain()
                    header = await reader.readuntil(b"\r\n\r\n")
                    status = re.match(rb"HTTP/1\.[01] ([0-9]{3})(?: |\r)", header)
                    if not status or status[1] != b"200":
                        raise GhelperProxyError("Ghelper HTTPS 节点拒绝转接")
                self.error = ""
                return reader, writer
            except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError,
                    asyncio.LimitOverrunError, GhelperProxyError):
                if writer:
                    writer.close()
                logging.warning("Ghelper Telegram 线路 %d 连接失败，尝试后续线路", index + 1)
            except BaseException:
                if writer:
                    writer.close()
                raise
        raise GhelperProxyError("Ghelper HTTPS 线路均无法连接，请在插件中切换线路后重试；未回退直连")

    async def _handshake(self, reader, writer):
        version, count = await reader.readexactly(2)
        methods = await reader.readexactly(count)
        if version != 5 or 2 not in methods:
            writer.write(b"\x05\xff")
            await writer.drain()
            return None
        writer.write(b"\x05\x02")
        await writer.drain()
        version, count = await reader.readexactly(2)
        username = await reader.readexactly(count)
        password = await reader.readexactly((await reader.readexactly(1))[0])
        valid = (version == 1 and hmac.compare_digest(username, self.username.encode())
                 and hmac.compare_digest(password, self.password.encode()))
        writer.write(b"\x01\x00" if valid else b"\x01\x01")
        await writer.drain()
        if not valid:
            return None
        version, command, reserved, kind = await reader.readexactly(4)
        if version != 5 or command != 1 or reserved != 0:
            raise GhelperProxyError("不支持的 Telegram 转接请求")
        if kind in {1, 4}:
            host = str(ipaddress.ip_address(await reader.readexactly(4 if kind == 1 else 16)))
        elif kind == 3:
            host = (await reader.readexactly((await reader.readexactly(1))[0])).decode("ascii")
        else:
            raise GhelperProxyError("Telegram 转接地址类型无效")
        port = struct.unpack("!H", await reader.readexactly(2))[0]
        return _destination(host, port)

    async def _handle(self, reader, writer):
        upstream = None
        connected = False
        pumps = []
        try:
            authority = await asyncio.wait_for(self._handshake(reader, writer), 10)
            if authority is None:
                return
            remote, upstream = await asyncio.wait_for(self._connect(authority), 42)
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            await writer.drain()
            connected = True

            async def pump(source, target):
                while data := await source.read(65536):
                    target.write(data)
                    await target.drain()

            pumps = [asyncio.create_task(pump(reader, upstream)), asyncio.create_task(pump(remote, writer))]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError, GhelperProxyError) as exc:
            if isinstance(exc, GhelperProxyError):
                self.error = str(exc)
            elif not isinstance(exc, asyncio.IncompleteReadError):
                self.error = "Ghelper Telegram 转接连接中断或超时"
            if not connected:
                try:
                    writer.write(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
                    await writer.drain()
                except OSError:
                    pass
        finally:
            for pump in pumps:
                pump.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            for connection in (upstream, writer):
                if connection:
                    connection.close()
                    try:
                        await asyncio.wait_for(connection.wait_closed(), 2)
                    except (OSError, TimeoutError):
                        pass

    async def _probe(self):
        started = time.monotonic()
        reader, writer = await self._connect("149.154.167.51:443")
        try:
            nonce = secrets.token_bytes(16)
            packet = struct.pack("<QQII", 0, int(time.time()) << 32, 20, 0xbe7e8ef1) + nonce
            writer.write(b"\xef\x0a" + packet)
            await writer.drain()
            head = (await reader.readexactly(1))[0]
            size = int.from_bytes(await reader.readexactly(3), "little") * 4 if head == 127 else head * 4
            if not 40 <= size <= 4096:
                raise GhelperProxyError("Ghelper 未返回有效 Telegram 协议响应")
            data = await reader.readexactly(size)
            if data[20:24] != struct.pack("<I", 0x05162463) or data[24:40] != nonce:
                raise GhelperProxyError("Ghelper Telegram 协议握手校验失败")
            return {"connected": True, "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "message": "Ghelper 线路可用，Telegram 协议握手通过"}
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (OSError, TimeoutError):
                pass

    def check(self):
        future = asyncio.run_coroutine_threadsafe(asyncio.wait_for(self._probe(), 50), self.loop)
        try:
            return future.result(timeout=53)
        except (TimeoutError, OSError, asyncio.IncompleteReadError):
            future.cancel()
            raise GhelperProxyError("Ghelper Telegram 连通性检测超时或连接中断，请切换线路重试") from None

    def close(self):
        self.stopping.set()
        if self.loop and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except RuntimeError:
                pass
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=5)


_bridges = {}
_lock = threading.RLock()


def get_bridge(profile="", *, create=True):
    with _lock:
        if not create:
            bridge = _bridges.get(profile)
            return bridge if bridge and bridge.thread.is_alive() and not bridge.stopping.is_set() else None
        route = discover_route(profile)
        bridge = _bridges.get(profile)
        if bridge and bridge.profile == route.profile and bridge.thread.is_alive():
            return bridge
        if bridge:
            bridge.close()
        bridge = GhelperBridge(route.profile)
        _bridges[profile] = bridge
        logging.info("Telegram 已启用 Ghelper 线路，本机转接端口=%d，HTTPS 线路数=%d", bridge.port, len(route.endpoints))
        return bridge


def proxy_for_ghelper(profile="", *, create=True):
    bridge = get_bridge(profile, create=create)
    return bridge.proxy() if bridge else None


def check_ghelper(profile=""):
    route = discover_route(profile)
    return {**route.summary(), **get_bridge(profile).check()}


def close_bridges():
    with _lock:
        bridges = list(_bridges.values())
        _bridges.clear()
    for bridge in bridges:
        bridge.close()


atexit.register(close_bridges)
