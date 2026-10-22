from __future__ import annotations

import io
import json
import logging
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from xianyu_pipeline import web_ui
from xianyu_pipeline.channel_sources import ChannelDirectory
from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.quark_browser import QuarkBrowserStorageAdapter
from xianyu_pipeline.workbench import TransferStore


TARGET = "https://pan.quark.cn/list?folder=A"
OTHER_TARGET = "https://pan.quark.cn/list?folder=B"
SHARE = "https://pan.quark.cn/s/Fixture"


@pytest.fixture
def app(tmp_path, monkeypatch):
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"MZ")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "telegram_access_mode": "api", "telegram_api_id": 12345, "telegram_api_hash": "a" * 32,
        "source_channel_url": "https://t.me/fixture_channel", "fetch_mode": "recent",
        "inbox_dir": str(tmp_path / "inbox"), "output_dir": str(tmp_path / "output"),
        "work_dir": str(tmp_path / "work"), "browser_executable_path": str(executable),
        "upload_provider": "quark_browser", "quark_folder_url": TARGET,
        "quark_upload_input_selector": "input", "quark_file_row_selector": "row",
        "quark_share_button_selector": "button", "quark_share_link_selector": "link",
        "quark_share_create_button_selector": "create",
    }), encoding="utf-8")
    result = SimpleNamespace(config_path=config_path, workers=[], calls=[], upload_error=False, share_error=False)
    result.store = TransferStore(PipelineConfig.from_json(config_path))
    result.channels = ChannelDirectory(result.store.config.output_dir)

    class Server:
        def __init__(self, address, handler):
            result.handler = handler

        def serve_forever(self):
            pass

    class WorkerThread:
        def __init__(self, *, target, args, daemon):
            self.target, self.args = target, args
            assert daemon

        def start(self):
            result.workers.append(self)

    class RecordingQuark(QuarkBrowserStorageAdapter):
        def record(self, operation, source, remote_folder):
            rows = json.loads(result.store.path.read_text(encoding="utf-8"))
            binding = next(row for row in rows if row["upload_filename"] == source.name)
            result.calls.append((operation, self.folder_url, source.name, remote_folder, binding))

        def upload(self, source, remote_folder):
            self.record("upload", source, remote_folder)
            self._report_stage("link_pending")
            if result.upload_error:
                raise RuntimeError("fixture upload result unknown")
            return SHARE

        def share_existing(self, source, remote_folder):
            self.record("share_existing", source, remote_folder)
            self._report_stage("link_pending")
            if result.share_error:
                raise RuntimeError("fixture link unavailable")
            return SHARE

    def configure_logging(directory, handler):
        result.state = handler.state
        result.log_handler = handler

    monkeypatch.setattr(web_ui, "configure_logging", configure_logging)
    monkeypatch.setattr(web_ui, "TransferStore", lambda config: result.store)
    monkeypatch.setattr(web_ui, "ChannelDirectory", lambda directory: result.channels)
    monkeypatch.setattr(web_ui, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(web_ui, "threading", SimpleNamespace(Lock=threading.Lock, Thread=WorkerThread))
    monkeypatch.setattr(web_ui, "QuarkBrowserStorageAdapter", RecordingQuark)
    web_ui.start_web_ui(config_path, port=18765, open_browser=False)

    def post(path, payload=None):
        handler = result.handler.__new__(result.handler)
        body = json.dumps({} if payload is None else payload).encode()
        handler.path = path
        handler.headers = {"Host": "127.0.0.1:18765", "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        responses = []
        handler.json_response = lambda payload, status=200: responses.append((status, payload))
        handler.send_error = lambda status: responses.append((status, {}))
        handler.do_POST()
        assert len(responses) == 1
        return responses[0]

    def run():
        worker = result.workers.pop(0)
        worker.target(*worker.args)
        assert not result.state.running

    def get(path):
        handler = result.handler.__new__(result.handler)
        handler.path = path
        handler.headers = {"Host": "127.0.0.1:18765"}
        responses = []
        handler.json_response = lambda payload, status=200: responses.append((status, payload))
        handler.send_error = lambda status: responses.append((status, {}))
        handler.do_GET()
        assert len(responses) == 1
        return responses[0]

    def task(name="asset.zip", **values):
        source = result.store.config.inbox_dir / name
        if source.suffix == ".zip":
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("asset.pdf", "fixture")
                archive.writestr("cover.png", b"\x89PNG\r\n\x1a\nfixture")
        else:
            source.write_bytes(b"rar fixture")
        identity = result.store.event({"chat": "fixture_channel", "message_id": 123, "filename": name,
                                       "local_path": str(source), "download_status": "completed"})
        if values:
            result.store.update(identity, **values)
        return identity

    def bind(identity, status="link_pending"):
        source = Path(result.store.get(identity)["local_path"])
        result.store.update(identity, upload_status=status, upload_provider="quark_browser",
                            upload_filename=source.name, upload_local_path=str(source), upload_target=TARGET)

    def acquire(avatar_url=""):
        result.channels.merge_joined([
            {"id": 123, "title": "Fixture", "username": "fixture_channel",
             "key": "fixture_channel", "target": "https://t.me/fixture_channel",
             "avatar_url": avatar_url},
        ])
        assert post("/api/channels/select", {"key": "fixture_channel"})[0] == 200

    def stop():
        snapshot = result.state.snapshot()
        return post("/api/stop", {key: snapshot[key] for key in ("task_id", "action")})

    result.post, result.get, result.run, result.task, result.bind = post, get, run, task, bind
    result.acquire = acquire
    result.stop = stop
    return result


@pytest.mark.parametrize("enabled,name,status,fail,expected", [
    (True, "new.zip", "waiting", False, 1),
    (False, "new.zip", "waiting", False, 0),
    (True, "new.part1.zip", "waiting", False, 0),
    (True, "new.zip", "completed", False, 0),
    (True, "new.zip", "link_pending", False, 0),
    (True, "new.zip", "waiting", True, 1),
])
def test_auto_upload_only_new_completed_downloads(app, monkeypatch, enabled, name, status, fail, expected):
    values = json.loads(app.config_path.read_text(encoding="utf-8"))
    values["auto_upload_after_download"] = enabled
    app.config_path.write_text(json.dumps(values), encoding="utf-8")
    historical = app.task("historical.zip")
    identity = app.task(name, upload_status=status)
    app.upload_error = fail
    values["download_workers"] = 2
    values["download_file_concurrency"] = 1
    app.config_path.write_text(json.dumps(values), encoding="utf-8")

    class Telegram(web_ui.TelethonTelegramAdapter):
        def fetch_messages(self, *args):
            assert self.download_workers == 2
            assert self.download_file_concurrency == 1
            self.on_event({"chat": "fixture_channel", "message_id": 123, "filename": name,
                           "local_path": app.store.get(identity)["local_path"], "download_status": "completed"})
            return []

    monkeypatch.setattr(web_ui, "TelethonTelegramAdapter", Telegram)
    app.acquire()
    assert app.post("/api/download")[0] == 200
    app.run()
    assert len(app.calls) == expected
    assert app.store.get(historical)["upload_status"] == "waiting"
    assert app.store.get(identity)["download_status"] == "completed"
    if fail:
        assert app.store.get(identity)["upload_status"] == "link_pending"
    elif expected:
        assert app.store.get(identity)["share_url"] == SHARE


def test_upload_binding_is_durable_before_cloud_side_effect(app):
    identity = app.task("asset-2.zip")
    assert app.post("/api/upload", {"ids": [identity]})[0] == 200
    app.run()
    operation, target, name, _, persisted = app.calls[0]
    assert (operation, target, name) == ("upload", TARGET, "asset-2.zip")
    assert persisted["upload_status"] == "uploading"
    assert persisted["upload_target"] == TARGET
    assert persisted["upload_provider"] == "quark_browser"
    assert persisted["upload_local_path"] == str(app.store.config.inbox_dir / name)
    assert app.store.get(identity)["upload_status"] == "completed"
    assert app.store.get(identity)["share_url"] == SHARE


def test_worker_uses_validated_snapshot_without_rereading_config(app, monkeypatch):
    identity = app.task()
    assert app.post("/api/upload", {"ids": [identity]})[0] == 200
    values = json.loads(app.config_path.read_text(encoding="utf-8"))
    values["quark_folder_url"] = OTHER_TARGET
    app.config_path.write_text(json.dumps(values), encoding="utf-8")

    def forbidden_read(*args):
        raise AssertionError("worker must not reread configuration")

    monkeypatch.setattr(PipelineConfig, "from_json", forbidden_read)
    app.run()
    assert not app.state.error
    assert app.calls[0][1] == TARGET
    assert app.store.get(identity)["upload_target"] == TARGET


@pytest.mark.parametrize("second_action", ["/api/config", "/api/upload"])
def test_validation_and_reservation_exclude_concurrent_save_or_start(app, monkeypatch, second_action):
    identity = app.task()
    app.bind(identity)
    validating, release, contending = threading.Event(), threading.Event(), threading.Event()
    lock = app.state.control_lock

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == "contender":
                contending.set()
            lock.acquire()

        def __exit__(self, *args):
            lock.release()

    app.state.control_lock = ObservedLock()
    validate = web_ui.validate_browser_path

    def blocked_validation(value):
        validating.set()
        assert release.wait(5)
        return validate(value)

    monkeypatch.setattr(web_ui, "validate_browser_path", blocked_validation)
    first_result, second_result, failures = [], [], []

    def request(results, path, payload):
        try:
            results.append(app.post(path, payload))
        except BaseException as exc:
            failures.append(exc)

    first = threading.Thread(target=request, args=(first_result, "/api/upload", {"ids": [identity]}))
    second_payload = {"quark_folder_url": OTHER_TARGET} if second_action == "/api/config" else {"ids": [identity]}
    second = threading.Thread(name="contender", target=request, args=(second_result, second_action, second_payload))
    first.start()
    try:
        assert validating.wait(5)
        second.start()
        assert contending.wait(5)
        assert not second_result
        assert PipelineConfig.from_json(app.config_path).quark_folder_url == TARGET
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not failures
    assert first_result[0][0] == 200 and second_result[0][0] == 409
    assert len(app.workers) == 1
    app.run()
    assert app.calls[0][:4] == ("share_existing", TARGET, "asset.zip", TARGET)


def test_config_and_action_validation_can_log_without_holding_state_lock(app, monkeypatch):
    original = web_ui.validate_browser_path

    def logging_validation(value):
        assert app.state.lock.acquire(blocking=False), "validation holds the logging lock"
        app.state.lock.release()
        app.log_handler.emit(logging.LogRecord("fixture", logging.INFO, __file__, 1, "validation log", (), None))
        return original(value)

    monkeypatch.setattr(web_ui, "validate_browser_path", logging_validation)
    assert app.post("/api/config", {"recent_message_limit": 3})[0] == 200
    identity = app.task()
    assert app.post("/api/upload", {"ids": [identity]})[0] == 200
    app.run()
    assert app.state.logs.count("validation log") == 2


@pytest.mark.parametrize("name", ["asset.zip", "asset.rar"])
@pytest.mark.parametrize("replacement_status", ["completed", "failed"])
def test_recovery_uses_original_name_without_any_local_file_or_archive_validation(app, monkeypatch, name, replacement_status):
    identity = app.task(name)
    app.bind(identity)
    original = Path(app.store.get(identity)["local_path"])
    original.unlink()
    replacement = original.with_name("asset-2" + original.suffix)
    replacement.write_bytes(b"broken replacement")
    app.store.event({"chat": "fixture_channel", "message_id": 123, "filename": name,
                     "download_status": replacement_status, "local_path": str(replacement)})
    values = json.loads(app.config_path.read_text(encoding="utf-8"))
    values["archive_test_executable_path"] = str(original.parent / "missing.exe")
    app.config_path.write_text(json.dumps(values), encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("link recovery must not read or validate a local archive")

    monkeypatch.setattr(app.store, "local_file", forbidden)
    monkeypatch.setattr(web_ui.zipfile, "ZipFile", forbidden)
    monkeypatch.setattr(web_ui, "validate_rar", forbidden)
    monkeypatch.setattr(web_ui, "find_archive_tester", forbidden)
    assert app.post("/api/upload", {"ids": [identity]})[0] == 200
    app.run()
    assert not app.state.error
    assert app.calls[0][:4] == ("share_existing", TARGET, name, TARGET)
    task = app.store.get(identity)
    assert task["upload_status"] == "completed"
    assert task["upload_filename"] == name and task["upload_local_path"] == str(original)
    assert task["local_path"] == str(replacement) and task["download_status"] == replacement_status


@pytest.mark.parametrize("changed", [{"quark_folder_url": OTHER_TARGET}, {"upload_provider": "local"}])
def test_recovery_still_rejects_different_provider_or_target(app, changed):
    identity = app.task()
    app.bind(identity)
    Path(app.store.get(identity)["local_path"]).unlink()
    assert app.post("/api/config", changed)[0] == 200
    assert app.post("/api/upload", {"ids": [identity]})[0] == 400
    assert not app.workers and not app.calls
    assert app.store.get(identity)["upload_target"] == TARGET


def test_ambiguous_upload_failure_recovers_without_reupload(app):
    identity = app.task()
    app.upload_error = True
    assert app.post("/api/upload", {"ids": [identity]})[0] == 200
    app.run()
    assert app.store.get(identity)["upload_status"] == "link_pending"
    Path(app.store.get(identity)["local_path"]).unlink()
    app.share_error = True
    assert app.post("/api/upload", {"ids": [identity]})[0] == 200
    app.run()
    assert app.store.get(identity)["upload_status"] == "link_pending"
    app.share_error = False
    assert app.post("/api/upload", {"ids": [identity]})[0] == 200
    app.run()
    assert [call[0] for call in app.calls] == ["upload", "share_existing", "share_existing"]
    assert app.store.get(identity)["upload_status"] == "completed"


def test_browser_mode_cannot_be_reenabled(app):
    identity = app.task(download_status="failed")
    response, body = app.post("/api/config", {"telegram_access_mode": "browser"})
    assert response == 400
    assert "Telegram API" in body["message"]
    assert not app.workers and not app.state.running
    assert app.store.get(identity)["download_status"] == "failed"


def test_api_retry_keeps_single_message_arguments(app, monkeypatch):
    identity = app.task(download_status="failed")
    calls = []

    class Telegram:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_messages(self, *args):
            calls.append(args)
            return []

    monkeypatch.setattr(web_ui, "TelethonTelegramAdapter", Telegram)
    assert app.post("/api/retry", {"id": identity})[0] == 200
    app.run()
    assert calls == [("fixture_channel", [123], "fixture_channel", "message_ids", 1)]
    assert not app.state.error


def test_simulation_can_upgrade_to_quark_but_neither_provider_can_repeat(app):
    identity = app.task()
    assert app.post("/api/config", {"upload_provider": "local"})[0] == 200
    assert app.post("/api/upload", {"ids": [identity]})[0] == 200
    app.run()
    assert app.store.get(identity)["upload_status"] == "local"
    assert app.store.get(identity)["share_url"].startswith("file:")
    assert app.post("/api/upload", {"ids": [identity]})[0] == 400
    assert app.post("/api/config", {"upload_provider": "quark_browser"})[0] == 200
    assert app.post("/api/upload", {"ids": [identity]})[0] == 200
    app.run()
    assert len(app.calls) == 1 and app.calls[0][0] == "upload"
    task = app.store.get(identity)
    assert task["upload_status"] == "completed" and task["upload_provider"] == "quark_browser"
    assert task["upload_target"] == TARGET and task["share_url"] == SHARE
    assert app.post("/api/upload", {"ids": [identity]})[0] == 400


def test_new_upload_still_requires_valid_local_archive(app):
    identity = app.task()
    source = Path(app.store.get(identity)["local_path"])
    source.unlink()
    assert app.post("/api/upload", {"ids": [identity]})[0] == 400
    source.write_bytes(b"broken zip")
    assert app.post("/api/upload", {"ids": [identity]})[0] == 200
    app.run()
    assert not app.calls
    assert app.store.get(identity)["upload_status"] == "failed"


def test_legacy_run_still_processes_local_material_and_writes_manifest(app):
    app.task()
    assert app.post("/api/config", {"source_channel_url": "", "upload_provider": "local"})[0] == 200
    assert app.post("/api/run")[0] == 200
    app.run()
    assert not app.state.error
    manifest = json.loads((app.store.config.output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest) == 1
    assert manifest[0]["share_url"].startswith("file:")


def api_settings(app, **changes):
    return app.post("/api/config", {
        "upload_provider": "quark_api", "quark_cookie": "__uid=fixture; __pus=fixture-secret",
        "quark_api_parent_fid": "folder-1", "browser_executable_path": "", **changes,
    })


def bound_api_task(app, **changes):
    identity = app.task()
    source = Path(app.store.get(identity)["local_path"])
    app.store.update(identity, **{
        "upload_status": "completed", "upload_provider": "quark_api",
        "upload_filename": source.name, "upload_target": "original-folder", "upload_remote_fid": "original-fid",
        "upload_size": source.stat().st_size, "upload_md5": "a" * 32, "upload_sha1": "b" * 40,
        "share_url": SHARE, "remote_filename": "中文模型.zip", **changes,
    })
    return identity


def delete_request(app, identity, **changes):
    return app.post("/api/cloud-delete", {"id": identity, "remote_fid": "original-fid", "confirm": True, **changes})


def test_library_remove_hides_record_preserves_archive_and_cloud_binding_without_worker(app):
    identity = bound_api_task(app)
    before = app.store.get(identity)
    source = Path(before["local_path"])
    body = source.read_bytes()
    assert app.get("/api/workbench")[1]["record_remove_supported"] is True
    assert app.post("/api/library/remove", {"id": identity, "confirm": True})[0] == 200
    assert app.get("/api/workbench")[1]["tasks"] == []
    assert source.read_bytes() == body
    assert app.store.tasks[identity]["upload_remote_fid"] == before["upload_remote_fid"]
    assert app.store.tasks[identity]["share_url"] == before["share_url"]
    assert not app.workers and not app.calls
    assert app.post("/api/library/remove", {"id": identity, "confirm": True})[0] == 400
    assert delete_request(app, identity)[0] == 400
    assert app.post("/api/upload", {"ids": [identity]})[0] == 400


@pytest.mark.parametrize("changes", [
    {"confirm": False}, {"confirm": 1}, {"id": "missing"},
])
def test_library_remove_requires_explicit_confirmation_and_own_task(app, changes):
    identity = app.task()
    assert app.post("/api/library/remove", {"id": identity, "confirm": True, **changes})[0] == 400
    assert app.store.get(identity)["record_removed_at"] == ""


def test_library_remove_rejects_busy_or_pending_cloud_operations(app):
    identity = bound_api_task(app, cloud_delete_status="pending", cloud_delete_task_id="delete-task")
    assert app.post("/api/library/remove", {"id": identity, "confirm": True})[0] == 400
    app.store.update(identity, cloud_delete_status="completed", upload_status="deleted")
    app.state.running = True
    assert app.post("/api/library/remove", {"id": identity, "confirm": True})[0] == 409
    app.state.running = False
    assert app.post("/api/library/remove", {"id": identity, "confirm": True})[0] == 200


def test_library_remove_accepts_cloud_success_without_local_success_or_file(app):
    identity = bound_api_task(app, download_status="failed")
    source = Path(app.store.get(identity)["local_path"])
    source.unlink()
    assert app.post("/api/library/remove", {"id": identity, "confirm": True})[0] == 200
    assert app.store.snapshot()["tasks"] == []


def test_cloud_delete_uses_original_binding_preserves_local_archive_and_clears_link(app, monkeypatch):
    assert api_settings(app)[0] == 200
    identity = bound_api_task(app)
    original_task = app.store.get(identity)
    original_bytes = Path(original_task["local_path"]).read_bytes()
    calls = []

    class API(web_ui.QuarkAPIStorageAdapter):
        def delete_existing(self, name, target, **options):
            persisted = next(row for row in json.loads(app.store.path.read_text(encoding="utf-8")) if row["id"] == identity)
            assert persisted["cloud_delete_status"] == "deleting"
            assert self.stop_event is app.state.stop_event
            calls.append((name, target, options))
            self._remote(cloud_delete_status="pending", cloud_delete_task_id="delete-task")

    monkeypatch.setattr(web_ui, "QuarkAPIStorageAdapter", API)
    assert app.get("/api/workbench")[1]["cloud_delete_supported"]
    assert delete_request(app, identity)[0] == 200
    assert delete_request(app, identity)[0] == 409
    app.run()
    task = app.store.get(identity)
    assert calls == [("中文模型.zip", "original-folder", {
        "remote_fid": "original-fid", "size": original_task["upload_size"], "md5": "a" * 32, "sha1": "b" * 40,
        "delete_task_id": "", "pending": False,
    })]
    assert task["upload_status"] == "deleted" and task["cloud_delete_status"] == "completed"
    assert task["cloud_deleted_at"] and task["share_url"] == "" and task["cloud_delete_error"] == ""
    assert task["download_status"] == "completed" and task["upload_remote_fid"] == "original-fid"
    assert Path(task["local_path"]).read_bytes() == original_bytes
    assert delete_request(app, identity)[0] == 400
    assert not app.state.error


@pytest.mark.parametrize("changes", [
    {"confirm": False}, {"confirm": 1}, {"remote_fid": "stale-file"}, {"id": "missing"},
])
def test_cloud_delete_rejects_unconfirmed_or_stale_requests_before_worker(app, changes):
    assert api_settings(app)[0] == 200
    identity = bound_api_task(app)
    assert delete_request(app, identity, **changes)[0] == 400
    assert not app.workers and app.store.get(identity)["upload_status"] == "completed"


@pytest.mark.parametrize("changes", [
    {"upload_provider": "quark_browser"}, {"upload_status": "link_pending"},
    {"upload_remote_fid": ""}, {"upload_target": ""}, {"upload_target": "../invalid"},
    {"upload_size": 0}, {"upload_filename": ""}, {"remote_device": True},
])
def test_cloud_delete_requires_confirmed_api_binding(app, changes):
    assert api_settings(app)[0] == 200
    identity = bound_api_task(app, **changes)
    assert delete_request(app, identity)[0] == 400
    assert not app.workers


def test_cloud_delete_pending_receipt_survives_retry_and_does_not_need_local_archive(app, monkeypatch):
    assert api_settings(app)[0] == 200
    identity = bound_api_task(app)
    Path(app.store.get(identity)["local_path"]).unlink()
    calls = []

    class API(web_ui.QuarkAPIStorageAdapter):
        def delete_existing(self, name, target, **options):
            calls.append(options)
            if len(calls) == 1:
                self._remote(cloud_delete_status="pending", cloud_delete_task_id="original-delete-task")
                raise web_ui.QuarkDeletePending("fixture deletion still running")

    monkeypatch.setattr(web_ui, "QuarkAPIStorageAdapter", API)
    assert delete_request(app, identity)[0] == 200
    app.run()
    task = app.store.get(identity)
    assert task["cloud_delete_status"] == "pending" and task["upload_status"] == "completed"
    assert task["share_url"] == SHARE and task["cloud_delete_error"]
    assert app.post("/api/upload", {"ids": [identity]})[0] == 400
    assert delete_request(app, identity)[0] == 200
    app.run()
    assert calls[-1]["pending"] is True and calls[-1]["delete_task_id"] == "original-delete-task"
    assert app.store.get(identity)["upload_status"] == "deleted"


def test_cloud_delete_preflight_failure_keeps_original_upload_and_link(app, monkeypatch):
    assert api_settings(app)[0] == 200
    identity = bound_api_task(app)

    class API(web_ui.QuarkAPIStorageAdapter):
        def delete_existing(self, *args, **options):
            raise ValueError("fixture remote file has changed")

    monkeypatch.setattr(web_ui, "QuarkAPIStorageAdapter", API)
    assert delete_request(app, identity)[0] == 200
    app.run()
    task = app.store.get(identity)
    assert task["cloud_delete_status"] == "failed" and task["share_url"] == SHARE
    assert task["upload_status"] == "completed" and task["cloud_deleted_at"] == ""
    assert app.state.error and "changed" in task["cloud_delete_error"]


def test_cloud_delete_updates_all_local_records_bound_to_same_remote_object(app, monkeypatch):
    assert api_settings(app)[0] == 200
    identity = bound_api_task(app)
    other = app.task("other.zip", upload_status="completed", upload_provider="quark_api",
                     upload_remote_fid="original-fid", upload_target="original-folder", share_url=SHARE)
    unrelated = app.task("unrelated.zip", upload_status="completed", upload_provider="quark_api",
                         upload_remote_fid="different-fid", upload_target="original-folder", share_url=SHARE)

    class API(web_ui.QuarkAPIStorageAdapter):
        def delete_existing(self, *args, **options):
            pass

    monkeypatch.setattr(web_ui, "QuarkAPIStorageAdapter", API)
    assert delete_request(app, identity)[0] == 200
    app.run()
    assert app.store.get(other)["upload_status"] == "deleted"
    assert app.store.get(unrelated)["upload_status"] == "completed"
    assert app.store.get(other)["download_status"] == "completed"


def test_reupload_after_cloud_delete_starts_new_binding_and_clears_old_delete_state(app, monkeypatch):
    assert api_settings(app)[0] == 200
    identity = bound_api_task(app, upload_status="deleted", cloud_delete_status="completed",
                             cloud_delete_task_id="old-delete-task", cloud_deleted_at="old-timestamp")

    class API(web_ui.QuarkAPIStorageAdapter):
        def upload(self, source, target, **options):
            task = app.store.get(identity)
            assert task["cloud_delete_status"] == "" and task["cloud_delete_task_id"] == ""
            assert task["cloud_deleted_at"] == "" and task["remote_filename"] == ""
            self._remote(upload_remote_fid="new-fid")
            return SHARE

    monkeypatch.setattr(web_ui, "QuarkAPIStorageAdapter", API)
    assert app.post("/api/upload", {"ids": [identity]})[0] == 200
    app.run()
    assert app.store.get(identity)["upload_remote_fid"] == "new-fid"
    assert app.store.get(identity)["upload_status"] == "completed"


def test_cloud_delete_state_survives_restart_and_download_events(app):
    identity = bound_api_task(app, cloud_delete_status="deleting", cloud_delete_task_id="delete-task")
    restored = TransferStore(app.store.config)
    assert restored.get(identity)["cloud_delete_status"] == "pending"
    restored.event({"chat": "fixture_channel", "message_id": 123, "filename": "asset.zip",
                    "cloud_delete_status": "completed", "cloud_delete_task_id": "wrong",
                    "cloud_deleted_at": "wrong"})
    task = restored.get(identity)
    assert task["cloud_delete_status"] == "pending" and task["cloud_delete_task_id"] == "delete-task"
    assert task["cloud_deleted_at"] == ""


def test_naming_candidates_can_be_generated_without_cloud_or_telegram(app):
    identity = app.task("Dragon Warrior Bust v2.zip")
    assert app.post("/api/config", {"rename_translation_provider": "local"})[0] == 200
    assert app.post("/api/names/generate", {"ids": [identity]})[0] == 200
    app.run()
    task = app.store.get(identity)
    assert task["chinese_filename"] == "龙战士 胸像_v2.zip"
    assert task["naming_status"] == "review"
    assert task["naming_quality"] == "matched"
    assert not task["rename_target"] and not app.calls


def test_work_and_character_names_are_corrected_without_remote_translation(app, monkeypatch):
    identity = app.task("Kuru Figure - Steins Gate - Kurisu Makise.rar")
    monkeypatch.setattr(web_ui.NameTranslator, "_online", lambda *_: pytest.fail("Unknown maker should be kept"))
    assert app.post("/api/names/generate", {"ids": [identity]})[0] == 200
    app.run()
    task = app.store.get(identity)
    assert task["chinese_filename"] == "Kuru 手办 - 命运石之门 - 牧濑红莉栖.rar"
    assert task["naming_untranslated"] == ["Kuru"]
    assert task["naming_quality"] == "partial"
    assert not app.calls
    assert app.post("/api/names/save", {"entries": [{"id": identity, "name": "命运石之门 牧濑红莉栖 手办.rar"}]})[0] == 200
    saved = app.store.get(identity)
    assert saved["naming_quality"] == "manual"
    assert saved["naming_untranslated"] == []
    assert saved["naming_note"] == "手动编辑，待确认"


def test_naming_save_validates_entire_batch_before_changes(app):
    first, second = app.task("one.zip"), app.task("two.rar")
    assert app.post("/api/names/save", {"entries": [
        {"id": first, "name": "第一个.zip"}, {"id": second, "name": "../第二个.rar"},
    ]})[0] == 400
    assert not app.store.get(first)["chinese_filename"]


@pytest.mark.parametrize("provider", ["unknown", [], {}, 123, None])
def test_invalid_naming_provider_is_rejected_without_changing_settings(app, provider):
    before = app.config_path.read_bytes()
    assert app.post("/api/config", {"rename_translation_provider": provider})[0] == 400
    assert app.config_path.read_bytes() == before


def test_approved_name_is_persisted_for_future_upload_and_remembered(app):
    assert api_settings(app)[0] == 200
    identity = app.task("Dragon Bust.zip")
    assert app.post("/api/names/save", {"entries": [{"id": identity, "name": "巨龙胸像.zip"}]})[0] == 200
    assert app.post("/api/names/apply", {"ids": [identity]})[0] == 200
    app.run()
    task = app.store.get(identity)
    assert task["rename_target"] == "巨龙胸像.zip" and task["naming_status"] == "approved"
    assert task["naming_quality"] == "confirmed"
    assert task["upload_status"] == "waiting" and Path(task["local_path"]).exists()
    assert not app.calls
    assert json.loads((app.store.config.output_dir / "name-memory.json").read_text(encoding="utf-8"))["dragon bust.zip"] == "巨龙胸像.zip"


def test_completed_upload_can_be_renamed_in_its_original_folder_without_reupload(app, monkeypatch):
    assert api_settings(app, quark_api_parent_fid="new-default")[0] == 200
    identity = app.task("uploaded.zip")
    app.store.update(identity, upload_status="completed", upload_provider="quark_api",
                     upload_filename="uploaded.zip", upload_target="original-folder", upload_remote_fid="original-fid",
                     upload_size=123, chinese_filename="模型.zip")
    calls = []

    class API(web_ui.QuarkAPIStorageAdapter):
        def upload(self, *args, **kwargs):
            raise AssertionError("rename must not upload")

        def share_existing(self, source, target, **values):
            calls.append((source.name, target, values))
            self._remote(remote_filename=values["rename_to"], naming_status="completed")
            return SHARE

    monkeypatch.setattr(web_ui, "QuarkAPIStorageAdapter", API)
    assert app.post("/api/names/apply", {"ids": [identity]})[0] == 200
    app.run()
    assert calls[0][0:2] == ("uploaded.zip", "original-folder")
    assert calls[0][2]["remote_fid"] == "original-fid"
    assert calls[0][2]["rename_to"] == "模型.zip"
    assert app.store.get(identity)["filename"] == "uploaded.zip"
    assert app.store.get(identity)["remote_filename"] == "模型.zip"
    assert app.store.get(identity)["share_url"] == SHARE


def test_rename_failure_is_separate_from_successful_upload(app, monkeypatch):
    assert api_settings(app)[0] == 200
    identity = app.task("uploaded.zip")
    app.store.update(identity, upload_status="completed", upload_provider="quark_api",
                     upload_filename="uploaded.zip", upload_target="folder-1", upload_remote_fid="original-fid",
                     upload_size=123, chinese_filename="模型.zip", share_url=SHARE)

    class API(web_ui.QuarkAPIStorageAdapter):
        def share_existing(self, *args, **values):
            raise RuntimeError("fixture conflict")

    monkeypatch.setattr(web_ui, "QuarkAPIStorageAdapter", API)
    assert app.post("/api/names/apply", {"ids": [identity]})[0] == 200
    app.run()
    assert app.store.get(identity)["naming_status"] == "failed"
    assert app.store.get(identity)["upload_status"] == "completed"
    assert app.store.get(identity)["share_url"] == SHARE


def test_duplicate_chinese_names_are_rejected_before_cloud_actions(app):
    assert api_settings(app)[0] == 200
    ids = [app.task("one.zip"), app.task("two.zip")]
    for identity in ids:
        app.store.update(identity, chinese_filename="同名.zip")
    assert app.post("/api/names/apply", {"ids": ids})[0] == 400
    assert not app.workers


def test_upload_batch_checks_previously_approved_chinese_name_collisions(app):
    assert api_settings(app)[0] == 200
    ids = [app.task("one.zip"), app.task("two.zip")]
    for identity in ids:
        app.store.update(identity, rename_target="同名.zip")
    assert app.post("/api/upload", {"ids": ids})[0] == 400
    assert not app.workers


def test_upload_passes_approved_chinese_name_and_keeps_english_binding(app, monkeypatch):
    assert api_settings(app)[0] == 200
    identity = app.task("english.zip", rename_target="中文模型.zip", naming_status="approved")

    class API(web_ui.QuarkAPIStorageAdapter):
        def upload(self, source, target, **values):
            assert source.name == "english.zip"
            assert values == {"new_name": "中文模型.zip"}
            assert app.store.get(identity)["upload_filename"] == "english.zip"
            assert app.store.get(identity)["rename_target"] == "中文模型.zip"
            self._remote(remote_filename="中文模型.zip", naming_status="completed")
            return SHARE

    monkeypatch.setattr(web_ui, "QuarkAPIStorageAdapter", API)
    assert app.post("/api/upload", {"ids": [identity]})[0] == 200
    app.run()
    assert app.store.get(identity)["filename"] == "english.zip"
    assert app.store.get(identity)["remote_filename"] == "中文模型.zip"
    assert app.store.get(identity)["upload_status"] == "completed"


def test_download_updates_do_not_change_approved_names(app):
    identity = app.task("Dragon.zip")
    app.store.update(identity, chinese_filename="龙.zip", rename_target="龙.zip", naming_status="approved",
                     naming_quality="confirmed", naming_untranslated=[])
    app.store.event({"chat": "fixture_channel", "message_id": 123, "filename": "Dragon.zip",
                     "rename_target": "unexpected.zip", "chinese_filename": "unexpected.zip",
                     "naming_quality": "partial", "naming_untranslated": ["unexpected"]})
    assert app.store.get(identity)["rename_target"] == "龙.zip"
    restored = TransferStore(PipelineConfig.from_json(app.config_path))
    assert restored.get(identity)["chinese_filename"] == "龙.zip"
    assert restored.get(identity)["naming_quality"] == "confirmed"
    assert restored.get(identity)["naming_untranslated"] == []


def test_quark_api_settings_save_without_browser_and_blank_cookie_preserves_secret(app):
    assert api_settings(app)[0] == 200
    status, config = app.get("/api/config")
    assert status == 200 and config["quark_cookie_configured"] is True
    assert "quark_cookie" not in config
    assert "fixture-secret" not in json.dumps(config)
    assert app.post("/api/config", {"quark_cookie": "", "quark_api_file_concurrency": 3})[0] == 200
    saved = PipelineConfig.from_json(app.config_path)
    assert saved.quark_cookie == "__uid=fixture; __pus=fixture-secret"
    assert saved.quark_api_file_concurrency == 3
    assert "夸克 API" in app.get("/api/workbench")[1]["upload_target"]


@pytest.mark.parametrize("changes", [
    {"quark_api_parent_fid": ""}, {"quark_api_parent_fid": "https://pan.quark.cn/"},
    {"quark_api_timeout_seconds": 9}, {"quark_api_timeout_seconds": True},
    {"quark_api_file_concurrency": 4}, {"quark_api_file_concurrency": "2"},
    {"quark_cookie": "__uid=fixture\nbad=x"}, {"quark_cookie": ["invalid"]},
])
def test_invalid_quark_api_settings_do_not_change_config(app, changes):
    before = app.config_path.read_bytes()
    assert api_settings(app, **changes)[0] == 400
    assert app.config_path.read_bytes() == before
    assert not app.workers


def test_api_batch_upload_concurrency_partial_failure_and_durable_remote_binding(app, monkeypatch):
    assert api_settings(app, quark_api_file_concurrency=2)[0] == 200
    identities = [app.task(f"batch-{index}.zip") for index in range(3)]
    lock = threading.Lock()
    first_pair = threading.Barrier(2)
    result = {"active": 0, "peak": 0, "calls": []}

    class API(web_ui.QuarkAPIStorageAdapter):
        def upload(self, source, target):
            assert target == "folder-1"
            assert self.stop_event is app.state.stop_event
            with lock:
                result["active"] += 1
                result["peak"] = max(result["peak"], result["active"])
                ordinal = len(result["calls"])
                result["calls"].append(source.name)
            try:
                if ordinal < 2:
                    first_pair.wait(timeout=3)
                self.on_progress(20, source.stat().st_size, 10, 2)
                self.on_remote({"upload_remote_fid": source.stem, "upload_md5": "a" * 32})
                self.on_stage("link_pending")
                rows = json.loads(app.store.path.read_text(encoding="utf-8"))
                bound = next(row for row in rows if row["upload_filename"] == source.name)
                assert bound["upload_remote_fid"] == source.stem
                assert bound["upload_provider"] == "quark_api"
                assert bound["upload_target"] == target
                if source.name == "batch-1.zip":
                    raise RuntimeError("fixture share unavailable")
                return SHARE
            finally:
                with lock:
                    result["active"] -= 1

    monkeypatch.setattr(web_ui, "QuarkAPIStorageAdapter", API)
    assert app.post("/api/upload", {"ids": identities})[0] == 200
    app.run()
    assert result["peak"] == 2 and len(result["calls"]) == 3
    assert [app.store.get(identity)["upload_status"] for identity in identities] == [
        "completed", "link_pending", "completed"]
    assert app.state.error
    assert all(app.store.get(identity)["upload_speed"] == 0 for identity in identities)


def test_api_recovery_uses_persisted_fid_hash_and_size_without_local_file(app, monkeypatch):
    assert api_settings(app)[0] == 200
    identity = app.task("recover.zip")
    source = Path(app.store.get(identity)["local_path"])
    expected_size = source.stat().st_size
    source.unlink()
    app.store.update(identity, upload_status="link_pending", upload_provider="quark_api",
                     upload_filename=source.name, upload_target="folder-1", upload_remote_fid="original-fid",
                     upload_task_id="original-upload-task",
                     upload_md5="a" * 32, upload_sha1="b" * 40, upload_size=expected_size)
    calls = []

    class API(web_ui.QuarkAPIStorageAdapter):
        def upload(self, *args):
            raise AssertionError("recovery must not upload")

        def share_existing(self, source, target, **values):
            calls.append((source.name, target, values))
            return SHARE

    monkeypatch.setattr(web_ui, "QuarkAPIStorageAdapter", API)
    assert app.post("/api/upload", {"ids": [identity]})[0] == 200
    app.run()
    assert calls == [("recover.zip", "folder-1", {
        "remote_fid": "original-fid", "size": expected_size, "md5": "a" * 32, "sha1": "b" * 40,
        "upload_task_id": "original-upload-task",
    })]
    assert app.store.get(identity)["upload_status"] == "completed"
    assert app.store.get(identity)["upload_received"] == expected_size


def test_api_upload_stop_leaves_unstarted_files_waiting(app, monkeypatch):
    from xianyu_pipeline.browser_download import DownloadCancelled
    assert api_settings(app, quark_api_file_concurrency=1)[0] == 200
    identities = [app.task(f"stop-{index}.zip") for index in range(3)]
    calls = []

    class API(web_ui.QuarkAPIStorageAdapter):
        def upload(self, source, target):
            calls.append(source.name)
            self.stop_event.set()
            raise DownloadCancelled("fixture cancelled")

    monkeypatch.setattr(web_ui, "QuarkAPIStorageAdapter", API)
    assert app.post("/api/upload", {"ids": identities})[0] == 200
    app.run()
    assert calls == ["stop-0.zip"]
    assert [app.store.get(identity)["upload_status"] for identity in identities] == ["failed", "waiting", "waiting"]
    assert not app.state.error


def test_api_batch_rejects_same_local_names_before_starting(app):
    assert api_settings(app)[0] == 200
    identities = [app.task(f"original-{index}.zip") for index in range(2)]
    for index, identity in enumerate(identities):
        original = Path(app.store.get(identity)["local_path"])
        folder = original.parent / f"channel-{index}"
        folder.mkdir()
        source = folder / "material.zip"
        source.write_bytes(original.read_bytes())
        app.store.update(identity, local_path=str(source))
    status, response = app.post("/api/upload", {"ids": identities})
    assert status == 400 and "同名" in response["message"]
    assert not app.workers
    assert all(app.store.get(identity)["upload_status"] == "waiting" for identity in identities)


def test_quark_api_check_does_not_upload_or_require_telegram(app, monkeypatch):
    assert app.post("/api/quark-api-check")[0] == 400
    assert api_settings(app)[0] == 200
    values = json.loads(app.config_path.read_text(encoding="utf-8"))
    values.update(telegram_api_id=0, telegram_api_hash="")
    app.config_path.write_text(json.dumps(values), encoding="utf-8")
    calls = []

    class API(web_ui.QuarkAPIStorageAdapter):
        def check(self):
            calls.append((self.parent_fid, self.stop_event))

    monkeypatch.setattr(web_ui, "QuarkAPIStorageAdapter", API)
    assert app.post("/api/quark-api-check")[0] == 200
    app.run()
    assert calls == [("folder-1", app.state.stop_event)]
    assert not app.state.error


def catalog_page(ids, cursor, has_more=True):
    return {"messages": [{"message_id": mid, "chat": "fixture_channel",
                          "filename": f"asset-{mid}.zip", "cover_status": "pending"}
                         for mid in ids],
            "oldest_scanned_id": cursor, "has_more": has_more}


def test_stop_requires_a_specific_current_task_and_rejects_stale_same_action(app, monkeypatch):
    monkeypatch.setattr(web_ui.TelethonTelegramAdapter, "list_joined_channels", lambda self, **options: [])
    assert app.post("/api/telegram-channels-scan")[0] == 200
    first = app.get("/api/workbench")[1]
    assert first["task_id"] and not first["stop_requested"]
    assert app.post("/api/stop")[0] == 400
    assert app.post("/api/stop", {"task_id": first["task_id"], "action": "channel-cover"})[0] == 409
    assert not app.state.stop_event.is_set() and app.state.running
    app.run()

    assert app.post("/api/telegram-channels-scan")[0] == 200
    second = app.get("/api/workbench")[1]
    assert second["task_id"] != first["task_id"]
    stale = {key: first[key] for key in ("task_id", "action")}
    assert app.post("/api/stop", stale)[0] == 409
    assert not app.state.stop_event.is_set() and app.state.running
    assert app.stop()[0] == 200
    assert app.get("/api/workbench")[1]["stop_requested"]
    app.run()
    assert app.get("/api/telegram-channels")[1]["stage"] == "cancelled"
    assert not app.get("/api/workbench")[1]["stop_requested"]
    assert app.post("/api/stop", stale)[0] == 409


@pytest.mark.parametrize("action", ["channel-cover", "channel-scan", "channel-download", "upload"])
def test_background_task_allows_directory_edits_without_changing_active_source(app, action):
    app.state.running = True
    app.state.action = action
    app.state.task_id = "fixture-running-task"
    assert app.post("/api/channels", {"url": "https://t.me/other_channel", "name": "Other"})[0] == 200
    assert app.post("/api/channels/rename", {"key": "fixture_channel", "name": "Custom name"})[0] == 200
    assert app.post("/api/channels/select", {"key": "other_channel"})[0] == 409
    assert app.post("/api/telegram-channels/select", {"key": "other_channel"})[0] == 409
    assert app.post("/api/channels/remove", {"key": "fixture_channel"})[0] == 400
    assert app.post("/api/channels/remove", {"key": "other_channel"})[0] == 200
    listing = app.get("/api/channels")[1]
    assert listing["active"] == "fixture_channel" and listing["channels"][0]["name"] == "Custom name"
    assert PipelineConfig.from_json(app.config_path).source_channel_url == "https://t.me/fixture_channel"
    assert app.state.running and app.state.action == action and not app.state.stop_event.is_set()
    assert not app.workers


@pytest.mark.parametrize("path,payload", [
    ("/api/channels", {"url": "https://t.me/other_channel"}),
    ("/api/channels/rename", {"key": "fixture_channel", "name": "Custom name"}),
    ("/api/channels/remove", {"key": "other_channel"}),
])
def test_account_scan_serializes_directory_mutations(app, path, payload):
    app.channels.add("https://t.me/other_channel")
    before = app.channels.list()
    assert app.post("/api/telegram-channels-scan")[0] == 200
    status, result = app.post(path, payload)
    assert status == 409 and "获取账号频道" in result["message"]
    assert app.channels.list() == before and not app.state.stop_event.is_set()


def test_picture_prefetch_never_starts_an_independent_telegram_session(app, monkeypatch):
    app.acquire()

    class Telegram(web_ui.TelethonTelegramAdapter):
        def list_archives(self, channel, **options):
            options["on_page"](catalog_page([100], 95))
            assert app.post("/api/channel-cover", {"id": 100, "prefetch": True})[0] == 200
            assert not app.workers
            assert options["take_request"]()["kind"] == "cover"
            assert options["close_requests"]()
            return catalog_page([100], 95)

    monkeypatch.setattr(web_ui, "TelethonTelegramAdapter", Telegram)
    assert app.post("/api/channel-cover", {"id": 100, "prefetch": True})[0] == 409
    assert not app.workers and not app.state.running
    assert app.post("/api/channel-scan")[0] == 200
    app.run()
    assert not app.state.error
    assert app.post("/api/channel-cover", {"id": 100, "prefetch": True})[0] == 409
    assert not app.workers and not app.state.running


def test_catalog_worker_streams_metadata_before_cover_completion(app, monkeypatch):
    app.acquire()
    covers_started, release = threading.Event(), threading.Event()

    class Telegram(web_ui.TelethonTelegramAdapter):
        def list_archives(self, channel, **options):
            assert options["limit"] == 100
            options["on_progress"]({"stage": "scanning", "found": 2, "scanned": 6})
            page = catalog_page([100, 95], 90)
            options["on_page"](page)
            options["on_progress"]({"stage": "covers", "found": 2, "total": 2, "processed": 0})
            covers_started.set()
            assert release.wait(5)
            options["on_progress"]({"stage": "completed", "processed": 2, "ready": 1, "failed": 1})
            return page

    monkeypatch.setattr(web_ui, "TelethonTelegramAdapter", Telegram)
    assert app.post("/api/channel-scan")[0] == 200
    status, body = app.get("/api/workbench")
    assert status == 200 and body["running"]
    assert body["channel_scan"]["stage"] == "connecting"
    worker = threading.Thread(target=app.run)
    worker.start()
    try:
        assert covers_started.wait(5)
        body = app.get("/api/workbench")[1]
        assert body["running"] and body["channel_scan"]["stage"] == "covers"
        assert [row["message_id"] for row in app.get("/api/channel-messages")[1]["messages"]] == [100, 95]
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    body = app.get("/api/workbench")[1]
    assert not body["running"] and not body["error"]
    assert body["channel_scan"]["stage"] == "completed"
    assert body["channel_scan"]["found"] == body["channel_scan"]["processed"] == 2
    assert body["channel_scan"]["failed"] == 1


def test_streamed_catalog_cursor_tracks_first_and_older_scans_but_not_refresh(app, monkeypatch):
    app.acquire()
    rounds = [
        (0, [catalog_page([100, 95], 90), catalog_page([89, 85], 80)]),
        (0, [catalog_page([110], 109), catalog_page([108], 107)]),
        (80, [catalog_page([79], 75), catalog_page([70], 60, False)]),
    ]
    observed = []

    class Telegram(web_ui.TelethonTelegramAdapter):
        def list_archives(self, channel, **options):
            before, pages = rounds.pop(0)
            assert options["before_id"] == before
            for page in pages:
                options["on_page"](page)
                observed.append(app.get("/api/channel-messages")[1]["oldest_scanned_id"])
            return {**pages[-1], "messages": [row for page in pages for row in page["messages"]]}

    monkeypatch.setattr(web_ui, "TelethonTelegramAdapter", Telegram)
    for older in (False, False, True):
        assert app.post("/api/channel-scan", {"older": older})[0] == 200
        app.run()
        assert not app.state.error
    assert observed == [90, 80, 80, 80, 75, 60]
    listing = app.get("/api/channel-messages")[1]
    assert listing["oldest_scanned_id"] == 60 and not listing["has_more"]
    assert len(listing["messages"]) == 8


def test_catalog_stop_retains_streamed_messages_without_fatal_error(app, monkeypatch):
    app.acquire()
    class Telegram(web_ui.TelethonTelegramAdapter):
        def list_archives(self, channel, **options):
            options["on_page"](catalog_page([100], 95))
            options["on_progress"]({"stage": "covers", "found": 1, "total": 1})
            assert app.stop()[0] == 200
            assert self.stop_event.is_set()
            assert app.get("/api/workbench")[1]["channel_scan"]["stop_requested"]
            raise web_ui.DownloadCancelled("fixture stop")

    monkeypatch.setattr(web_ui, "TelethonTelegramAdapter", Telegram)
    assert app.post("/api/channel-scan")[0] == 200
    app.run()
    body = app.get("/api/workbench")[1]
    assert body["channel_scan"]["stage"] == "cancelled"
    assert not body["running"] and not body["error"]
    assert len(app.get("/api/channel-messages")[1]["messages"]) == 1


def test_catalog_failure_retains_metadata_and_next_scan_resets_progress(app, monkeypatch):
    app.acquire()
    class Telegram(web_ui.TelethonTelegramAdapter):
        def list_archives(self, channel, **options):
            options["on_page"](catalog_page([100], 95))
            options["on_progress"]({"stage": "covers", "found": 1, "total": 1})
            raise RuntimeError("fixture connection lost")

    monkeypatch.setattr(web_ui, "TelethonTelegramAdapter", Telegram)
    assert app.post("/api/channel-scan")[0] == 200
    app.run()
    body = app.get("/api/workbench")[1]
    assert body["channel_scan"]["stage"] == "failed"
    assert body["error"] == "fixture connection lost" and not body["running"]
    assert len(app.get("/api/channel-messages")[1]["messages"]) == 1
    assert app.post("/api/channel-scan")[0] == 200
    body = app.get("/api/workbench")[1]
    assert body["channel_scan"]["stage"] == "connecting"
    assert body["channel_scan"]["found"] == 0 and not body["error"]
    assert "elapsed_seconds" not in app.state.channel_scan
    app.run()


def test_catalog_scan_accepts_file_and_full_image_jobs_without_a_second_worker(app, monkeypatch):
    app.acquire()
    observed = []

    class Telegram(web_ui.TelethonTelegramAdapter):
        def list_archives(self, channel, **options):
            page = catalog_page([100, 99], 95)
            options["on_page"](page)
            assert app.get("/api/workbench")[1]["channel_requests_open"]
            assert app.post("/api/channel-download", {"ids": [100]})[0] == 200
            assert app.post("/api/channel-cover", {"id": 99})[0] == 200
            assert not app.workers, "Do not open a competing Telegram session"
            assert app.post("/api/channel-download", {"ids": [100]})[0] == 409
            assert app.post("/api/config", {"recent_message_limit": 2})[0] == 409
            assert not options["close_requests"]()
            while (job := options["take_request"]()) is not None:
                observed.append(job["kind"])
                if job["kind"] == "download":
                    self.on_event({"chat": "fixture_channel", "message_id": 100,
                                   "filename": "asset-100.zip", "download_status": "failed",
                                   "error": "fixture download failure"})
                    options["on_request_done"](job, {}, "fixture download failure")
                else:
                    full = options["preview_dir"] / "full.jpg"
                    full.parent.mkdir(parents=True, exist_ok=True)
                    full.write_bytes(b"\xff\xd8\xfffixture")
                    options["on_request_done"](job, {"full_cover_path": str(full),
                                                     "full_status": "ready", "full_quality": "full"}, "")
            assert options["close_requests"]()
            assert app.post("/api/channel-cover", {"id": 100})[0] == 409
            return page

    monkeypatch.setattr(web_ui, "TelethonTelegramAdapter", Telegram)
    assert app.post("/api/channel-scan")[0] == 200
    app.run()
    assert not app.state.error
    # v49 前台图片请求进入队首，不再按提交顺序排在文件任务后面。
    assert observed == ["cover", "download"]
    tasks = app.get("/api/workbench")[1]["tasks"]
    assert len(tasks) == 1 and tasks[0]["download_status"] == "failed"
    rows = app.get("/api/channel-messages")[1]["messages"]
    assert rows[1]["full_cover_url"] and rows[1]["full_status"] == "ready"
    assert app.post("/api/channel-cover", {"id": 99})[0] == 200
    assert not app.workers, "Cached full images do not start a new API job"


def test_stop_rejects_new_jobs_and_cancels_queued_file_and_full_image(app, monkeypatch):
    app.acquire()
    class Telegram(web_ui.TelethonTelegramAdapter):
        def list_archives(self, channel, **options):
            options["on_page"](catalog_page([100], 95))
            assert app.post("/api/channel-download", {"ids": [100]})[0] == 200
            assert app.post("/api/channel-cover", {"id": 100})[0] == 200
            assert app.stop()[0] == 200
            assert app.post("/api/channel-cover", {"id": 100})[0] == 409
            raise web_ui.DownloadCancelled("fixture stop")

    monkeypatch.setattr(web_ui, "TelethonTelegramAdapter", Telegram)
    assert app.post("/api/channel-scan")[0] == 200
    app.run()
    assert not app.state.error and not app.state.channel_requests_open
    assert app.get("/api/workbench")[1]["tasks"][0]["download_status"] == "cancelled"
    assert app.get("/api/channel-messages")[1]["messages"][0]["full_status"] == "cancelled"


def test_queue_storage_failure_returns_error_without_starting_or_losing_a_job(app, monkeypatch):
    app.acquire()
    class Telegram(web_ui.TelethonTelegramAdapter):
        def list_archives(self, channel, **options):
            return catalog_page([100], 95)

    monkeypatch.setattr(web_ui, "TelethonTelegramAdapter", Telegram)
    assert app.post("/api/channel-scan")[0] == 200
    app.run()

    def failing_write(event):
        raise OSError("fixture disk unavailable")

    monkeypatch.setattr(app.store, "event", failing_write)
    status, result = app.post("/api/channel-download", {"ids": [100]})
    assert status == 400 and "fixture disk unavailable" in result["message"]
    assert not app.workers and not app.state.running
    assert not app.state.channel_requests and not app.state.channel_request_keys
