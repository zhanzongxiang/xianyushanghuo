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


def test_legacy_web_config_migrates_to_api_and_browser_is_only_for_quark(ui_server, tmp_path):
    url, config = ui_server
    with urlopen(url + "/api/config") as response:
        initial = json.load(response)
        assert initial["telegram_access_mode"] == "api"
        assert initial["download_workers"] == 4
        assert initial["download_file_concurrency"] == 2
        assert initial["telegram_crypto_backend"] in {"cryptg", "OpenSSL", "Python"}
    for path in ("/api/config", "/api/run", "/api/telegram-api-login", "/api/quark-login", "/api/browser-check"):
        status, response = request(url, path)
        assert status == 400
    with pytest.raises(HTTPError) as removed:
        urlopen(Request(url + "/api/telegram-login", data=b"{}", method="POST"))
    assert removed.value.code == 404
    assert request(url, "/api/config", {"telegram_access_mode": "browser"})[0] == 400
    assert request(url, "/api/config", {"telegram_run_mode": "watch"})[0] == 400
    before = config.read_bytes()
    status, _ = request(url, "/api/config", {"browser_executable_path": "C:/missing.exe"}, origin="https://untrusted.example")
    assert status == 403
    assert config.read_bytes() == before
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"MZ")
    values = {
        "browser_executable_path": str(executable),
        "telegram_api_id": 12345,
        "telegram_api_hash": "a" * 32,
        "download_start_timeout_seconds": 210,
        "download_transfer_timeout_seconds": 7200,
        "download_workers": 1,
        "download_file_concurrency": 1,
        "telegram_crypto_backend": "must-not-be-saved",
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
    assert "telegram_crypto_backend" not in saved
    assert saved["download_file_concurrency"] == 1
    with urlopen(url + "/api/config") as response:
        loaded = json.load(response)
    assert loaded["download_start_timeout_seconds"] == 210
    assert loaded["download_workers"] == saved["download_workers"] == 1
    for invalid in (0, 9, True, 1.5, "4", None):
        before = config.read_bytes()
        assert request(url, "/api/config", {"download_workers": invalid})[0] == 400
        assert config.read_bytes() == before
    for invalid in (0, 3, True, 1.5, "2", None):
        before = config.read_bytes()
        assert request(url, "/api/config", {"download_file_concurrency": invalid})[0] == 400
        assert config.read_bytes() == before
    assert loaded["telegram_access_mode"] == "api"
    assert Path(loaded["log_path"]).is_file()
    assert loaded["download_dir"] == str(tmp_path / "inbox")
    assert Path(loaded["unverified_dir"]) == tmp_path / "inbox" / ".unverified"
    assert not loaded["browser_profile_error"]
    for value in ([], {"browser_executable_path": []}, {**values, "archive_test_executable_path": []}):
        assert request(url, "/api/config", value)[0] == 400
    for invalid in (0, 86401, 120.5, "180"):
        status, _ = request(url, "/api/config", {**values, "download_start_timeout_seconds": invalid})
        assert status == 400
    assert request(url, "/api/config", {**values, "browser_executable_path": "",
                                          "upload_provider": "quark_browser"})[0] == 400
    assert request(url, "/api/config", {**values, "browser_executable_path": "",
                                          "upload_provider": "local"})[0] == 200


def test_download_directory_is_editable_and_invalid_paths_do_not_change_config(ui_server, tmp_path):
    url, config_path = ui_server
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"MZ")
    browser = {"browser_executable_path": str(executable),
               "telegram_api_id": 12345, "telegram_api_hash": "a" * 32}
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
    config.update(browser_executable_path=executable, quark_browser_profile=str(tmp_path / "profile"),
                  upload_provider="quark_browser", telegram_api_id=12345,
                  telegram_api_hash="a" * 32, telegram_access_mode="api")
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
    assert request(url, "/api/config", {"recent_message_limit": 17})[0] == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["telegram_api_id"] == 12345 and saved["telegram_api_hash"] == "a" * 32
    assert saved["telegram_api_proxy_url"] == payload["telegram_api_proxy_url"]
    assert saved["telegram_api_dc2_server"] == payload["telegram_api_dc2_server"]
    before = config_path.read_bytes()
    assert request(url, "/api/config", {"telegram_api_hash": "********"})[0] == 400
    assert config_path.read_bytes() == before
    assert request(url, "/api/config", {**payload, "telegram_api_hash": "bad"})[0] == 400
    assert request(url, "/api/config", {**payload, "telegram_run_mode": "watch"})[0] == 400
    assert request(url, "/api/config", {**payload, "telegram_api_proxy_url": "socks5://user:pass@localhost:7890"})[0] == 400
    assert request(url, "/api/config", {**payload, "telegram_api_dc2_server": "149.154.167.40:443"})[0] == 400
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["telegram_api_proxy_url"] = "socks5://user:pass@localhost:7890"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    with urlopen(url + "/api/config") as response:
        assert "user:pass" not in response.read().decode("utf-8")


def test_download_diagnostics_export_is_a_zip_and_does_not_export_credentials(ui_server):
    import io

    url, config_path = ui_server
    private_hash = "1234567890abcdef" * 2
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.update(telegram_api_hash=private_hash)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    with urlopen(url + "/api/download-diagnostics/export") as response:
        assert response.headers["Content-Type"] == "application/zip"
        assert response.headers["Cache-Control"] == "no-store"
        body = response.read()
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert archive.testzip() is None
        assert "analysis.txt" in archive.namelist()
        assert "暂无文件下载诊断记录" in archive.read("analysis.txt").decode("utf-8")
        assert not any(private_hash.encode() in archive.read(name) for name in archive.namelist())
        assert not any(name.endswith((".session", ".log")) for name in archive.namelist())
    with urlopen(url + "/?view=history") as response:
        assert b'id="export-download-diagnostics"' in response.read()


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


def test_channel_catalog_requires_api_and_valid_cached_messages(ui_server):
    url, _ = ui_server
    with urlopen(url + "/api/channel-messages") as response:
        listing = json.load(response)
    assert listing["channel"] == "free_3d_models_download" and listing["messages"] == []
    assert request(url, "/api/channel-scan")[0] == 400
    assert request(url, "/api/channel-download", {"ids": [1]})[0] == 400
    assert request(url, "/api/channel-scan", origin="https://evil.example")[0] == 403
    with pytest.raises(HTTPError) as failure:
        urlopen(url + "/api/channel-preview/123")
    assert failure.value.code == 404

    api = {"telegram_access_mode": "api", "telegram_api_id": 12345, "telegram_api_hash": "a" * 32,
           "telegram_run_mode": "once", "browser_executable_path": "",
           "exclude_forwarded_posts": True, "channel_message_limit": 100}
    assert request(url, "/api/config", api)[0] == 200
    with urlopen(url + "/api/config") as response:
        loaded = json.load(response)
    assert loaded["exclude_forwarded_posts"] is True
    assert loaded["channel_message_limit"] == 100
    for invalid in (0, 101, 1.5, "100"):
        assert request(url, "/api/config", {**api, "channel_message_limit": invalid})[0] == 400
    for payload in ({"older": True}, {"older": "true"}):
        assert request(url, "/api/channel-scan", payload)[0] == 400
    for payload in ({"ids": []}, {"ids": [1]}, {"ids": [True]}, {"ids": [1, 1]}, {"ids": "1"}):
        assert request(url, "/api/channel-download", payload)[0] == 400


def test_saved_channels_can_be_added_selected_renamed_and_removed(ui_server):
    url, config_path = ui_server
    with urlopen(url + "/api/channels") as response:
        initial = json.load(response)
    assert initial["active"] == "free_3d_models_download"
    assert [row["key"] for row in initial["channels"]] == ["free_3d_models_download"]

    status, added = request(url, "/api/channels", {"url": "https://t.me/Example_Models", "name": "示例模型"})
    assert status == 200 and added["channel"]["key"] == "example_models"
    assert request(url, "/api/channels", {"url": "https://t.me/example_models"})[0] == 400
    assert request(url, "/api/channels", {"url": "https://example.com/models"})[0] == 400

    status, selected = request(url, "/api/channels/select", {"key": "example_models"})
    assert status == 200 and selected["channel"]["name"] == "示例模型"
    assert json.loads(config_path.read_text(encoding="utf-8"))["source_channel_url"] == "https://t.me/example_models"
    with urlopen(url + "/api/channel-messages") as response:
        assert json.load(response)["channel"] == "example_models"

    assert request(url, "/api/channels/rename", {"key": "example_models", "name": "新名称"})[0] == 200
    assert request(url, "/api/channels/remove", {"key": "free_3d_models_download"})[0] == 200
    assert request(url, "/api/channels/remove", {"key": "example_models"})[0] == 400


def test_channel_scan_and_selected_download_use_existing_task_store(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "telegram_access_mode": "api", "telegram_api_id": 12345, "telegram_api_hash": "a" * 32,
        "source_channel_url": "https://t.me/example_channel", "upload_provider": "local",
        "inbox_dir": str(tmp_path / "inbox"), "output_dir": str(tmp_path / "output"),
        "work_dir": str(tmp_path / "work"), "telegram_session": str(tmp_path / "telegram.session"),
    }), encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    code = """
import sys
from pathlib import Path
import zipfile
from xianyu_pipeline import web_ui
from xianyu_pipeline.models import SourceMessage
def listing(self, channel, *, before_id=0, limit=12, preview_dir, known_covers=None,
            on_page=None, on_progress=None, take_request=None, close_requests=None,
            on_request_done=None, media_only=False):
    if media_only:
        while (job := take_request()) is not None:
            assert job["kind"] == "download" and job["message_id"] == 42
            target = self.download_dir / "fixture.zip"
            with zipfile.ZipFile(target, "w") as archive:
                archive.writestr("file.txt", "fixture")
            self.on_event({"chat": "example_channel", "message_id": 42,
                           "filename": "fixture.zip", "local_path": str(target),
                           "download_status": "completed", "received": target.stat().st_size,
                           "size": target.stat().st_size})
            on_request_done(job, {}, "")
        assert close_requests()
        return {"messages": [], "oldest_scanned_id": 0, "has_more": False}
    preview_dir.mkdir(parents=True, exist_ok=True)
    cover = preview_dir / "cover.jpg"
    cover.write_bytes(b"\\xff\\xd8\\xffpreview")
    return {"messages": [{"message_id": 42, "chat": "example_channel",
             "filename": "fixture.zip", "size": 125, "text": "preview",
             "date": "2026-10-02T08:00:00+00:00", "source_url": "https://t.me/example_channel/42",
             "cover_path": str(cover), "cover_message_id": 41}],
            "oldest_scanned_id": 42, "has_more": False}
def fetch(self, chat, ids, channel_url="", mode="message_ids", limit=20):
    assert ids == [42] and mode == "message_ids"
    target = self.download_dir / "fixture.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("file.txt", "fixture")
    return [SourceMessage(message_id=42, chat="example_channel", attachment_paths=[target])]
web_ui.TelethonTelegramAdapter.list_archives = listing
web_ui.TelethonTelegramAdapter.fetch_messages = fetch
web_ui.TelethonTelegramAdapter.list_joined_channels = lambda self, **options: [{
    "id": 123, "title": "Example", "username": "example_channel", "key": "example_channel",
    "target": "https://t.me/example_channel", "url": "https://t.me/example_channel",
    "public": True, "avatar_url": "",
}]
web_ui.start_web_ui(sys.argv[1], port=int(sys.argv[2]), open_browser=False)
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(config), str(port)],
                               cwd=Path(__file__).resolve().parents[1],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    url = f"http://127.0.0.1:{port}"

    def wait_done():
        for _ in range(100):
            with urlopen(url + "/api/workbench") as response:
                data = json.load(response)
            if not data["running"]:
                return data
            time.sleep(0.05)
        raise AssertionError("worker did not finish")

    try:
        for _ in range(100):
            try:
                with urlopen(url + "/api/config", timeout=0.5):
                    break
            except (URLError, OSError):
                if process.poll() is not None:
                    raise AssertionError("UI server exited")
                time.sleep(0.05)
        assert request(url, "/api/channel-scan")[0] == 200
        wait_done()
        assert request(url, "/api/telegram-channels-scan")[0] == 200
        wait_done()
        assert request(url, "/api/telegram-channels/select", {"key": "example_channel"})[0] == 200
        assert request(url, "/api/channel-scan")[0] == 200
        wait_done()
        with urlopen(url + "/api/channel-messages") as response:
            rows = json.load(response)["messages"]
        assert len(rows) == 1 and rows[0]["message_id"] == 42
        assert rows[0]["cover_url"].startswith("/api/channel-preview/42?v=")
        with urlopen(url + rows[0]["cover_url"]) as response:
            assert response.headers["Content-Type"] == "image/jpeg"
        assert not list((tmp_path / "inbox").glob("*.zip"))
        assert request(url, "/api/channel-download", {"ids": [42]})[0] == 200
        state = wait_done()
        assert len(state["tasks"]) == 1
        assert state["tasks"][0]["message_id"] == 42
        assert state["tasks"][0]["download_status"] == "completed"
        assert request(url, "/api/channel-download", {"ids": [42]})[0] == 400
        assert len(list((tmp_path / "inbox").glob("*.zip"))) == 1
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_workbench_static_assets_are_served_locally(ui_server):
    url, _ = ui_server
    for path, mime in (("/", "text/html"), ("/static/workbench.js", "text/javascript"),
                       ("/static/workbench.css", "text/css"), ("/static/lucide.js", "text/javascript")):
        with urlopen(url + path) as response:
            assert response.headers["Content-Type"].startswith(mime)
            assert len(response.read()) > 100
