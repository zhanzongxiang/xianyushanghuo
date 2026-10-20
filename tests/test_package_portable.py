import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from package_portable import package


def test_package_uses_clean_sample_instead_of_local_config(tmp_path: Path):
    folder = tmp_path / "Pipeline"
    (folder / "_internal").mkdir(parents=True)
    (folder / "Pipeline.exe").write_bytes(b"exe")
    (folder / "start_ui.cmd").write_text("start", encoding="utf-8")
    (folder / "README_TEST.md").write_text("readme", encoding="utf-8")
    (folder / "_internal/runtime.bin").write_bytes(b"runtime")
    (folder / "test.local.json").write_text('{"quark_cookie": "PRIVATE"}', encoding="utf-8")
    (folder / "telegram-settings.json").write_text('{"telegram_api_hash": "PRIVATE"}', encoding="utf-8")
    (folder / "sample_data/output/logs").mkdir(parents=True)
    (folder / "sample_data/output/logs/pipeline.log").write_text("PRIVATE", encoding="utf-8")
    example = tmp_path / "example.json"
    example.write_text(json.dumps({"browser_executable_path": "", "source_channel_url": "public"}),
                       encoding="utf-8")
    archive = tmp_path / "Pipeline.zip"
    package(folder, example, archive)
    with ZipFile(archive) as contents:
        assert contents.testzip() is None
        assert sorted(contents.namelist()) == sorted([
            "Pipeline/Pipeline.exe", "Pipeline/README_TEST.md", "Pipeline/start_ui.cmd",
            "Pipeline/_internal/runtime.bin", "Pipeline/test.local.json",
        ])
        assert b"PRIVATE" not in contents.read("Pipeline/test.local.json")


def test_package_rejects_sample_with_account_values(tmp_path: Path):
    folder = tmp_path / "Pipeline"
    folder.mkdir()
    example = tmp_path / "example.json"
    example.write_text('{"telegram_api_hash": "PRIVATE"}', encoding="utf-8")
    archive = tmp_path / "Pipeline.zip"
    with pytest.raises(ValueError, match="Sample"):
        package(folder, example, archive)
    assert not archive.exists()
    example.write_text('{"data_server_token": "PRIVATE"}', encoding="utf-8")
    with pytest.raises(ValueError, match="Sample"):
        package(folder, example, archive)
    assert not archive.exists()
    example.write_text('{"telegram_api_id": 12345}', encoding="utf-8")
    with pytest.raises(ValueError, match="Sample"):
        package(folder, example, archive)
    assert not archive.exists()


def test_package_rejects_shared_settings_inside_runtime(tmp_path: Path):
    folder = tmp_path / "Pipeline"
    (folder / "_internal").mkdir(parents=True)
    (folder / "Pipeline.exe").write_bytes(b"exe")
    (folder / "start_ui.cmd").write_text("start", encoding="utf-8")
    (folder / "README_TEST.md").write_text("readme", encoding="utf-8")
    (folder / "_internal/telegram-settings.json").write_text('{"telegram_api_hash": "PRIVATE"}',
                                                           encoding="utf-8")
    example = tmp_path / "example.json"
    example.write_text("{}", encoding="utf-8")
    archive = tmp_path / "Pipeline.zip"
    with pytest.raises(ValueError, match="runtime or browser state"):
        package(folder, example, archive)
    assert not archive.exists()
