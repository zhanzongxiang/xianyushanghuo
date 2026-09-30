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


def test_browser_attachment_regex_matches_file_extension(tmp_path: Path) -> None:
    class FakeLocator:
        def __init__(self, selector):
            self.pattern = None
            self.selector = selector
        def count(self):
            return 0
        def filter(self, has_text):
            self.pattern = has_text
            return self
    class FakeMessage:
        def __init__(self):
            self.locators = []
        def locator(self, selector):
            result = FakeLocator(selector)
            self.locators.append(result)
            return result
    message = FakeMessage()
    TelegramBrowserAdapter(tmp_path / "profile", tmp_path / "inbox")._attachment_locators(message)
    button_result = message.locators[3]
    assert button_result.pattern.search("商品.rar")
    assert button_result.pattern.search("下载")
    assert "[class*='media']" not in message.locators[0].selector


def test_browser_prefers_document_card_over_media(tmp_path: Path) -> None:
    class FakeLocator:
        def __init__(self, selector):
            self.selector = selector
        def count(self):
            return 1
    class FakeMessage:
        def locator(self, selector):
            return FakeLocator(selector)
    locator = TelegramBrowserAdapter(tmp_path / "profile", tmp_path / "inbox")._attachment_locators(FakeMessage())
    assert "document-container" in locator.selector
    assert "[class*='media']" not in locator.selector


def test_unique_messages_count_by_id_not_dom_nodes() -> None:
    class Node:
        def __init__(self, mid):
            self.mid = mid
        def get_attribute(self, name, timeout=5000):
            return str(self.mid) if name == "data-mid" else None
    class Nodes:
        def __init__(self):
            self.items = [Node(1), Node(1), Node(2), Node(3), Node(3)]
        def count(self):
            return len(self.items)
        def nth(self, index):
            return self.items[index]
    assert [mid for mid, _ in TelegramBrowserAdapter._unique_message_nodes(Nodes(), 2)] == [2, 3]


def test_unique_messages_keep_outer_node_for_duplicate_id() -> None:
    class Node:
        def __init__(self, mid, name):
            self.mid = mid
            self.name = name
        def get_attribute(self, name, timeout=5000):
            return str(self.mid) if name == "data-mid" else None
    class Nodes:
        items = [Node(10, "outer"), Node(10, "inner"), Node(11, "outer")]
        def count(self):
            return len(self.items)
        def nth(self, index):
            return self.items[index]
    result = TelegramBrowserAdapter._unique_message_nodes(Nodes(), 2)
    assert [(mid, node.name) for mid, node in result] == [(10, "outer"), (11, "outer")]


def test_archive_message_filter_and_scan_newest_first(tmp_path: Path) -> None:
    class Node:
        def __init__(self, mid, text):
            self.mid, self.text = mid, text
        def get_attribute(self, name, timeout=5000):
            return str(self.mid) if name == "data-mid" else None
        def inner_text(self, timeout=5000):
            return self.text
        def evaluate(self, script):
            return None
    class Nodes:
        items = [Node(1, "old.rar"), Node(2, "skip.png"), Node(3, "new.zip"), Node(4, "latest.rar")]
        def count(self):
            return len(self.items)
        def nth(self, index):
            return self.items[index]
    class Mouse:
        def wheel(self, x, y):
            return None
    class Page:
        mouse = Mouse()
        def wait_for_timeout(self, ms):
            return None
    adapter = TelegramBrowserAdapter(tmp_path / "profile", tmp_path / "inbox")
    assert adapter._scan_archive_ids(Page(), Nodes(), 2) == [3, 4]
    assert not adapter._archive_message(Nodes.items[1])


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
