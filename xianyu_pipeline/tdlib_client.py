"""TDLib JSON transport, local authorization and persistent download sessions."""
from __future__ import annotations

import atexit
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
import platform
import threading
import time
from concurrent.futures import Future, InvalidStateError, TimeoutError as FutureTimeout
from pathlib import Path
from uuid import uuid4


class TdLibError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(f"TDLib {code}: {message}")
        self.code = code


def runtime_status() -> dict:
    try:
        version = importlib.metadata.version("tdjson")
        return {"available": True, "version": version, "error": ""}
    except importlib.metadata.PackageNotFoundError:
        return {"available": False, "version": "", "error": "未安装 TDLib，请安装 requirements.txt"}


class _DatabaseLease:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.stream = (root / "application.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.stream.seek(0, os.SEEK_END) == 0:
                    self.stream.write(b"\0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise RuntimeError("TDLib 会话正在其他程序实例中使用，请先退出该实例的 TDLib 下载") from exc

    def close(self):
        self.stream.close()


class _JsonHub:
    """td_receive is process-wide and must have exactly one receiving thread."""
    def __init__(self):
        try:
            self.native = importlib.import_module("tdjson")
            self.native.td_execute(json.dumps({"@type": "setLogVerbosityLevel", "new_verbosity_level": 0}).encode())
            self.native.td_execute(json.dumps({"@type": "setLogStream", "log_stream": {"@type": "logStreamEmpty"}}).encode())
        except (ImportError, OSError) as exc:
            raise RuntimeError("TDLib 原生组件无法加载，请安装 requirements.txt 中的 tdjson") from exc
        self.lock = threading.RLock()
        self.clients: dict[int, TdLibClient] = {}
        self.pending: dict[tuple[int, str], Future] = {}
        self.thread = threading.Thread(target=self._receive, name="tdlib-json", daemon=True)
        self.thread.start()

    def register(self, client):
        identity = self.native.td_create_client_id()
        with self.lock:
            self.clients[identity] = client
        return identity

    def request(self, identity: int, query: dict) -> Future:
        future = Future()
        token = uuid4().hex
        key = identity, token
        with self.lock:
            self.pending[key] = future
        try:
            self.native.td_send(identity, json.dumps({**query, "@extra": token}, ensure_ascii=False).encode("utf-8"))
        except Exception as exc:
            with self.lock:
                self.pending.pop(key, None)
            future.set_exception(exc)

        def cancelled(done):
            if done.cancelled():
                with self.lock:
                    self.pending.pop(key, None)

        future.add_done_callback(cancelled)
        return future

    def _receive(self):
        while True:
            raw = self.native.td_receive(0.2)
            if not raw:
                continue
            try:
                message = json.loads(raw)
                identity = message.get("@client_id")
                with self.lock:
                    client = self.clients.get(identity)
                    future = self.pending.pop((identity, message.get("@extra")), None)
                if client is not None:
                    client.observe(message)
                if future is not None and not future.done():
                    if message.get("@type") == "error":
                        text = str(message.get("message", "请求失败"))
                        if client is not None:
                            text = text.replace(client.api_hash, "[hidden]")
                        future.set_exception(TdLibError(message.get("code", 0), text))
                    else:
                        future.set_result(message)
            except (ValueError, TypeError, KeyError, InvalidStateError):
                continue

    def unregister(self, identity):
        with self.lock:
            self.clients.pop(identity, None)
            pending = [self.pending.pop(key) for key in list(self.pending) if key[0] == identity]
        for future in pending:
            if not future.done():
                future.set_exception(RuntimeError("TDLib 会话已关闭"))


_hub = None
_hub_lock = threading.Lock()


def _get_hub():
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = _JsonHub()
        return _hub


class TdLibClient:
    def __init__(self, api_id: int, api_hash: str, session_path: str | Path,
                 download_dir: Path, proxy: dict | None = None):
        profile = Path(session_path).resolve()
        self.root = profile.parent / f"{profile.stem}-tdlib-{api_id}"
        self.files_dir = self.root / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.api_id, self.api_hash, self.proxy = api_id, api_hash, proxy
        self.lease = _DatabaseLease(self.root)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.state = {"@type": "authorizationStateWaitTdlibParameters"}
        self.error = ""
        self.network_ready = False
        self.closed = False
        self.login_method = ""
        self.qr_requested = False
        self.auth_pending = False
        self.files: dict[int, dict] = {}
        self.active_files: set[int] = set()
        try:
            self.hub = _get_hub()
            self.identity = self.hub.register(self)
            self.send({"@type": "getAuthorizationState"})
        except BaseException:
            self.lease.close()
            raise

    def send(self, query: dict) -> Future:
        return self.hub.request(self.identity, query)

    def call(self, query: dict, timeout: float = 30) -> dict:
        future = self.send(query)
        try:
            return future.result(timeout)
        except FutureTimeout as exc:
            future.cancel()
            raise TimeoutError(f"TDLib {query['@type']} 等待超过 {timeout:g} 秒") from exc

    async def call_async(self, query: dict, timeout: float = 30) -> dict:
        import asyncio
        try:
            return await asyncio.wait_for(asyncio.wrap_future(self.send(query)), timeout)
        except TimeoutError as exc:
            raise TimeoutError(f"TDLib {query['@type']} 等待超过 {timeout:g} 秒") from exc

    def _automatic(self, query, callback=None):
        def done(future):
            try:
                future.result()
                if callback:
                    callback()
            except Exception:
                # Authentication responses must never put submitted credentials in logs.
                with self.changed:
                    self.error = f"TDLib {query['@type']} 失败，请检查网络和 API 配置后重试"
                    self.changed.notify_all()
        self.send(query).add_done_callback(done)

    def observe(self, message: dict):
        kind = message.get("@type")
        if kind == "updateFile":
            message = message["file"]
            kind = "file"
        if kind == "file":
            with self.changed:
                self.files[message["id"]] = message
                self.changed.notify_all()
        elif kind == "updateAuthorizationState":
            self._authorization(message["authorization_state"])
        elif isinstance(kind, str) and kind.startswith("authorizationState"):
            self._authorization(message)

    def _authorization(self, state):
        kind = state["@type"]
        with self.changed:
            previous = self.state["@type"]
            self.state = dict(state)
            self.error = ""
            self.changed.notify_all()
        if kind == "authorizationStateWaitTdlibParameters" and previous == kind and not hasattr(self, "_parameters_sent"):
            self._parameters_sent = True
            self._automatic({
                "@type": "setTdlibParameters", "use_test_dc": False,
                "database_directory": str(self.root / "database"), "files_directory": str(self.files_dir),
                "database_encryption_key": "", "use_file_database": True,
                "use_chat_info_database": True, "use_message_database": True,
                "use_secret_chats": False, "api_id": self.api_id, "api_hash": self.api_hash,
                "system_language_code": "zh-hans", "device_model": "Material Pipeline",
                "system_version": platform.system(), "application_version": "1.0",
            }, self._configure_proxy)
        elif kind == "authorizationStateWaitEncryptionKey":
            self._automatic({"@type": "checkDatabaseEncryptionKey", "encryption_key": ""})
        elif kind == "authorizationStateWaitPhoneNumber":
            self._request_qr()

    def _configure_proxy(self):
        def ready():
            with self.changed:
                self.network_ready = True
                self.changed.notify_all()
            self._request_qr()

        if self.proxy:
            auth = {"username": self.proxy.get("username", ""), "password": self.proxy.get("password", "")}
            proxy_type = ({"@type": "proxyTypeSocks5", **auth}
                          if self.proxy["proxy_type"] == "socks5" else
                          {"@type": "proxyTypeHttp", **auth, "http_only": False})
            self._automatic({"@type": "addProxy", "enable": True, "proxy": {
                "@type": "proxy", "server": self.proxy["addr"], "port": self.proxy["port"], "type": proxy_type,
            }}, ready)
        else:
            self._automatic({"@type": "disableProxy"}, ready)

    def _request_qr(self):
        with self.lock:
            if (not self.network_ready or self.login_method != "qr" or self.qr_requested
                    or self.state["@type"] != "authorizationStateWaitPhoneNumber"):
                return
            self.qr_requested = True
            self.error = ""

        def done(future):
            try:
                future.result()
            except Exception:
                with self.changed:
                    self.qr_requested = False
                    self.error = "TDLib 二维码获取失败，请检查代理后重试或使用手机号登录"
                    self.changed.notify_all()

        self.send({"@type": "requestQrCodeAuthentication", "other_user_ids": []}).add_done_callback(done)

    def login(self, method="qr"):
        if not isinstance(method, str) or method not in {"qr", "phone"}:
            raise ValueError("TDLib 登录方式无效")
        with self.lock:
            self.login_method = method
        self._request_qr()
        return self.status()

    def submit(self, field: str, value: str):
        expected = {
            "phone": ("authorizationStateWaitPhoneNumber",
                      {"@type": "setAuthenticationPhoneNumber", "phone_number": value}),
            "code": ("authorizationStateWaitCode", {"@type": "checkAuthenticationCode", "code": value}),
            "password": ("authorizationStateWaitPassword",
                         {"@type": "checkAuthenticationPassword", "password": value}),
            "email": ("authorizationStateWaitEmailAddress",
                      {"@type": "setAuthenticationEmailAddress", "email_address": value}),
            "email_code": ("authorizationStateWaitEmailCode", {"@type": "checkAuthenticationEmailCode",
                           "code": {"@type": "emailAddressAuthenticationCode", "code": value}}),
        }
        if not isinstance(field, str) or field not in expected or not isinstance(value, str) or not value or len(value) > 512:
            raise ValueError("TDLib 登录信息无效")
        state, query = expected[field]
        with self.lock:
            if self.state["@type"] != state:
                raise ValueError("TDLib 登录步骤已变化，请刷新登录状态")
            if self.auth_pending:
                raise ValueError("TDLib 正在验证，请等待登录状态更新")
            self.error = ""
            self.auth_pending = True

        def submitted(future):
            with self.changed:
                self.auth_pending = False
                try:
                    future.result()
                except Exception:
                    self.error = "TDLib 登录未通过，请核对输入内容、验证码有效期和网络"
                self.changed.notify_all()

        self.send(query).add_done_callback(submitted)
        return self.status()

    def status(self):
        names = {
            "authorizationStateWaitTdlibParameters": "initializing",
            "authorizationStateWaitEncryptionKey": "initializing",
            "authorizationStateWaitPhoneNumber": "qr_pending" if self.login_method == "qr" else "phone",
            "authorizationStateWaitOtherDeviceConfirmation": "qr",
            "authorizationStateWaitCode": "code", "authorizationStateWaitPassword": "password",
            "authorizationStateWaitEmailAddress": "email", "authorizationStateWaitEmailCode": "email_code",
            "authorizationStateReady": "ready" if self.network_ready else "initializing",
            "authorizationStateClosing": "closing", "authorizationStateClosed": "closed",
            "authorizationStateLoggingOut": "closing",
        }
        with self.lock:
            kind = self.state["@type"]
            result = {"state": names.get(kind, "unsupported"), "authorized": kind == "authorizationStateReady"
                      and self.network_ready, "error": self.error, "active_files": len(self.active_files),
                      "auth_pending": self.auth_pending}
            if kind == "authorizationStateWaitOtherDeviceConfirmation":
                revision = hashlib.sha256(self.state["link"].encode()).hexdigest()[:16]
                result["qr_url"] = f"/api/tdlib/qr?v={revision}"
            if kind == "authorizationStateWaitPassword":
                result["hint"] = self.state.get("password_hint", "")[:120]
            if result["state"] == "unsupported":
                result["error"] = "请先在 Telegram 官方客户端完成账号注册或额外验证，再重试 TDLib 登录"
            return result

    def qr_png(self):
        with self.lock:
            if self.state["@type"] != "authorizationStateWaitOtherDeviceConfirmation":
                raise ValueError("TDLib 登录二维码暂未生成或已失效")
            link = self.state["link"]
        import qrcode
        image = qrcode.make(link)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def ensure_ready(self, timeout=20):
        deadline = time.monotonic() + timeout
        with self.changed:
            while True:
                state = self.status()
                if state["authorized"]:
                    return
                if state["error"]:
                    raise RuntimeError(state["error"])
                if state["state"] not in {"initializing"}:
                    raise RuntimeError("TDLib 尚未登录，请在设置中点击“登录 TDLib”完成授权")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("TDLib 初始化超时，请检查代理并在设置中查看登录状态")
                self.changed.wait(min(remaining, .5))

    def close(self):
        if self.closed:
            return
        try:
            if self.status()["state"] != "closed":
                self.call({"@type": "close"}, timeout=5)
            deadline = time.monotonic() + 10
            with self.changed:
                while self.status()["state"] != "closed" and time.monotonic() < deadline:
                    self.changed.wait(.2)
            if self.status()["state"] != "closed":
                raise RuntimeError("TDLib 尚未关闭，请等待下载连接结束后重试")
        finally:
            if self.status()["state"] == "closed":
                self.closed = True
                self.hub.unregister(self.identity)
                self.lease.close()


_clients = {}
_clients_lock = threading.RLock()


def get_client(api_id, api_hash, session_path, download_dir, proxy=None) -> TdLibClient:
    profile = str(Path(session_path).resolve())
    key = profile, api_id, api_hash, json.dumps(proxy, sort_keys=True)
    with _clients_lock:
        for old_key, client in list(_clients.items()):
            if old_key[:2] == key[:2] and old_key != key:
                if client.status()["active_files"]:
                    raise RuntimeError("TDLib 正在下载，不能更换账号或代理")
                client.close()
                _clients.pop(old_key)
        if key not in _clients or _clients[key].closed:
            _clients[key] = TdLibClient(api_id, api_hash, session_path, download_dir, proxy)
        return _clients[key]


def client_for_config(config, *, create=True):
    from .adapters import parse_telegram_api_proxy
    if config.telegram_proxy_mode == "ghelper":
        from .ghelper_proxy import proxy_for_ghelper
        proxy = proxy_for_ghelper(config.telegram_ghelper_profile, create=create)
        if not create and proxy is None:
            return None
    else:
        proxy = parse_telegram_api_proxy(config.telegram_api_proxy_url)
    key = (str(Path(config.telegram_session).resolve()), config.telegram_api_id, config.telegram_api_hash,
           json.dumps(proxy, sort_keys=True))
    if not create:
        with _clients_lock:
            return _clients.get(key)
    return get_client(config.telegram_api_id, config.telegram_api_hash, config.telegram_session, config.inbox_dir, proxy)


def status_for_config(config):
    status = runtime_status()
    client = client_for_config(config, create=False)
    return {**status, **(client.status() if client else {"state": "idle", "authorized": False, "active_files": 0})}


def close_clients():
    with _clients_lock:
        clients = list(_clients.values())
    for client in clients:
        try:
            client.close()
        except Exception:
            pass


atexit.register(close_clients)
