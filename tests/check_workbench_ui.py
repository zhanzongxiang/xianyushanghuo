"""Offline workbench acceptance against source or an optional portable executable.

Run with the project's Python environment:
    python tests/check_workbench_ui.py [--browser CHROME] [--executable APP.exe]

BROWSER_TEST_PATH remains supported. No login, live channel, external request,
or existing account/configuration is used. Screenshots go to tests/artifacts.
The supervisor stops the whole worker process tree after 78 seconds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import zipfile
import zlib


REPO = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO / "tests" / "artifacts"
DEFAULT_BROWSER = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
WORKER_TIMEOUT = 78
API_ID = 12345
API_HASH = "a" * 32
NEW_API_HASH = "b" * 32
COOKIE = "offline-fixture-cookie-not-a-real-credential"
FILES = {"arch": "01-architectural-forms.zip", "vase": "02-ceramic-vases.zip",
         "plain": "03-reference-documents.zip", "broken": "04-damaged-archive.zip"}


def require(condition, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class Budget:
    def __init__(self, seconds=65):
        self.deadline = time.monotonic() + seconds

    def seconds(self, maximum=8):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, "Offline UI acceptance exhausted its 65-second work budget")
        return min(maximum, remaining)

    def ms(self, maximum=8):
        return max(1, int(self.seconds(maximum) * 1000))


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise AssertionError(f"Offline server unexpectedly redirected a request: {code}")


class LocalAPI:
    def __init__(self, origin: str, budget: Budget):
        self.origin = origin
        self.budget = budget
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def request(self, path: str, payload=None):
        require(path.startswith("/") and not path.startswith("//"), "Expected a local API path")
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(self.origin + path, data=data,
                          headers={"Content-Type": "application/json", "Origin": self.origin})
        with self.opener.open(request, timeout=self.budget.seconds(2)) as response:
            require(response.status == 200, f"Unexpected HTTP status for {path}: {response.status}")
            return response.headers.get_content_type(), response.read()

    def json(self, path: str, payload=None):
        mime, content = self.request(path, payload)
        require(mime == "application/json", f"Expected JSON from {path}, got {mime}")
        return json.loads(content)

    def wait_state(self, predicate, label: str):
        deadline = time.monotonic() + self.budget.seconds(10)
        while True:
            state = self.json("/api/workbench")
            if predicate(state):
                return state
            require(time.monotonic() < deadline, f"Timed out waiting for {label}: {state}")
            time.sleep(min(0.1, self.budget.seconds()))


def small_png() -> bytes:
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    width, height = 48, 36
    pixels = b"".join(b"\0" + bytes((102, 151, 128)) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def make_fixtures(root: Path, browser: Path):
    for directory in ("inbox", "work", "output", "sessions", "profiles", "temp", "home", "appdata", "localappdata"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    asset_dir = REPO / "outputs" / "ui-redesign-v2" / "assets"
    for key in ("arch", "vase", "plain"):
        with zipfile.ZipFile(root / "inbox" / FILES[key], "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("README.txt", f"Offline acceptance material: {key}. No production content.")
            if key != "plain":
                images = [asset_dir / f"{key}{suffix}.png" for suffix in ("", "-side", "-back")]
                images = [path for path in images if path.is_file() and path.stat().st_size <= 8 * 1024 * 1024]
                for index, image in enumerate(images):
                    archive.writestr(f"preview-{index + 1}.png", image.read_bytes())
                if not images:
                    archive.writestr("preview-1.png", small_png())
    (root / "inbox" / FILES["broken"]).write_bytes(b"PK\x03\x04Offline intentionally damaged ZIP")
    (root / "inbox" / "standalone-preview.png").write_bytes(small_png())
    settings = {
        "source_channel_url": "", "source_chat": "", "fetch_mode": "recent", "message_ids": [],
        "telegram_access_mode": "api", "telegram_api_id": API_ID, "telegram_api_hash": API_HASH,
        "telegram_api_proxy_url": "", "telegram_api_dc2_server": "", "telegram_run_mode": "once",
        "recent_message_limit": 20, "telegram_scan_limit": 77,
        "upload_provider": "local", "cloud_folder": "", "quark_folder_url": "", "quark_cookie": COOKIE,
        "browser_executable_path": str(browser), "telegram_browser_headless": True,
        "quark_headless": True, "telegram_browser_timeout_seconds": 180,
        "download_start_timeout_seconds": 180, "download_transfer_timeout_seconds": 3600,
        "archive_test_executable_path": "", "make_sfx": False, "allow_executable": False,
        "require_manual_review": True,
        "inbox_dir": str(root / "inbox"), "work_dir": str(root / "work"), "output_dir": str(root / "output"),
        "telegram_session": str(root / "sessions" / "telegram.session"),
        "telegram_browser_profile": str(root / "profiles" / "telegram"),
        "quark_browser_profile": str(root / "profiles" / "quark"),
        "quark_upload_input_selector": "fixture-upload-must-not-be-used",
        "quark_file_row_selector": "fixture-row-must-not-be-used",
        "quark_share_button_selector": "fixture-share-must-not-be-used",
        "quark_share_link_selector": "fixture-link-must-not-be-used",
    }
    path = root / "offline.json"
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    # Seed a persisted API album without contacting Telegram or altering live data.
    source = root / "inbox" / FILES["arch"]
    preview_dir = root / "inbox" / ".previews"
    preview_dir.mkdir()
    previews = []
    with zipfile.ZipFile(source) as archive:
        pictures = [archive.read(name) for name in archive.namelist() if name.endswith(".png")]
    for index in range(5):
        preview = preview_dir / f"album-{index}.png"
        preview.write_bytes(pictures[index % len(pictures)])
        previews.append(str(preview))
    identity = hashlib.sha256(f"fixture_channel:100:{source.name}".encode()).hexdigest()[:24]
    task = {"id": identity, "message_id": 100, "chat": "fixture_channel", "filename": source.name,
            "title": source.stem, "source_url": "", "size": source.stat().st_size,
            "received": source.stat().st_size, "speed": 0, "eta": None, "download_status": "completed",
            "upload_status": "waiting", "validation": "pending", "local_path": str(source),
            "image_paths": previews, "image_source": "preceding_album", "image_message_ids": [90, 92, 94, 96, 98],
            "image_group_id": "9007199254740993123", "share_url": "", "error": "", "upload_provider": "",
            "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"}
    (root / "output" / "workbench-tasks.json").write_text(json.dumps([task]), encoding="utf-8")
    return path, settings


def isolated_environment(root: Path):
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            environment.pop(key)
    environment.update(NO_PROXY="127.0.0.1,localhost", PYTHONUNBUFFERED="1", PYTHONUTF8="1",
                       HOME=str(root / "home"), USERPROFILE=str(root / "home"),
                       APPDATA=str(root / "appdata"), LOCALAPPDATA=str(root / "localappdata"),
                       TEMP=str(root / "temp"), TMP=str(root / "temp"))
    return environment


def stop_process(process, *, whole_group=False):
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=4, check=False)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    elif whole_group:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def verify_http(api: LocalAPI, root: Path):
    for path, expected in (("/", "text/html"), ("/static/workbench.css", "text/css"),
                           ("/static/workbench.js", "text/javascript"), ("/static/lucide.js", "text/javascript")):
        mime, body = api.request(path)
        require(mime == expected and len(body) > 100, f"Missing or invalid new UI asset: {path}")
        if path == "/":
            require(b'id="task-list"' in body and b'id="config-form"' in body,
                    "Server is not serving the new static workbench HTML")
    config = api.json("/api/config")
    require(config["source_channel_url"] == "" and config["source_chat"] == "", "Source must remain offline")
    require(config["telegram_access_mode"] == "api" and config["upload_provider"] == "local", "Unsafe fixture mode")
    require(config["telegram_api_configured"], "Fake API credentials were not loaded")
    require("telegram_api_hash" not in config and "quark_cookie" not in config, "GET /api/config leaked secrets")
    require(API_HASH not in json.dumps(config) and COOKIE not in json.dumps(config), "Config payload contains a secret")
    require(Path(config["download_dir"]).resolve() == root / "inbox", "Server is using a non-fixture inbox")
    logs = api.json("/api/logs")
    require(isinstance(logs["logs"], list) and not logs["running"], "Unexpected initial log state")
    state = api.json("/api/workbench")
    require(not state["running"] and state["disk"]["total"] >= state["disk"]["free"] > 0, "Invalid initial status")
    tasks = {task["filename"]: task for task in state["tasks"]}
    require(set(tasks) == set(FILES.values()) and len(state["tasks"]) == len(FILES), "Fixture tasks missing or duplicated")
    for key, name in FILES.items():
        task = tasks[name]
        require(task["download_status"] == "completed", f"Imported file not available: {name}")
        require(Path(task["local_path"]).resolve() == root / "inbox" / name, "Task references a non-fixture file")
        require("image_paths" not in task, "Workbench exposed internal preview paths")
        require(bool(task["images"]) == (key in {"arch", "vase"}), f"Wrong preview state for {name}")
        for image in task["images"]:
            mime, data = api.request(image["url"])
            require(mime == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"), "Invalid preview response")
    return {key: tasks[name] for key, name in FILES.items()}


def verify_saved_config(path: Path, original: dict, expected_hash: str, expected_limit: int):
    current = json.loads(path.read_text(encoding="utf-8"))
    require(current["source_channel_url"] == current["source_chat"] == "", "Save enabled a live source")
    require(current["upload_provider"] == "local" and current["telegram_access_mode"] == "api", "Save changed offline mode")
    require(current["telegram_api_hash"] == expected_hash and current["telegram_api_id"] == API_ID,
            "Saving cleared or changed retained fake API credentials")
    require(current["recent_message_limit"] == expected_limit, "Edited setting was not saved")
    for key in ("quark_cookie", "telegram_session", "telegram_browser_profile", "quark_browser_profile",
                "inbox_dir", "work_dir", "output_dir", "quark_upload_input_selector", "quark_file_row_selector",
                "quark_share_button_selector", "quark_share_link_selector", "telegram_scan_limit"):
        require(current[key] == original[key], f"Save lost hidden configuration: {key}")


def exercise_browser(args, root, config_path, original, tasks, api, budget):
    from playwright.sync_api import sync_playwright

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    errors, external, posts = [], [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(args.browser))
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(budget.ms(6))
            page.on("pageerror", lambda error: errors.append(str(error)))

            def route_request(route):
                url = route.request.url
                if not url.startswith(api.origin + "/") and not url.startswith("data:"):
                    external.append(url)
                    return route.abort()
                if route.request.method == "POST":
                    posts.append(url.removeprefix(api.origin))
                route.continue_()

            page.route("**/*", route_request)

            def open_view(view):
                page.goto(api.origin + "/?view=" + view)
                page.wait_for_function("document.querySelector('#last-sync').textContent.includes(':')")

            def row(key):
                return page.locator(f'.task[data-task-id="{tasks[key]["id"]}"]')

            open_view("download")
            require(page.locator(".task:visible").count() == 4, "Imported task list missing")
            row("arch").locator(".task-title").click()
            page.wait_for_function("document.querySelector('#preview img')?.naturalWidth > 0")
            require(page.locator("#preview-strip button").count() == 5, "Five-photo album missing")
            require(page.locator("#detail-image-source").inner_text() == "前置图集（自动关联）", "Album source missing")
            require(page.locator("#detail-image-messages").inner_text() == "90, 92, 94, 96, 98", "Album message IDs missing")
            page.locator("#preview-strip button").nth(4).click()
            require(page.locator("#photo-info").inner_text() == "5 / 5 张", "Fifth image cannot be selected")
            page.wait_for_function("document.querySelector('#preview img')?.naturalWidth > 0")
            row("plain").locator(".task-title").click()
            require(page.locator("#preview img").count() == 0, "Missing image was replaced by fake media")
            row("arch").locator(".task-title").click()
            row("arch").locator(".task-checkbox").check()
            page.locator("#refresh").click()
            page.wait_for_timeout(400)
            require(row("arch").locator(".task-checkbox").is_checked(), "Refresh lost selection")
            page.locator("#task-search").fill("architectural")
            require(page.locator(".task:visible").count() == 1, "Search does not filter tasks")
            page.locator("#refresh").click()
            page.wait_for_timeout(250)
            require(page.locator("#task-search").input_value() == "architectural", "Refresh reset search")
            page.locator("#task-search").fill("")
            page.screenshot(path=str(ARTIFACTS / "workbench-download-desktop.png"), full_page=True)

            # An empty source deliberately exercises the local adapter, never Telegram.
            api.json("/api/download", {})
            api.wait_state(lambda state: not state["running"], "local-only download")
            page.locator('.nav-item[data-view="upload"]').click()
            page.locator("#refresh").click()
            page.wait_for_timeout(250)
            page.locator("#upload").click()
            done = api.wait_state(lambda state: not state["running"] and any(
                task["id"] == tasks["arch"]["id"] and task["upload_status"] == "local"
                for task in state["tasks"]), "selected local upload")
            copied = root / "output" / "cloud_fixture" / "default" / FILES["arch"]
            require(copied.read_bytes() == (root / "inbox" / FILES["arch"]).read_bytes(), "Local upload content differs")
            require(sum(task["upload_status"] == "local" for task in done["tasks"]) == 1, "Uploaded unselected tasks")
            page.locator("#refresh").click()
            page.wait_for_timeout(300)
            page.screenshot(path=str(ARTIFACTS / "workbench-upload-desktop.png"), full_page=True)

            api.json("/api/upload", {"ids": [tasks["broken"]["id"]]})
            failed = api.wait_state(lambda state: not state["running"] and any(
                task["id"] == tasks["broken"]["id"] and task["upload_status"] == "failed"
                for task in state["tasks"]), "invalid archive rejection")
            require(any(task.get("error") for task in failed["tasks"]), "Failure omitted real error")
            require(not (root / "output/cloud_fixture/default" / FILES["broken"]).exists(), "Invalid ZIP uploaded")
            page.locator("#refresh").click()
            page.wait_for_timeout(300)
            row("broken").locator(".task-title").click()
            require(page.locator("#detail-error").is_visible(), "Task failure not visible in details")

            open_view("settings")
            page.wait_for_function("!document.querySelector('#config-fields').disabled")
            require(page.locator("#telegram_api_hash").input_value() == "", "Secret hash echoed in UI")
            require(page.locator("#telegram_api_id").input_value() == "", "API ID echoed in UI")
            page.locator("#recent_message_limit").fill("17")
            page.locator("#refresh").click()
            page.wait_for_timeout(1200)
            require(page.locator("#recent_message_limit").input_value() == "17", "Polling overwrote unsaved form")
            with page.expect_response(lambda response: response.url.endswith("/api/config") and response.request.method == "POST"):
                page.locator("#save-config").click()
            page.wait_for_function("!document.querySelector('#save-config').disabled")
            verify_saved_config(config_path, original, API_HASH, 17)
            page.locator("#telegram_api_hash").fill(NEW_API_HASH)
            with page.expect_response(lambda response: response.url.endswith("/api/config") and response.request.method == "POST"):
                page.locator("#save-config").click()
            page.wait_for_function("document.querySelector('#telegram_api_hash').value === ''")
            verify_saved_config(config_path, original, NEW_API_HASH, 17)
            require(NEW_API_HASH not in json.dumps(api.json("/api/config")), "Secret hash returned by API")

            new_dir = root / "custom-downloads"
            new_dir.mkdir()
            with zipfile.ZipFile(new_dir / "new-material.zip", "w") as archive:
                archive.writestr("asset.txt", "fixture in selected directory")
            page.locator("#inbox_dir").fill(str(new_dir))
            with page.expect_response(lambda response: response.url.endswith("/api/config") and response.request.method == "POST"):
                page.locator("#save-config").click()
            page.wait_for_function("document.querySelector('#config-download-dir').textContent.includes('custom-downloads')")
            require(api.json("/api/config")["inbox_dir"] == str(new_dir), "Selected download directory not saved")
            changed = api.json("/api/workbench")
            require(changed["download_dir"] == str(new_dir), "Workbench still points to old directory")
            require(any(task["filename"] == "new-material.zip" for task in changed["tasks"]), "New directory was not imported")
            require(api.request(tasks["arch"]["images"][0]["url"])[0] == "image/png", "Old preview broke after directory change")
            require((root / "inbox" / FILES["arch"]).is_file(), "Changing directory moved existing files")

            for name, width, height in (("desktop", 1440, 1000), ("mobile", 390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                for view in ("download", "upload", "library", "history", "channels", "settings"):
                    budget.seconds()
                    open_view(view)
                    require(page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Overflow: {view}/{name}")
                    page.evaluate("Array.from(document.images).forEach(img => { img.loading = 'eager'; })")
                    page.wait_for_function("Array.from(document.images).filter(img => img.getClientRects().length).every(img => img.complete && img.naturalWidth > 0)")
                    page.screenshot(path=str(ARTIFACTS / f"workbench-{view}-{name}.png"), full_page=True)
            open_view("download")
            row("arch").locator(".task-title").click()
            require(page.locator("#inspector").is_visible(), "Mobile details did not open")
            page.locator("#preview-strip button").nth(4).click()
            require(page.locator("#photo-info").inner_text() == "5 / 5 张", "Mobile fifth image cannot be selected")
            page.wait_for_function("document.querySelector('#preview img')?.naturalWidth > 0")
            page.screenshot(path=str(ARTIFACTS / "workbench-mobile-details.png"), full_page=True)
            page.locator("#close-inspector").click()
            require(not page.locator("#inspector").is_visible(), "Mobile details did not close")
            require(not errors, f"Browser errors: {errors}")
            require(not external, f"External requests attempted: {external}")
            require(set(posts) <= {"/api/upload", "/api/config"}, f"Unexpected browser mutations: {posts}")
            print("PASS: real local upload, validation failure, previews, settings, polling and desktop/mobile layouts", flush=True)
        finally:
            browser.close()


def run_checks(args, root: Path):
    budget = Budget()
    config_path, original = make_fixtures(root, args.browser)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    api = LocalAPI(f"http://127.0.0.1:{port}", budget)
    command = ([str(args.executable)] if args.executable else [sys.executable, str(REPO / "run_pipeline.py")])
    command += ["--config", str(config_path), "--web-ui", "--no-open-browser", "--web-port", str(port)]
    server = None
    log_path = root / "server.log"
    try:
        with log_path.open("wb") as output:
            server = subprocess.Popen(command, cwd=root, env=isolated_environment(root), stdin=subprocess.DEVNULL,
                                      stdout=output, stderr=subprocess.STDOUT,
                                      creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            deadline = time.monotonic() + budget.seconds(18)
            while True:
                require(server.poll() is None, "Temporary UI server exited during startup")
                try:
                    api.json("/api/config")
                    break
                except (URLError, OSError):
                    require(time.monotonic() < deadline, "Temporary UI server did not start in time")
                    time.sleep(0.1)
            print("PASS: isolated temporary server started", flush=True)
            tasks = verify_http(api, root)
            print("PASS: HTML, static assets, config, workbench, logs and local previews", flush=True)
            exercise_browser(args, root, config_path, original, tasks, api, budget)
    except BaseException:
        if log_path.is_file():
            print("Temporary server log (last 6000 characters):", file=sys.stderr)
            print(log_path.read_text(encoding="utf-8", errors="replace")[-6000:], file=sys.stderr)
        raise
    finally:
        stop_process(server)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--browser", type=Path, default=Path(os.environ.get("BROWSER_TEST_PATH") or DEFAULT_BROWSER),
                        help="Chrome/Chromium executable; defaults to BROWSER_TEST_PATH or Program Files Google Chrome")
    parser.add_argument("--executable", type=Path, help="Test a portable EXE instead of launching source with sys.executable")
    parser.add_argument("--keep-temp", action="store_true", help="Keep disposable fixtures and server logs for diagnosis")
    parser.add_argument("--worker-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.browser = args.browser.expanduser().resolve()
    if not args.browser.is_file():
        parser.error(f"Browser not found: {args.browser}; pass --browser or BROWSER_TEST_PATH")
    if args.executable:
        args.executable = args.executable.expanduser().resolve()
        if not args.executable.is_file():
            parser.error(f"Portable executable not found: {args.executable}")
    if args.worker_root:
        run_checks(args, args.worker_root.resolve())
        return
    root = Path(tempfile.mkdtemp(prefix="xianyu-workbench-ui-")).resolve()
    worker = None
    try:
        command = [sys.executable, str(Path(__file__).resolve()), "--worker-root", str(root), "--browser", str(args.browser)]
        if args.executable:
            command += ["--executable", str(args.executable)]
        worker = subprocess.Popen(command, cwd=REPO, stdin=subprocess.DEVNULL,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                  start_new_session=os.name != "nt")
        try:
            code = worker.wait(timeout=WORKER_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise SystemExit(f"FAIL: offline UI acceptance exceeded {WORKER_TIMEOUT}s; stopping owned child processes")
        if code:
            raise SystemExit(code)
        print(f"PASS: offline workbench acceptance; screenshots: {ARTIFACTS}")
    finally:
        stop_process(worker, whole_group=True)
        if args.keep_temp:
            print(f"Disposable fixtures retained: {root}")
        else:
            require(root.parent == Path(tempfile.gettempdir()).resolve() and root.name.startswith("xianyu-workbench-ui-"),
                    "Refusing to remove an unexpected temporary directory")
            try:
                shutil.rmtree(root)
            except OSError:
                print(f"Temporary directory could not be removed: {root}", file=sys.stderr)


if __name__ == "__main__":
    main()
