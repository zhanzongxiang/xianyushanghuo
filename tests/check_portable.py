"""Acceptance of the shipped EXE using disposable, local-only fixtures."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
import zipfile


def snapshot(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*") if path.is_file()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    executable = args.executable.resolve()
    root = Path(tempfile.mkdtemp(prefix="xianyu-v31-portable-"))
    legacy = root / "legacy"
    browser = os.environ["BROWSER_TEST_PATH"]
    old_browser = os.environ["LEGACY_BROWSER_TEST_PATH"]
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(legacy), executable_path=old_browser,
                                                                headless=True)
        context.close()
    before = snapshot(legacy)
    settings = {
        "browser_executable_path": browser, "telegram_browser_profile": str(legacy),
        "telegram_browser_headless": False, "upload_provider": "local",
        "source_channel_url": "",
        "inbox_dir": str(root / "inbox"), "work_dir": str(root / "work"),
        "output_dir": str(root / "output"), "telegram_access_mode": "browser",
    }
    config = root / "test.local.json"
    config.write_text(json.dumps(settings), encoding="utf-8")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    command = [str(executable), "--config", str(config)]
    def run(arguments):
        result = subprocess.run(command + arguments, cwd=root, capture_output=True, timeout=90,
                                creationflags=flags)
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        return result
    run(["--browser-check"])
    log_file = root / "output/logs/pipeline.log"
    assert "浏览器检查通过" in log_file.read_text(encoding="utf-8")
    assert snapshot(legacy) == before
    inbox = root / "inbox"
    inbox.mkdir(exist_ok=True)
    with zipfile.ZipFile(inbox / "fixture.zip", "w") as archive:
        archive.writestr("asset.pdf", b"local acceptance fixture")
        archive.writestr("cover.png", base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9WQAAAAASUVORK5CYII="))
    run([])
    manifest = json.loads((root / "output/manifest.json").read_text(encoding="utf-8"))
    assert len(manifest) == 1 and manifest[0]["status"] == "pending_review"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        unused_port = listener.getsockname()[1]
    api_config = root / "api-smoke.json"
    api_config.write_text(json.dumps({
        "telegram_api_id": 12345, "telegram_api_hash": "a" * 32,
        "telegram_api_proxy_url": f"socks5://127.0.0.1:{unused_port}",
        "telegram_api_dc2_server": "149.154.167.50:443",
        "telegram_session": str(root / "smoke.session"),
    }), encoding="utf-8")
    api_result = subprocess.run([str(executable), "--config", str(api_config), "--telegram-api-login"],
                                cwd=root, capture_output=True, timeout=90, creationflags=flags)
    api_error = api_result.stderr.decode("utf-8", errors="replace")
    assert api_result.returncode != 0 and "ModuleNotFoundError" not in api_error, api_error
    assert "Telegram API" in api_error and "RuntimeError" in api_error, api_error
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(command + ["--web-ui", "--no-open-browser", "--web-port", str(port)],
                               cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=flags)
    try:
        for _ in range(200):
            try:
                with urlopen(url + "/api/config", timeout=0.5) as response:
                    loaded = json.load(response)
                break
            except (URLError, OSError):
                assert process.poll() is None, "packaged UI exited"
                time.sleep(0.1)
        else:
            raise AssertionError("packaged UI did not start")
        assert loaded["browser_version"]
        assert Path(loaded["browser_profile_dir"]) != legacy
        with urlopen(url) as response:
            assert "v31" in response.read().decode("utf-8")
        with urlopen(Request(url + "/api/browser-check", data=b"", method="POST")) as response:
            assert response.status == 200
        for _ in range(300):
            with urlopen(url + "/api/logs") as response:
                state = json.load(response)
            if not state["running"]:
                break
            time.sleep(0.1)
        assert not state["running"]
        assert any("浏览器检查通过" in line for line in state["logs"])
        assert not any("任务失败" in line for line in state["logs"])
        assert snapshot(legacy) == before
    finally:
        process.terminate()
        process.wait(timeout=15)
    print(f"PASS: packaged EXE browser check, legacy preservation, offline ZIP pipeline, "
          f"API proxy import, UI and check API; fixtures: {root}")


if __name__ == "__main__":
    main()
