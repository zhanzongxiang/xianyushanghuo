from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import time

from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.product_drafts import DraftConflict, ProductDraftStore
from xianyu_pipeline.qianniu_browser import QianniuSavePending
from xianyu_pipeline import web_ui
from test_workbench_actions import app
from test_server_sync import desktop, service, settled


TEMPLATE = {
    "qianniu_shop_name": "测试素材店", "qianniu_default_price": "9.90",
    "qianniu_default_stock": 12, "qianniu_default_category": "实际店铺类目",
    "qianniu_default_format": "STL",
    "qianniu_publish_url": "https://item.publish.taobao.com/sell/publish.htm",
    "qianniu_form_selectors": {key: "#" + key for key in (
        "shop", "title", "price", "stock", "category", "description", "images", "save", "success")},
}


def imported(app, **values):
    assert app.post("/api/drafts/template", TEMPLATE)[0] == 200
    identity = app.task(**{"upload_status": "completed", "upload_provider": "quark_api",
                           "share_url": "https://pan.quark.cn/s/fixture", **values})
    # Task enrichment has a separate cache; draft creation uses the persisted task images.
    image = app.store.config.inbox_dir / "preview.jpg"
    image.write_bytes(b"\xff\xd8\xffoffline-preview")
    app.store.update(identity, image_paths=[str(image)])
    status, result = app.post("/api/drafts/create", {"ids": [identity]})
    assert status == 200, result
    return identity, app.get("/api/drafts")[1]["drafts"][0]


def test_create_edit_restart_and_immutable_image_cache(app):
    identity, row = imported(app)
    assert row["status"] == "ready" and row["price"] == "9.90" and len(row["images"]) == 1
    assert row["image_indices"] == [0]
    assert row["filename"] not in row["description"] or "【交付内容】" in row["description"]
    assert row["share_url"] not in row["description"]
    result = app.post("/api/drafts/save", {"id": row["id"], "revision": row["revision"],
                                         "values": {"title": "人工编辑标题", "stock": 19}})
    assert result[0] == 200
    assert app.post("/api/drafts/create", {"ids": [identity]})[0] == 200
    draft_store = ProductDraftStore(app.store.config.output_dir)
    saved = draft_store.get(row["id"])
    assert saved["title"] == "人工编辑标题" and saved["stock"] == 19
    Path(app.store.get(identity)["image_paths"][0]).unlink()
    image, mime = draft_store.preview(row["id"], 0)
    assert image.is_file() and mime == "image/jpeg"
    assert "image_paths" not in app.get("/api/drafts")[1]["drafts"][0]


def test_draft_revision_and_image_validation(app):
    _, row = imported(app)
    endpoint = {"id": row["id"], "revision": row["revision"]}
    for values in ({"price": "nan"}, {"price": "-1"}, {"stock": True}, {"image_indices": [0, 0]},
                   {"image_indices": [5]}, {"image_indices": [True]}, {"title": "x" * 61},
                   {"share_url": "https://evil.example"}):
        assert app.post("/api/drafts/save", {**endpoint, "values": values})[0] == 400
    assert app.post("/api/drafts/save", {**endpoint, "values": {"title": "新标题"}})[0] == 200
    assert app.post("/api/drafts/save", {**endpoint, "values": {"title": "过期编辑"}})[0] == 409
    assert app.get("/api/drafts")[1]["drafts"][0]["title"] == "新标题"


@pytest.mark.parametrize("values", [
    {"upload_status": "waiting"}, {"upload_status": "link_pending"}, {"upload_provider": "local"},
    {"share_url": ""}, {"record_removed_at": "removed"},
])
def test_import_rejects_unconfirmed_or_removed_sources(app, values):
    assert app.post("/api/drafts/template", TEMPLATE)[0] == 200
    identity = app.task(**{"upload_status": "completed", "upload_provider": "quark_api", "share_url": "https://pan.quark.cn/s/a", **values})
    assert app.post("/api/drafts/create", {"ids": [identity]})[0] == 400
    assert app.get("/api/drafts")[1]["drafts"] == []


@pytest.mark.parametrize("change", [
    {"qianniu_default_price": "NaN"}, {"qianniu_default_price": "1.234"},
    {"qianniu_default_stock": 0}, {"qianniu_auto_create_draft": "true"},
    {"qianniu_auto_save_draft": True}, {"qianniu_publish_url": "https://taobao.com.evil.test"},
    {"qianniu_publish_url": "https://item.publish.taobao.com@evil.test"},
    {"qianniu_form_selectors": {"unexpected": "button"}},
])
def test_invalid_templates_preserve_configuration(app, change):
    before = app.config_path.read_bytes()
    assert app.post("/api/drafts/template", change)[0] == 400
    assert before == app.config_path.read_bytes()
    assert app.post("/api/config", change)[0] == 400
    assert before == app.config_path.read_bytes()


def test_upload_creates_draft_without_overwriting_previous_edits(app):
    assert app.post("/api/drafts/template", {**TEMPLATE, "qianniu_auto_create_draft": True})[0] == 200
    identity = app.task()
    assert app.post("/api/upload", {"ids": [identity]})[0] == 200
    app.run()
    row = app.get("/api/drafts")[1]["drafts"][0]
    assert app.store.get(identity)["upload_status"] == "completed"
    assert row["task_id"] == identity
    assert app.post("/api/drafts/save", {"id": row["id"], "revision": row["revision"], "values": {"title": "编辑保留"}})[0] == 200
    assert app.post("/api/drafts/create", {"ids": [identity]})[0] == 200
    assert app.get("/api/drafts")[1]["drafts"][0]["title"] == "编辑保留"


def test_editorial_failure_does_not_reclassify_successful_upload(app, monkeypatch):
    assert app.post("/api/drafts/template", {**TEMPLATE, "qianniu_auto_create_draft": True})[0] == 200
    identity = app.task()
    monkeypatch.setattr(ProductDraftStore, "create", lambda *args: (_ for _ in ()).throw(OSError("fixture disk failure")))
    assert app.post("/api/upload", {"ids": [identity]})[0] == 200
    app.run()
    assert app.store.get(identity)["upload_status"] == "completed"
    assert app.store.get(identity)["share_url"]
    assert len(app.calls) == 1 and app.state.error


@pytest.mark.parametrize("outcome,expected", [("ok", "saved"), ("pending", "pending"), ("error", "attention")])
def test_publish_records_actual_adapter_result_and_prevents_repeated_save(app, monkeypatch, outcome, expected):
    identity, row = imported(app)
    calls = []

    class Adapter:
        def __init__(self, *args):
            pass

        def save(self, data, images):
            calls.append((data, images))
            if outcome == "pending":
                raise QianniuSavePending("fixture receipt missing")
            if outcome == "error":
                raise ValueError("fixture wrong shop")
            return {"remote_url": "https://qn.taobao.com/drafts/fixture", "confirmed_by": "receipt"}

    monkeypatch.setattr(web_ui, "QianniuDraftAdapter", Adapter)
    payload = {"entries": [{"id": row["id"], "revision": row["revision"]}]}
    assert app.post("/api/drafts/publish", payload)[0] == 200
    app.run()
    saved = app.get("/api/drafts")[1]["drafts"][0]
    assert saved["status"] == expected and len(calls) == 1 and calls[0][1][0].is_file()
    assert app.store.get(identity)["upload_status"] == "completed"
    if expected in {"saved", "pending"}:
        assert app.post("/api/drafts/publish", {"entries": [{"id": row["id"], "revision": saved["revision"]}]})[0] == 400
        assert app.post("/api/drafts/save", {"id": row["id"], "revision": saved["revision"], "values": {"title": "wrong"}})[0] == 409
    if expected == "pending":
        assert app.post("/api/drafts/confirm", {"id": row["id"], "revision": saved["revision"], "confirm": True,
                                              "remote_url": "https://qn.taobao.com/drafts/fixture"})[0] == 200
        assert app.get("/api/drafts")[1]["drafts"][0]["confirmed_by"] == "manual"


def test_edit_while_publish_is_queued_does_not_submit_stale_data(app, monkeypatch):
    _, row = imported(app)
    monkeypatch.setattr(web_ui.QianniuDraftAdapter, "save", lambda *args: pytest.fail("Stale data submitted"))
    assert app.post("/api/drafts/publish", {"entries": [{"id": row["id"], "revision": row["revision"]}]})[0] == 200
    assert app.post("/api/drafts/save", {"id": row["id"], "revision": row["revision"], "values": {"title": "更正标题"}})[0] == 200
    app.run()
    assert app.get("/api/drafts")[1]["drafts"][0]["title"] == "更正标题"


def test_source_changed_and_missing_adapter_reject_before_worker(app):
    identity, row = imported(app)
    app.store.update(identity, upload_status="deleted", share_url="")
    assert app.post("/api/drafts/publish", {"entries": [{"id": row["id"], "revision": row["revision"]}]})[0] == 400
    assert not app.workers
    assert app.post("/api/drafts/template", {"qianniu_form_selectors": {}})[0] == 200
    assert app.post("/api/drafts/publish", {"entries": [{"id": row["id"], "revision": row["revision"]}]})[0] == 400


def test_upload_can_automatically_save_complete_product_data(app, monkeypatch):
    calls = []
    assert app.post("/api/drafts/template", {**TEMPLATE, "qianniu_auto_create_draft": True,
                                            "qianniu_auto_save_draft": True})[0] == 200
    identity = app.task()
    image = app.store.config.inbox_dir / "photo.jpg"
    image.write_bytes(b"\xff\xd8\xffpreview")
    app.store.update(identity, image_paths=[str(image)])
    monkeypatch.setattr(web_ui.QianniuDraftAdapter, "save", lambda self, row, images:
                        calls.append(row["task_id"]) or {"remote_url": "https://qn.taobao.com/draft/fixture",
                                                       "confirmed_by": "receipt"})
    assert app.post("/api/upload", {"ids": [identity]})[0] == 200
    app.run()
    assert calls == [identity] and app.store.get(identity)["upload_status"] == "completed"
    assert app.get("/api/drafts")[1]["drafts"][0]["status"] == "saved"


def test_persistence_error_after_receipt_blocks_repeated_creation(app, monkeypatch):
    _, row = imported(app)
    original = ProductDraftStore.update_status

    def update(self, identity, status, **values):
        if status == "saved":
            raise OSError("fixture persistence error after receipt")
        return original(self, identity, status, **values)

    monkeypatch.setattr(ProductDraftStore, "update_status", update)
    monkeypatch.setattr(web_ui.QianniuDraftAdapter, "save", lambda *args:
                        {"remote_url": "https://qn.taobao.com/draft/fixture", "confirmed_by": "receipt"})
    assert app.post("/api/drafts/publish", {"entries": [{"id": row["id"], "revision": row["revision"]}]})[0] == 200
    app.run()
    saved = app.get("/api/drafts")[1]["drafts"][0]
    assert saved["status"] == "pending"
    assert app.post("/api/drafts/publish", {"entries": [{"id": saved["id"], "revision": saved["revision"]}]})[0] == 400


def test_interrupted_save_is_pending_after_restart(app):
    _, row = imported(app)
    store = ProductDraftStore(app.store.config.output_dir)
    store.update_status(row["id"], "saving")
    recovered = ProductDraftStore(app.store.config.output_dir).get(row["id"])
    assert recovered["status"] == "pending" and recovered["revision"] > row["revision"]
    with pytest.raises(DraftConflict):
        store.edit(row["id"], store.get(row["id"])["revision"], {"title": "no"})


def test_drafts_and_images_sync_without_local_paths_or_credentials(tmp_path, service):
    first, second = desktop(tmp_path / "first", service), desktop(tmp_path / "second", service)
    for client in (first, second):
        client.sync.drafts = ProductDraftStore(client.config.output_dir)
    image = first.config.inbox_dir / "image.jpg"
    image.write_bytes(b"\xff\xd8\xffoffline-image")
    identity = "d" * 24
    task = dict(id=identity, filename="model.zip", upload_status="completed", upload_provider="quark_api",
                share_url="https://pan.quark.cn/s/a", image_paths=[str(image)])
    row = first.sync.drafts.create(task, replace(first.config, **TEMPLATE))
    settled(first)
    settled(second)
    foreign = second.sync.drafts.snapshot()["drafts"][0]
    assert foreign["title"] == row["title"] and foreign["remote_device"] and foreign["images"][0]["url"]
    assert "image_paths" not in foreign and str(first.config.inbox_dir) not in str(foreign)
    with pytest.raises(ValueError):
        second.sync.drafts.get(foreign["id"])
    assert "qianniu_form_selectors" not in str(first.sync.collect())
    first.sync.close()
    second.sync.close()


def test_local_drafts_do_not_require_shop_and_binding_preserves_edits_and_identity(app):
    template = {**TEMPLATE, "qianniu_shop_name": "", "qianniu_auto_create_draft": True}
    assert app.post("/api/drafts/template", template)[0] == 200
    identity = app.task(upload_status="completed", upload_provider="quark_api",
                        share_url="https://pan.quark.cn/s/local")
    assert app.post("/api/drafts/create", {"ids": [identity]})[0] == 200
    row = app.get("/api/drafts")[1]["drafts"][0]
    assert row["shop_name"] == "" and "目标店铺" not in row["missing"]
    assert app.post("/api/drafts/save", {"id": row["id"], "revision": row["revision"],
                                       "values": {"title": "保留人工标题"}})[0] == 200
    assert app.post("/api/drafts/publish", {"entries": [{"id": row["id"], "revision": row["revision"] + 1}]})[0] == 400
    assert app.post("/api/drafts/template", {"qianniu_shop_name": "识别测试店"})[0] == 200
    saved = app.get("/api/drafts")[1]["drafts"][0]
    assert saved["shop_name"] == "识别测试店" and saved["title"] == "保留人工标题"
    assert saved["id"] == row["id"] and saved["revision"] == row["revision"] + 2
    assert app.post("/api/drafts/create", {"ids": [identity]})[1]["ids"] == [row["id"]]
    assert len(app.get("/api/drafts")[1]["drafts"]) == 1
    assert app.post("/api/drafts/template", {"qianniu_shop_name": "第二家店"})[0] == 200
    assert app.get("/api/drafts")[1]["drafts"][0]["shop_name"] == "识别测试店"


def test_shop_detection_requires_explicit_confirmation_and_keeps_other_template_fields(app, monkeypatch):
    candidates = [{"name": "自动识别素材店", "shop_id": "123456", "source": "页面店铺信息"}]
    calls = []

    def login(self, on_status):
        calls.append((self.config.qianniu_publish_url, dict(self.config.qianniu_form_selectors)))
        on_status({"stage": "waiting", "error": "", "candidates": []})
        return candidates

    monkeypatch.setattr(web_ui.QianniuDraftAdapter, "login", login)
    before = app.config_path.read_bytes()
    assert app.post("/api/qianniu/login", {"url": "", "shop_selector": ""})[0] == 200
    assert app.post("/api/qianniu/shop/confirm", {"ticket": "wrong", "index": 0})[0] == 409
    app.run()
    snapshot = app.get("/api/drafts")[1]
    detected = snapshot["qianniu_shop"]
    assert detected["stage"] == "detected" and detected["candidates"] == candidates
    assert app.config_path.read_bytes() == before
    assert calls == [("", {})]
    assert app.post("/api/drafts/template", {"qianniu_default_price": "12.50"})[0] == 200
    assert app.post("/api/qianniu/shop/confirm", {"ticket": detected["ticket"], "index": 0})[0] == 200
    config = PipelineConfig.from_json(app.config_path)
    assert config.qianniu_shop_name == candidates[0]["name"] and config.qianniu_default_price == "12.50"
    assert app.get("/api/drafts")[1]["qianniu_shop"]["stage"] == "confirmed"
    assert app.post("/api/qianniu/shop/confirm", {"ticket": detected["ticket"], "index": 0})[0] == 409


@pytest.mark.parametrize("change", ["ticket", "index", "expired", "shop_changed"])
def test_shop_confirmation_rejects_stale_results(app, change):
    app.state.qianniu_shop = {
        "stage": "detected", "ticket": "fixture", "base_shop": "", "detected_at": time.time(),
        "candidates": [{"name": "识别店铺", "shop_id": "123", "source": "页面店铺信息"}], "error": "",
    }
    arguments = {"ticket": "fixture", "index": 0}
    if change == "ticket":
        arguments["ticket"] = "other"
    elif change == "index":
        arguments["index"] = -1
    elif change == "expired":
        app.state.qianniu_shop["detected_at"] -= 901
    else:
        assert app.post("/api/drafts/template", {"qianniu_shop_name": "人工修改店铺"})[0] == 200
    before = app.config_path.read_bytes()
    assert app.post("/api/qianniu/shop/confirm", arguments)[0] == 409
    assert app.config_path.read_bytes() == before


@pytest.mark.parametrize("outcome,stage", [("failed", "failed"), ("cancelled", "cancelled")])
def test_failed_or_cancelled_shop_detection_does_not_claim_login_success(app, monkeypatch, outcome, stage):
    def login(self, on_status):
        if outcome == "cancelled":
            raise web_ui.DownloadCancelled("fixture stop")
        raise ValueError("fixture no shop")

    monkeypatch.setattr(web_ui.QianniuDraftAdapter, "login", login)
    assert app.post("/api/qianniu/login")[0] == 200
    app.run()
    detected = app.get("/api/drafts")[1]["qianniu_shop"]
    assert detected["stage"] == stage and not detected["candidates"] and detected["error"]
    assert PipelineConfig.from_json(app.config_path).qianniu_shop_name == ""


@pytest.mark.parametrize("arguments", [
    {"shop_selector": False}, {"shop_selector": []}, {"shop_selector": "x" * 1001},
    {"url": None}, {"url": "https://taobao.com.evil.test/"}, {"name": "伪造店铺"},
])
def test_detection_rejects_invalid_overrides_before_launching_browser(app, arguments):
    before = app.config_path.read_bytes()
    assert app.post("/api/qianniu/login", arguments)[0] == 400
    assert not app.workers and app.config_path.read_bytes() == before


def test_confirmation_storage_failure_keeps_shop_unbound_and_result_retryable(app, monkeypatch):
    app.state.qianniu_shop = {
        "stage": "detected", "ticket": "fixture", "base_shop": "", "detected_at": time.time(),
        "candidates": [{"name": "识别店铺", "shop_id": "123", "source": "页面店铺信息"}], "error": "",
    }
    arguments = {"ticket": "fixture", "index": 0}
    with monkeypatch.context() as patch:
        patch.setattr(ProductDraftStore, "bind_unassigned_shop",
                      lambda *args: (_ for _ in ()).throw(OSError("fixture disk failure")))
        assert app.post("/api/qianniu/shop/confirm", arguments)[0] == 400
        assert PipelineConfig.from_json(app.config_path).qianniu_shop_name == ""
        assert app.get("/api/drafts")[1]["qianniu_shop"]["stage"] == "detected"
    assert app.post("/api/qianniu/shop/confirm", arguments)[0] == 200
