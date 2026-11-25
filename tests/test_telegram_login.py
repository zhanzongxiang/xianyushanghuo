from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import io
import json
import threading
import time
from types import SimpleNamespace

from PIL import Image
import pytest
from telethon import errors

from xianyu_pipeline import telegram_login, web_ui
from xianyu_pipeline.telegram_login import TelegramLogin, TelegramLoginBusy
from test_workbench_actions import app


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError("Login state did not settle")


class FakeClient:
    def __init__(self):
        self.authorized = False
        self.closed = self.disconnected = False
        self.qr_count = self.waiters = 0
        self.password_needed = self.expire_first = False
        self.phone_invalid = self.code_expired = False
        self.connect_error = None
        self.inputs = []
        self.session = SimpleNamespace(close=self.close_session)
        self.loop = self.confirmation = None

    def close_session(self):
        self.closed = True

    async def connect(self):
        self.loop = asyncio.get_running_loop()
        if self.connect_error:
            raise self.connect_error

    async def disconnect(self):
        self.disconnected = True

    async def is_user_authorized(self):
        return self.authorized

    async def qr_login(self):
        client = self
        self.qr_count += 1
        self.confirmation = asyncio.Event()
        count = self.qr_count

        class QR:
            url = f"tg://login?token=PRIVATE-QR-TOKEN-{count}"
            expires = datetime.now(timezone.utc) + timedelta(seconds=30)

            async def wait(self):
                client.waiters += 1
                try:
                    if client.expire_first and count == 1:
                        await asyncio.sleep(.08)
                        raise TimeoutError
                    await client.confirmation.wait()
                    if client.password_needed:
                        raise errors.SessionPasswordNeededError(request=None)
                    client.authorized = True
                finally:
                    client.waiters -= 1

        return QR()

    def scan(self):
        self.loop.call_soon_threadsafe(self.confirmation.set)

    async def send_code_request(self, phone):
        self.inputs.append(("phone", phone))
        if self.phone_invalid:
            raise errors.PhoneNumberInvalidError(request=None)
        return SimpleNamespace(phone_code_hash="PRIVATE-HASH")

    async def sign_in(self, phone=None, code=None, *, password=None, phone_code_hash=None):
        if password is not None:
            self.inputs.append(("password", password))
            if password != " PRIVATE-PASSWORD ":
                raise errors.PasswordHashInvalidError(request=None)
        else:
            self.inputs.append(("code", code))
            assert phone_code_hash == "PRIVATE-HASH"
            if self.code_expired:
                raise errors.PhoneCodeExpiredError(request=None)
            if code != "12345":
                raise errors.PhoneCodeInvalidError(request=None)
            if self.password_needed:
                raise errors.SessionPasswordNeededError(request=None)
        self.authorized = True


@pytest.fixture
def login(tmp_path):
    manager, client = TelegramLogin(), FakeClient()
    calls = []

    def factory():
        calls.append("factory")
        return SimpleNamespace(session_path=tmp_path / "sessions" / "telegram.session",
                               _check_proxy=lambda: calls.append("proxy"),
                               _client=lambda: client)

    result = SimpleNamespace(manager=manager, client=client, factory=factory, calls=calls)

    def state(name):
        return wait_for(lambda: manager.status() if manager.status()["state"] == name else None)

    result.state = state
    result.start = lambda method="qr": manager.start(factory, method)
    result.submit = lambda field, value: manager.submit(manager.status()["ticket"], field, value)
    yield result
    manager.close()
    assert not manager.active
    assert not manager.thread or not manager.thread.is_alive()


def test_reading_status_has_no_network_or_session_side_effect(login):
    assert login.manager.status()["state"] == "idle"
    assert not login.calls


def test_qr_login_saves_original_session_and_exposes_no_token(login):
    ticket = login.start()["ticket"]
    state = login.state("qr")
    assert login.client.waiters == 1
    revision = login.manager.qr_revision
    png = login.manager.qr_png(ticket, revision)
    with Image.open(io.BytesIO(png)) as image:
        assert image.width == image.height and image.width >= 200
    assert "PRIVATE" not in json.dumps(state)
    assert "tg://" not in json.dumps(state)
    assert login.start()["ticket"] == ticket
    assert login.calls == ["factory", "proxy"]
    login.client.scan()
    state = login.state("ready")
    assert state["authorized"] and not state["active"]
    assert login.client.closed and login.client.disconnected and login.client.waiters == 0
    with pytest.raises(ValueError):
        login.manager.qr_png(ticket, revision)


def test_existing_authorization_skips_qr_and_releases_session(login):
    login.client.authorized = True
    login.start()
    assert login.state("ready")["authorized"]
    assert login.client.qr_count == 0 and login.client.closed


def test_expired_qr_refreshes_and_rejects_old_images(login):
    login.client.expire_first = True
    ticket = login.start()["ticket"]
    login.state("qr")
    first = login.manager.qr_revision
    wait_for(lambda: login.client.qr_count >= 2 and login.manager.status()["state"] == "qr")
    assert login.manager.qr_revision != first
    with pytest.raises(ValueError):
        login.manager.qr_png(ticket, first)
    assert login.client.waiters == 1
    login.client.scan()
    login.state("ready")


def test_qr_two_step_password_can_retry_without_exposing_secret(login, caplog):
    login.client.password_needed = True
    login.start()
    login.state("qr")
    login.client.scan()
    login.state("password")
    login.submit("password", "WRONG-PRIVATE-PASSWORD")
    wait_for(lambda: login.manager.status()["error"])
    assert login.manager.status()["state"] == "password"
    login.submit("password", " PRIVATE-PASSWORD ")
    state = login.state("ready")
    assert state["authorized"]
    assert "PRIVATE" not in caplog.text + json.dumps(state)
    assert login.client.inputs[-1] == ("password", " PRIVATE-PASSWORD ")


def test_phone_and_code_validation_retry_and_two_step_login(login):
    login.client.password_needed = True
    login.start("phone")
    login.state("phone")
    for field, value in (("phone", "abc"), ("phone", "123"), ("phone", [])):
        with pytest.raises(ValueError):
            login.submit(field, value)
    assert not login.client.inputs
    login.submit("phone", "+8613800000000")
    login.state("code")
    with pytest.raises(ValueError):
        login.submit("code", "aa")
    login.submit("code", "99999")
    wait_for(lambda: login.manager.status()["error"])
    assert login.manager.status()["state"] == "code"
    login.submit("code", "12345")
    login.state("password")
    login.submit("password", " PRIVATE-PASSWORD ")
    login.state("ready")


def test_expired_code_allows_requesting_another_in_same_session(login):
    login.client.code_expired = True
    login.start("phone")
    login.state("phone")
    login.submit("phone", "8613800000000")
    login.state("code")
    login.submit("code", "12345")
    assert login.state("phone")["error"]
    login.client.code_expired = False
    login.submit("phone", "+8613800000000")
    login.state("code")
    login.submit("code", "12345")
    login.state("ready")
    assert login.calls.count("factory") == 1


def test_cancel_releases_qr_waiter_and_old_window_cannot_cancel_new_login(login):
    first = login.start()["ticket"]
    login.state("qr")
    with pytest.raises(TelegramLoginBusy):
        login.start("phone")
    login.manager.cancel(first)
    login.state("cancelled")
    assert login.client.waiters == 0 and login.client.closed
    second = login.start("phone")["ticket"]
    assert second != first
    login.state("phone")
    with pytest.raises(ValueError):
        login.manager.cancel(first)
    with pytest.raises(ValueError):
        login.manager.submit(first, "phone", "+8613800000000")
    assert login.manager.active


def test_duplicate_submissions_are_rejected_before_a_second_rpc(login):
    login.start("phone")
    login.state("phone")
    with login.manager.lock:
        login.submit("phone", "+8613800000000")
        with pytest.raises(TelegramLoginBusy):
            login.submit("phone", "+8613800000000")


def test_abandoned_login_expires_and_releases_session(login, monkeypatch):
    monkeypatch.setattr(telegram_login, "LOGIN_TIMEOUT", .15)
    login.start("phone")
    login.state("expired")
    assert login.client.closed and login.client.disconnected


@pytest.mark.parametrize("exception", [ConnectionError("PRIVATE-CREDENTIAL"), RuntimeError("PRIVATE-CREDENTIAL")])
def test_failure_releases_session_and_redacts_error(login, caplog, exception):
    login.client.connect_error = exception
    login.start()
    state = login.state("failed")
    assert not state["active"] and not state["authorized"]
    assert login.client.closed
    assert "PRIVATE" not in json.dumps(state) + caplog.text


@pytest.mark.parametrize("method", [None, [], {}, 1, True, "browser"])
def test_invalid_login_method_does_not_start_client(login, method):
    with pytest.raises(ValueError):
        login.start(method)
    assert not login.calls and not login.manager.active


def test_web_login_uses_dialog_flow_and_blocks_tasks_until_disconnect(app, login, monkeypatch):
    monkeypatch.setattr(web_ui.TelethonTelegramAdapter, "_check_proxy", lambda self: None)
    monkeypatch.setattr(web_ui.TelethonTelegramAdapter, "_client", lambda self: login.client)
    monkeypatch.setattr(web_ui.subprocess, "Popen", lambda *a, **kw: pytest.fail("Started terminal login"))
    assert app.get("/api/telegram-auth/status")[1]["state"] == "idle"
    assert login.client.loop is None
    assert app.post("/api/telegram-api-login", {"method": "browser"})[0] == 400
    app.state.running = True
    assert app.post("/api/telegram-api-login")[0] == 409
    app.state.running = False
    code, result = app.post("/api/telegram-api-login")
    assert code == 200 and result["ticket"] and not app.workers
    wait_for(lambda: app.get("/api/telegram-auth/status")[1]["state"] == "qr")
    try:
        before = app.config_path.read_bytes()
        assert app.get("/api/workbench")[1]["telegram_login"]["active"]
        current = app.get("/api/telegram-auth/status")[1]
        handler = app.handler.__new__(app.handler)
        handler.path = current["qr_url"]
        handler.headers = {"Host": "127.0.0.1:18765"}
        headers = {}
        handler.wfile = io.BytesIO()
        handler.send_response = lambda code: headers.update(status=code)
        handler.send_header = lambda name, value: headers.update({name: value})
        handler.end_headers = lambda: None
        handler.do_GET()
        assert headers["status"] == 200 and headers["Content-Type"] == "image/png"
        assert headers["Cache-Control"] == "no-store"
        assert handler.wfile.getvalue().startswith(b"\x89PNG")
        assert app.post("/api/config", {"channel_message_limit": 25})[0] == 409
        assert app.config_path.read_bytes() == before
        for path in ("/api/telegram-channels-scan", "/api/channel-scan", "/api/download", "/api/tdlib/login"):
            assert app.post(path)[0] == 409
        assert app.post("/api/telegram-auth/cancel", {"ticket": "stale"})[0] == 400
        assert app.post("/api/telegram-auth/auth", {"ticket": result["ticket"], "field": "password", "value": "PRIVATE"})[0] == 400
        assert app.post("/api/telegram-auth/cancel", {"ticket": result["ticket"]})[0] == 200
        wait_for(lambda: not app.get("/api/telegram-auth/status")[1]["active"])
        assert app.post("/api/config", {"channel_message_limit": 25})[0] == 200
        assert not app.state.running
    finally:
        app.post("/api/telegram-auth/cancel", {"ticket": result["ticket"]})
        wait_for(lambda: not app.get("/api/telegram-auth/status")[1]["active"])


def test_web_phone_login_accepts_password_only_at_correct_step_and_unlocks_tasks(app, login, monkeypatch):
    login.client.password_needed = True
    monkeypatch.setattr(web_ui.TelethonTelegramAdapter, "_check_proxy", lambda self: None)
    monkeypatch.setattr(web_ui.TelethonTelegramAdapter, "_client", lambda self: login.client)
    code, result = app.post("/api/telegram-api-login", {"method": "phone"})
    assert code == 200
    ticket = result["ticket"]
    state = lambda: app.get("/api/telegram-auth/status")[1]
    submit = lambda field, value: app.post("/api/telegram-auth/auth", {"ticket": ticket, "field": field, "value": value})
    try:
        wait_for(lambda: state()["state"] == "phone")
        assert submit("code", "12345")[0] == 400
        assert submit("phone", "+8613800000000")[0] == 200
        wait_for(lambda: state()["state"] == "code")
        assert submit("code", "12345")[0] == 200
        wait_for(lambda: state()["state"] == "password")
        assert submit("password", " PRIVATE-PASSWORD ")[0] == 200
        wait_for(lambda: state()["state"] == "ready")
        assert not state()["active"] and login.client.closed
        assert "PRIVATE" not in json.dumps(state()) + app.config_path.read_text(encoding="utf-8")
        assert app.post("/api/config", {"channel_message_limit": 25})[0] == 200
        assert app.post("/api/telegram-channels-scan")[0] == 200
    finally:
        app.post("/api/telegram-auth/cancel", {"ticket": ticket})
        wait_for(lambda: not state()["active"])
