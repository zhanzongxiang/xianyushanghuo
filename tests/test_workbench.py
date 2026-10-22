from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path

import pytest

from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.models import SourceMessage
from xianyu_pipeline.workbench import TransferStore, is_library_item, task_key


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aPioAAAAASUVORK5CYII=")


@pytest.fixture
def config(tmp_path):
    return PipelineConfig(inbox_dir=tmp_path / "inbox", work_dir=tmp_path / "work", output_dir=tmp_path / "output")


def event(**extra):
    return {"chat": "free_3d_models_download", "message_id": 123, "filename": "asset.zip", **extra}


def test_progress_survives_refresh_and_recovers_after_restart(config):
    store = TransferStore(config)
    identity = store.event(event(download_status="downloading", size=100, received=40, speed=10, eta=6))
    assert store.snapshot()["tasks"][0]["received"] == 40
    restored = TransferStore(config)
    task = restored.get(identity)
    assert task["download_status"] == "interrupted" and task["speed"] == 0
    assert task["received"] == 40 and task["error"]
    assert task_key("https://t.me/free_3d_models_download/", 123, "asset.zip") == identity


@pytest.mark.parametrize("download,upload,expected", [
    ("completed", "waiting", True), ("failed", "completed", True), ("failed", "deleted", True),
    ("failed", "failed", False), ("queued", "waiting", False), ("failed", "local", False),
])
def test_library_collects_download_or_upload_success(download, upload, expected):
    task = {"download_status": download, "upload_status": upload}
    assert is_library_item(task) is expected
    assert not is_library_item({**task, "record_removed_at": "removed"})


def test_record_removal_preserves_files_but_stays_out_of_lists_and_imports_after_restart(config):
    store = TransferStore(config)
    source, image = config.inbox_dir / "asset.zip", config.inbox_dir / "preview.png"
    source.write_bytes(b"archive fixture")
    image.write_bytes(PNG)
    identity = store.event(event(download_status="completed", local_path=str(source), image_paths=[str(image)]))
    store.update(identity, upload_status="completed", upload_provider="quark_api", upload_remote_fid="bound-file",
                 share_url="https://pan.quark.cn/s/Fixture")
    store.remove_record(identity)
    assert store.snapshot()["tasks"] == []
    assert not store.has_completed_message("free_3d_models_download", 123)
    with pytest.raises(ValueError):
        store.get(identity)
    assert source.read_bytes() == b"archive fixture" and image.read_bytes() == PNG
    stored = store.tasks[identity]
    assert stored["record_removed_at"] and stored["upload_remote_fid"] == "bound-file"
    assert stored["share_url"] == "https://pan.quark.cn/s/Fixture"
    restored = TransferStore(config)
    assert restored.snapshot()["tasks"] == [] and list(restored.tasks) == [identity]


def test_record_removal_can_only_be_revoked_by_a_new_download(config):
    store = TransferStore(config)
    identity = store.event(event(download_status="completed"))
    store.remove_record(identity)
    removed_at = store.tasks[identity]["record_removed_at"]
    store.event(event(download_status="completed", record_removed_at=""))
    assert store.tasks[identity]["record_removed_at"] == removed_at
    assert store.snapshot()["tasks"] == []
    store.event(event(download_status="queued"))
    assert store.get(identity)["record_removed_at"] == ""
    assert store.snapshot()["tasks"][0]["download_status"] == "queued"
    store.event(event(download_status="completed"))
    assert store.has_completed_message("free_3d_models_download", 123)


@pytest.mark.parametrize("changes", [
    {"download_status": "failed", "upload_status": "waiting"},
    {"download_status": "downloading", "upload_status": "completed"},
    {"upload_status": "uploading"}, {"upload_status": "link_pending"},
    {"cloud_delete_status": "pending"}, {"cloud_delete_status": "deleting"}, {"remote_device": True},
])
def test_record_removal_refuses_incomplete_or_unconfirmed_operations(config, changes):
    store = TransferStore(config)
    identity = store.event(event(download_status="completed"))
    store.update(identity, **changes)
    with pytest.raises(ValueError):
        store.remove_record(identity)
    assert not store.get(identity)["record_removed_at"]


def test_completed_download_not_lost_when_later_task_fails(config):
    store = TransferStore(config)
    done = store.event(event(download_status="completed", size=100, received=100))
    pending = store.event(event(message_id=124, download_status="queued"))
    store.finish("network failed")
    assert store.get(done)["download_status"] == "completed"
    assert store.get(pending)["download_status"] == "interrupted"
    assert store.get(done)["validation"] == "passed"


def test_interrupted_upload_requires_link_check_not_duplicate_upload(config):
    store = TransferStore(config)
    identity = store.event(event(download_status="completed"))
    store.update(identity, upload_status="uploading", upload_provider="quark_browser")
    restored = TransferStore(config)
    assert restored.get(identity)["upload_status"] == "link_pending"


def test_preview_extraction_is_bounded_and_cannot_traverse(config, tmp_path):
    store = TransferStore(config)
    archive = config.inbox_dir / "asset.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../../outside.png", PNG)
        output.writestr("page.svg", "<svg onload='alert(1)'></svg>")
        output.writestr("fake.png", b"<html>not an image</html>")
        output.writestr("big.png", b"x" * (9 * 1024 * 1024))
    identity = store.event(event(download_status="completed", local_path=str(archive)))
    store.enrich(identity)
    snapshot = store.snapshot()["tasks"][0]
    assert len(snapshot["images"]) == 1
    assert snapshot["image_source"] == "zip"
    assert "image_paths" not in snapshot
    image, mime = store.preview(identity, 0)
    assert mime == "image/png" and image.is_relative_to(config.inbox_dir / ".previews")
    assert not (tmp_path / "outside.png").exists()
    with pytest.raises(ValueError):
        store.preview(identity, -1)
    with pytest.raises(ValueError):
        store.preview(identity, 1)


def test_album_provenance_and_all_five_images_survive_restart(config):
    store = TransferStore(config)
    folder = config.inbox_dir / ".previews"
    folder.mkdir()
    paths = []
    for index in range(5):
        path = folder / f"photo-{index}.png"
        path.write_bytes(PNG)
        paths.append(str(path))
    identity = store.event(event(download_status="completed", image_paths=paths,
                                 image_source="preceding_album", image_message_ids=[10, 12, 14, 16, 18],
                                 image_group_id="9007199254740993123"))
    restored = TransferStore(config)
    task = restored.snapshot()["tasks"][0]
    assert task["image_source"] == "preceding_album"
    assert task["image_message_ids"] == [10, 12, 14, 16, 18]
    assert task["image_group_id"] == "9007199254740993123"
    assert len(task["images"]) == 5
    for index in range(5):
        assert restored.preview(identity, index)[1] == "image/png"


def test_legacy_tasks_accept_new_preview_metadata(config):
    store = TransferStore(config)
    identity = store.event(event(download_status="completed"))
    for key in ("image_source", "image_message_ids", "image_group_id"):
        del store.tasks[identity][key]
    store.flush()
    restored = TransferStore(config)
    restored.event(event(download_status="queued", image_source="preceding_photo", image_message_ids=[99]))
    assert restored.get(identity)["image_source"] == "preceding_photo"
    assert restored.get(identity)["image_message_ids"] == [99]


def test_preview_and_open_file_must_stay_inside_inbox(config, tmp_path):
    store = TransferStore(config)
    outside = tmp_path / "private.png"
    outside.write_bytes(PNG)
    identity = store.event(event(download_status="completed", image_paths=[str(outside)], local_path=str(outside)))
    with pytest.raises(ValueError):
        store.preview(identity, 0)
    with pytest.raises(ValueError):
        store.local_file(identity)


def test_changed_download_directory_keeps_old_files_and_previews_across_restart(config, tmp_path):
    store = TransferStore(config)
    old_file = config.inbox_dir / "old.zip"
    with zipfile.ZipFile(old_file, "w") as output:
        output.writestr("asset.txt", "old")
    preview = config.inbox_dir / ".previews" / "old.png"
    preview.parent.mkdir()
    preview.write_bytes(PNG)
    identity = store.event(event(download_status="completed", local_path=str(old_file),
                                 image_paths=[str(preview)]))
    new_dir = tmp_path / "new-inbox"
    new_dir.mkdir()
    new_file = new_dir / "new.zip"
    with zipfile.ZipFile(new_file, "w") as output:
        output.writestr("asset.txt", "new")
    new_config = PipelineConfig(inbox_dir=new_dir, work_dir=config.work_dir, output_dir=config.output_dir)
    store.use_config(new_config)
    assert old_file.is_file() and preview.is_file()
    assert store.local_file(identity, completed=True) == old_file
    assert store.preview(identity, 0) == (preview, "image/png")
    assert store.snapshot()["download_dir"] == str(new_dir.resolve())
    assert "storage_root" not in store.snapshot()["tasks"][0]
    imported = next(task for task in store.snapshot()["tasks"] if task["filename"] == "new.zip")
    assert store.local_file(imported["id"], completed=True) == new_file
    restored = TransferStore(new_config)
    assert restored.local_file(identity, completed=True) == old_file
    assert restored.preview(identity, 0) == (preview, "image/png")
    assert restored.local_file(imported["id"], completed=True) == new_file
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    restored.update(identity, image_paths=[str(outside)])
    with pytest.raises(ValueError):
        restored.preview(identity, 0)


def test_same_named_local_import_never_overwrites_old_task(config, tmp_path):
    store = TransferStore(config)
    old_file = config.inbox_dir / "same.zip"
    with zipfile.ZipFile(old_file, "w") as output:
        output.writestr("old.txt", "old")
    store.import_existing()
    identity = task_key("local-inbox", 0, "same.zip")
    new_dir = tmp_path / "new-inbox"
    new_dir.mkdir()
    with zipfile.ZipFile(new_dir / "same.zip", "w") as output:
        output.writestr("new.txt", "new")
    store.use_config(PipelineConfig(inbox_dir=new_dir, work_dir=config.work_dir, output_dir=config.output_dir))
    assert store.local_file(identity, completed=True) == old_file
    assert len(store.tasks) == 1


def test_import_existing_does_not_claim_verified_or_overwrite_records(config):
    config.ensure_directories()
    source = config.inbox_dir / "old.rar"
    source.write_bytes(b"Rar!test")
    store = TransferStore(config)
    task = store.snapshot()["tasks"][0]
    assert task["validation"] == "pending"
    assert task["download_status"] == "completed"
    store.update(task["id"], upload_status="local")
    restored = TransferStore(config)
    assert len(restored.tasks) == 1
    assert restored.get(task["id"])["upload_status"] == "local"


def test_accept_message_keeps_original_filename_id_on_renamed_download(config):
    store = TransferStore(config)
    source = config.inbox_dir / "asset-2.zip"
    with zipfile.ZipFile(source, "w") as output:
        output.writestr("model.txt", "fixture")
    identity = store.event(event(download_status="completed", local_path=str(source)))
    store.accept_message(SourceMessage(message_id=123, chat="free_3d_models_download", attachment_paths=[source]))
    assert len(store.tasks) == 1 and store.get(identity)["filename"] == "asset.zip"


def test_unverified_file_cannot_be_uploaded(config):
    store = TransferStore(config)
    source = config.inbox_dir / ".unverified" / "broken.rar"
    source.parent.mkdir()
    source.write_bytes(b"partial")
    identity = store.event(event(download_status="completed", local_path=str(source)))
    with pytest.raises(ValueError, match="不可上传"):
        store.local_file(identity, completed=True)


def test_bad_task_file_is_preserved(config):
    config.ensure_directories()
    record = config.output_dir / "workbench-tasks.json"
    record.write_text("invalid JSON", encoding="utf-8")
    store = TransferStore(config)
    backups = list(config.output_dir.glob("workbench-tasks-unreadable-*.json"))
    assert len(backups) == 1 and backups[0].read_text() == "invalid JSON"
    assert not store.tasks and json.loads(record.read_text()) == []


@pytest.mark.parametrize("status", ["link_pending", "completed"])
def test_repeated_download_preserves_upload_binding_and_restart_identity(config, status):
    store = TransferStore(config)
    original = config.inbox_dir / "asset.zip"
    replacement = config.inbox_dir / "asset-2.zip"
    for path in (original, replacement):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("asset.txt", "fixture")
    identity = store.event(event(download_status="completed", local_path=str(original)))
    binding = dict(upload_status=status, upload_provider="quark_browser", upload_filename=original.name,
                   upload_local_path=str(original), upload_target="https://pan.quark.cn/list?folder=A",
                   share_url="https://pan.quark.cn/s/Original" if status == "completed" else "")
    store.update(identity, **binding)
    for download_status in ("queued", "downloading", "completed"):
        assert store.event(event(download_status=download_status, local_path=str(replacement),
                                 upload_status="waiting", upload_provider="local", upload_filename=replacement.name,
                                 upload_local_path=str(replacement), upload_target="wrong", share_url="wrong")) == identity
        assert {key: store.get(identity)[key] for key in binding} == binding
    store.accept_message(SourceMessage(message_id=0, chat="local-inbox", attachment_paths=[original, replacement]))
    assert len(store.tasks) == 1
    restored = TransferStore(config)
    assert list(restored.tasks) == [identity]
    assert restored.get(identity)["local_path"] == str(replacement)
    assert {key: restored.get(identity)[key] for key in binding} == binding


@pytest.mark.parametrize("status", ["uploading", "link_pending", "completed", "local"])
def test_legacy_upload_binding_is_migrated_without_requiring_local_file(config, status):
    store = TransferStore(config)
    original = config.inbox_dir / "actual-2.zip"
    identity = store.event(event(download_status="completed", local_path=str(original)))
    store.update(identity, upload_status=status, upload_provider="quark_browser", upload_target="original-target")
    record = store.get(identity)
    for key in ("upload_filename", "upload_local_path"):
        record.pop(key)
    store.path.write_text(json.dumps([record]), encoding="utf-8")
    restored = TransferStore(config)
    task = restored.get(identity)
    assert task["upload_filename"] == original.name
    assert task["upload_local_path"] == str(original)
    assert task["upload_target"] == "original-target"
    assert task["upload_status"] == ("link_pending" if status == "uploading" else status)
    assert restored.upload_name(identity) == original.name


@pytest.mark.parametrize("name", ["", "../asset.zip", "..\\asset.rar", "C:asset.zip", "/asset.zip", "asset.svg", None])
def test_upload_recovery_name_is_a_bounded_archive_filename(config, name):
    store = TransferStore(config)
    identity = store.event(event(download_status="completed"))
    store.update(identity, upload_filename=name)
    with pytest.raises(ValueError):
        store.upload_name(identity)
