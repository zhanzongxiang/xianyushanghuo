import json
import zipfile
from pathlib import Path

import pytest

from xianyu_pipeline.adapters import LocalCloudStorageAdapter, LocalTelegramAdapter, TelegramBrowserAdapter, launch_telegram_context, download_resource_status
from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.pipeline import run_pipeline


def test_default_profile_survives_portable_version_change(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "user_app_data"))
    profiles = []
    for version in ("v13", "v14"):
        folder = tmp_path / version
        folder.mkdir()
        config_path = folder / "test.local.json"
        config_path.write_text(json.dumps({"telegram_browser_profile": "sample_data/telegram_browser_profile"}), encoding="utf-8")
        profiles.append(PipelineConfig.from_json(config_path).telegram_browser_profile)
    assert profiles[0] == profiles[1] == tmp_path / "user_app_data" / "XianYuMaterialPipeline" / "telegram_browser_profile"


def test_custom_profile_path_is_respected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "user_app_data"))
    config_path = tmp_path / "test.local.json"
    config_path.write_text(json.dumps({"telegram_browser_profile": "custom/session"}), encoding="utf-8")
    assert PipelineConfig.from_json(config_path).telegram_browser_profile == tmp_path / "custom/session"


def test_browser_channel_uses_web_chat_not_public_preview() -> None:
    assert TelegramBrowserAdapter.channel_web_url("https://t.me/example_channel") == "https://web.telegram.org/k/#@example_channel"
    assert TelegramBrowserAdapter.channel_web_url("https://t.me/example_channel/123") == "https://web.telegram.org/k/#@example_channel"
    assert PipelineConfig().source_channel_url == "https://t.me/free_3d_models_download"


def test_missing_channel_messages_distinguishes_network_wait() -> None:
    reason = TelegramBrowserAdapter._missing_messages_reason("Waiting for network...")
    assert "网络" in reason and "代理" in reason
    assert "登录态" in TelegramBrowserAdapter._missing_messages_reason("Log in with QR")


def test_channel_page_and_message_wait_use_configured_timeout(tmp_path: Path) -> None:
    class Nodes:
        @property
        def first(self):
            return self
        def wait_for(self, **kwargs):
            assert kwargs == {"timeout": 180000}
        def count(self):
            return 1
    class Page:
        url = ""
        nodes = Nodes()
        navigations = []
        def set_default_timeout(self, timeout):
            assert timeout == 180000
        def goto(self, url, **kwargs):
            self.navigations.append((url, kwargs))
            self.url = url
        def title(self):
            return "Telegram Web"
        def locator(self, selector):
            assert selector == adapter.message_selector
            return self.nodes
    adapter = TelegramBrowserAdapter(tmp_path / "profile", tmp_path / "inbox", timeout_seconds=180)
    page = Page()
    assert adapter._open_channel_messages(page, "https://t.me/free_3d_models_download") is page.nodes
    assert page.navigations == [
        ("https://web.telegram.org/k/", {"wait_until": "domcontentloaded", "timeout": 180000}),
        ("https://web.telegram.org/k/#@free_3d_models_download",
         {"wait_until": "domcontentloaded", "timeout": 180000}),
    ]


def test_only_named_zip_rar_cards_are_parsed(tmp_path: Path) -> None:
    adapter = TelegramBrowserAdapter(tmp_path / "profile", tmp_path / "inbox")
    cards = adapter._parse_archive_cards([
        {"name": "商品.rar", "source": "document"},
        {"name": "asset.zip", "source": "link"},
        {"name": "picture.png", "source": "document"},
        {"name": "../wrong.rar", "source": "name"},
        {"name": "archive.rar", "source": "unknown"},
    ])
    assert [(card.name, card.source) for card in cards] == [
        ("商品.rar", "document"), ("asset.zip", "link")]


def test_unique_messages_count_by_id_not_dom_nodes() -> None:
    class Nodes:
        def evaluate_all(self, script, selector=""):
            return [{"message_id": None, "mid": str(mid), "text": f"{mid}.zip",
                     "cards": [{"name": f"{mid}.zip", "source": "document"}]}
                    for mid in (1, 1, 2, 3, 3)]
        def nth(self, index):
            raise AssertionError("动态列表不能逐个按下标读取")
    adapter = TelegramBrowserAdapter(Path("profile"), Path("inbox"))
    assert [item.message_id for item in adapter._unique_message_nodes(Nodes(), 2)] == [2, 3]


def test_unique_messages_keep_outer_node_for_duplicate_id() -> None:
    class Nodes:
        def evaluate_all(self, script, selector=""):
            return [{"message_id": None, "mid": f"channel-{mid}", "text": name, "cards": []}
                    for mid, name in ((10, "outer"), (10, "inner"), (11, "outer"))]
    result = TelegramBrowserAdapter(Path("profile"), Path("inbox"))._unique_message_nodes(Nodes(), 2)
    assert [(item.message_id, item.text) for item in result] == [(10, "outer"), (11, "outer")]


def test_archive_message_filter_and_scan_newest_first(tmp_path: Path) -> None:
    class Locator:
        @property
        def first(self):
            return self
        def and_(self, other):
            return self
        def evaluate(self, script):
            return None
    class Nodes:
        def evaluate_all(self, script, selector=""):
            return [{"message_id": None, "mid": str(mid), "text": text,
                     "cards": [{"name": text, "source": "document"}] if card else []}
                    for mid, text, card in ((1, "old.rar", True), (2, "skip.png", False),
                                            (3, "new.zip", True), (4, "latest.rar", True))]
    class Mouse:
        def wheel(self, x, y):
            return None
    class Page:
        mouse = Mouse()
        def locator(self, selector):
            return Locator()
        def wait_for_timeout(self, ms):
            return None
    adapter = TelegramBrowserAdapter(tmp_path / "profile", tmp_path / "inbox")
    assert adapter._scan_archive_ids(Page(), Nodes(), 2) == [3, 4]


def test_scan_survives_virtual_list_replacement(tmp_path: Path) -> None:
    class Nodes:
        round = 0
        def evaluate_all(self, script, selector=""):
            if self.round == 0:
                return [{"message_id": str(mid), "mid": None, "text": text,
                         "cards": [{"name": text, "source": "document"}] if card else []}
                        for mid, text, card in ((3, "older.png", False), (4, "latest.zip", True))]
            return [{"message_id": str(mid), "mid": None, "text": text,
                     "cards": [{"name": text, "source": "document"}] if card else []}
                    for mid, text, card in ((1, "old.rar", True), (2, "skip.png", False),
                                            (3, "older.png", False))]
        def nth(self, index):
            raise AssertionError("扫描不应逐个读取已变动的 nth(index)")
    nodes = Nodes()
    class Locator:
        @property
        def first(self):
            return self
        def and_(self, other):
            return self
        def evaluate(self, script, timeout=None):
            return None
    class Mouse:
        def wheel(self, x, y):
            nodes.round += 1
    class Page:
        mouse = Mouse()
        def locator(self, selector):
            return Locator()
        def wait_for_timeout(self, ms):
            return None
    adapter = TelegramBrowserAdapter(tmp_path / "profile", tmp_path / "inbox")
    assert adapter._scan_archive_ids(Page(), nodes, 2) == [1, 4]


def test_locate_message_can_scroll_back_more_than_twelve_steps(tmp_path: Path) -> None:
    class Nodes:
        position = 0
        def evaluate_all(self, script, selector=""):
            return [{"message_id": str(self.position), "mid": None, "text": "", "cards": []}]
    nodes = Nodes()
    class Locator:
        @property
        def first(self):
            return self
        def and_(self, other):
            return self
    class Page:
        class Mouse:
            def wheel(self, x, y):
                nodes.position += 1
        mouse = Mouse()
        def wait_for_timeout(self, ms):
            return None
        def locator(self, selector):
            return Locator()
    adapter = TelegramBrowserAdapter(tmp_path / "profile", tmp_path / "inbox", scan_limit=30)
    assert adapter._locate_message(Page(), nodes, 20) is not None
    assert nodes.position == 20


def test_uuid_download_uses_card_name_only_after_zip_validation(tmp_path: Path) -> None:
    class Download:
        suggested_filename = "41fa022d-3027-42ba-96bd-85ab3709b59a"
        def __init__(self, content):
            self.content = content
        def path(self):
            source = tmp_path / "browser-download.tmp"
            source.write_bytes(self.content)
            return source
        def save_as(self, filename):
            raise AssertionError("同机大文件不应调用 save_as")
        def failure(self):
            return None
    class Card:
        def inner_text(self, timeout=5000):
            return "Varok Saurfang.zip\n580.4 MB · 22:34"
    names = TelegramBrowserAdapter._visible_archive_names(Card())
    assert names == ["Varok Saurfang.zip"]
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("asset.txt", "licensed")
    result = TelegramBrowserAdapter._verified_download(Download(archive.read_bytes()), tmp_path, names[0])
    assert result.name == "Varok Saurfang.zip"
    with pytest.raises((zipfile.BadZipFile, RuntimeError)):
        TelegramBrowserAdapter._verified_download(Download(b"not zip"), tmp_path, "broken.zip")
    assert not (tmp_path / "broken.zip").exists()
    with pytest.raises(ValueError, match="无法确认"):
        TelegramBrowserAdapter._verified_download(Download(archive.read_bytes()), tmp_path)
    Download.suggested_filename = "different.pdf"
    with pytest.raises(ValueError, match="扩展名不是 ZIP/RAR"):
        TelegramBrowserAdapter._verified_download(Download(archive.read_bytes()), tmp_path, "safe.zip")


def test_launch_browser_enables_chromium_diagnostics(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("xianyu_pipeline.browser_profiles.executable_version", lambda _: (142, 0, 0, 0))
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"MZ")
    class Context:
        browser = None
        def on(self, event, callback):
            if event == "close":
                callback(self)
    class Chromium:
        def launch_persistent_context(self, directory, **options):
            assert directory != str(tmp_path)
            assert Path(directory).name == "profile-142.0.0.0"
            assert "--enable-logging" in options["args"]
            assert options["args"][1].startswith(f"--log-file={Path(directory) / 'logs'}")
            assert options["executable_path"] == str(executable)
            assert options["accept_downloads"]
            return Context()
    class Playwright:
        chromium = Chromium()
    assert isinstance(launch_telegram_context(Playwright(), tmp_path, False, str(executable)), Context)


def test_disk_resource_probe_reports_download_directory(tmp_path: Path) -> None:
    assert download_resource_status(tmp_path)["disk_free"] > 0


def test_browser_download_path_failure_logs_state_before_partial_cleanup(tmp_path: Path, caplog) -> None:
    class Download:
        suggested_filename = "complete.rar"
        def path(self):
            raise RuntimeError("Target page, context or browser has been closed")
        def save_as(self, filename):
            raise AssertionError("不应调用 save_as")
        def failure(self):
            raise AssertionError("closed page must not query download failure")
    class Page:
        url = "https://web.telegram.org/k/#@example"
        def is_closed(self):
            return True
    class Context:
        @property
        def pages(self):
            raise RuntimeError("connection closed")
    with pytest.raises(RuntimeError, match="Target page"):
        TelegramBrowserAdapter._verified_download(Download(), tmp_path, "com…plete.rar",
                                                  page=Page(), context=Context())
    assert "临时文件大小=不存在" in caplog.text
    assert "下载失败资源快照" in caplog.text
    assert "页面已关闭=True" in caplog.text
    assert "上下文页面数=不可读取" in caplog.text
    assert not (tmp_path / ".complete.rar.part").exists()


def test_full_browser_filename_wins_over_truncated_card(tmp_path: Path, monkeypatch) -> None:
    # Filename selection is independent from the separately tested real RAR validator.
    monkeypatch.setattr("xianyu_pipeline.adapters.validate_rar", lambda *args: None)
    class Download:
        suggested_filename = "KitNo – OC, Naughty Leashed Girl.rar"
        def path(self):
            source = tmp_path / "browser-rar.tmp"
            source.write_bytes(b"rar fixture")
            return source
        def save_as(self, filename):
            raise AssertionError("不应调用 save_as")
        def failure(self):
            return None
    result = TelegramBrowserAdapter._verified_download(Download(), tmp_path,
                                                        "KitNo – OC, Naug…eashed Girl.rar")
    assert result.name == Download.suggested_filename


def test_verified_zip_does_not_overwrite_and_rejects_broken(tmp_path: Path) -> None:
    class Download:
        suggested_filename = "product.zip"
        def __init__(self, content):
            self.content = content
        def path(self):
            source = tmp_path / "browser-download.tmp"
            source.write_bytes(self.content)
            return source
        def save_as(self, filename):
            raise AssertionError("同机大文件不应调用 save_as")
        def failure(self):
            return None
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr("asset.txt", "licensed")
    good = archive.read_bytes()
    first = TelegramBrowserAdapter._verified_download(Download(good), tmp_path)
    second = TelegramBrowserAdapter._verified_download(Download(good), tmp_path)
    assert first.name == "product.zip" and second.name == "product-2.zip"
    assert first.read_bytes() == good
    with pytest.raises((zipfile.BadZipFile, RuntimeError)):
        TelegramBrowserAdapter._verified_download(Download(b"not a zip"), tmp_path)
    assert sorted(p.name for p in tmp_path.glob("product*.zip")) == ["product-2.zip", "product.zip"]
    assert not (tmp_path / ".product-3.zip.part").exists()


def test_zip_is_extracted_and_images_are_matched(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    archive_root = tmp_path / "source"
    archive_root.mkdir()
    (archive_root / "readme.pdf").write_bytes(b"pdf")
    (archive_root / "cover.jpg").write_bytes(b"jpg")
    import zipfile
    with zipfile.ZipFile(inbox / "product.zip", "w") as archive:
        archive.write(archive_root / "readme.pdf", "readme.pdf")
        archive.write(archive_root / "cover.jpg", "cover.jpg")

    config = PipelineConfig(
        source_chat="fixture",
        message_ids=[12],
        inbox_dir=inbox,
        work_dir=tmp_path / "work",
        output_dir=tmp_path / "output",
        cloud_folder="fixed",
    )
    candidates = run_pipeline(config, LocalTelegramAdapter(inbox), LocalCloudStorageAdapter(tmp_path / "cloud"))

    assert len(candidates) == 1
    assert candidates[0].status == "pending_review"
    assert [path.name for path in candidates[0].images] == ["cover.jpg"]
    assert [path.name for path in candidates[0].files] == ["readme.pdf"]
    assert candidates[0].share_url.startswith("file://")
    assert "source_text" in json.loads((tmp_path / "output" / "manifest.json").read_text(encoding="utf-8"))[0]


def test_executable_is_rejected_by_default(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "danger.exe").write_bytes(b"not executed")
    config = PipelineConfig(inbox_dir=inbox, work_dir=tmp_path / "work", output_dir=tmp_path / "output")
    candidates = run_pipeline(config, LocalTelegramAdapter(inbox), LocalCloudStorageAdapter(tmp_path / "cloud"))

    assert candidates[0].status == "needs_review"
    assert any("可执行文件" in warning for warning in candidates[0].warnings)
