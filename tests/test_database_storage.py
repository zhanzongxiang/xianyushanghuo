from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
import ssl
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import mysql, postgresql

from xianyu_pipeline import database_storage
from xianyu_pipeline.channel_catalog import ChannelCatalog
from xianyu_pipeline.channel_sources import ChannelDirectory
from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.data_protocol import MAX_ASSET
from xianyu_pipeline.database_storage import SQLDatabase
from xianyu_pipeline.server_sync import ServerSync
from xianyu_pipeline.web_ui import config_payload
from xianyu_pipeline.workbench import TransferStore
from test_server_sync import operation, settled
from test_workbench_actions import app


JPEG = b"\xff\xd8\xffdatabase-preview"
PASSWORD = "offline-db-password:/@% secret"


@pytest.fixture
def database(tmp_path):
    engine = create_engine("sqlite:///" + (tmp_path / "remote.sqlite3").as_posix(),
                           connect_args={"timeout": 15})
    store = SQLDatabase(engine, "default")
    try:
        yield store
    finally:
        store.close()


def exchange(database, operations, cursor=0):
    return database.exchange({"workspace": database.workspace, "device_id": "a" * 32,
                              "cursor": cursor, "operations": operations})


def desktop(root, monkeypatch, remote_path):
    config = PipelineConfig(inbox_dir=root / "inbox", work_dir=root / "work", output_dir=root / "output",
                            data_server_enabled=True, data_server_backend="postgresql",
                            data_server_db_password=PASSWORD, telegram_api_hash="private-telegram",
                            quark_cookie="private-cookie")
    config.ensure_directories()
    channels, catalog, transfers = ChannelDirectory(config.output_dir), ChannelCatalog(config.output_dir), TransferStore(config)
    monkeypatch.setattr(database_storage, "database_engine", lambda config: create_engine(
        "sqlite:///" + remote_path.as_posix(), connect_args={"timeout": 15}))
    sync = ServerSync(config, channels, catalog, transfers)
    return SimpleNamespace(config=config, channels=channels, catalog=catalog, transfers=transfers, sync=sync)


def test_sql_idempotency_revisions_tombstones_and_transaction_rollback(database):
    op = operation()
    initial = exchange(database, [op])
    assert initial["cursor"] == 1
    assert exchange(database, [op]) == initial
    assert exchange(database, [operation()])["cursor"] == 1
    assert exchange(database, [operation(name="Other")])["results"][0]["status"] == "conflict"
    assert exchange(database, [operation(name="Other", revision=1)])["cursor"] == 2
    with pytest.raises(ValueError, match="ID"):
        exchange(database, [operation("should_rollback"), {**op, "value": {**op["value"], "name": "Changed"}}])
    assert len(exchange(database, [])["changes"]) == 2
    deletion = {**operation(revision=2), "value": None}
    assert exchange(database, [deletion])["results"][0]["record"]["value"] is None
    assert exchange(database, [operation()])["results"][0]["status"] == "conflict"
    assert exchange(database, [operation(revision=3)])["cursor"] == 4


def test_sql_pagination_and_workspace_isolation(database):
    for batch in range(6):
        exchange(database, [operation(f"model_{batch}_{index}") for index in range(50)])
    page = exchange(database, [])
    assert len(page["changes"]) == 250 and page["has_more"] and page["cursor"] == 250
    tail = exchange(database, [], cursor=page["cursor"])
    assert len(tail["changes"]) == 50 and not tail["has_more"] and tail["cursor"] == 300
    other = SQLDatabase(database.engine, "other")
    assert exchange(other, [])["changes"] == []
    assert exchange(other, [operation()])["cursor"] == 1
    assert len(exchange(database, [], cursor=250)["changes"]) == 50


def test_sql_concurrent_edits_preserve_cursor_and_report_conflict(database):
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda name: exchange(database, [operation(name=name)]), ["First", "Second"]))
    assert sorted(result["results"][0]["status"] for result in responses) == ["applied", "conflict"]
    assert len(exchange(database, [])["changes"]) == 1
    assert exchange(database, [])["cursor"] == 1


def test_sql_assets_are_database_blobs_deduplicated_and_scoped(database):
    body = JPEG + b"x" * (1024 * 1024)
    digest = hashlib.sha256(body).hexdigest()
    database.put_asset(digest, body)
    database.put_asset(digest, body)
    assert database.get_asset(digest) == body
    with database.engine.connect() as db:
        assert len(db.execute(select(database.assets)).all()) == 1
    with pytest.raises(ValueError):
        database.put_asset("0" * 64, body)
    with pytest.raises(ValueError):
        database.put_asset(digest, JPEG + b"x" * MAX_ASSET)
    with pytest.raises(ValueError):
        SQLDatabase(database.engine, "other").get_asset(digest)


def test_sql_rejects_private_fields_and_cross_device_task_writes_atomically(database):
    with pytest.raises(ValueError):
        exchange(database, [operation(), operation("bad", quark_cookie="must-never-store")])
    with pytest.raises(ValueError):
        exchange(database, [{"id": uuid4().hex, "kind": "tasks", "key": "b" * 32 + ":" + "c" * 24,
                             "base_revision": 0, "value": {"id": "c" * 24, "device_id": "b" * 32}}])
    assert exchange(database, [])["changes"] == []


def test_database_sync_moves_metadata_images_and_tasks_without_credentials(tmp_path, monkeypatch):
    remote = tmp_path / "database.sqlite3"
    first = desktop(tmp_path / "one", monkeypatch, remote)
    second = desktop(tmp_path / "two", monkeypatch, remote)
    try:
        first.channels.ensure("https://t.me/models")
        cover = first.catalog.preview_root / "cover.jpg"
        cover.parent.mkdir(parents=True)
        cover.write_bytes(JPEG)
        first.catalog.merge("models", {"messages": [
            {"message_id": 42, "chat": "models", "filename": "model.rar", "text": "Model",
             "size": 12, "cover_path": str(cover), "cover_quality": "preview", "cover_status": "ready"},
        ], "oldest_scanned_id": 42, "has_more": True})
        identity = first.transfers.event({"chat": "models", "message_id": 42, "filename": "model.rar",
                                          "download_status": "failed"})
        settled(first)
        settled(second)
        assert second.channels.list()["channels"][0]["key"] == "models"
        assert second.catalog._current("models")["messages"][0]["message_id"] == 42
        assert second.transfers.remote_tasks[0]["remote_device"]
        assert second.transfers.remote_tasks[0]["message_id"] == 42
        assert identity not in second.transfers.tasks
        assert second.sync.database.get_asset(hashlib.sha256(JPEG).hexdigest()) == JPEG
        with first.sync.database.engine.connect() as db:
            records = json.dumps([dict(row) for row in db.execute(select(first.sync.database.records)).mappings()])
        for secret in (PASSWORD, "private-telegram", "private-cookie", str(first.config.inbox_dir)):
            assert secret not in records
    finally:
        first.sync.close()
        second.sync.close()


def test_database_offline_outbox_recovers_after_restart_and_password_rotation(tmp_path, monkeypatch):
    remote = tmp_path / "database.sqlite3"
    first = desktop(tmp_path / "one", monkeypatch, remote)
    first.channels.ensure("https://t.me/models")

    def offline(config):
        raise OperationalError("secret-db-url", {"password": PASSWORD}, RuntimeError(PASSWORD))

    with monkeypatch.context() as patch:
        patch.setattr(database_storage, "database_engine", offline)
        status = first.sync.sync_once()
        assert status["stage"] == "offline" and status["pending"] == 1
        assert PASSWORD not in status["error"] and "secret-db-url" not in status["error"]
    restarted = desktop(tmp_path / "one", monkeypatch, remote)
    try:
        settled(restarted)
        target = restarted.sync.journal.target
        restarted.sync.configure(replace(restarted.config, data_server_db_password="rotated"))
        assert restarted.sync.journal.target == target
        assert restarted.sync.journal.counts()["pending"] == 0
        settled(restarted)
    finally:
        first.sync.close()
        restarted.sync.close()


def test_database_identity_change_stops_sync_instead_of_reusing_cursor(tmp_path, monkeypatch):
    client = desktop(tmp_path / "one", monkeypatch, tmp_path / "database.sqlite3")
    try:
        client.channels.ensure("https://t.me/models")
        settled(client)
        with client.sync.database.engine.begin() as db:
            db.execute(client.sync.database.meta.update().where(
                client.sync.database.meta.c.key == "server_id").values(value=uuid4().hex))
        status = client.sync.sync_once()
        assert status["stage"] == "offline" and "已更换" in status["error"]
    finally:
        client.sync.close()


@pytest.mark.parametrize("fields", [
    {"data_server_db_port": True}, {"data_server_db_port": 65536},
    {"data_server_db_host": "user:password@host"}, {"data_server_db_name": "x;DROP TABLE x"},
    {"data_server_db_user": ""}, {"data_server_db_password": 123},
    {"data_server_db_tls": "prefer"}, {"data_server_backend": "sqlite"},
    {"data_server_enabled": True}, {"data_server_enabled": True, "data_server_db_password": PASSWORD,
                                   "data_server_db_tls": "disable"},
])
def test_database_configuration_validation(fields):
    with pytest.raises(ValueError):
        PipelineConfig(**fields)


def test_database_settings_save_password_privately_and_validate_before_writing(app, monkeypatch):
    monkeypatch.setattr(ServerSync, "start", lambda self: None)
    values = {"data_server_enabled": True, "data_server_backend": "postgresql",
              "data_server_db_host": "47.113.126.29", "data_server_db_port": 5432,
              "data_server_db_password": PASSWORD}
    original = app.config_path.read_text(encoding="utf-8")
    assert app.post("/api/config", {**values, "data_server_db_port": 70000})[0] == 400
    assert app.config_path.read_text(encoding="utf-8") == original
    assert app.post("/api/config", values)[0] == 200
    public = app.get("/api/config")[1]
    assert public["data_server_backend"] == "postgresql" and public["data_server_db_password_configured"]
    assert "data_server_db_password" not in public and PASSWORD not in json.dumps(public)
    assert app.post("/api/config", {"data_server_db_password": "", "recent_message_limit": 25})[0] == 200
    assert PipelineConfig.from_json(app.config_path).data_server_db_password == PASSWORD


def test_database_connection_test_checks_saved_settings_without_creating_tables(app, monkeypatch, tmp_path):
    assert app.post("/api/data-check")[0] == 400
    assert app.post("/api/config", {"data_server_db_password": PASSWORD})[0] == 200
    engine = create_engine("sqlite:///" + (tmp_path / "connection-check.sqlite3").as_posix())
    monkeypatch.setattr(database_storage, "database_engine", lambda config: engine)
    assert app.post("/api/data-check") == (200, {"message": "数据库连接成功"})
    with engine.connect() as db:
        assert db.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").all() == []
    monkeypatch.setattr(database_storage, "database_engine", lambda config: (_ for _ in ()).throw(RuntimeError(PASSWORD)))
    status, result = app.post("/api/data-check")
    assert status == 400 and PASSWORD not in result["message"]
    engine.dispose()


def test_database_driver_urls_preserve_passwords_without_interpolation():
    for backend, port, driver in (("postgresql", 5432, "psycopg"), ("mysql", 3306, "pymysql")):
        config = PipelineConfig(data_server_backend=backend, data_server_db_password=PASSWORD,
                                data_server_db_port=port)
        engine = database_storage.database_engine(config)
        try:
            assert engine.url.password == PASSWORD
            assert engine.url.drivername.endswith("+" + driver)
            assert PASSWORD not in str(engine.url)
            assert config_payload(config)["data_server_db_password_configured"]
            assert PASSWORD not in repr(config)
        finally:
            engine.dispose()


def test_mysql_tls_verifies_identity_without_needing_a_custom_ca(monkeypatch):
    captured = {}

    def factory(*args, **kwargs):
        captured.update(kwargs["connect_args"])
        return create_engine(*args, **kwargs)

    monkeypatch.setattr(database_storage, "create_engine", factory)
    for mode in ("verify-full", "require", "disable"):
        captured.clear()
        config = PipelineConfig(data_server_backend="mysql", data_server_db_tls=mode)
        engine = database_storage.database_engine(config)
        try:
            if mode == "disable":
                assert "ssl" not in captured
            else:
                context = captured["ssl"]
                assert isinstance(context, ssl.SSLContext)
                assert context.check_hostname is (mode == "verify-full")
                assert context.verify_mode == (ssl.CERT_REQUIRED if mode == "verify-full" else ssl.CERT_NONE)
        finally:
            engine.dispose()


def test_remote_database_schema_compiles_with_large_image_and_record_types(database):
    assets = str(CreateTable(database.assets).compile(dialect=mysql.dialect()))
    records = str(CreateTable(database.records).compile(dialect=mysql.dialect()))
    assert "MEDIUMBLOB" in assets and "utf8mb4_bin" in assets and "ENGINE=InnoDB" in assets
    assert "MEDIUMTEXT" in records
    assert "BYTEA" in str(CreateTable(database.assets).compile(dialect=postgresql.dialect()))


@pytest.mark.skipif(not os.environ.get("XIANYU_TEST_DATABASE_URL"), reason="No isolated PostgreSQL/MySQL test database")
def test_real_database_contract():
    engine = create_engine(os.environ["XIANYU_TEST_DATABASE_URL"], hide_parameters=True)
    database = SQLDatabase(engine, "test_" + uuid4().hex)
    try:
        initial = operation()
        assert exchange(database, [initial])["cursor"] == 1
        assert exchange(database, [initial])["results"][0]["status"] == "applied"
        assert exchange(database, [operation(name="Conflict")])["results"][0]["status"] == "conflict"
        body = JPEG + b"x" * (1024 * 1024)
        digest = hashlib.sha256(body).hexdigest()
        database.put_asset(digest, body)
        assert database.get_asset(digest) == body
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda index: exchange(database, [operation(f"thread_{index}")]), [1, 2]))
        assert all(response["results"][0]["status"] == "applied" for response in responses)
        assert exchange(database, [])["cursor"] == 3
    finally:
        with engine.begin() as db:
            for table in (database.assets, database.operations, database.changes, database.records, database.workspaces):
                db.execute(table.delete().where(table.c.workspace == database.workspace))
        database.close()
