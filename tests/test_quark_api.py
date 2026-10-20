from __future__ import annotations

import base64
import hashlib
import json
import traceback
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from threading import Event

import httpx
import pytest

from xianyu_pipeline import quark_api
from xianyu_pipeline.browser_download import DownloadCancelled
from xianyu_pipeline.quark_api import QuarkAPIError, QuarkAPIStorageAdapter, validate_cookie


COOKIE = "__uid=fixture-user; __pus=fixture-secret"
SHARE = "https://pan.quark.cn/s/Fixture"


@pytest.fixture
def service(tmp_path, monkeypatch):
    source = tmp_path / "material.rar"
    source.write_bytes(bytes(range(256)) * 2400)
    fixture = SimpleNamespace(source=source, calls=[], parts={}, rows=[], instant=False,
                              merged=False, merge_status=200, merge_body=None, fail_part=0,
                              retries=0, size_delta=0, fail_share=False, share_url=SHARE,
                              waits=[], auth_keys=[], stages=[], metadata={}, progress=[], phases=[],
                              pre_fid="file-1", hash_fid="", publish=True, receipt=None, pages=None,
                              lose_rename_response=False)
    original_client = httpx.Client

    def ok(data, metadata=None):
        return httpx.Response(200, json={"status": 200, "code": 0, "data": data, "metadata": metadata or {}})

    def remote_row():
        body = source.read_bytes()
        return {"fid": "file-1", "file_name": source.name, "size": len(body) + fixture.size_delta,
                "pdir_fid": "folder-1", "md5": hashlib.md5(body).hexdigest(),
                "sha1": hashlib.sha1(body).hexdigest()}

    fixture.row = remote_row

    def directory_row():
        return {key: value for key, value in remote_row().items() if key not in {"md5", "sha1"}}

    def handle(request):
        path = request.url.path.removeprefix("/1/clouddrive/")
        body = request.read()
        fixture.calls.append((request, body))
        if request.url.host == "drive-pc.quark.cn":
            assert request.headers["cookie"] == COOKIE
            payload = json.loads(body) if body else {}
            if path == "file/sort":
                rows = fixture.rows if fixture.pages is None else fixture.pages.get(int(request.url.params["_page"]), [])
                return ok({"list": rows})
            if path == "file/upload/pre":
                assert payload["size"] == source.stat().st_size
                assert payload["pdir_fid"] == "folder-1"
                return ok({"task_id": "upload-task", "auth_info": "auth-info", "upload_id": "upload-id",
                           "bucket": "ul-zb", "obj_key": "objects/material", "callback": {"callbackUrl": "fixture"},
                           "fid": fixture.pre_fid}, {"part_size": 256 * 1024})
            if path == "file/update/hash":
                assert payload["md5"] == remote_row()["md5"]
                assert payload["sha1"] == remote_row()["sha1"]
                if fixture.instant and fixture.publish:
                    fixture.rows = [directory_row()]
                return ok({"finish": fixture.instant, "fid": fixture.hash_fid})
            if path == "file/upload/auth":
                fixture.auth_keys.append(payload["auth_meta"])
                return ok({"auth_key": "OSS fixture-authorization"})
            if path == "file/upload/finish":
                assert fixture.merged or fixture.instant, "finish must never hide failed multipart merge"
                assert payload["task_id"] == "upload-task"
                if fixture.publish and not fixture.rows:
                    fixture.rows = [directory_row()]
                if fixture.receipt is None:
                    row = remote_row()
                    fixture.receipt = {
                        "task_id": "upload-task", "finish": True, "fid": row["fid"], "size": row["size"],
                        "md5": base64.b64encode(bytes.fromhex(row["md5"])).decode(), "sha1": row["sha1"],
                        "file_struct": {"platform_source": "fixture"},
                    }
                return ok(fixture.receipt)
            if path == "file":
                raise AssertionError("GET file ignores fids on the real service; use the target directory")
            if path == "file/rename":
                row = next(row for row in fixture.rows if row["fid"] == payload["fid"])
                row["file_name"] = payload["file_name"]
                if fixture.lose_rename_response:
                    raise httpx.ReadTimeout("fixture lost rename response", request=request)
                return ok({})
            if path == "share":
                assert fixture.rows
                if fixture.fail_share:
                    return httpx.Response(200, json={"status": 400, "code": 1, "message": COOKIE})
                assert payload["fid_list"] == ["file-1"]
                return ok({"task_id": "share-task"})
            if path == "task":
                return ok({"status": 2, "share_id": "share-id"})
            if path == "share/password":
                return ok({"share_url": fixture.share_url})
            raise AssertionError(path)
        assert request.url.host == "ul-zb.pds.quark.cn"
        assert "cookie" not in request.headers
        assert request.headers["authorization"] == "OSS fixture-authorization"
        if request.method == "PUT":
            number = int(request.url.params["partNumber"])
            assert int(request.headers["content-length"]) == len(body)
            if number == fixture.fail_part:
                fixture.retries += 1
                fixture.fail_part = 0
                raise httpx.ReadTimeout(COOKIE, request=request)
            fixture.parts[number] = body
            return httpx.Response(200, headers={"ETag": f'"{hashlib.md5(body).hexdigest()}"'})
        assert request.method == "POST"
        if fixture.merge_body is not None:
            return httpx.Response(fixture.merge_status, content=fixture.merge_body)
        if fixture.merge_status not in {200, 203}:
            return httpx.Response(fixture.merge_status, text=COOKIE)
        parsed = ET.fromstring(body)
        for index, part in enumerate(parsed.findall("Part"), 1):
            assert part.findtext("PartNumber") == str(index)
            assert part.findtext("ETag") == f'"{hashlib.md5(fixture.parts[index]).hexdigest()}"'
        assert base64.b64encode(hashlib.md5(body).digest()).decode() == request.headers["Content-MD5"]
        callback = json.loads(base64.b64decode(request.headers["x-oss-callback"]))
        assert callback == {"callbackUrl": "fixture"}
        fixture.merged = True
        return httpx.Response(fixture.merge_status, content=b"<CompleteMultipartUploadResult/>")

    def clients(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(quark_api.httpx, "Client", clients)
    cloud = QuarkAPIStorageAdapter(
        COOKIE, parent_fid="folder-1", on_stage=fixture.stages.append,
        on_remote=fixture.metadata.update, on_progress=lambda *values: fixture.progress.append(values),
        on_phase=fixture.phases.append,
    )
    monkeypatch.setattr(cloud, "_wait", fixture.waits.append)
    fixture.cloud = cloud
    return fixture


@pytest.mark.parametrize("body_size", [1024, 614400])
def test_upload_streams_exact_bytes_merges_and_confirms_before_share(service, body_size):
    service.source.write_bytes(b"x" * body_size)
    assert service.cloud.upload(service.source) == SHARE
    assert b"".join(service.parts.values()) == service.source.read_bytes()
    assert len(service.parts) == (body_size + 256 * 1024 - 1) // (256 * 1024)
    assert service.metadata["upload_remote_fid"] == "file-1"
    assert service.metadata["upload_task_id"] == "upload-task"
    assert service.metadata["upload_md5"] == hashlib.md5(service.source.read_bytes()).hexdigest()
    assert service.stages[-1] == "completed"
    assert service.phases == ["hashing", "checking", "uploading", "merging", "checking", "sharing", "completed"]
    assert service.progress[-1][0:2] == (body_size, body_size)
    assert all(0 <= current <= total for current, total, *_ in service.progress)
    assert all("x-oss-hash-ctx" not in meta for meta in service.auth_keys)
    assert "x-oss-callback:" in service.auth_keys[-1]


def test_instant_upload_skips_oss_and_checks_remote_file(service):
    service.instant = True
    assert service.cloud.upload(service.source) == SHARE
    assert not service.parts and not service.merged and not service.auth_keys
    assert "uploading" not in service.phases
    assert service.metadata["upload_remote_fid"] == "file-1"
    assert "md5" not in service.rows[0] and "sha1" not in service.rows[0]
    finish = [json.loads(body) for req, body in service.calls if req.url.path.endswith("upload/finish")]
    assert finish == [{"task_id": "upload-task"}]


def test_existing_identical_file_is_shared_without_pre_upload(service):
    service.rows = [service.row()]
    assert service.cloud.upload(service.source) == SHARE
    assert not any(req.url.path.endswith("upload/pre") for req, _ in service.calls)
    assert service.stages[-1] == "completed"


@pytest.mark.parametrize("change", [{"size": 12}, {"md5": "0" * 32}, {"md5": "", "sha1": ""}])
def test_same_name_with_different_or_unverifiable_content_is_rejected(service, change):
    service.rows = [{**service.row(), **change}]
    with pytest.raises(QuarkAPIError):
        service.cloud.upload(service.source)
    assert not any(req.method == "POST" for req, _ in service.calls)
    assert service.stages[-1] == "uploading"


def test_existing_file_with_base64_hash_can_be_compared(service):
    row = service.row()
    service.rows = [{**row, "md5": base64.b64encode(bytes.fromhex(row["md5"])).decode(),
                    "sha1": row["sha1"].upper()}]
    assert service.cloud.upload(service.source) == SHARE
    assert not any(req.url.path.endswith("upload/pre") for req, _ in service.calls)


@pytest.mark.parametrize("origin", ["pre", "hash", "finish"])
def test_remote_fid_is_saved_before_directory_confirmation_can_fail(service, origin):
    service.instant = True
    service.publish = False
    service.pre_fid = "file-1" if origin == "pre" else ""
    service.hash_fid = "file-1" if origin == "hash" else ""
    with pytest.raises(QuarkAPIError, match="尚未确认"):
        service.cloud.upload(service.source)
    assert service.metadata["upload_remote_fid"] == "file-1"
    assert service.metadata["upload_task_id"] == "upload-task"
    assert service.stages[-1] == "link_pending"
    assert not any(req.url.path.endswith("/share") for req, _ in service.calls)


@pytest.mark.parametrize("origin", ["pre", "hash"])
def test_early_fid_survives_missing_completion_receipt(service, monkeypatch, origin):
    service.instant = True
    service.pre_fid = "file-1" if origin == "pre" else ""
    service.hash_fid = "file-1" if origin == "hash" else ""
    original = service.cloud._data

    def fail(method, endpoint, **kwargs):
        if endpoint == ("file/update/hash" if origin == "pre" else "file/upload/finish"):
            raise QuarkAPIError("test response lost")
        return original(method, endpoint, **kwargs)

    monkeypatch.setattr(service.cloud, "_data", fail)
    with pytest.raises(QuarkAPIError, match="response lost"):
        service.cloud.upload(service.source)
    assert service.metadata["upload_remote_fid"] == "file-1"
    assert service.metadata["upload_task_id"] == "upload-task"
    assert service.stages[-1] == "link_pending"


def test_part_retry_is_bounded_and_progress_does_not_double_count(service):
    service.fail_part = 2
    assert service.cloud.upload(service.source) == SHARE
    assert service.retries == 1
    assert b"".join(service.parts.values()) == service.source.read_bytes()
    assert service.waits == [1]
    receipts = [values[0] for values in service.progress]
    assert receipts == sorted(receipts)
    assert max(receipts) == service.source.stat().st_size


@pytest.mark.parametrize("status,body", [(500, None), (200, b"<Error><Code>InternalError</Code></Error>"),
                                       (200, b"<broken")])
def test_merge_errors_never_finish_or_share_and_preserve_uncertainty(service, status, body):
    service.merge_status, service.merge_body = status, body
    with pytest.raises(QuarkAPIError):
        service.cloud.upload(service.source)
    assert service.stages[-1] == "link_pending"
    assert not any(req.url.path.endswith(("upload/finish", "/share")) for req, _ in service.calls)


def test_203_callback_failure_is_only_success_after_finish_and_remote_verification(service):
    service.merge_status = 203
    service.size_delta = 1
    with pytest.raises(QuarkAPIError, match="大小"):
        service.cloud.upload(service.source)
    assert service.stages[-1] == "link_pending"
    assert not any(req.url.path.endswith("/share") for req, _ in service.calls)


def test_share_failure_retains_fid_and_recovery_never_reads_local_file_or_reuploads(service, caplog):
    service.fail_share = True
    with pytest.raises(QuarkAPIError) as error:
        service.cloud.upload(service.source)
    assert COOKIE not in "".join(traceback.format_exception(error.value))
    assert COOKIE not in caplog.text
    assert service.stages[-1] == "link_pending"
    metadata = service.metadata.copy()
    original_size = service.source.stat().st_size
    service.source.unlink()
    service.calls.clear()
    service.fail_share = False
    assert service.cloud.share_existing(
        service.source, "folder-1", remote_fid=metadata["upload_remote_fid"], size=original_size,
        md5=metadata["upload_md5"], sha1=metadata["upload_sha1"],
    ) == SHARE
    assert not any(req.url.host.endswith(".pds.quark.cn") or "/upload/" in req.url.path
                   for req, _ in service.calls)


def test_legacy_recovery_uses_upload_receipt_when_fid_and_directory_hashes_are_missing(service):
    service.instant = True
    service.fail_share = True
    with pytest.raises(QuarkAPIError):
        service.cloud.upload(service.source)
    metadata = service.metadata.copy()
    size = service.source.stat().st_size
    service.source.unlink()
    service.calls.clear()
    service.fail_share = False
    assert service.cloud.share_existing(
        service.source, "folder-1", upload_task_id=metadata["upload_task_id"], size=size,
        md5=metadata["upload_md5"], sha1=metadata["upload_sha1"],
    ) == SHARE
    assert service.metadata["upload_remote_fid"] == "file-1"
    assert "md5" not in service.rows[0] and "sha1" not in service.rows[0]
    assert not any(req.url.host.endswith(".pds.quark.cn") or req.method == "PUT" or
                   req.url.path.endswith(("upload/pre", "update/hash", "upload/auth"))
                   for req, _ in service.calls)
    finish = [json.loads(body) for req, body in service.calls if req.url.path.endswith("upload/finish")]
    assert finish == [{"task_id": "upload-task"}]


@pytest.mark.parametrize("changes", [
    {"task_id": "another-task"}, {"finish": False}, {"fid": ""}, {"size": 1},
    {"md5": "0" * 32}, {"sha1": "0" * 40}, {"md5": "not-a-hash"},
])
def test_recovery_rejects_unfinished_or_mismatched_receipts(service, changes):
    service.instant = True
    service.fail_share = True
    with pytest.raises(QuarkAPIError):
        service.cloud.upload(service.source)
    service.receipt.update(changes)
    service.calls.clear()
    with pytest.raises(QuarkAPIError):
        service.cloud.share_existing(
            service.source, upload_task_id="upload-task", size=service.source.stat().st_size,
            md5=service.metadata["upload_md5"], sha1=service.metadata["upload_sha1"],
        )
    assert service.stages[-1] == "link_pending"
    assert not any(req.url.path.endswith("/share") for req, _ in service.calls)


def test_recovery_does_not_replace_saved_fid_with_different_receipt_fid(service):
    service.instant = True
    service.fail_share = True
    with pytest.raises(QuarkAPIError):
        service.cloud.upload(service.source)
    service.receipt["fid"] = "another-file"
    service.calls.clear()
    with pytest.raises(QuarkAPIError, match="FID"):
        service.cloud.share_existing(
            service.source, upload_task_id="upload-task", remote_fid="file-1",
            size=service.source.stat().st_size, md5=service.metadata["upload_md5"],
            sha1=service.metadata["upload_sha1"],
        )
    assert service.metadata["upload_remote_fid"] == "file-1"
    assert not any(req.url.path.endswith("/share") for req, _ in service.calls)


def test_known_fid_finds_exact_file_across_directory_pages_with_duplicate_names(service):
    row = {key: value for key, value in service.row().items() if key not in {"md5", "sha1"}}
    service.rows = [row]
    service.pages = {
        1: [{**row, "fid": "different-same-name"}] + [
            {**row, "fid": f"other-{index}", "file_name": f"other-{index}.rar"} for index in range(99)],
        2: [row],
    }
    assert service.cloud.share_existing(
        service.source, remote_fid="file-1", size=service.source.stat().st_size,
    ) == SHARE
    pages = [req.url.params["_page"] for req, _ in service.calls if req.url.path.endswith("file/sort")]
    assert pages == ["1", "2"]


@pytest.mark.parametrize("changes", [
    {"fid": "different-file"}, {"size": 1}, {"file_name": "different.rar"},
    {"pdir_fid": "different-folder"}, {"dir": True},
])
def test_known_fid_never_shares_a_different_or_changed_file(service, changes):
    service.rows = [{**service.row(), **changes}]
    with pytest.raises(QuarkAPIError):
        service.cloud.share_existing(service.source, remote_fid="file-1", size=service.source.stat().st_size)
    assert service.stages[-1] == "link_pending"
    assert not any(req.url.path.endswith("/share") for req, _ in service.calls)


def test_recovery_without_fid_or_task_cannot_accept_unrelated_hashless_file(service):
    service.rows = [{key: value for key, value in service.row().items() if key not in {"md5", "sha1"}}]
    with pytest.raises(QuarkAPIError, match="缺少可比对"):
        service.cloud.share_existing(
            service.source, size=service.source.stat().st_size,
            md5=service.row()["md5"], sha1=service.row()["sha1"],
        )
    assert not any(req.method == "POST" for req, _ in service.calls)


def test_approved_name_is_applied_before_creating_share_without_changing_local_filename(service):
    service.instant = True
    original_name = service.source.name
    assert service.cloud.upload(service.source, new_name="中文模型.rar") == SHARE
    assert service.source.name == original_name and service.source.exists()
    assert service.metadata["remote_filename"] == "中文模型.rar"
    assert service.metadata["naming_status"] == "completed"
    paths = [req.url.path for req, _ in service.calls]
    assert paths.index("/1/clouddrive/file/rename") < paths.index("/1/clouddrive/share")
    share = next(json.loads(body) for req, body in service.calls if req.url.path.endswith("/share"))
    assert share["title"] == "中文模型.rar"


def test_rename_does_not_overwrite_a_different_file_with_same_chinese_name(service):
    row = service.row()
    service.rows = [row, {**row, "fid": "other", "file_name": "中文模型.rar"}]
    with pytest.raises(QuarkAPIError, match="已有"):
        service.cloud.share_existing(service.source, remote_fid="file-1", size=row["size"], rename_to="中文模型.rar")
    assert not any(req.url.path.endswith(("file/rename", "/share")) for req, _ in service.calls)


def test_lost_rename_response_is_verified_and_recovery_is_idempotent(service):
    service.instant = True
    service.lose_rename_response = True
    assert service.cloud.upload(service.source, new_name="中文模型.rar") == SHARE
    service.calls.clear()
    assert service.cloud.share_existing(
        service.source, remote_fid="file-1", size=service.source.stat().st_size,
        remote_name="中文模型.rar", rename_to="中文模型.rar",
    ) == SHARE
    assert not any(req.url.path.endswith("file/rename") for req, _ in service.calls)


def test_recovery_finds_renamed_file_after_crash_before_saving_remote_name(service):
    service.rows = [{**service.row(), "file_name": "中文模型.rar"}]
    assert service.cloud.share_existing(
        service.source, remote_fid="file-1", size=service.source.stat().st_size,
        rename_to="中文模型.rar",
    ) == SHARE
    assert service.metadata["remote_filename"] == "中文模型.rar"
    assert not any(req.url.path.endswith("file/rename") for req, _ in service.calls)


def test_second_rename_preserves_original_extension_and_recovers_by_fid(service):
    service.rows = [{**service.row(), "file_name": "中文模型.rar"}]
    assert service.cloud.share_existing(
        service.source, remote_fid="file-1", size=service.source.stat().st_size,
        remote_name="中文模型.rar", rename_to="已修正的模型.rar",
    ) == SHARE
    assert service.metadata["remote_filename"] == "已修正的模型.rar"


def test_user_can_correct_name_after_remote_rename_succeeded_but_confirmation_was_lost(service, monkeypatch):
    service.instant = True
    original_confirm = service.cloud._confirm

    def interrupted(name, *args, **kwargs):
        if name == "第一次译名.rar":
            raise QuarkAPIError("fixture confirmation connection lost")
        return original_confirm(name, *args, **kwargs)

    monkeypatch.setattr(service.cloud, "_confirm", interrupted)
    with pytest.raises(QuarkAPIError, match="connection lost"):
        service.cloud.upload(service.source, new_name="第一次译名.rar")
    assert service.metadata["rename_pending_name"] == "第一次译名.rar"
    assert service.rows[0]["file_name"] == "第一次译名.rar"
    assert service.metadata["remote_filename"] == service.source.name
    service.calls.clear()
    monkeypatch.setattr(service.cloud, "_confirm", original_confirm)
    assert service.cloud.share_existing(
        service.source, remote_fid="file-1", size=service.source.stat().st_size,
        remote_name=service.metadata["remote_filename"], pending_name=service.metadata["rename_pending_name"],
        rename_to="修正译名.rar",
    ) == SHARE
    assert service.metadata["remote_filename"] == "修正译名.rar"
    assert not service.metadata["rename_pending_name"]
    assert not any(req.url.host.endswith(".pds.quark.cn") or req.url.path.endswith("upload/pre")
                   for req, _ in service.calls)


@pytest.mark.parametrize("url", ["https://evil.invalid/s/Fixture", "https://pan.quark.cn.evil.invalid/s/Fixture",
                                "https://pan.quark.cn/s/Fixture?password=secret", "", None])
def test_invalid_share_link_never_completes_task(service, url):
    service.instant = True
    service.share_url = url
    with pytest.raises(QuarkAPIError, match="链接"):
        service.cloud.upload(service.source)
    assert service.stages[-1] == "link_pending"


@pytest.mark.parametrize("days,kind", [(0, 1), (7, 2), (1, 3), (30, 4)])
def test_share_expiry_mapping(service, days, kind):
    service.instant = True
    service.cloud.share_expire_days = days
    service.cloud.upload(service.source)
    payloads = [json.loads(body) for req, body in service.calls if req.url.path.endswith("/share")]
    assert payloads[0]["expired_type"] == kind


def test_cancel_before_upload_sends_no_requests(service):
    service.cloud.stop_event.set()
    with pytest.raises(DownloadCancelled):
        service.cloud.upload(service.source)
    assert not service.calls


def test_cancel_during_part_stops_before_merge(service):
    def progress(*values):
        service.cloud.stop_event.set()
    service.cloud.on_progress = progress
    with pytest.raises(DownloadCancelled):
        service.cloud.upload(service.source)
    assert not service.merged
    assert service.stages[-1] == "uploading"


@pytest.mark.parametrize("cookie", ["", "Cookie: foo=bar", "__pus=secret\nInjected=x", "not-a-cookie", "x=中文", 123])
def test_cookie_validation_has_no_credential_echo(cookie):
    with pytest.raises(ValueError) as error:
        validate_cookie(cookie)
    assert "secret" not in str(error.value)


def test_transient_errors_do_not_expose_cookies_in_traceback(service, monkeypatch):
    def error(*args, **kwargs):
        raise httpx.ReadTimeout(COOKIE)
    with service.cloud._clients():
        monkeypatch.setattr(service.cloud._api_client, "request", error)
        with pytest.raises(QuarkAPIError) as caught:
            service.cloud._api("GET", "file/sort")
    assert COOKIE not in "".join(traceback.format_exception(caught.value))
    assert service.waits == [1, 2]
