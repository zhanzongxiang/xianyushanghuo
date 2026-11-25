"""Interactive Telethon authorization owned by one short-lived background loop."""
from __future__ import annotations

import asyncio
import io
import logging
import re
import threading
import time
from pathlib import Path
from uuid import uuid4


LOGIN_TIMEOUT = 300
REQUEST_TIMEOUT = 60


class TelegramLoginBusy(RuntimeError):
    pass


class TelegramLogin:
    def __init__(self):
        self.lock = threading.RLock()
        self.thread = None
        self.loop = self.task = self.inputs = None
        self.cancel_requested = False
        self.png = None
        self.qr_revision = ""
        self.expires_at = 0
        self.data = {"state": "idle", "active": False, "authorized": False,
                     "auth_pending": False, "error": "", "ticket": "", "method": "qr"}

    @property
    def active(self):
        with self.lock:
            return self.data["active"]

    def status(self):
        with self.lock:
            result = dict(self.data)
            if self.png is not None and self.data["state"] == "qr":
                result.update(qr_url=f"/api/telegram-auth/qr?ticket={self.data['ticket']}&v={self.qr_revision}",
                              expires_in=max(0, int(self.expires_at - time.time())))
            return result

    def _state(self, state, **values):
        with self.lock:
            self.data.update({"state": state, "auth_pending": False, "error": "", **values})
            if state != "qr":
                self.png = None

    def start(self, adapter_factory, method="qr"):
        if not isinstance(method, str) or method not in {"qr", "phone"}:
            raise ValueError("Telegram 登录方式无效")
        with self.lock:
            if self.active:
                if self.data["method"] == method and self.data["state"] != "closing":
                    return self.status()
                raise TelegramLoginBusy("请先关闭当前 Telegram 登录，再切换登录方式")
            self.data = {"state": "initializing", "active": True, "authorized": False,
                         "auth_pending": False, "error": "", "ticket": uuid4().hex, "method": method}
            self.png = None
            self.cancel_requested = False
            self.thread = threading.Thread(target=self._worker, args=(adapter_factory, method),
                                           name="telegram-login", daemon=True)
            try:
                self.thread.start()
            except Exception:
                self.data.update(state="failed", active=False, error="Telegram 登录线程未启动，请重试")
                raise RuntimeError(self.data["error"]) from None
            return self.status()

    def _ticket(self, ticket):
        if not isinstance(ticket, str) or not ticket or ticket != self.data["ticket"]:
            raise ValueError("Telegram 登录窗口已失效，请重新打开")

    def submit(self, ticket, field, value):
        if not isinstance(field, str) or field not in {"phone", "code", "password"}:
            raise ValueError("Telegram 登录字段无效")
        if not isinstance(value, str) or not value or len(value) > 512:
            raise ValueError("请输入有效的 Telegram 登录信息")
        if field == "phone":
            value = value.strip()
            if not re.fullmatch(r"\+?[1-9][0-9]{6,14}", value):
                raise ValueError("请输入包含国际区号的手机号，例如 +8613800000000")
            value = "+" + value.lstrip("+")
        if field == "code":
            value = value.strip()
            if not re.fullmatch(r"[0-9]{4,8}", value):
                raise ValueError("请输入 Telegram 发来的数字验证码")
        with self.lock:
            self._ticket(ticket)
            if not self.active or self.data["state"] != field or self.inputs is None:
                raise ValueError("Telegram 登录步骤已变化，请等待状态更新")
            if self.data["auth_pending"]:
                raise TelegramLoginBusy("Telegram 正在验证，请等待结果")
            self.data.update(auth_pending=True, error="")
            self.loop.call_soon_threadsafe(self.inputs.put_nowait, value)
            return self.status()

    def cancel(self, ticket):
        with self.lock:
            self._ticket(ticket)
            if self.active and not self.cancel_requested:
                self.cancel_requested = True
                self._state("closing")
                if self.loop is not None and self.task is not None:
                    self.loop.call_soon_threadsafe(self.task.cancel)
            return self.status()

    def qr_png(self, ticket, revision):
        with self.lock:
            self._ticket(ticket)
            if (self.data["state"] != "qr" or self.png is None
                    or revision != self.qr_revision or time.time() >= self.expires_at):
                raise ValueError("Telegram 二维码已过期或暂未生成")
            return self.png

    def _worker(self, adapter_factory, method):
        try:
            asyncio.run(self._run(adapter_factory, method))
        except Exception as exc:
            logging.warning("Telegram 登录线程已退出，错误类型=%s", type(exc).__name__)
            with self.lock:
                self._state("failed", error="Telegram 登录组件异常，请检查依赖后重试")
                self.data.update(active=False, authorized=False)
                self.loop = self.task = self.inputs = None

    async def _request(self, awaitable):
        return await asyncio.wait_for(awaitable, REQUEST_TIMEOUT)

    async def _run(self, adapter_factory, method):
        from telethon import errors

        client = None
        result, error = "failed", "Telegram 登录未完成，请重试"
        with self.lock:
            self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
            self.inputs = asyncio.Queue()
        try:
            async with asyncio.timeout(LOGIN_TIMEOUT):
                if self.cancel_requested:
                    raise asyncio.CancelledError
                adapter = adapter_factory()
                Path(adapter.session_path).parent.mkdir(parents=True, exist_ok=True)
                adapter._check_proxy()
                client = adapter._client()
                client.flood_sleep_threshold = 0
                await self._request(client.connect())
                if not await self._request(client.is_user_authorized()):
                    if method == "qr":
                        await self._qr(client)
                    else:
                        await self._phone(client)
                if not await self._request(client.is_user_authorized()):
                    raise RuntimeError("Authorization incomplete")
                result, error = "ready", ""
                self._state("finishing")
        except asyncio.CancelledError:
            result, error = "cancelled", ""
        except TimeoutError:
            result, error = "expired", "登录等待超时，请检查代理后重新获取二维码或使用手机号登录"
        except errors.FloodWaitError as exc:
            error = f"Telegram 请求过于频繁，请等待 {exc.seconds} 秒后重试"
        except errors.PhoneNumberUnoccupiedError:
            error = "请先在 Telegram 官方客户端注册账号，再登录本系统"
        except (ConnectionError, OSError):
            error = "Telegram 连接失败，请检查浏览器代理或本地代理后重试"
        except Exception as exc:
            # RPC errors can contain submitted credentials. Log only their type.
            logging.warning("Telegram 页面登录失败，错误类型=%s", type(exc).__name__)
            error = "Telegram 登录失败，请检查 API 配置、代理和账号状态后重试"
        finally:
            with self.lock:
                self.data.update(state="closing" if result != "ready" else "finishing", auth_pending=False)
                self.png = None
            if client is not None:
                try:
                    await asyncio.wait_for(client.disconnect(), 5)
                except (Exception, asyncio.CancelledError):
                    logging.warning("Telegram 登录连接关闭超时，正在释放本机会话")
                finally:
                    try:
                        client.session.close()
                    except Exception:
                        result, error = "failed", "Telegram 会话保存失败，请检查文件权限后重试"
            with self.lock:
                self._state(result, error=error)
                self.data.update(active=False, authorized=result == "ready")
                self.loop = self.task = self.inputs = None
            if result == "ready":
                logging.info("Telegram 扫码或页面登录成功，会话已保存在本机")

    async def _qr(self, client):
        from telethon import errors
        import qrcode

        while True:
            self._state("qr_pending")
            qr = await self._request(client.qr_login())
            output = io.BytesIO()
            qrcode.make(qr.url).save(output, format="PNG")
            # Register the login-token handler before exposing the image to a browser.
            waiter = asyncio.create_task(qr.wait())
            try:
                await asyncio.sleep(0)
                with self.lock:
                    self.png, self.qr_revision = output.getvalue(), uuid4().hex
                    self.expires_at = qr.expires.timestamp()
                    self.data.update(state="qr", auth_pending=False, error="")
                await waiter
                return
            except (TimeoutError, errors.AuthTokenExpiredError):
                continue
            except errors.SessionPasswordNeededError:
                await self._password(client)
                return
            finally:
                if not waiter.done():
                    waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

    async def _input(self, state, error=""):
        self._state(state, error=error)
        return await self.inputs.get()

    async def _password(self, client):
        from telethon import errors

        error = ""
        while True:
            password = await self._input("password", error)
            try:
                await self._request(client.sign_in(password=password))
                return
            except errors.PasswordHashInvalidError:
                error = "两步验证密码不正确，请重新输入"
            finally:
                password = None

    async def _phone(self, client):
        from telethon import errors

        error = ""
        while True:
            phone = await self._input("phone", error)
            try:
                sent = await self._request(client.send_code_request(phone))
            except errors.PhoneNumberInvalidError:
                error = "手机号无效，请检查国际区号和手机号"
                continue
            error = ""
            while True:
                code = await self._input("code", error)
                try:
                    await self._request(client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash))
                    return
                except errors.SessionPasswordNeededError:
                    await self._password(client)
                    return
                except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
                    error = "验证码不正确，请输入 Telegram 发来的最新验证码"
                except errors.PhoneCodeExpiredError:
                    error = "验证码已过期，请重新输入手机号获取验证码"
                    break
                finally:
                    code = None

    def close(self):
        with self.lock:
            if self.active:
                self.cancel(self.data["ticket"])
            thread = self.thread
        if thread is not None:
            thread.join(timeout=8)
