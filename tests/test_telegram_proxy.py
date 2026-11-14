from __future__ import annotations

import asyncio
import socket
import struct
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from xianyu_pipeline import telegram_proxy as tp, tdlib_client
from xianyu_pipeline.adapters import TelethonTelegramAdapter
from xianyu_pipeline.config import PipelineConfig
from test_workbench_actions import app


class Writer:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


@pytest.fixture
def router(tmp_path):
    bridge = tp.TelegramProxyBridge(str(tmp_path))
    yield bridge
    bridge.close()
    assert not bridge.thread.is_alive()


def run(router, coroutine):
    return asyncio.run_coroutine_threadsafe(coroutine, router.loop).result(timeout=5)


def routes(router, monkeypatch, available):
    opened, writers = [], []

    async def open_route(source, authority):
        opened.append((source, authority))
        await asyncio.sleep(0)
        if not available[source]:
            raise OSError("private-route-password")
        writer = Writer()
        writers.append((source, writer))
        return object(), writer

    monkeypatch.setattr(router, "_open_route", open_route)
    monkeypatch.setattr(tp, "probe_telegram", AsyncMock())
    return opened, writers


@pytest.mark.parametrize("browser,local,selected", [
    (True, True, "browser"), (True, False, "browser"), (False, True, "local"),
])
def test_prefers_browser_and_shares_probes_across_concurrent_connections(
    router, monkeypatch, browser, local, selected
):
    opened, writers = routes(router, monkeypatch, {"browser": browser, "local": local})

    async def check():
        streams = await asyncio.gather(*(router._connect("91.108.56.138:443") for _ in range(6)))
        assert len([call for call in opened if call[1] == tp.PROBE_AUTHORITY]) == 2
        assert [call[0] for call in opened if call[1] != tp.PROBE_AUTHORITY] == [selected] * 6
        assert router.selected == selected
        assert sum(writer.closed for _, writer in writers) == int(browser) + int(local)
        for _, writer in streams:
            await tp._close(writer)

    run(router, check())


def test_both_unavailable_fail_without_connecting_directly(router, monkeypatch, caplog):
    opened, _ = routes(router, monkeypatch, {"browser": False, "local": False})
    with pytest.raises(tp.GhelperProxyError, match="均不可用"):
        run(router, router._connect("91.108.56.138:443"))
    assert len(opened) == 2 and all(authority == tp.PROBE_AUTHORITY for _, authority in opened)
    assert "private-route-password" not in caplog.text
    result = router.check()
    assert not result["connected"] and result["selected"] == ""
    assert not result["routes"]["browser"]["connected"]
    assert not result["routes"]["local"]["connected"]


def test_live_destination_failure_falls_back_and_next_probe_can_restore_browser(router, monkeypatch):
    available = {"browser": True, "local": True}
    opened, writers = routes(router, monkeypatch, available)
    original = router._open_route

    async def fail_dc(source, authority):
        if source == "browser" and authority == "91.108.56.138:443":
            raise OSError("account-host-must-not-be-logged")
        return await original(source, authority)

    monkeypatch.setattr(router, "_open_route", fail_dc)

    async def check():
        _, ongoing = await router._connect("91.108.56.138:443")
        assert router.selected == "local" and not ongoing.closed
        monkeypatch.setattr(router, "_open_route", original)
        router.checked_at = float("-inf")
        _, replacement = await router._connect("91.108.56.138:443")
        assert router.selected == "browser" and not ongoing.closed
        await tp._close(ongoing)
        await tp._close(replacement)

    run(router, check())


def test_stalled_browser_is_bounded_and_does_not_block_local(router, monkeypatch):
    monkeypatch.setattr(tp, "ROUTE_TIMEOUT", .05)
    monkeypatch.setattr(tp, "probe_telegram", AsyncMock())
    started, cancelled = [], []

    async def open_route(source, authority):
        started.append(source)
        if source == "browser":
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(source)
        return object(), Writer()

    monkeypatch.setattr(router, "_open_route", open_route)
    start = time.monotonic()
    result = router.check()
    assert time.monotonic() - start < 1
    assert result["selected"] == "local" and started == ["browser", "local"]
    assert cancelled == ["browser"]


def test_browser_nodes_share_deadline_and_leave_time_for_backup(router, monkeypatch, tmp_path):
    monkeypatch.setattr(tp.ghelper_proxy, "discover_route", lambda hint: SimpleNamespace(
        profile=tmp_path, endpoints=tuple((f"node{i}.example", 443) for i in range(8))))
    opened = AsyncMock(return_value=(object(), Writer()))
    monkeypatch.setattr(tp.GhelperBridge, "_connect", opened)
    run(router, router._open_route("browser", tp.PROBE_AUTHORITY))
    assert opened.call_args.kwargs["connect_timeout"] == tp.ROUTE_TIMEOUT / 8


def test_periodic_health_checks_detect_both_channels_without_interrupting_streams(router, monkeypatch):
    monkeypatch.setattr(tp, "HEALTH_INTERVAL", .03)
    available = {"browser": False, "local": True}
    routes(router, monkeypatch, available)

    async def check():
        _, existing = await router._connect("91.108.56.138:443")
        assert router.selected == "local"
        available.update(browser=True, local=False)
        for _ in range(50):
            if router.health["browser"]["connected"] and not router.health["local"]["connected"]:
                break
            await asyncio.sleep(.01)
        assert router.health["browser"]["connected"] and not router.health["local"]["connected"]
        _, new = await router._connect("91.108.56.138:443")
        assert router.selected == "browser" and not existing.closed
        await tp._close(existing)
        await tp._close(new)

    run(router, check())


@pytest.mark.parametrize("scheme,valid", [("socks5", True), ("http", True), ("http", False)])
def test_local_proxy_requires_real_telegram_response_and_cleans_up(monkeypatch, scheme, valid):
    authorities, finished = [], []

    async def check():
        async def proxy(reader, writer):
            try:
                if scheme == "http":
                    header = await reader.readuntil(b"\r\n\r\n")
                    authorities.append(header.split(b"\r\n")[0])
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                else:
                    head = await reader.readexactly(2)
                    await reader.readexactly(head[1])
                    writer.write(b"\x05\x00")
                    await writer.drain()
                    assert await reader.readexactly(4) == b"\x05\x01\x00\x01"
                    address = socket.inet_ntoa(await reader.readexactly(4))
                    port = struct.unpack("!H", await reader.readexactly(2))[0]
                    authorities.append(f"{address}:{port}")
                    writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
                await writer.drain()
                assert await reader.readexactly(2) == b"\xef\x0a"
                request = await reader.readexactly(40)
                response = bytearray(40)
                response[20:24] = struct.pack("<I", 0x05162463)
                response[24:40] = request[24:40] if valid else b"wrong-nonce-data"
                writer.write(b"\x0a" + response)
                await writer.drain()
                assert await reader.read() == b""
            finally:
                finished.append(True)
                await tp._close(writer)

        server = await asyncio.start_server(proxy, "127.0.0.1", 0)
        bridge = object.__new__(tp.TelegramProxyBridge)
        bridge.local_url = f"{scheme}://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            result = await bridge._check_route("local")
            assert result["connected"] == valid
            await asyncio.sleep(.02)
            assert finished == [True]
            assert len(authorities) == 1 and "149.154.167.51:443" in str(authorities[0])
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(check())


@pytest.mark.parametrize("mode", ["auto", "manual", "ghelper"])
def test_legacy_configs_use_auto_routing_and_preserve_addresses(mode):
    config = PipelineConfig(telegram_proxy_mode=mode, telegram_api_proxy_url="http://127.0.0.1:8888")
    assert config.telegram_proxy_mode == "auto"
    assert config.telegram_api_proxy_url == "http://127.0.0.1:8888"


def test_telethon_and_tdlib_share_a_stable_authenticated_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(tp, "_bridges", {})
    config = PipelineConfig(telegram_api_id=12345, telegram_api_hash="a" * 32,
                            telegram_session=str(tmp_path / "account.session"))
    assert tp.proxy_for_telegram(create=False) is None
    monkeypatch.setattr(tp.ghelper_proxy, "discover_route",
                        lambda *args: pytest.fail("Constructing client must not start probing"))
    adapter = TelethonTelegramAdapter(config.telegram_api_id, config.telegram_api_hash,
                                      config.telegram_session, tmp_path, proxy_mode=config.telegram_proxy_mode)
    recorded = []
    service = SimpleNamespace(status=lambda: {"active_files": 0}, closed=False)
    monkeypatch.setattr(tdlib_client, "get_client", lambda *args: recorded.append(args) or service)
    client = adapter._client()
    try:
        assert tdlib_client.client_for_config(config) is service
        assert adapter.proxy == recorded[0][-1] == client._proxy
        assert adapter.proxy["username"] and adapter.proxy["password"]
        assert len(tp._bridges) == 1
        assert tp.get_bridge().health == {}
    finally:
        client.session.close()
        tp.close_bridges()


def test_config_reads_do_not_start_network_and_unsaved_check_uses_both_fields(app, monkeypatch):
    monkeypatch.setattr(tp, "_bridges", {})
    monkeypatch.setattr(tp, "get_bridge", lambda *args, **kw: pytest.fail("Config read started bridge"))
    assert app.get("/api/config")[1]["telegram_proxy_mode"] == "auto"
    recorded = []
    monkeypatch.setattr(tp, "check_telegram_proxy", lambda *args: recorded.append(args) or {"connected": True})
    payload = {"telegram_ghelper_profile": "C:\\Browser\\Default",
               "telegram_api_proxy_url": "http://127.0.0.1:8888"}
    original = app.config_path.read_bytes()
    assert app.post("/api/telegram-proxy-check", payload) == (200, {"connected": True})
    assert recorded == [(payload["telegram_ghelper_profile"], payload["telegram_api_proxy_url"])]
    assert app.config_path.read_bytes() == original and not app.workers


def test_invalid_fallback_settings_are_rejected_even_with_legacy_browser_mode(app):
    before = app.config_path.read_bytes()
    for value in (None, "socks5://user:secret@127.0.0.1:7890", "http://127.0.0.1:0"):
        assert app.post("/api/config", {"telegram_proxy_mode": "ghelper",
                                       "telegram_api_proxy_url": value})[0] == 400
    assert app.config_path.read_bytes() == before


@pytest.mark.parametrize("value", [None, [], "http://127.0.0.1:0", "socks5://user:pass@127.0.0.1:7890"])
def test_proxy_check_validates_local_address_before_starting_bridge(app, monkeypatch, value):
    monkeypatch.setattr(tp, "TelegramProxyBridge",
                        lambda *args: pytest.fail("Invalid request started proxy"))
    assert app.post("/api/telegram-proxy-check", {"telegram_api_proxy_url": value})[0] == 400


def test_default_local_address_and_passive_lookup_reuse_bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(tp, "_bridges", {})
    assert tp.get_bridge(create=False) is None
    bridge = tp.get_bridge()
    try:
        assert bridge.local_url == tp.DEFAULT_LOCAL_PROXY
        assert tp.get_bridge(local_url=tp.DEFAULT_LOCAL_PROXY) is bridge
        assert tp.proxy_for_telegram(create=False) == bridge.proxy()
        assert not bridge.health and bridge.monitor is None
    finally:
        tp.close_bridges()
    assert not bridge.thread.is_alive()


def test_unsaved_diagnostic_closes_its_monitor_and_keeps_active_bridge(router, monkeypatch):
    monkeypatch.setattr(tp, "_bridges", {})
    routes(router, monkeypatch, {"browser": True, "local": True})
    recorded = []

    class Diagnostic:
        def __init__(self, *args):
            recorded.append(("created", args))

        def check(self):
            return {"connected": True}

        def close(self):
            recorded.append("closed")

    monkeypatch.setattr(tp, "TelegramProxyBridge", Diagnostic)
    assert tp.check_telegram_proxy("", "http://127.0.0.1:8888") == {"connected": True}
    assert recorded[-1] == "closed" and not tp._bridges
    tp._bridges[("", tp.DEFAULT_LOCAL_PROXY)] = router
    assert tp.check_telegram_proxy()["selected"] == "browser"
    assert router.thread.is_alive() and len(recorded) == 2
