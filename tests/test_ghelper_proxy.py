from __future__ import annotations

import asyncio
import base64
import json
import socket
import ssl
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from xianyu_pipeline import ghelper_proxy as gp, tdlib_client
from xianyu_pipeline.adapters import TelethonTelegramAdapter
from xianyu_pipeline.config import PipelineConfig
from test_workbench_actions import app


def pac(text='var proxy="HTTPS account.example:443;HTTPS backup.example:443";'):
    return "data:application/x-ns-proxy-autoconfig;base64," + base64.b64encode(text.encode()).decode()


def profile(path, text=None, **entry):
    path.mkdir(parents=True, exist_ok=True)
    value = {"state": 1, "preferences": {"proxy": {"mode": "pac_script", "pac_url": pac(text) if text else pac()}}, **entry}
    (path / "Secure Preferences").write_text(json.dumps({"extensions": {"settings": {
        next(iter(gp.KNOWN_IDS)): value}}}), encoding="utf-8")
    return path


def test_reads_only_static_default_route_and_redacts_summary(tmp_path):
    folder = profile(tmp_path / "profile", 'var proxy="HTTPS account.example:443"; evil(); var test="HTTPS unrelated.example:443";')
    result = gp.discover_route(str(folder))
    assert result.endpoints == (("account.example", 443),)
    assert result.summary()["nodes"] == 1
    assert "account.example" not in repr(result) + json.dumps(result.summary())
    assert gp.parse_pac('data:text/plain,var%20proxy%3D%22HTTPS%20node.example%3A443%22%3B') == (("node.example", 443),)


@pytest.mark.parametrize("text", ['var proxy="HTTPS node.example:443;DIRECT";',
    'var proxy="PROXY node.example:80";', 'var proxy="HTTPS localhost:443";',
    'var proxy="HTTPS node.example:65536";', 'var proxy="HTTPS user:secret@node.example:443";',
    'var proxy=malicious();', 'function FindProxyForURL(){return "HTTPS node.example:443";}'])
def test_unsupported_or_direct_routes_fail_closed(text):
    with pytest.raises(gp.GhelperProxyError) as error:
        gp.parse_pac(pac(text))
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("uri", [None, {}, "https://example.test/pac", "data:text/plain;base64,!!!"])
def test_invalid_pac_is_not_fetched_or_executed(uri):
    with pytest.raises(gp.GhelperProxyError):
        gp.parse_pac(uri)


def test_profile_discovery_handles_missing_disabled_and_ambiguous_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    with pytest.raises(gp.GhelperProxyError, match="未找到"):
        gp.discover_route()
    folder = profile(tmp_path / "Google/Chrome/User Data/Default")
    assert gp.discover_route().profile == folder
    profile(tmp_path / "Microsoft/Edge/User Data/Profile 1")
    with pytest.raises(gp.GhelperProxyError, match="多个"):
        gp.discover_route()
    assert gp.discover_route(str(folder)).profile == folder
    profile(folder, state=0)
    with pytest.raises(gp.GhelperProxyError, match="停用"):
        gp.discover_route(str(folder))
    (folder / "Secure Preferences").write_text("invalid-account-secret", encoding="utf-8")
    with pytest.raises(gp.GhelperProxyError) as error:
        gp.discover_route(str(folder))
    assert "account-secret" not in str(error.value)


@pytest.mark.parametrize("mode,path", [(None, ""), ([], ""), ("bad", ""), ("ghelper", "relative"), ("ghelper", None)])
def test_config_rejects_invalid_proxy_mode_or_profile(mode, path):
    with pytest.raises(ValueError):
        PipelineConfig(telegram_proxy_mode=mode, telegram_ghelper_profile=path)


@pytest.fixture
def bridge(tmp_path):
    instance = gp.GhelperBridge(tmp_path)
    yield instance
    instance.close()
    assert not instance.thread.is_alive()


async def socks_login(instance, password=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", instance.port)
    writer.write(b"\x05\x01\x02")
    await writer.drain()
    assert await reader.readexactly(2) == b"\x05\x02"
    username = instance.username.encode()
    password = (instance.password if password is None else password).encode()
    writer.write(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
    await writer.drain()
    return reader, writer, await reader.readexactly(2)


async def finish(writer):
    writer.close()
    await writer.wait_closed()


def test_socks_requires_auth_and_rejects_local_destinations(bridge, monkeypatch):
    upstream = AsyncMock(side_effect=AssertionError("Must not contact upstream"))
    monkeypatch.setattr(bridge, "_connect", upstream)

    async def check():
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        assert await reader.readexactly(2) == b"\x05\xff"
        await finish(writer)
        reader, writer, status = await socks_login(bridge, "wrong")
        assert status == b"\x01\x01"
        await finish(writer)
        reader, writer, status = await socks_login(bridge)
        assert status == b"\x01\x00"
        writer.write(b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", 443))
        await writer.drain()
        assert (await reader.readexactly(10))[1] != 0
        await finish(writer)

    asyncio.run(asyncio.wait_for(check(), 5))
    assert not upstream.called


def test_socks_concurrent_binary_relay_upstream_eof_and_shutdown(bridge, monkeypatch):
    calls = []
    payload = bytes(range(256)) * 2000

    async def check():
        async def echo(reader, writer):
            try:
                data = await reader.readexactly(len(payload))
                writer.write(data)
                await writer.drain()
            finally:
                await finish(writer)

        server = await asyncio.start_server(echo, "127.0.0.1", 0)

        async def connect(authority):
            calls.append(authority)
            return await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])

        monkeypatch.setattr(bridge, "_connect", connect)

        async def transfer():
            reader, writer, status = await socks_login(bridge)
            assert status == b"\x01\x00"
            writer.write(b"\x05\x01\x00\x01" + socket.inet_aton("149.154.167.51") + struct.pack("!H", 443))
            await writer.drain()
            assert (await reader.readexactly(10))[1] == 0
            writer.write(payload)
            await writer.drain()
            assert await reader.readexactly(len(payload)) == payload
            assert await reader.read(1) == b""
            await finish(writer)

        try:
            await asyncio.gather(*(transfer() for _ in range(4)))
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(asyncio.wait_for(check(), 10))
    assert calls == ["149.154.167.51:443"] * 4
    port = bridge.port
    bridge.close()
    assert not bridge.thread.is_alive()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=.1)


@pytest.fixture
def certificates(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "node.example")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("node.example")]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


def test_https_connect_validates_tls_sni_fallback_and_reloads_route(tmp_path, monkeypatch, certificates, caplog):
    cert, key = certificates
    folder = profile(tmp_path / "profile", 'var proxy="HTTPS failed.example:443;HTTPS node.example:443";')
    context_factory, open_connection = ssl.create_default_context, asyncio.open_connection
    headers, sni, hosts = [], [], []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    context.set_servername_callback(lambda sock, host, ctx: sni.append(host))

    async def check():
        async def tls_proxy(reader, writer):
            try:
                headers.append(await reader.readuntil(b"\r\n\r\n"))
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\nBINARY\0")
                await writer.drain()
                await reader.read()
            finally:
                await finish(writer)

        server = await asyncio.start_server(tls_proxy, "127.0.0.1", 0, ssl=context)
        async def dns(host, port, **kw):
            hosts.append(host)
            if host == "failed.example":
                raise OSError("private account route secret")
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", port))]

        async def local_connection(host, port, **kw):
            assert host == "8.8.8.8" and port == 443
            assert kw["ssl"].verify_mode == ssl.CERT_REQUIRED and kw["ssl"].check_hostname
            return await open_connection("127.0.0.1", server.sockets[0].getsockname()[1], **kw)

        monkeypatch.setattr(gp.ssl, "create_default_context", lambda: context_factory(cafile=str(cert)))
        monkeypatch.setattr(gp.asyncio, "open_connection", local_connection)
        instance = object.__new__(gp.GhelperBridge)
        instance.profile, instance.loop = folder, SimpleNamespace(getaddrinfo=dns)
        try:
            reader, writer = await instance._connect("149.154.167.51:443")
            assert await reader.readexactly(7) == b"BINARY\0"
            await finish(writer)
            assert hosts == ["failed.example", "node.example"] and sni == ["node.example"]
            assert headers == [b"CONNECT 149.154.167.51:443 HTTP/1.1\r\nHost: 149.154.167.51:443\r\n\r\n"]
            profile(folder, 'var proxy="HTTPS wrong-name.example:443";')
            with pytest.raises(gp.GhelperProxyError, match="未回退直连"):
                await instance._connect("149.154.167.51:443")
            assert hosts[-1] == "wrong-name.example" and len(headers) == 1
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(asyncio.wait_for(check(), 8))
    assert all(value not in caplog.text for value in ("node.example", "failed.example", "private account", "wrong-name.example"))


def test_status_does_not_start_bridge_or_tdlib(tmp_path, monkeypatch):
    monkeypatch.setattr(gp, "_bridges", {})
    monkeypatch.setattr(gp, "discover_route", lambda *args: pytest.fail("Status read started discovery"))
    assert gp.proxy_for_ghelper(create=False) is None
    assert tdlib_client.client_for_config(PipelineConfig(telegram_proxy_mode="ghelper"), create=False) is None


def test_telethon_uses_authenticated_bridge_and_keeps_manual_config(tmp_path, monkeypatch):
    import telethon.sync
    proxy = {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 54321,
             "username": "telegram", "password": "fixture"}
    monkeypatch.setattr(gp, "proxy_for_ghelper", lambda path: proxy)
    calls = []
    monkeypatch.setattr(telethon.sync, "TelegramClient", lambda *args, **kw: calls.append(kw) or SimpleNamespace())
    adapter = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "session", tmp_path,
                                     proxy_mode="ghelper", proxy_url="socks5://127.0.0.1:7890")
    adapter._client()
    assert calls[-1]["proxy"] == proxy and calls[-1]["timeout"] >= 42
    manual = TelethonTelegramAdapter(12345, "a" * 32, tmp_path / "manual", tmp_path,
                                    proxy_url="socks5://127.0.0.1:7890")
    assert manual.proxy["port"] == 7890


def test_proxy_check_does_not_change_config_or_start_task(app, monkeypatch):
    calls = []
    monkeypatch.setattr(gp, "check_ghelper", lambda value: calls.append(value) or {"connected": True})
    before = app.config_path.read_bytes()
    app.state.running = True
    app.state.stop_event.set()
    assert app.post("/api/telegram-proxy-check", {"telegram_ghelper_profile": ""}) == (200, {"connected": True})
    assert calls == [""] and not app.workers
    assert app.state.running and app.state.stop_event.is_set() and app.config_path.read_bytes() == before
    assert app.post("/api/telegram-proxy-check", {"destination": "example.com"})[0] == 400


def test_ghelper_mode_save_retains_manual_proxy_and_quark_settings(app):
    before = PipelineConfig.from_json(app.config_path)
    assert app.post("/api/config", {"telegram_proxy_mode": "ghelper", "telegram_ghelper_profile": "",
                                    "telegram_api_proxy_url": "socks5://127.0.0.1:7890"})[0] == 200
    config = PipelineConfig.from_json(app.config_path)
    assert config.telegram_proxy_mode == "ghelper" and config.telegram_api_proxy_url.endswith(":7890")
    assert config.quark_folder_url == before.quark_folder_url and config.upload_provider == before.upload_provider
    assert app.get("/api/config")[1]["telegram_proxy_mode"] == "ghelper"
    saved = app.config_path.read_bytes()
    assert app.post("/api/config", {"telegram_proxy_mode": "invalid"})[0] == 400
    assert app.config_path.read_bytes() == saved
