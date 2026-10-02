from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest


@pytest.fixture
def ui_server(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "telegram_access_mode": "browser", "inbox_dir": str(tmp_path / "inbox"),
        "output_dir": str(tmp_path / "output"), "work_dir": str(tmp_path / "work"),
    }), encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    code = "from xianyu_pipeline.web_ui import start_web_ui; import sys; start_web_ui(sys.argv[1], port=int(sys.argv[2]), open_browser=False)"
    process = subprocess.Popen([sys.executable, "-c", code, str(config), str(port)], cwd=root,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with urlopen(url + "/api/config", timeout=0.5):
                    break
            except (URLError, OSError):
                if process.poll() is not None:
                    raise AssertionError("UI server exited")
                time.sleep(0.1)
        else:
            raise AssertionError("UI server did not become ready")
        yield url, config
    finally:
        process.terminate()
        process.wait(timeout=10)


def request(url, path, payload=None, origin=None):
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    req = Request(url + path, data=json.dumps({} if payload is None else payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def test_browser_required_on_save_and_start_and_origin_guard(ui_server, tmp_path):
    url, config = ui_server
    for path in ("/api/config", "/api/run", "/api/telegram-login", "/api/quark-login", "/api/browser-check"):
        status, response = request(url, path)
        assert status == 400 and "必填" in response["message"]
    before = config.read_bytes()
    status, _ = request(url, "/api/config", {"browser_executable_path": "C:/missing.exe"}, origin="https://untrusted.example")
    assert status == 403
    assert config.read_bytes() == before
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"MZ")
    values = {
        "browser_executable_path": str(executable),
        "telegram_browser_timeout_seconds": 240,
        "download_start_timeout_seconds": 210,
        "download_transfer_timeout_seconds": 7200,
        "archive_test_executable_path": "",
        "browser_profile_dir": "C:/must-not-be-set-via-api",
        "browser_version": "999.0.0.0",
    }
    status, _ = request(url, "/api/config", values, origin=url)
    assert status == 200
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["browser_executable_path"] == str(executable)
    assert saved["inbox_dir"] == str(tmp_path / "inbox")
    assert "browser_profile_dir" not in saved and "browser_version" not in saved
    with urlopen(url + "/api/config") as response:
        loaded = json.load(response)
    assert loaded["download_start_timeout_seconds"] == 210
    assert loaded["telegram_browser_timeout_seconds"] == 240
    assert Path(loaded["log_path"]).is_file()
    assert loaded["download_dir"] == str(tmp_path / "inbox")
    assert Path(loaded["unverified_dir"]) == tmp_path / "inbox" / ".unverified"
    assert loaded["browser_profile_error"]
    for value in ([], {"browser_executable_path": []}, {**values, "archive_test_executable_path": []}):
        assert request(url, "/api/config", value)[0] == 400
    for invalid in (29, 601, 120.5, "180"):
        status, _ = request(url, "/api/config", {**values, "telegram_browser_timeout_seconds": invalid})
        assert status == 400


def test_download_directory_is_editable_and_invalid_paths_do_not_change_config(ui_server, tmp_path):
    url, config_path = ui_server
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"MZ")
    browser = {"browser_executable_path": str(executable)}
    original = json.loads(config_path.read_text(encoding="utf-8"))
    file_path = tmp_path / "not-a-folder"
    file_path.write_text("keep", encoding="utf-8")
    for invalid in ("", "relative/downloads", str(file_path), [], str(Path(tmp_path.anchor))):
        status, _ = request(url, "/api/config", {**browser, "inbox_dir": invalid})
        assert status == 400
        assert json.loads(config_path.read_text(encoding="utf-8")) == original
    new_dir = tmp_path / "chosen" / "downloads"
    assert request(url, "/api/config", {**browser, "inbox_dir": str(new_dir)})[0] == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["inbox_dir"] == str(new_dir.resolve())
    assert new_dir.is_dir()
    with urlopen(url + "/api/config") as response:
        loaded = json.load(response)
    assert loaded["inbox_dir"] == str(new_dir.resolve())
    assert loaded["download_dir"] == str(new_dir.resolve())
    assert loaded["unverified_dir"] == str(new_dir / ".unverified")
    with urlopen(url + "/api/workbench") as response:
        assert json.load(response)["download_dir"] == str(new_dir.resolve())
    assert saved["output_dir"] == original["output_dir"]


def test_real_browser_check_api_without_channel(ui_server, tmp_path):
    executable = os.environ.get("BROWSER_TEST_PATH")
    if not executable:
        pytest.skip("BROWSER_TEST_PATH required")
    url, config_path = ui_server
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.update(browser_executable_path=executable, telegram_browser_profile=str(tmp_path / "profile"))
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert request(url, "/api/browser-check")[0] == 200
    for _ in range(200):
        with urlopen(url + "/api/logs") as response:
            state = json.load(response)
        if not state["running"]:
            break
        time.sleep(0.1)
    assert not state["running"]
    assert any("浏览器检查通过" in line for line in state["logs"])
    assert not any("任务失败" in line for line in state["logs"])
    with urlopen(url + "/api/config") as response:
        loaded = json.load(response)
    assert loaded["browser_version"] and loaded["browser_profile_status"] == "复用兼容专用目录"


def test_api_credentials_are_write_only_in_web_config(ui_server):
    url, config_path = ui_server
    payload = {
        "telegram_access_mode": "api",
        "telegram_api_id": 12345,
        "telegram_api_hash": "a" * 32,
        "telegram_api_proxy_url": "socks5://127.0.0.1:7890",
        "telegram_api_dc2_server": "149.154.167.50:443",
        "telegram_run_mode": "once",
        "browser_executable_path": "",
        "download_start_timeout_seconds": 180,
        "download_transfer_timeout_seconds": 3600,
        "archive_test_executable_path": "",
    }
    assert request(url, "/api/telegram-api-login")[0] == 400
    assert request(url, "/api/config", payload)[0] == 200
    assert json.loads(config_path.read_text(encoding="utf-8"))["telegram_api_hash"] == "a" * 32
    with urlopen(url + "/api/config") as response:
        body = response.read().decode("utf-8")
        data = json.loads(body)
    assert "a" * 32 not in body and "telegram_api_hash" not in data
    assert data["telegram_api_configured"] and data["telegram_api_id"] == 12345
    assert data["telegram_access_mode"] == "api"
    assert data["telegram_api_proxy_url"] == "socks5://127.0.0.1:7890"
    assert data["telegram_api_dc2_server"] == "149.154.167.50:443"
    assert request(url, "/api/config", {**payload, "telegram_api_hash": ""})[0] == 200
    assert json.loads(config_path.read_text(encoding="utf-8"))["telegram_api_hash"] == "a" * 32
    assert request(url, "/api/config", {**payload, "telegram_api_hash": "bad"})[0] == 400
    assert request(url, "/api/config", {**payload, "telegram_run_mode": "watch"})[0] == 400
    assert request(url, "/api/config", {**payload, "telegram_api_proxy_url": "socks5://user:pass@localhost:7890"})[0] == 400
    assert request(url, "/api/config", {**payload, "telegram_api_dc2_server": "149.154.167.40:443"})[0] == 400
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["telegram_api_proxy_url"] = "socks5://user:pass@localhost:7890"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    with urlopen(url + "/api/config") as response:
        assert "user:pass" not in response.read().decode("utf-8")


def test_workbench_endpoints_validate_tasks_and_do_not_expose_arbitrary_files(ui_server):
    url, config_path = ui_server
    with urlopen(url + "/api/workbench") as response:
        state = json.load(response)
    assert state["tasks"] == [] and not state["running"]
    assert state["disk"]["total"] >= state["disk"]["free"] > 0
    assert request(url, "/api/upload", {"ids": []})[0] == 400
    assert request(url, "/api/upload", {"ids": ["missing"]})[0] == 400
    assert request(url, "/api/upload", {"ids": "missing"})[0] == 400
    assert request(url, "/api/open-task", {"id": "../config.json"})[0] == 400
    assert request(url, "/api/upload", {"ids": ["a"]}, origin="https://evil.example")[0] == 403
    for path in ("/api/preview/../config.json", "/static/../web_ui.py", "/static/no-such-file.js"):
        with pytest.raises(HTTPError) as failure:
            urlopen(url + path)
        assert failure.value.code == 404


def test_workbench_static_assets_are_served_locally(ui_server):
    url, _ = ui_server
    for path, mime in (("/", "text/html"), ("/static/workbench.js", "text/javascript"),
                       ("/static/workbench.css", "text/css"), ("/static/lucide.js", "text/javascript")):
        with urlopen(url + path) as response:
            assert response.headers["Content-Type"].startswith(mime)
            assert len(response.read()) > 100
