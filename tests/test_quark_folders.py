from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from xianyu_pipeline import quark_api, web_ui
from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.quark_api import QuarkAPIError, QuarkAPIStorageAdapter
from test_workbench_actions import app, api_settings


COOKIE = "__uid=fixture; __pus=fixture-secret"


@pytest.fixture
def directory(monkeypatch):
    fixture = SimpleNamespace(calls=[], rows=[], metadata={}, status=200, data=None)
    original = httpx.Client

    def handle(request):
        fixture.calls.append(request)
        assert request.method == "GET" and request.url.path.endswith("/file/sort")
        assert request.headers["cookie"] == COOKIE
        return httpx.Response(fixture.status, json={"status": fixture.status,
            "message": COOKIE, "data": fixture.data if fixture.data is not None else {"list": fixture.rows},
            "metadata": fixture.metadata})

    monkeypatch.setattr(quark_api.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    fixture.client = QuarkAPIStorageAdapter(COOKIE)
    return fixture


def test_only_directories_are_returned_without_remote_private_fields(directory):
    directory.rows = [
        {"fid": "a", "file_name": "Models", "dir": True, "pdir_fid": "parent", "private": COOKIE},
        {"fid": "b", "file_name": "Images", "file_type": 0},
        {"fid": "c", "file_name": "model.rar", "dir": False, "file_type": 1},
        {"fid": "d", "file_name": "unknown", "file_type": False},
        {"fid": "a", "file_name": "Models", "dir": True},
    ]
    result = directory.client.list_folders("parent")
    assert result == {"parent_fid": "parent", "folders": [{"fid": "a", "name": "Models"},
        {"fid": "b", "name": "Images"}], "page": 1, "has_more": False, "next_page": None}
    assert COOKIE not in json.dumps(result)
    assert directory.calls[0].url.params["pdir_fid"] == "parent"
    assert directory.client._api_client is None


def test_pagination_counts_files_even_when_no_folders_are_visible(directory):
    directory.rows = [{"fid": str(n), "file_name": "file.zip", "file_type": 1} for n in range(100)]
    assert directory.client.list_folders()["next_page"] == 2
    directory.metadata = {"_total": 200}
    result = directory.client.list_folders(page=2)
    assert result["folders"] == [] and result["next_page"] is None
    directory.rows = [{"fid": "last-folder", "file_name": "Last", "dir": True}]
    assert directory.client.list_folders(page=3)["folders"][0]["fid"] == "last-folder"


@pytest.mark.parametrize("parent,page", [(None, 1), ([], 1), ("../", 1), ("", 1),
    ("0", 0), ("0", True), ("0", "1"), ("0", 1001)])
def test_invalid_directory_request_never_contacts_quark(directory, parent, page):
    with pytest.raises(ValueError):
        directory.client.list_folders(parent, page=page)
    assert not directory.calls


@pytest.mark.parametrize("data", [[], {"list": None}, {"list": ["bad"]},
    {"list": [{"fid": None, "file_name": "bad", "dir": True}]},
    {"list": [{"fid": "a", "file_name": "bad", "dir": True, "pdir_fid": "different"}]}])
def test_malformed_list_is_not_treated_as_an_empty_valid_directory(directory, data):
    directory.data = data
    with pytest.raises(QuarkAPIError):
        directory.client.list_folders()


def test_expired_cookie_returns_sanitized_error(directory):
    directory.status = 401
    with pytest.raises(QuarkAPIError, match="登录失效") as error:
        directory.client.list_folders()
    assert COOKIE not in str(error.value)


def test_endpoint_reads_saved_cookie_without_changing_config_or_running_task(app, monkeypatch):
    assert api_settings(app)[0] == 200
    before = app.config_path.read_bytes()
    calls = []

    def folders(self, parent_fid, *, page):
        calls.append((self.cookie, parent_fid, page, self.stop_event))
        return {"parent_fid": parent_fid, "folders": [], "page": page, "next_page": None, "has_more": False}

    monkeypatch.setattr(web_ui.QuarkAPIStorageAdapter, "list_folders", folders)
    app.state.running = True
    app.state.stop_event.set()
    status, result = app.post("/api/quark-folders", {"parent_fid": "nested", "page": 2})
    assert status == 200 and result["parent_fid"] == "nested"
    assert calls[0][:3] == (COOKIE, "nested", 2) and not calls[0][3].is_set()
    assert not app.workers and app.state.running and app.state.stop_event.is_set()
    assert app.config_path.read_bytes() == before and COOKIE not in json.dumps(result)


def test_unsaved_cookie_can_browse_without_saving_any_fields(app, monkeypatch):
    calls = []
    before = app.config_path.read_bytes()
    monkeypatch.setattr(web_ui.QuarkAPIStorageAdapter, "list_folders",
        lambda self, parent_fid, **kw: calls.append(self.cookie) or {"folders": []})
    assert app.post("/api/quark-folders", {"quark_cookie": COOKIE})[0] == 200
    assert calls == [COOKIE] and app.config_path.read_bytes() == before
    for payload in ({}, {"quark_cookie": "********"}, {"quark_cookie": []},
                    {"quark_cookie": COOKIE, "endpoint": "https://example.invalid"}):
        assert app.post("/api/quark-folders", payload)[0] == 400
    assert len(calls) == 1


def test_endpoint_reports_directory_error_without_starting_worker(app, monkeypatch):
    def fail(*args, **kw):
        raise QuarkAPIError("目录不可访问")

    monkeypatch.setattr(web_ui.QuarkAPIStorageAdapter, "list_folders", fail)
    assert app.post("/api/quark-folders", {"quark_cookie": COOKIE}) == (502, {"message": "目录不可访问"})
    assert not app.workers and not app.state.running


def test_selected_directory_survives_reload_and_is_used_as_upload_target(app):
    assert api_settings(app, quark_api_parent_fid="models", quark_api_folder_path="/素材/模型")[0] == 200
    saved = PipelineConfig.from_json(app.config_path)
    assert saved.cloud_upload_target() == "models" and saved.quark_api_folder_path == "/素材/模型"
    assert app.get("/api/config")[1]["quark_api_folder_path"] == "/素材/模型"
    assert app.post("/api/config", {"quark_api_parent_fid": "other"})[0] == 200
    assert PipelineConfig.from_json(app.config_path).quark_api_folder_path == ""
    assert app.post("/api/config", {"quark_api_parent_fid": "0", "quark_api_folder_path": "/"})[0] == 200
    assert PipelineConfig.from_json(app.config_path).cloud_upload_target() == "0"


@pytest.mark.parametrize("path", [[], None, "bad\npath", "x" * 8193])
def test_bad_display_path_does_not_modify_saved_config(app, path):
    before = app.config_path.read_bytes()
    assert app.post("/api/config", {"quark_api_folder_path": path})[0] == 400
    assert app.config_path.read_bytes() == before
