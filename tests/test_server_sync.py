from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from xianyu_pipeline.channel_catalog import ChannelCatalog
from xianyu_pipeline.channel_sources import ChannelDirectory
from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.data_protocol import validate_endpoint
from xianyu_pipeline.data_server import DataDatabase, create_server
from xianyu_pipeline.server_sync import ServerSync
from xianyu_pipeline.workbench import TransferStore
from test_workbench_actions import app

TOKEN = "offline-data-service-" + "a" * 32
JPEG = b"\xff\xd8\xffoffline-fixture"


@pytest.fixture
def service(tmp_path):
    root = tmp_path / "service"
    server = create_server(("127.0.0.1", 0), root, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    result = SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", root=root)
    try:
        yield result
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def desktop(root, service):
    config = PipelineConfig(inbox_dir=root / "inbox", work_dir=root / "work", output_dir=root / "output",
                            data_server_enabled=True, data_server_url=service.url, data_server_token=TOKEN,
                            telegram_api_id=123, telegram_api_hash="private-api-key",
                            quark_cookie="private-cookie", telegram_api_proxy_url="socks5://user:password@127.0.0.1:7890")
    config.ensure_directories()
    channels, catalog, transfers = ChannelDirectory(config.output_dir), ChannelCatalog(config.output_dir), TransferStore(config)
    sync = ServerSync(config, channels, catalog, transfers)
    return SimpleNamespace(config=config, channels=channels, catalog=catalog, transfers=transfers, sync=sync)


def settled(app, attempts=10):
    for _ in range(attempts):
        status = app.sync.sync_once()
        assert status["stage"] == "connected", status
        if status["pending"] == 0 and not app.sync.wake.is_set():
            return status
        app.sync.wake.clear()
    raise AssertionError(status)


def operation(key="models", *, revision=0, name="Models", **fields):
    return {"id": uuid4().hex, "kind": "channels", "key": key, "base_revision": revision,
            "value": {"key": key, "url": f"https://t.me/{key}", "name": name, **fields}}


def exchange(database, operations, **fields):
    return database.exchange({"workspace": "default", "device_id": "a" * 32,
                              "cursor": 0, "operations": operations, **fields})


def test_database_idempotency_conflicts_workspaces_and_restart(tmp_path):
    database = DataDatabase(tmp_path / "business.sqlite3")
    op = operation()
    first = exchange(database, [op])
    assert first["results"][0]["status"] == "applied" and first["cursor"] == 1
    assert exchange(database, [op]) == first
    equal = exchange(database, [operation()])
    assert equal["results"][0]["status"] == "applied" and equal["cursor"] == 1
    conflict = exchange(database, [operation(name="Other")])
    assert conflict["results"][0]["status"] == "conflict" and conflict["cursor"] == 1
    assert conflict["results"][0]["record"]["value"]["name"] == "Models"
    applied = exchange(database, [operation(name="Other", revision=1)])
    assert applied["results"][0]["record"]["revision"] == 2
    assert exchange(database, [], workspace="separate")["changes"] == []
    assert DataDatabase(database.path).server_id == database.server_id
    assert DataDatabase(database.path).exchange({"workspace": "default", "device_id": "a" * 32,
                                                "cursor": 1, "operations": []})["changes"][0]["value"]["name"] == "Other"


def test_cloud_deletion_metadata_syncs_to_another_device_and_survives_restart(tmp_path, service):
    first, second = desktop(tmp_path / "first", service), desktop(tmp_path / "second", service)
    identity = first.transfers.event({"chat": "models", "message_id": 88, "filename": "model.rar",
                                      "download_status": "completed"})
    first.transfers.update(identity, upload_status="completed", upload_provider="quark_api",
                           upload_remote_fid="bound-file", upload_target="original-folder",
                           cloud_delete_status="pending", cloud_delete_task_id="delete-receipt",
                           cloud_delete_error="等待云端确认")
    settled(first)
    settled(second)
    task = second.transfers.snapshot()["tasks"][0]
    assert task["remote_device"] and task["cloud_delete_status"] == "pending"
    assert task["cloud_delete_task_id"] == "delete-receipt" and task["cloud_delete_error"] == "等待云端确认"
    first.transfers.update(identity, upload_status="deleted", cloud_delete_status="completed",
                           cloud_delete_error="", cloud_deleted_at="2026-10-22T04:00:00+00:00", share_url="")
    settled(first)
    settled(second)
    restarted = desktop(tmp_path / "second", service)
    settled(restarted)
    task = restarted.transfers.snapshot()["tasks"][0]
    assert task["upload_status"] == "deleted" and task["cloud_delete_status"] == "completed"
    assert task["cloud_deleted_at"] == "2026-10-22T04:00:00+00:00" and task["share_url"] == ""
    assert identity not in restarted.transfers.tasks


def test_removed_library_record_stays_hidden_across_sync_restart_and_local_import(tmp_path, service, monkeypatch):
    first, second = desktop(tmp_path / "first", service), desktop(tmp_path / "second", service)
    source = first.config.inbox_dir / "model.rar"
    source.write_bytes(b"archive fixture")
    image = first.config.inbox_dir / "cover.jpg"
    image.write_bytes(JPEG)
    identity = first.transfers.event({"chat": "models", "message_id": 90, "filename": source.name,
                                      "download_status": "completed", "local_path": str(source),
                                      "image_paths": [str(image)]})
    first.transfers.update(identity, upload_status="completed", upload_provider="quark_api",
                           upload_remote_fid="bound-file", share_url="https://pan.quark.cn/s/Fixture")
    settled(first)
    settled(second)
    assert len(second.transfers.snapshot()["tasks"]) == 1
    first.transfers.remove_record(identity)
    settled(first)
    settled(second)
    assert second.transfers.snapshot()["tasks"] == [] and not second.transfers.remote_tasks
    assert source.read_bytes() == b"archive fixture" and image.read_bytes() == JPEG
    restarted = desktop(tmp_path / "first", service)
    settled(restarted)
    assert restarted.transfers.snapshot()["tasks"] == [] and len(restarted.transfers.tasks) == 1
    assert restarted.transfers.tasks[identity]["record_removed_at"]
    third = desktop(tmp_path / "third", service)

    def unexpected_asset(*args):
        raise AssertionError("Removed records must not download images")

    monkeypatch.setattr(third.sync, "_download_image", unexpected_asset)
    settled(third)
    assert third.transfers.snapshot()["tasks"] == []
    first.transfers.event({"chat": "models", "message_id": 90, "filename": source.name, "download_status": "queued"})
    first.transfers.event({"chat": "models", "message_id": 90, "filename": source.name, "download_status": "completed"})
    settled(first)
    settled(second)
    assert len(second.transfers.snapshot()["tasks"]) == 1
    assert second.transfers.snapshot()["tasks"][0]["record_removed_at"] == ""


def test_database_batch_validation_and_operation_reuse_are_atomic(tmp_path):
    database = DataDatabase(tmp_path / "business.sqlite3")
    with pytest.raises(ValueError):
        exchange(database, [operation(), operation(key="../bad")])
    assert exchange(database, [])["changes"] == []
    op = operation()
    exchange(database, [op])
    with pytest.raises(ValueError, match="ID"):
        exchange(database, [operation("new"), {**op, "value": {**op["value"], "name": "Changed"}}])
    assert len(exchange(database, [])["changes"]) == 1
    with pytest.raises(ValueError):
        exchange(database, [operation(telegram_api_hash="must-not-store")])
    with pytest.raises(ValueError, match="其他设备"):
        exchange(database, [{"id": uuid4().hex, "kind": "tasks", "key": "b" * 32 + ":" + "c" * 24,
                              "base_revision": 0, "value": {"id": "c" * 24, "device_id": "b" * 32}}])


def test_service_requires_auth_checks_asset_hash_and_rejects_bad_records(service):
    with httpx.Client(base_url=service.url, trust_env=False) as client:
        assert client.get("/v1/health").status_code == 401
        assert client.post("/v1/sync", json={}).status_code == 401
        client.headers["Authorization"] = "Bearer " + TOKEN
        assert client.get("/v1/health").json()["protocol"] == 1
        digest = hashlib.sha256(JPEG).hexdigest()
        assert client.put("/v1/assets/" + digest, content=JPEG).status_code == 200
        assert client.get("/v1/assets/" + digest).content == JPEG
        assert client.put("/v1/assets/" + "0" * 64, content=JPEG).status_code == 400
        assert client.put("/v1/assets/../../../secret", content=JPEG).status_code == 404
        assert client.post("/v1/sync", json={"workspace": "x", "device_id": "a" * 32,
                                            "cursor": 0, "operations": [operation(quark_cookie="secret")]}).status_code == 400
        assert client.post("/v1/sync", content=b"{bad").status_code == 400


@pytest.mark.parametrize("url", [
    "http://47.113.126.29:8780", "ftp://example.com", "https://user:password@example.com",
    "https://example.com/v1", "https://example.com/?token=secret", "https://example.com:abc",
])
def test_endpoint_rejects_cleartext_remote_and_credentials(url):
    with pytest.raises(ValueError):
        validate_endpoint(url)


def test_two_desktops_sync_directory_messages_images_and_device_tasks(tmp_path, service):
    first, second = desktop(tmp_path / "first", service), desktop(tmp_path / "second", service)
    first.channels.merge_joined([{"id": 123, "key": "models", "target": "models", "title": "Models",
                                  "avatar_url": "/api/channel-avatar/123"}])
    first.channels.rename("models", "我的模型")
    avatars = first.config.output_dir / ".channel-avatars"
    avatars.mkdir()
    (avatars / "123.jpg").write_bytes(JPEG)
    cover = first.catalog.preview_root / "models" / "42.jpg"
    cover.parent.mkdir(parents=True)
    cover.write_bytes(JPEG)
    first.catalog.merge("models", {"messages": [
        {"message_id": 42, "chat": "models", "filename": "model.rar", "text": "Model",
         "size": 12, "cover_path": str(cover), "cover_quality": "preview", "cover_status": "ready"},
    ], "oldest_scanned_id": 42, "has_more": True})
    source = first.config.inbox_dir / "model.rar"
    source.write_bytes(b"local-archive-bytes")
    image = first.config.inbox_dir / "preview.jpg"
    image.write_bytes(JPEG)
    task = first.transfers.event({"chat": "models", "message_id": 42, "filename": source.name,
                                  "download_status": "completed", "local_path": str(source), "image_paths": [str(image)]})
    first.transfers.update(task, chinese_filename="中文模型.rar", share_url="https://pan.quark.cn/s/fixture")
    settled(first)
    settled(second)
    assert second.channels.get("models")["name"] == "我的模型"
    assert second.channels.get("models")["avatar_url"]
    listing = second.catalog.snapshot("models")
    assert listing["oldest_scanned_id"] == 42 and listing["messages"][0]["cover_url"]
    assert second.catalog.preview("models", 42)[0].read_bytes() == JPEG
    tasks = second.transfers.snapshot()["tasks"]
    assert len(tasks) == 1 and tasks[0]["remote_device"] and tasks[0]["local_path"] == ""
    assert tasks[0]["chinese_filename"] == "中文模型.rar" and tasks[0]["images"]
    assert not second.transfers.has_completed_message("models", 42)
    with pytest.raises(ValueError):
        second.transfers.local_file(tasks[0]["id"])
    stored = b"".join(path.read_bytes() for path in service.root.iterdir() if path.is_file())
    for forbidden in (b"private-api-key", b"private-cookie", b"user:password", b"local-archive-bytes",
                      str(first.config.inbox_dir).encode(), b"local_path", b"storage_root"):
        assert forbidden not in stored
    assert all(path.read_bytes() == JPEG for path in (service.root / "assets").iterdir())
    restarted = desktop(tmp_path / "second", service)
    settled(restarted)
    assert len(restarted.transfers.snapshot()["tasks"]) == 1
    assert restarted.channels.get("models")["name"] == "我的模型"
    settled(second)
    assert second.sync.snapshot()["conflicts"] == 0


def test_disconnect_restart_and_resume_preserve_outbox(tmp_path, service, monkeypatch):
    app = desktop(tmp_path / "desktop", service)
    app.channels.add("models", "Local")
    original = httpx.Client.get

    def offline(*args, **kwargs):
        raise httpx.ConnectError("secret URL must not be exposed")

    with monkeypatch.context() as patch:
        patch.setattr(httpx.Client, "get", offline)
        status = app.sync.sync_once()
        assert status["stage"] == "offline" and status["pending"] == 1
        assert "secret URL" not in status["error"] and TOKEN not in json.dumps(status)
        restarted = desktop(tmp_path / "desktop", service)
        assert restarted.sync.journal.device_id == app.sync.journal.device_id
        assert restarted.sync.sync_once()["pending"] == 1
    assert httpx.Client.get is original
    settled(restarted)
    receiver = desktop(tmp_path / "receiver", service)
    settled(receiver)
    assert receiver.channels.get("models")["name"] == "Local"


@pytest.mark.parametrize("choice,expected", [("server", "First edit"), ("local", "Second edit")])
def test_concurrent_edits_require_explicit_resolution(tmp_path, service, choice, expected):
    first, second = desktop(tmp_path / "first", service), desktop(tmp_path / "second", service)
    first.channels.add("models", "Original")
    settled(first)
    settled(second)
    first.channels.rename("models", "First edit")
    second.channels.rename("models", "Second edit")
    settled(first)
    status = second.sync.sync_once()
    assert status["conflicts"] == 1 and second.channels.get("models")["name"] == "Second edit"
    details = status["conflict_rows"][0]
    assert details["kind"] == "channels" and details["key"] == "models"
    assert {"field": "name", "local": "Second edit", "server": "First edit"} in details["fields"]
    second.sync.resolve(choice)
    settled(second)
    settled(first)
    assert first.channels.get("models")["name"] == second.channels.get("models")["name"] == expected


def test_deleted_channel_stays_deleted_and_busy_client_defers_apply(tmp_path, service):
    first, second = desktop(tmp_path / "first", service), desktop(tmp_path / "second", service)
    first.channels.add("models")
    settled(first)
    second.sync.is_busy = lambda: True
    settled(second)
    assert second.channels.list()["channels"] == []
    second.sync.is_busy = lambda: False
    settled(second)
    assert second.channels.get("models")
    first.channels.remove("models")
    settled(first)
    settled(second)
    assert second.channels.list()["channels"] == []
    settled(second)
    settled(first)
    assert first.channels.list()["channels"] == []


def test_paging_and_server_database_replacement_detection(tmp_path, service):
    app = desktop(tmp_path / "desktop", service)
    for index in range(270):
        app.channels.add(f"model_{index}")
    settled(app)
    receiver = desktop(tmp_path / "receiver", service)
    settled(receiver)
    assert len(receiver.channels.list()["channels"]) == 270
    with sqlite3.connect(service.root / "business.sqlite3") as db:
        db.execute("UPDATE meta SET value=? WHERE key='server_id'", (uuid4().hex,))
    # A process restart re-reads the database identity; the client rejects a changed identity.
    app.sync.journal.meta("server_id", "different-database")
    status = app.sync.sync_once()
    assert status["stage"] == "offline" and "数据库已更换" in status["error"]


def test_disabled_sync_retains_all_local_behavior(tmp_path, service):
    app = desktop(tmp_path / "desktop", service)
    app.sync.configure(replace(app.config, data_server_enabled=False))
    app.channels.add("models")
    assert app.sync.sync_once()["stage"] == "disabled"
    assert app.channels.get("models")


def test_settings_hide_token_validate_before_write_and_keep_saved_secret(app, monkeypatch):
    monkeypatch.setattr(ServerSync, "start", lambda self: None)
    values = {"data_server_enabled": True, "data_server_url": "http://127.0.0.1:8780",
              "data_server_token": TOKEN, "data_server_workspace": "fixture"}
    before = app.config_path.read_bytes()
    assert app.post("/api/config", {**values, "data_server_url": "http://47.113.126.29:8780"})[0] == 400
    assert app.config_path.read_bytes() == before
    assert app.post("/api/config", {**values, "data_server_token": "short"})[0] == 400
    assert app.config_path.read_bytes() == before
    assert app.post("/api/config", values)[0] == 200
    config = app.get("/api/config")[1]
    assert config["data_server_enabled"] and config["data_server_token_configured"]
    assert "data_server_token" not in config and TOKEN not in json.dumps(config)
    assert app.post("/api/config", {"data_server_token": "", "recent_message_limit": 25})[0] == 200
    assert PipelineConfig.from_json(app.config_path).data_server_token == TOKEN
    assert app.post("/api/data-sync", {"unexpected": True})[0] == 400
    assert app.post("/api/data-sync")[0] == 200


def test_local_edit_during_request_is_not_overwritten(tmp_path, service, monkeypatch):
    first, second = desktop(tmp_path / "first", service), desktop(tmp_path / "second", service)
    first.channels.add("models", "Original")
    settled(first)
    settled(second)
    first.channels.rename("models", "Remote edit")
    settled(first)
    original = httpx.Client.post

    def racing(client, *args, **kwargs):
        response = original(client, *args, **kwargs)
        second.channels.rename("models", "Edited during request")
        return response

    with monkeypatch.context() as patch:
        patch.setattr(httpx.Client, "post", racing)
        second.sync.sync_once()
    assert second.channels.get("models")["name"] == "Edited during request"
    status = second.sync.sync_once()
    assert status["conflicts"] == 1


def test_foreign_running_task_is_not_interrupted_by_other_desktop(tmp_path, service):
    first, second = desktop(tmp_path / "first", service), desktop(tmp_path / "second", service)
    first.transfers.event({"chat": "models", "message_id": 43, "filename": "active.zip", "download_status": "downloading"})
    settled(first)
    settled(second)
    assert second.transfers.snapshot()["tasks"][0]["download_status"] == "downloading"
    second.transfers.finish()
    assert second.transfers.snapshot()["tasks"][0]["download_status"] == "downloading"


def test_live_progress_during_upload_does_not_conflict_with_own_acknowledgement(tmp_path, service, monkeypatch):
    app = desktop(tmp_path / "desktop", service)
    identity = app.transfers.event({"chat": "models", "message_id": 44, "filename": "active.zip",
                                    "download_status": "downloading", "received": 10, "size": 100})
    original = httpx.Client.post

    def progress(client, *args, **kwargs):
        response = original(client, *args, **kwargs)
        app.transfers.update(identity, received=20)
        return response

    with monkeypatch.context() as patch:
        patch.setattr(httpx.Client, "post", progress)
        status = app.sync.sync_once()
    assert status["conflicts"] == 0
    settled(app)
    receiver = desktop(tmp_path / "receiver", service)
    settled(receiver)
    assert receiver.transfers.snapshot()["tasks"][0]["received"] == 20
