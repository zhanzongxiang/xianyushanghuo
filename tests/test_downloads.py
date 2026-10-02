from __future__ import annotations

import io
import base64
import logging
import os
import subprocess
import shutil
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from xianyu_pipeline.adapters import TelegramBrowserAdapter, launch_telegram_context
from xianyu_pipeline.archive_validation import ArchiveValidationError, validate_rar
from xianyu_pipeline.browser_download import BrowserDownloadMonitor, DownloadStartTimeout
from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.runtime import configure_logging, validate_browser_path
from xianyu_pipeline.web_ui import validate_browser_settings


def test_browser_path_is_required_and_not_a_command(tmp_path):
    for value in ("", "chrome.exe", str(tmp_path), str(tmp_path / "missing.exe")):
        with pytest.raises(ValueError):
            validate_browser_path(value)
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"MZ")
    assert validate_browser_path(f'"{executable}"') == executable
    with pytest.raises(ValueError):
        validate_browser_path(f"{executable} --incognito")


def test_required_browser_validated_on_backend(tmp_path):
    config = {"download_start_timeout_seconds": 180, "download_transfer_timeout_seconds": 3600}
    with pytest.raises(ValueError, match="必填"):
        validate_browser_settings(config)
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"MZ")
    config["browser_executable_path"] = str(executable)
    validate_browser_settings(config)
    for value in (0, -1, True, "10"):
        with pytest.raises(ValueError):
            validate_browser_settings({**config, "download_start_timeout_seconds": value})


def test_download_options_are_passed_without_changing_profiles():
    config = PipelineConfig(browser_executable_path="C:/browser/chrome.exe",
                            download_start_timeout_seconds=200, download_transfer_timeout_seconds=5000)
    adapter = TelegramBrowserAdapter(config.telegram_browser_profile, config.inbox_dir, **config.browser_download_options())
    assert adapter.start_timeout_seconds == 200
    assert adapter.transfer_timeout_seconds == 5000
    assert adapter.browser_executable_path == config.browser_executable_path


def test_log_persists_exception_and_timestamps(tmp_path):
    log_path = configure_logging(tmp_path)
    try:
        raise RuntimeError("persisted diagnostic")
    except RuntimeError:
        logging.exception("test failure")
    content = log_path.read_text(encoding="utf-8")
    assert "Traceback" in content and "persisted diagnostic" in content and "ERROR [" in content
    for handler in list(logging.getLogger().handlers):
        if getattr(handler, "_pipeline_handler", False):
            logging.getLogger().removeHandler(handler)
            handler.close()


@pytest.mark.parametrize(("output", "code", "category"), [
    (b"Wrong password", 11, "password_required"),
    (b"Cannot find volume second.rar", 6, "missing_volume"),
    (b"CRC failed", 3, "corrupt"),
    (b"Unknown error", 2, "test_failed"),
])
def test_rar_errors_are_classified(tmp_path, monkeypatch, output, code, category):
    archive = tmp_path / "test.rar"
    archive.write_bytes(b"Rar!\x1a\x07\x01\x00data")
    monkeypatch.setattr("xianyu_pipeline.archive_validation.find_archive_tester", lambda _: Path("UnRAR.exe"))
    def run(args, **kwargs):
        assert args[1] == "t" and "-p-" in args
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert "shell" not in kwargs
        return SimpleNamespace(returncode=code, stdout=output, stderr=b"")
    monkeypatch.setattr("xianyu_pipeline.archive_validation.subprocess.run", run)
    with pytest.raises(ArchiveValidationError) as caught:
        validate_rar(archive)
    assert caught.value.category == category


def test_fake_rar_is_not_accepted(tmp_path):
    archive = tmp_path / "fake.rar"
    archive.write_bytes(b"rar fixture")
    with pytest.raises(ArchiveValidationError) as caught:
        validate_rar(archive)
    assert caught.value.category == "invalid_format"


def test_rar_timeout(tmp_path, monkeypatch):
    archive = tmp_path / "test.rar"
    archive.write_bytes(b"Rar!\x1a\x07\x01\x00data")
    monkeypatch.setattr("xianyu_pipeline.archive_validation.find_archive_tester", lambda _: Path("UnRAR.exe"))
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("UnRAR.exe", 1)
    monkeypatch.setattr("xianyu_pipeline.archive_validation.subprocess.run", run)
    with pytest.raises(ArchiveValidationError) as caught:
        validate_rar(archive, timeout_seconds=1)
    assert caught.value.category == "timeout"


def test_failed_rar_is_quarantined_not_lost(tmp_path):
    source = tmp_path / "source.tmp"
    source.write_bytes(b"not rar")
    download = SimpleNamespace(suggested_filename="test.rar", path=lambda: source, failure=lambda: None)
    destination = tmp_path / "inbox"
    with pytest.raises(ArchiveValidationError):
        TelegramBrowserAdapter._verified_download(download, destination)
    assert not (destination / "test.rar").exists()
    preserved = list((destination / ".unverified").glob("*/test.rar"))
    assert len(preserved) == 1 and preserved[0].read_bytes() == source.read_bytes()


def test_copy_failure_preserves_completed_browser_file(tmp_path, monkeypatch):
    source = tmp_path / "browser.tmp"
    source.write_bytes(b"complete transfer")
    download = SimpleNamespace(suggested_filename="test.zip", path=lambda: source, failure=lambda: None)
    def fail_copy(*args):
        raise OSError("copy blocked")
    monkeypatch.setattr("xianyu_pipeline.adapters.shutil.copyfile", fail_copy)
    with pytest.raises(RuntimeError, match="已转存"):
        TelegramBrowserAdapter._verified_download(download, tmp_path / "inbox")
    preserved = list((tmp_path / "inbox/.unverified").glob("*/test.zip"))
    assert len(preserved) == 1 and preserved[0].read_bytes() == b"complete transfer"
    assert not source.exists()
    assert not (tmp_path / "inbox/test.zip").exists()


class FakePage:
    def __init__(self, clock):
        self.clock = clock
        self.listeners = {}
        self.closed = False
        self.tick = lambda: None

    def on(self, name, callback):
        self.listeners[name] = callback

    def wait_for_timeout(self, timeout):
        self.clock[0] += timeout / 1000
        self.tick()

    def is_closed(self):
        return self.closed


class FakeSession:
    def __init__(self):
        self.calls = []
    def on(self, *args):
        pass
    def send(self, name, args):
        self.calls.append((name, args))


def fake_monitor(tmp_path, monkeypatch):
    clock = [0]
    monkeypatch.setattr("xianyu_pipeline.browser_download.time.monotonic", lambda: clock[0])
    page = FakePage(clock)
    session = FakeSession()
    context = SimpleNamespace(browser=SimpleNamespace(new_browser_cdp_session=lambda: session))
    monitor = BrowserDownloadMonitor(page, context, tmp_path)
    clicks = []
    button = SimpleNamespace(click=lambda **kwargs: clicks.append(1))
    return clock, page, session, monitor, clicks, button


def test_start_grace_captures_late_download_without_reclick(tmp_path, monkeypatch):
    clock, page, session, monitor, clicks, button = fake_monitor(tmp_path, monkeypatch)
    guid = "00000000-0000-0000-0000-000000000001"
    page.tick = lambda: monitor._begin({"guid": guid, "url": "local", "suggestedFilename": "test.zip"}) if clock[0] == 2.5 else None
    assert monitor.start(button, 2, 1).guid == guid
    assert clicks == [1]


def test_start_timeout_stops_without_reclick(tmp_path, monkeypatch):
    clock, page, session, monitor, clicks, button = fake_monitor(tmp_path, monkeypatch)
    with pytest.raises(DownloadStartTimeout):
        monitor.start(button, 1, 1)
    assert clicks == [1]


def test_transfer_timeout_explicitly_cancels(tmp_path, monkeypatch):
    clock, page, session, monitor, clicks, button = fake_monitor(tmp_path, monkeypatch)
    monitor.active_guids = set()
    monitor._begin({"guid": "job", "url": "test"})
    with pytest.raises(RuntimeError, match="传输超时"):
        monitor.wait_complete(SimpleNamespace(url="test"), 1)
    assert ("Browser.cancelDownload", {"guid": "job"}) in session.calls


def test_closed_browser_is_not_reported_as_corrupt_archive(tmp_path, monkeypatch):
    clock, page, session, monitor, clicks, button = fake_monitor(tmp_path, monkeypatch)
    monitor.active_guids = set()
    page.closed = True
    with pytest.raises(RuntimeError, match="页面已关闭"):
        monitor.wait_complete(SimpleNamespace(url="test"), 1)


@pytest.fixture
def browser_path():
    path = os.environ.get("BROWSER_TEST_PATH")
    if not path:
        pytest.skip("Set BROWSER_TEST_PATH to run local Chromium integration tests")
    return str(validate_browser_path(path))


@pytest.fixture
def download_server():
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, "w") as archive:
        archive.writestr("asset.txt", "local diagnostic fixture")
    payload = contents.getvalue()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/file"):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="fixture.zip"')
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                encoded = base64.b64encode(payload).decode("ascii")
                self.wfile.write((
                    '<button id="download">Download</button><script>'
                    'document.querySelector("button").onclick=()=>setTimeout(()=>{'
                    f'const data=Uint8Array.from(atob("{encoded}"),c=>c.charCodeAt(0));'
                    'const a=document.createElement("a");a.download="fixture.zip";'
                    'a.href=URL.createObjectURL(new Blob([data],{type:"application/zip"}));'
                    'a.click();},13500);</script>'
                ).encode("utf-8"))
        def log_message(self, *args):
            logging.info("Fixture HTTP: %s", args)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_browser_delayed_download_and_durable_copy(tmp_path, browser_path, download_server):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        staging = tmp_path / "staging"
        context = launch_telegram_context(playwright, tmp_path / "profile", True, browser_path, staging)
        try:
            page = context.pages[0]
            page.on("console", lambda message: logging.info("Fixture browser: %s", message.text))
            page.on("pageerror", lambda error: logging.error("Fixture browser error: %s", error))
            monitor = BrowserDownloadMonitor(page, context, staging)
            page.goto(download_server)
            started = time.monotonic()
            download = monitor.start(page.locator("button"), 12, 5)
            assert time.monotonic() - started >= 13
            monitor.wait_complete(download, 10)
            browser_file = Path(download.path())
            saved = TelegramBrowserAdapter._verified_download(download, tmp_path / "inbox")
            assert browser_file.is_file() and saved.is_file()
            monitor.release(download)
        finally:
            context.close()
        assert not browser_file.exists()
        assert saved.is_file()
        with zipfile.ZipFile(saved) as archive:
            assert archive.testzip() is None


def test_real_browser_scan_with_dynamic_message_nodes(tmp_path, browser_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=browser_path)
        try:
            page = browser.new_page()
            page.set_content("<main id='messages'></main>")
            page.evaluate("""() => {
                const list = document.querySelector('#messages');
                for (let i = 1; i <= 100; i++) {
                    const el = document.createElement('article');
                    el.setAttribute('data-mid', `channel-${i}`);
                    if (i % 4 === 0) {
                        el.innerHTML = `<div class="document-container"><span class="document-name">asset-${i}.rar</span></div>`;
                    } else {
                        el.textContent = `mention-${i}.rar`;
                    }
                    list.appendChild(el);
                }
                window.setInterval(() => {
                    const item = list.firstElementChild;
                    list.appendChild(item);
                    list.insertBefore(item, list.firstElementChild);
                }, 1);
            }""")
            adapter = TelegramBrowserAdapter(tmp_path / "profile", tmp_path / "inbox")
            nodes = page.locator(adapter.message_selector)
            for _ in range(5):
                snapshots = adapter._unique_message_nodes(nodes, 200)
                assert len(snapshots) == 100
                assert sum(bool(item.cards) for item in snapshots) == 25
                assert adapter._message_locator(page, snapshots[-1]).inner_text(timeout=1000)
                assert adapter._scan_archive_ids(page, nodes, 20)
        finally:
            browser.close()


def test_real_browser_card_scan_ignores_caption_and_other_files(tmp_path, browser_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=browser_path)
        try:
            page = browser.new_page()
            page.set_content("""
                <article data-mid="1">Please download report.rar <button>Download</button></article>
                <article data-mid="2">See guide.zip
                  <div class="document-container"><span class="document-name">guide.pdf</span></div>
                  <div class="document-container" id="rar"><span class="document-name">asset.rar</span></div>
                </article>
                <article data-mid="3">
                  <span class="file-name" id="zip">archive.zip</span>
                  <div class="document-container"><span class="document-name">photo.jpg</span></div>
                </article>
                <article data-mid="4"><a download="saved.rar" href="#" id="link">Download</a></article>
                <article data-mid="5">duplicate.rar
                  <div class="document-container"><span class="document-name">duplicate.rar</span></div>
                  <div class="document-container"><span class="document-name">duplicate.rar</span></div>
                </article>
            """)
            adapter = TelegramBrowserAdapter(tmp_path / "profile", tmp_path / "inbox")
            nodes = page.locator(adapter.message_selector)
            snapshots = adapter._unique_message_nodes(nodes, 10)
            assert [item.message_id for item in snapshots if item.cards] == [2, 3, 4, 5]
            assert [item.cards[0].name for item in snapshots if item.cards] == [
                "asset.rar", "archive.zip", "saved.rar", "duplicate.rar"]
            assert adapter._scan_archive_ids(page, nodes, 4) == [2, 3, 4, 5]
            for mid, target in ((2, "rar"), (3, "zip"), (4, "link")):
                message = adapter._locate_message(page, nodes, mid)
                cards = adapter._archive_cards(message)
                assert len(cards) == 1
                button = adapter._card_locator(message, cards[0])
                assert button.count() == 1 and adapter._card_still_matches(button, cards[0])
                assert button.get_attribute("id") == target
            duplicate = adapter._archive_cards(adapter._locate_message(page, nodes, 5))
            assert len(duplicate) == 2 and duplicate[0].name == duplicate[1].name
        finally:
            browser.close()


def test_real_browser_custom_attachment_selector_needs_filename(tmp_path, browser_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=browser_path)
        try:
            page = browser.new_page()
            page.set_content("""
                <article data-mid="1"><div class="download-card">Only text: file.rar</div></article>
                <article data-mid="2"><div class="download-card"><span>asset.zip</span></div></article>
            """)
            adapter = TelegramBrowserAdapter(tmp_path / "profile", tmp_path / "inbox",
                                             attachment_selector=".download-card")
            nodes = page.locator(adapter.message_selector)
            assert adapter._scan_archive_ids(page, nodes, 1) == [2]
            card = adapter._archive_cards(adapter._locate_message(page, nodes, 2))[0]
            button = adapter._card_locator(adapter._locate_message(page, nodes, 2), card)
            assert button.count() == 1 and adapter._card_still_matches(button, card)
        finally:
            browser.close()


def test_real_rar_integrity_and_password(tmp_path):
    rar = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "WinRAR/Rar.exe"
    if not rar.is_file():
        pytest.skip("WinRAR needed only to generate genuine RAR test fixtures")
    source = tmp_path / "asset.txt"
    source.write_text("Local archive validation test " * 100, encoding="utf-8")
    good = tmp_path / "good.rar"
    protected = tmp_path / "protected.rar"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    for archive, options in ((good, []), (protected, ["-hpfixture-password"])):
        subprocess.run([str(rar), "a", "-idq", *options, str(archive), str(source)],
                       check=True, stdin=subprocess.DEVNULL, capture_output=True, creationflags=flags)
    validate_rar(good)
    with pytest.raises(ArchiveValidationError) as caught:
        validate_rar(protected)
    assert caught.value.category == "password_required"
    broken = tmp_path / "broken.rar"
    broken.write_bytes(good.read_bytes()[:30])
    with pytest.raises(ArchiveValidationError):
        validate_rar(broken)
    source.write_bytes(os.urandom(160000))
    subprocess.run([str(rar), "a", "-idq", "-m0", "-v64k", str(tmp_path / "split.rar"), str(source)],
                   check=True, stdin=subprocess.DEVNULL, capture_output=True, creationflags=flags)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    first = next(tmp_path.glob("split.part1.rar"))
    missing_volume = isolated / first.name
    shutil.copyfile(first, missing_volume)
    with pytest.raises(ArchiveValidationError) as caught:
        validate_rar(missing_volume)
    assert caught.value.category == "missing_volume"


@pytest.mark.parametrize("valid_zip", [True, False])
def test_real_browser_fetch_pipeline_marks_only_validated_files(tmp_path, browser_path, monkeypatch, valid_zip):
    import json
    from xianyu_pipeline import adapters
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, "w") as archive:
        archive.writestr("asset.txt", "local end-to-end fixture")
    payload = contents.getvalue() if valid_zip else b"not a rar archive"
    filename = "fixture.zip" if valid_zip else "fixture.rar"
    encoded = base64.b64encode(payload).decode("ascii")
    html = (
        f'<div data-message-id="42"><div class="document-container" onclick="save()">'
        f'<span class="document-name">{filename}</span></div></div>'
        '<script>function save(){'
        f'const bytes=Uint8Array.from(atob("{encoded}"),c=>c.charCodeAt(0));'
        f'const a=document.createElement("a");a.download="{filename}";'
        'a.href=URL.createObjectURL(new Blob([bytes],{type:"application/octet-stream"}));a.click();}'
        '</script>'
    )
    original_launch = adapters.launch_telegram_context
    def launch(*args, **kwargs):
        context = original_launch(*args, **kwargs)
        context.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html))
        return context
    monkeypatch.setattr(adapters, "launch_telegram_context", launch)
    adapter = TelegramBrowserAdapter(tmp_path / "profile", tmp_path / "inbox", headless=True,
                                     browser_executable_path=browser_path, start_timeout_seconds=5,
                                     start_grace_seconds=0, transfer_timeout_seconds=10)
    state = tmp_path / "inbox/.telegram_archive_processed.json"
    if valid_zip:
        messages = adapter.fetch_messages("", [], "https://t.me/local_fixture", "recent", 1)
        assert len(messages) == 1 and messages[0].attachment_paths[0].is_file()
        assert json.loads(state.read_text(encoding="utf-8"))["local_fixture"] == [42]
        assert not list((tmp_path / "inbox/.browser-downloads").glob("*/*"))
    else:
        with pytest.raises(RuntimeError, match="校验失败"):
            adapter.fetch_messages("", [], "https://t.me/local_fixture", "recent", 1)
        assert not state.exists()
        assert not (tmp_path / "inbox/fixture.rar").exists()
        assert len(list((tmp_path / "inbox/.unverified").glob("*/fixture.rar"))) == 1
