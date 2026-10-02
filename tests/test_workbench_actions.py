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
    }), encoding="utf-8")
    result = SimpleNamespace(config_path=config_path, workers=[], calls=[], upload_error=False, share_error=False)
    result.store = TransferStore(PipelineConfig.from_json(config_path))

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

    result.post, result.run, result.task, result.bind = post, run, task, bind
    return result


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


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
def test_browser_retry_returns_explicit_400_without_starting_worker(app, status):
    identity = app.task(download_status=status)
    assert app.post("/api/config", {"telegram_access_mode": "browser"})[0] == 200
    response, body = app.post("/api/retry", {"id": identity})
    assert response == 400
    assert "Telegram API" in body["message"]
    assert not app.workers and not app.state.running
    assert app.store.get(identity)["download_status"] == status


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
