"""Exercise recovery through the HTTP API without contacting Telegram."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from test_web_api import request
from xianyu_pipeline.channel_catalog import ChannelCatalog


@pytest.fixture
def image_server(tmp_path):
    output = tmp_path / "output"
    catalog = ChannelCatalog(output)
    catalog.preview_root.mkdir(parents=True)
    image = catalog.preview_root / "existing.jpg"
    image.write_bytes(b"\xff\xd8\xfffixture")
    catalog.merge("example_channel", {"messages": [
        {"message_id": 10, "chat": "example_channel", "filename": "model.rar",
         "cover_path": str(image), "cover_status": "ready", "cover_quality": "preview",
         "full_cover_path": str(image), "full_status": "ready", "full_quality": "large"},
        {"message_id": 9, "chat": "example_channel", "filename": "other.rar",
         "cover_status": "pending", "full_status": "loading"}],
        "oldest_scanned_id": 9, "has_more": True})
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "telegram_api_id": 12345, "telegram_api_hash": "a" * 32,
        "telegram_api_proxy_url": "", "telegram_access_mode": "api",
        "source_channel_url": "https://t.me/example_channel", "upload_provider": "quark_api",
        "quark_cookie": "",
        "inbox_dir": str(tmp_path / "inbox"), "output_dir": str(output),
        "work_dir": str(tmp_path / "work"), "telegram_session": str(tmp_path / "account"),
        "data_server_enabled": False,
    }), encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    code = """
import sys
from pathlib import Path
from xianyu_pipeline import web_ui
base = Path(sys.argv[1]).parent
def listing(self, channel, *, preview_dir, take_request, on_request_done, close_requests, on_page, **kwargs):
    if (base / 'fail').exists():
        raise RuntimeError('fixture proxy unavailable')
    if on_page:
        on_page({'messages': [{'message_id': 8, 'chat': 'example_channel',
                              'filename': 'pending.rar', 'cover_status': 'pending'}],
                 'oldest_scanned_id': 8, 'has_more': False})
        raise RuntimeError('fixture disconnected during scan')
    job = take_request()
    (base / 'calls').write_text(str(job['message_id']))
    preview_dir.mkdir(parents=True, exist_ok=True)
    result = preview_dir / 'repaired.jpg'
    result.write_bytes(bytes([255, 216, 255]) + b'repaired')
    on_request_done(job, {'full_cover_path': str(result), 'full_cover_message_id': 7,
                         'full_status': 'ready', 'full_quality': 'large', 'full_error': ''}, '')
    assert close_requests()
    return {'messages': [], 'oldest_scanned_id': 0, 'has_more': False}
web_ui.TelethonTelegramAdapter.list_archives = listing
web_ui.start_web_ui(sys.argv[1], port=int(sys.argv[2]), open_browser=False)
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(config), str(port)],
                               cwd=Path(__file__).resolve().parents[1],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    address = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with urlopen(address + "/api/config", timeout=0.5):
                    break
            except (URLError, OSError):
                assert process.poll() is None
                time.sleep(0.05)
        else:
            raise AssertionError("UI did not start")
        yield address, tmp_path
    finally:
        process.terminate()
        process.wait(timeout=10)


def get(address, path):
    with urlopen(address + path, timeout=5) as response:
        return json.load(response)


def wait_done(address):
    for _ in range(100):
        if not get(address, "/api/workbench")["running"]:
            return
        time.sleep(0.05)
    raise AssertionError("worker did not finish")


def test_startup_and_forced_refresh_of_existing_cached_images(image_server):
    address, base = image_server
    rows = get(address, "/api/channel-messages")["messages"]
    assert rows[1]["full_status"] == rows[1]["cover_status"] == "cancelled"
    assert request(address, "/api/channel-cover", {"id": 10})[1]["url"] == rows[0]["full_cover_url"]
    assert not (base / "calls").exists()
    assert request(address, "/api/channel-cover", {"id": 10, "refresh": "true"})[0] == 400
    assert request(address, "/api/channel-cover", {"id": 10, "refresh": True})[0] == 200
    wait_done(address)
    fixed = get(address, "/api/channel-messages")["messages"][0]
    assert fixed["full_status"] == fixed["cover_status"] == "ready"
    assert fixed["full_cover_url"] != rows[0]["full_cover_url"]
    assert fixed["cover_url"] != rows[0]["cover_url"]
    assert (base / "calls").read_text() == "10"
    with urlopen(address + fixed["cover_url"]) as response:
        assert response.read() == b"\xff\xd8\xffrepaired"


def test_worker_connection_failure_is_retryable_and_preserves_medium_image(image_server):
    address, base = image_server
    (base / "fail").touch()
    assert request(address, "/api/channel-cover", {"id": 10, "refresh": True})[0] == 200
    wait_done(address)
    row = get(address, "/api/channel-messages")["messages"][0]
    assert row["full_status"] == "failed" and "proxy unavailable" in row["full_error"]
    with urlopen(address + row["cover_url"]) as response:
        assert response.read() == b"\xff\xd8\xfffixture"


def test_scan_failure_does_not_leave_pending_covers(image_server):
    address, _ = image_server
    assert request(address, "/api/channel-scan", {})[0] == 200
    wait_done(address)
    rows = get(address, "/api/channel-messages")["messages"]
    pending = next(row for row in rows if row["message_id"] == 8)
    assert pending["cover_status"] == "cancelled"
    assert pending["cover_error"]
