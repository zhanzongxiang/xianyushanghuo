import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from xianyu_pipeline import quark_browser
from xianyu_pipeline.quark_browser import QuarkBrowserError, QuarkBrowserStorageAdapter


FOLDER_URL = "https://pan.quark.cn/list?folder=target#files"
SHARE_URL = "https://pan.quark.cn/s/Ab12_-"
SELECTORS = {"upload": "fixture-upload", "rows": "fixture-rows",
             "share": "fixture-share", "link": "fixture-link"}


class FakeLocator:
    def __init__(self, page, kind, filename=None):
        self.page = page
        self.kind = kind
        self.filename = filename

    def matches(self):
        return [name for name in self.page.files
                if self.filename.casefold() in name.casefold()]

    def filter(self, *, has_text):
        assert self.kind == "rows"
        return FakeLocator(self.page, "rows", has_text)

    @property
    def first(self):
        assert self.kind == "rows"
        return FakeLocator(self.page, "row", self.filename)

    def count(self):
        if self.kind == "upload":
            return self.page.input_count
        if self.kind in {"rows", "row"}:
            return len(self.matches())
        if self.kind == "share":
            return self.page.share_count
        assert self.kind == "link"
        return self.page.link_count

    def wait_for(self, **kwargs):
        assert kwargs == {"state": "visible", "timeout": 7000}
        self.page.step(f"{self.kind}_wait")
        if self.kind == "row" and self.page.files_after_wait is not None:
            self.page.files = self.page.files_after_wait
        if not self.count():
            raise TimeoutError(f"missing {self.kind}")

    def locator(self, selector):
        assert self.kind in {"rows", "row"} and selector == SELECTORS["share"]
        return FakeLocator(self.page, "share", self.filename)

    def get_by_text(self, text, *, exact):
        assert self.kind in {"rows", "row"} and exact is True
        count = int(self.matches()[0] == text)
        return SimpleNamespace(count=lambda: count)

    def inner_text(self):
        if self.kind in {"rows", "row"}:
            self.page.step("row_read")
            if self.page.files_on_read is not None:
                self.page.files = self.page.files_on_read
            if self.kind == "rows" and self.count() != 1:
                raise RuntimeError("fixture strict file rows")
            name = self.matches()[0]
            return f"{name}\n42 MB" if self.page.row_metadata else name
        assert self.kind == "link"
        self.page.step("link_read")
        return self.page.link_text

    def evaluate(self, script):
        assert self.kind == "link" and script == "el => 'value' in el"
        return self.page.link_is_input

    def input_value(self):
        assert self.kind == "link" and self.page.link_is_input
        self.page.step("link_read")
        return self.page.link_text

    def click(self):
        assert self.kind == "share"
        self.page.step("share_click")

    def set_input_files(self, filename):
        assert self.kind == "upload"
        self.page.uploads.append(filename)
        self.page.files.append(Path(filename).name)
        self.page.step("set_input_files")


class FakePage:
    def __init__(self):
        self.events = []
        self.files = []
        self.uploads = []
        self.input_count = self.share_count = self.link_count = 1
        self.row_metadata = True
        self.link_is_input = True
        self.link_text = SHARE_URL
        self.failure = None
        self.redirect = None
        self.files_after_wait = None
        self.files_on_read = None
        self.forbid_upload = False
        self.url = "about:blank"

    def step(self, event):
        self.events.append(event)
        if event == self.failure:
            raise RuntimeError(f"fixture failure: {event}")

    def set_default_timeout(self, timeout):
        assert timeout == 7000

    def goto(self, url, **kwargs):
        assert url == FOLDER_URL and kwargs == {"wait_until": "domcontentloaded"}
        self.step("goto")
        self.url = self.redirect or url

    def locator(self, selector):
        kind = next(kind for kind, value in SELECTORS.items() if value == selector)
        assert kind != "share", "share must be scoped to the file row"
        if kind == "upload":
            assert not self.forbid_upload, "share_existing must not access the upload input"
        return FakeLocator(self, kind)


@pytest.fixture
def browser(tmp_path, monkeypatch):
    page = FakePage()
    stages = []

    class Context:
        pages = [page]

        def new_page(self):
            page.step("new_page")
            return page

        def close(self):
            page.step("close")

    context = Context()

    class Playwright:
        def __enter__(self):
            page.step("playwright_enter")
            return self

        def __exit__(self, *args):
            page.step("playwright_exit")

    package = ModuleType("playwright")
    sync_api = ModuleType("playwright.sync_api")
    sync_api.sync_playwright = Playwright
    package.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    def launch(playwright, profile_dir, executable, **kwargs):
        assert isinstance(playwright, Playwright)
        assert profile_dir == tmp_path / "profile"
        assert executable == "fixture-browser"
        assert kwargs == {"headless": True, "accept_downloads": False}
        page.step("launch")
        return context

    monkeypatch.setattr(quark_browser, "launch_browser_context", launch)

    def record_stage(stage):
        stages.append(stage)
        page.events.append(stage)

    options = dict(folder_url=FOLDER_URL, profile_dir=tmp_path / "profile", headless=True,
                   upload_input_selector=SELECTORS["upload"], file_row_selector=SELECTORS["rows"],
                   share_button_selector=SELECTORS["share"], share_link_selector=SELECTORS["link"],
                   timeout_seconds=7, browser_executable_path="fixture-browser")
    adapter = QuarkBrowserStorageAdapter(**options, on_stage=record_stage)
    local_path = tmp_path / "asset [v1].zip"
    local_path.write_bytes(b"fixture data")
    return SimpleNamespace(page=page, context=context, stages=stages, adapter=adapter,
                           local_path=local_path, options=options, record_stage=record_stage)


@pytest.mark.parametrize("row_metadata", [True, False])
@pytest.mark.parametrize("link_is_input", [True, False])
def test_upload_reports_stages_at_side_effect_boundaries(browser, row_metadata, link_is_input):
    browser.page.row_metadata = row_metadata
    browser.page.link_is_input = link_is_input
    assert browser.adapter.upload(browser.local_path, FOLDER_URL) == SHARE_URL
    assert browser.stages == ["uploading", "link_pending", "completed"]
    assert browser.page.uploads == [str(browser.local_path.resolve())]
    events = browser.page.events
    index = events.index("set_input_files")
    assert events[index - 1:index + 2] == ["uploading", "set_input_files", "link_pending"]
    assert events.index("link_pending") < events.index("row_wait")
    assert events.index("link_read") < events.index("close") < events.index("completed")
    assert events.index("playwright_exit") < events.index("completed")


def test_share_existing_uses_only_unique_file_and_does_not_need_local_file(browser):
    browser.page.files = ["unrelated.zip", browser.local_path.name]
    browser.page.forbid_upload = True
    browser.page.input_count = 0
    browser.local_path.unlink()
    assert browser.adapter.share_existing(browser.local_path, FOLDER_URL) == SHARE_URL
    assert browser.page.uploads == []
    assert browser.page.files == ["unrelated.zip", browser.local_path.name]
    assert browser.stages == ["link_pending", "completed"]


@pytest.mark.parametrize("operation", ["upload", "share_existing"])
def test_duplicate_rows_are_rejected_without_upload_or_share(browser, operation):
    browser.page.files = [browser.local_path.name] * 2
    with pytest.raises(QuarkBrowserError):
        getattr(browser.adapter, operation)(browser.local_path, "")
    assert browser.page.uploads == []
    assert "share_click" not in browser.page.events
    assert browser.stages == ([] if operation == "upload" else ["link_pending"])


@pytest.mark.parametrize("suffix", [".bak", " copy", ""])
def test_share_existing_does_not_mistake_substring_or_case_match_for_filename(browser, suffix):
    browser.page.files = [browser.local_path.name + suffix if suffix else browser.local_path.name.upper()]
    with pytest.raises(QuarkBrowserError):
        browser.adapter.share_existing(browser.local_path, "")
    assert browser.page.uploads == []
    assert "share_click" not in browser.page.events
    assert browser.stages == ["link_pending"]


def test_missing_existing_file_never_falls_back_to_upload(browser):
    browser.page.forbid_upload = True
    with pytest.raises(TimeoutError):
        browser.adapter.share_existing(browser.local_path, "")
    assert browser.page.uploads == []
    assert browser.stages == ["link_pending"]


@pytest.mark.parametrize("failure", ["set_input_files", "row_wait", "row_read", "share_click", "link_wait",
                                     "link_read", "close", "playwright_exit"])
def test_any_post_submission_failure_stays_link_pending(browser, failure):
    browser.page.failure = failure
    with pytest.raises(RuntimeError, match=failure):
        browser.adapter.upload(browser.local_path, "")
    assert len(browser.page.uploads) == 1
    assert browser.stages == ["uploading", "link_pending"]
    assert "close" in browser.page.events


@pytest.mark.parametrize("failure", ["playwright_enter", "launch", "goto"])
@pytest.mark.parametrize("operation", ["upload", "share_existing"])
def test_preflight_failure_does_not_mark_a_new_upload_but_preserves_recovery(browser, failure, operation):
    browser.page.failure = failure
    with pytest.raises(RuntimeError, match=failure):
        getattr(browser.adapter, operation)(browser.local_path, "")
    assert browser.page.uploads == []
    assert browser.stages == ([] if operation == "upload" else ["link_pending"])


@pytest.mark.parametrize("precondition", ["missing_local", "existing_row", "no_input", "duplicate_input"])
def test_upload_preconditions_do_not_report_uploading(browser, precondition):
    if precondition == "missing_local":
        browser.local_path.unlink()
    elif precondition == "existing_row":
        browser.page.files = [browser.local_path.name]
    else:
        browser.page.input_count = 0 if precondition == "no_input" else 2
    with pytest.raises((FileNotFoundError, QuarkBrowserError)):
        browser.adapter.upload(browser.local_path, "")
    assert browser.stages == []
    assert browser.page.uploads == []


@pytest.mark.parametrize("operation", ["upload", "share_existing"])
@pytest.mark.parametrize("remote_folder", ["target", FOLDER_URL + "-other"])
def test_remote_folder_must_be_the_configured_target(browser, operation, remote_folder):
    with pytest.raises(QuarkBrowserError):
        getattr(browser.adapter, operation)(browser.local_path, remote_folder)
    assert "launch" not in browser.page.events
    assert browser.page.uploads == []
    assert "completed" not in browser.stages


@pytest.mark.parametrize("operation", ["upload", "share_existing"])
@pytest.mark.parametrize("redirect", [
    "https://pan.quark.cn.evil.invalid/list?folder=target#files",
    "https://pan.quark.cn/list-other?folder=target#files",
    "https://pan.quark.cn/list?folder=other#files",
    "https://pan.quark.cn/list?folder=target#other",
    "https://pan.quark.cn/list",
])
def test_redirect_to_an_unconfirmed_folder_is_rejected(browser, operation, redirect):
    browser.page.redirect = redirect
    browser.page.files = [browser.local_path.name] if operation == "share_existing" else []
    with pytest.raises(QuarkBrowserError):
        getattr(browser.adapter, operation)(browser.local_path, "")
    assert browser.page.uploads == []
    assert "share_click" not in browser.page.events
    assert "completed" not in browser.stages


@pytest.mark.parametrize("field,count", [("share_count", 0), ("share_count", 2),
                                         ("link_count", 0), ("link_count", 2)])
def test_visible_file_row_is_not_upload_confirmation(browser, field, count):
    setattr(browser.page, field, count)
    with pytest.raises((QuarkBrowserError, TimeoutError)):
        browser.adapter.upload(browser.local_path, "")
    assert browser.page.files == [browser.local_path.name]
    assert browser.stages == ["uploading", "link_pending"]


def test_duplicate_rows_appearing_during_wait_are_rejected(browser):
    browser.page.files_after_wait = [browser.local_path.name] * 2
    with pytest.raises(QuarkBrowserError):
        browser.adapter.upload(browser.local_path, "")
    assert "share_click" not in browser.page.events
    assert browser.stages == ["uploading", "link_pending"]


@pytest.mark.parametrize("operation", ["upload", "share_existing"])
def test_duplicate_rows_appearing_after_count_are_not_hidden_by_first(browser, operation):
    if operation == "share_existing":
        browser.page.files = [browser.local_path.name]
    browser.page.files_on_read = [browser.local_path.name] * 2
    with pytest.raises(RuntimeError, match="strict file rows"):
        getattr(browser.adapter, operation)(browser.local_path, "")
    assert "share_click" not in browser.page.events
    assert browser.stages[-1] == "link_pending"


def test_interrupted_submission_also_reports_link_pending(browser, monkeypatch):
    original = FakeLocator.set_input_files

    def interrupted(locator, filename):
        original(locator, filename)
        raise KeyboardInterrupt()

    monkeypatch.setattr(FakeLocator, "set_input_files", interrupted)
    with pytest.raises(KeyboardInterrupt):
        browser.adapter.upload(browser.local_path, "")
    assert len(browser.page.uploads) == 1
    assert browser.stages == ["uploading", "link_pending"]


def test_recovering_after_ambiguous_upload_does_not_resubmit(browser):
    browser.page.failure = "set_input_files"
    with pytest.raises(RuntimeError):
        browser.adapter.upload(browser.local_path, "")
    browser.page.failure = None
    browser.page.forbid_upload = True
    assert browser.adapter.share_existing(browser.local_path, "") == SHARE_URL
    assert len(browser.page.uploads) == 1
    assert browser.stages == ["uploading", "link_pending", "link_pending", "completed"]


@pytest.mark.parametrize("callback", [None, False, "not callable"])
def test_optional_or_non_callable_stage_handler_does_not_change_upload(browser, callback):
    browser.adapter = QuarkBrowserStorageAdapter(**browser.options)
    assert browser.adapter.on_stage is None
    browser.adapter.on_stage = callback
    assert browser.adapter.upload(browser.local_path, "") == SHARE_URL


@pytest.mark.parametrize("failure", [None, "set_input_files"])
def test_callback_exceptions_do_not_change_business_result(browser, failure):
    def broken_callback(stage):
        browser.record_stage(stage)
        raise ValueError("observer failed")

    browser.adapter.on_stage = broken_callback
    browser.page.failure = failure
    if failure:
        with pytest.raises(RuntimeError, match=failure):
            browser.adapter.upload(browser.local_path, "")
        assert browser.stages == ["uploading", "link_pending"]
    else:
        assert browser.adapter.upload(browser.local_path, "") == SHARE_URL
        assert browser.stages == ["uploading", "link_pending", "completed"]


@pytest.mark.parametrize("operation", ["upload", "share_existing"])
@pytest.mark.parametrize("value", [
    "http://pan.quark.cn/s/Ab12_-",
    "https://pan.quark.cn.evil.invalid/s/Ab12_-",
    "https://pan.quark.cn@evil.invalid/s/Ab12_-",
    "https://user:password@pan.quark.cn/s/Ab12_-",
    "https://pan.quark.cn:8443/s/Ab12_-",
    "https://drive.quark.cn/s/Ab12_-",
    "https://evil.invalid/?next=https://pan.quark.cn/s/Ab12_-",
    "javascript:https://pan.quark.cn/s/Ab12_-",
    "https://pan.quark.cn/s/Ab12_-/extra",
    "https://pan.quark.cn/s/Ab12_-@evil.invalid",
    "https://pan.quark.cn/s/Ab12_-%2fextra",
    "https://pan.quark.cn/s/Ab12_-.invalid",
    "https://pan.quark.cn/s/Ab12_-;extra",
    "https://pan.quark.cn/s/",
    "https://pan.quark.cn/not-a-share/Ab12_-",
    "https://[invalid/s/Ab12_-",
    SHARE_URL + " https://pan.quark.cn/s/another",
    "upload visible but no confirmed share link",
    "",
])
def test_invalid_share_url_never_completes(browser, operation, value):
    browser.page.link_text = value
    if operation == "share_existing":
        browser.page.files = [browser.local_path.name]
        browser.page.forbid_upload = True
    with pytest.raises(QuarkBrowserError):
        getattr(browser.adapter, operation)(browser.local_path, "")
    assert browser.stages == (["uploading", "link_pending"] if operation == "upload" else ["link_pending"])


@pytest.mark.parametrize("value", [SHARE_URL, f"Link: {SHARE_URL}\nCode: 1234",
                                  f"<{SHARE_URL}>", f"({SHARE_URL}).",
                                  SHARE_URL + "?pwd=1234#details"])
def test_valid_share_url_is_canonicalized(browser, value):
    browser.page.link_text = value
    assert browser.adapter.upload(browser.local_path, "") == SHARE_URL
    assert browser.stages[-1] == "completed"


@pytest.mark.parametrize("folder_url", [
    "http://pan.quark.cn/list",
    "https://pan.quark.cn.evil.invalid/list",
    "https://pan.quark.cn@evil.invalid/list",
    "https://user@pan.quark.cn/list",
    "https://pan.quark.cn:8443/list",
    "https://pan.quark.cn/list\n",
    "https://pan.quark.cn/list\\other",
])
def test_unsafe_configured_folder_url_is_rejected(browser, folder_url):
    options = dict(browser.options, folder_url=folder_url)
    with pytest.raises(ValueError):
        QuarkBrowserStorageAdapter(**options)


def test_browser_without_initial_page_uses_new_page(browser):
    browser.context.pages = []
    assert browser.adapter.upload(browser.local_path, "") == SHARE_URL
    assert "new_page" in browser.page.events
