"""Quark web upload protocol adapted from QuarkPan; see third_party/QuarkPan.LICENSE.

Each file owns its HTTP clients. The workbench bounds concurrent files; parts within
a file are streamed in order. Credentials never go to the separate OSS client.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import mimetypes
import re
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from email.utils import formatdate
from pathlib import Path
from threading import Event
from urllib.parse import quote, urlencode

import httpx

from .browser_download import DownloadCancelled
from .naming import validate_name


API_URL = "https://drive-pc.quark.cn/1/clouddrive/"
OSS_AGENT = "aliyun-sdk-js/6.6.1 Chrome 98.0.4758.80 on Windows 10 64-bit"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/142.0.0.0 Safari/537.36"
FID = re.compile(r"[A-Za-z0-9_-]{1,128}")
SHARE_URL = re.compile(r"https://pan\.quark\.cn/s/[A-Za-z0-9_-]+")


class QuarkAPIError(RuntimeError):
    pass


class QuarkDeletePending(QuarkAPIError):
    """A delete may have reached Quark; keep its receipt for reconciliation."""


def _hash_hex(value, kind):
    length = {"md5": 16, "sha1": 20}[kind]
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(rf"[0-9A-Fa-f]{{{length * 2}}}", value):
            return value.lower()
        try:
            digest = base64.b64decode(value, validate=True)
        except ValueError:
            pass
        else:
            if len(digest) == length:
                return digest.hex()
    raise QuarkAPIError("网盘文件哈希格式无效，无法确认内容")


def validate_cookie(value: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError("夸克 Cookie 必须为字符串")
    value = value.strip()
    if not value:
        if required:
            raise ValueError("请先在设置中填写夸克 Cookie")
        return ""
    if len(value) > 16384 or re.search(r"[^\x20-\x7e]", value):
        raise ValueError("夸克 Cookie 格式无效，请粘贴请求头中的 Cookie 值")
    pairs = [part.strip() for part in value.split(";") if part.strip()]
    if not pairs:
        raise ValueError("夸克 Cookie 格式无效")
    for part in pairs:
        if part.strip() and not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+=[^;]*", part.strip()):
            raise ValueError("夸克 Cookie 格式无效，请粘贴请求头中的 Cookie 值")
    return value


def validate_quark_api_settings(payload: dict, existing=None, *, require_cookie: bool = True) -> None:
    def value(key, default):
        return payload.get(key, getattr(existing, key, default))

    cookie = payload.get("quark_cookie") or (getattr(existing, "quark_cookie", "") if require_cookie else "")
    validate_cookie(cookie, required=require_cookie)
    folder = value("quark_api_parent_fid", "0")
    if not isinstance(folder, str) or not FID.fullmatch(folder):
        raise ValueError("夸克 API 目标目录 FID 必须为字母、数字、下划线或连字符；根目录填 0")
    for key, lower, upper in (("quark_api_timeout_seconds", 10, 600),
                               ("quark_api_file_concurrency", 1, 3)):
        number = value(key, lower)
        if type(number) is not int or not lower <= number <= upper:
            raise ValueError(f"{key} 必须为 {lower} 到 {upper} 的整数")


class QuarkAPIStorageAdapter:
    def __init__(self, cookie: str, *, parent_fid: str = "0", timeout_seconds: int = 60,
                 share_expire_days: int = 7, on_stage=None, on_progress=None,
                 on_remote=None, on_phase=None, stop_event: Event | None = None):
        validate_quark_api_settings({"quark_cookie": cookie, "quark_api_parent_fid": parent_fid,
                                     "quark_api_timeout_seconds": timeout_seconds})
        if type(share_expire_days) is not int or share_expire_days not in {0, 1, 7, 30}:
            raise ValueError("分享有效期仅支持1、7、30天或永久")
        self.cookie = validate_cookie(cookie)
        self.parent_fid = parent_fid
        self.timeout_seconds = timeout_seconds
        self.share_expire_days = share_expire_days
        self.on_stage, self.on_progress = on_stage, on_progress
        self.on_remote, self.on_phase = on_remote, on_phase
        self.stop_event = stop_event or Event()
        self._api_client = self._oss_client = None
        self._started = 0.0
        self._last_report = 0.0
        self._high_water = 0

    def _check_stop(self):
        if self.stop_event.is_set():
            raise DownloadCancelled("夸克上传已停止")

    def _wait(self, seconds):
        if self.stop_event.wait(seconds):
            self._check_stop()

    def _stage(self, value):
        if self.on_stage:
            self.on_stage(value)

    def _phase(self, value):
        if self.on_phase:
            self.on_phase(value)

    def _remote(self, **values):
        if self.on_remote:
            self.on_remote(values)

    def _remember_fid(self, fid):
        if not isinstance(fid, str) or not FID.fullmatch(fid):
            raise QuarkAPIError("未获得有效的网盘文件 FID")
        self._remote(upload_remote_fid=fid)
        return fid

    def _progress(self, received, total, *, force=False):
        now = time.monotonic()
        self._high_water = max(self._high_water, received)
        if self.on_progress and (force or now - self._last_report >= 0.5):
            self._last_report = now
            speed = self._high_water / max(0.001, now - self._started)
            eta = (total - self._high_water) / speed if speed else None
            self.on_progress(self._high_water, total, speed, eta)

    @contextmanager
    def _clients(self):
        # httpx's INFO request log would include temporary signed-upload identifiers.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        with httpx.Client(timeout=self.timeout_seconds, trust_env=False,
                          follow_redirects=False) as api, \
                httpx.Client(timeout=self.timeout_seconds, trust_env=False,
                             follow_redirects=False) as oss:
            self._api_client, self._oss_client = api, oss
            try:
                yield
            finally:
                self._api_client = self._oss_client = None

    def _api(self, method, endpoint, *, data=None, params=None):
        safe_retry = method == "GET" or endpoint == "file/upload/auth"
        for attempt in range(3 if safe_retry else 1):
            self._check_stop()
            try:
                response = self._api_client.request(
                    method, API_URL + endpoint,
                    params={"pr": "ucpro", "fr": "pc", "uc_param_str": "", **(params or {})},
                    json=data,
                    headers={"Cookie": self.cookie, "User-Agent": USER_AGENT,
                             "Origin": "https://pan.quark.cn", "Referer": "https://pan.quark.cn/"},
                )
            except httpx.RequestError:
                if safe_retry and attempt < 2:
                    self._wait(attempt + 1)
                    continue
                raise QuarkAPIError(f"夸克接口 {endpoint} 连接失败或超时，请检查网络") from None
            if response.status_code in {401, 403}:
                raise QuarkAPIError("夸克登录失效或访问被拒绝，请更新 Cookie")
            if safe_retry and response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                self._wait(attempt + 1)
                continue
            if response.status_code != 200:
                raise QuarkAPIError(f"夸克接口 {endpoint} 返回 HTTP {response.status_code}")
            try:
                result = response.json()
            except ValueError:
                raise QuarkAPIError(f"夸克接口 {endpoint} 返回了无效 JSON") from None
            if not isinstance(result, dict):
                raise QuarkAPIError(f"夸克接口 {endpoint} 响应格式无效")
            if result.get("status") != 200 or result.get("code", 0) != 0:
                # Do not persist arbitrary server messages, which may contain credentials.
                code = result.get("code")
                detail = f"（错误码 {code}）" if type(code) is int else ""
                raise QuarkAPIError(f"夸克接口 {endpoint} 拒绝操作{detail}，请检查登录、目录权限或网盘容量")
            cookies = dict(item.strip().split("=", 1) for item in self.cookie.split(";") if item.strip())
            for key, val in response.cookies.items():
                cookies[key] = val
            self.cookie = "; ".join(f"{key}={val}" for key, val in cookies.items())
            return result

    def _data(self, method, endpoint, **kwargs):
        data = self._api(method, endpoint, **kwargs).get("data")
        if not isinstance(data, dict):
            raise QuarkAPIError(f"夸克接口 {endpoint} 缺少结果数据")
        return data

    def _folder(self, target):
        folder = target or self.parent_fid
        if not isinstance(folder, str) or not FID.fullmatch(folder):
            raise ValueError("夸克 API 目标目录 FID 无效")
        return folder

    def _find(self, name, folder, *, fid=""):
        matches = []
        for page in range(1, 1001):
            result = self._api("GET", "file/sort", params={
                "pdir_fid": folder, "_page": page, "_size": 100, "_fetch_total": 1,
                "_sort": "file_name:asc", "fetch_all_file": 1,
            })
            rows = result.get("data", {}).get("list")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise QuarkAPIError("夸克目录列表格式无效")
            if fid:
                row = next((row for row in rows if str(row.get("fid")) == fid), None)
                if row is not None:
                    return row
            else:
                matches.extend(row for row in rows if row.get("file_name") == name and not row.get("dir"))
            total = result.get("metadata", {}).get("_total", result.get("metadata", {}).get("total"))
            if len(rows) < 100 or (type(total) is int and page * 100 >= total):
                break
        else:
            raise QuarkAPIError("夸克目录内容过多，无法完整核对重名文件，请选择子目录")
        if len(matches) > 1:
            raise QuarkAPIError("目标目录存在多个同名文件，请先在网盘中核对")
        return matches[0] if matches else None

    def _verify_hashes(self, row, hashes, *, require_hash=False):
        matched_hash = False
        for kind in ("md5", "sha1"):
            remote = row.get(kind)
            expected = hashes.get(kind, "")
            if remote and expected:
                if _hash_hex(remote, kind) != _hash_hex(expected, kind):
                    raise QuarkAPIError("网盘文件哈希与本地文件不符")
                matched_hash = True
        if require_hash and not matched_hash:
            raise QuarkAPIError("同名文件缺少可比对的哈希，无法确认内容；请更换目录或人工核对")

    def _verify(self, row, name, folder, size, hashes, *, require_hash=False):
        if not isinstance(row, dict) or not FID.fullmatch(str(row.get("fid", ""))):
            raise QuarkAPIError("未获得有效的网盘文件 FID")
        if row.get("file_name") != name or row.get("dir"):
            raise QuarkAPIError("网盘文件名称或类型与上传文件不符")
        if str(row.get("pdir_fid", folder)) != folder:
            raise QuarkAPIError("网盘文件不在原上传目录")
        if size is None or row.get("size") != size:
            raise QuarkAPIError("网盘文件大小与本地文件不符，不能生成分享链接")
        self._verify_hashes(row, hashes, require_hash=require_hash)
        return str(row["fid"])

    def _receipt_fid(self, receipt, task_id, size, hashes):
        if receipt.get("task_id", task_id) != task_id:
            raise QuarkAPIError("夸克完成回执与原上传任务不符")
        if receipt.get("finish") is not True:
            raise QuarkAPIError("夸克上传任务尚未完成，请稍后补取链接；不会重复上传")
        fid = receipt.get("fid")
        if not isinstance(fid, str) or not FID.fullmatch(fid):
            raise QuarkAPIError("夸克完成回执缺少有效文件 FID，请稍后补取链接")
        if size is None or receipt.get("size", size) != size:
            raise QuarkAPIError("夸克完成回执的文件大小与原上传文件不符")
        self._verify_hashes(receipt, hashes)
        return fid

    def _confirm(self, name, folder, size, hashes, fid=""):
        for attempt in range(5):
            row = self._find(name, folder, fid=fid)
            if row is not None:
                return self._verify(row, name, folder, size, hashes, require_hash=not fid)
            self._wait(1)
        raise QuarkAPIError("网盘尚未确认文件完成，请稍后补取链接；不会自动重复上传")

    def _hashes(self, source):
        md5, sha1 = hashlib.md5(), hashlib.sha1()
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                self._check_stop()
                md5.update(chunk)
                sha1.update(chunk)
        return {"md5": md5.hexdigest(), "sha1": sha1.hexdigest()}

    def _oss_url(self, pre, params):
        bucket, key = pre.get("bucket"), pre.get("obj_key")
        if not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9-]{1,63}", bucket):
            raise QuarkAPIError("预上传返回无效 OSS 存储桶")
        if not isinstance(key, str) or not key or any(ord(char) < 32 for char in key):
            raise QuarkAPIError("预上传返回无效 OSS 对象路径")
        # Never trust an arbitrary upload_url with the account's signed authorization.
        return f"https://{bucket}.pds.quark.cn/{quote(key, safe='/~')}" + "?" + urlencode(params)

    def _auth(self, pre, method, mime, query, extra=None, content_md5=""):
        date = formatdate(usegmt=True)
        headers = {"Content-Type": mime, "x-oss-date": date, "x-oss-user-agent": OSS_AGENT, **(extra or {})}
        canonical = "".join(f"{key}:{value}\n" for key, value in sorted(headers.items()) if key.startswith("x-oss-"))
        meta = f"{method}\n{content_md5}\n{mime}\n{date}\n{canonical}/{pre['bucket']}/{pre['obj_key']}?{query}"
        result = self._data("POST", "file/upload/auth", data={
            "task_id": pre["task_id"], "auth_info": pre["auth_info"], "auth_meta": meta,
        })
        key = result.get("auth_key")
        if not isinstance(key, str) or not key:
            raise QuarkAPIError("夸克未返回上传授权")
        headers["Authorization"] = key
        if content_md5:
            headers["Content-MD5"] = content_md5
        return headers

    def _part(self, source, pre, mime, number, offset, length, total):
        for attempt in range(3):
            self._check_stop()
            query = f"partNumber={number}&uploadId={pre['upload_id']}"
            headers = self._auth(pre, "PUT", mime, query)
            headers["Content-Length"] = str(length)

            def content():
                with source.open("rb") as stream:
                    stream.seek(offset)
                    remaining = length
                    while remaining:
                        self._check_stop()
                        chunk = stream.read(min(64 * 1024, remaining))
                        if not chunk:
                            raise QuarkAPIError("本地文件在上传期间发生变化")
                        yield chunk
                        remaining -= len(chunk)
                        self._progress(offset + length - remaining, total)

            try:
                response = self._oss_client.put(self._oss_url(pre, {
                    "partNumber": number, "uploadId": pre["upload_id"],
                }), content=content(), headers=headers)
            except httpx.RequestError:
                response = None
            if response is not None and response.status_code == 200:
                etag = response.headers.get("etag", "").strip('"')
                if re.fullmatch(r"[0-9A-Fa-f]{32}", etag):
                    self._progress(offset + length, total, force=True)
                    return etag
                raise QuarkAPIError("OSS 分片上传缺少有效 ETag")
            if response is not None and response.status_code not in {408, 429, 500, 502, 503, 504}:
                raise QuarkAPIError(f"OSS 分片 {number} 上传失败，HTTP {response.status_code}")
            if attempt < 2:
                logging.warning("夸克分片 %d 传输失败，正在重试 (%d/2)", number, attempt + 1)
                self._wait(attempt + 1)
        raise QuarkAPIError(f"OSS 分片 {number} 上传超时或暂时不可用，重试后仍失败")

    def _merge(self, pre, etags):
        root = ET.Element("CompleteMultipartUpload")
        for number, etag in enumerate(etags, 1):
            part = ET.SubElement(root, "Part")
            ET.SubElement(part, "PartNumber").text = str(number)
            ET.SubElement(part, "ETag").text = f'"{etag}"'
        body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode("ascii")
        callback = base64.b64encode(json.dumps(pre["callback"], separators=(",", ":")).encode()).decode("ascii")
        headers = self._auth(pre, "POST", "application/xml", f"uploadId={pre['upload_id']}",
                             {"x-oss-callback": callback}, content_md5)
        self._stage("link_pending")
        try:
            response = self._oss_client.post(self._oss_url(pre, {"uploadId": pre["upload_id"]}),
                                             content=body, headers=headers)
        except httpx.RequestError:
            raise QuarkAPIError("OSS 合并请求超时，文件结果待确认，请稍后补取链接") from None
        if response.status_code not in {200, 203}:
            raise QuarkAPIError(f"OSS 合并失败，HTTP {response.status_code}；文件结果待确认")
        if response.content.lstrip().startswith(b"<"):
            try:
                result = ET.fromstring(response.content)
            except ET.ParseError:
                raise QuarkAPIError("OSS 合并响应格式无效，文件结果待确认") from None
            if result.tag.rsplit("}", 1)[-1] == "Error":
                raise QuarkAPIError("OSS 返回合并错误，文件结果待确认")
        # 203 may mean storage succeeded but the callback failed. Confirmation is mandatory.
        return self._data("POST", "file/upload/finish", data={
            "task_id": pre["task_id"], "obj_key": pre["obj_key"],
        })

    def _share(self, fid, name):
        self._check_stop()
        self._phase("sharing")
        self._stage("link_pending")
        payload = {"fid_list": [fid], "title": name, "url_type": 1,
                   "expired_type": {0: 1, 7: 2, 1: 3, 30: 4}[self.share_expire_days]}
        result = self._data("POST", "share", data=payload)
        share_id = result.get("share_id")
        if not share_id:
            task_id = result.get("task_id")
            if not task_id:
                raise QuarkAPIError("夸克未返回分享任务 ID")
            for retry in range(30):
                task = self._data("GET", "task", params={"task_id": task_id, "retry_index": retry})
                if task.get("status") == 3:
                    raise QuarkAPIError("夸克分享创建失败，文件已上传，可稍后补取链接")
                if task.get("status") == 2:
                    share_id = task.get("share_id")
                    break
                self._wait(1)
        if not share_id:
            raise QuarkAPIError("夸克分享生成超时，文件已上传，可稍后补取链接")
        details = self._data("POST", "share/password", data={"share_id": share_id})
        url = details.get("share_url")
        if not isinstance(url, str) or not SHARE_URL.fullmatch(url):
            raise QuarkAPIError("夸克返回的分享链接无效，文件已上传")
        self._phase("completed")
        self._stage("completed")
        return url

    def check(self):
        with self._clients():
            result = self._data("GET", "file/sort", params={
                "pdir_fid": self.parent_fid, "_page": 1, "_size": 1,
            })
            if not isinstance(result.get("list"), list):
                raise QuarkAPIError("夸克目录列表格式无效")
        logging.info("夸克 API 登录及目标目录访问检查通过")

    def _rename(self, fid, name, new_name, folder, size, hashes):
        new_name = validate_name(new_name, name)
        self._phase("renaming")
        self._remote(naming_status="applying", naming_error="")
        row = self._find(name, folder, fid=fid)
        if row is None:
            raise QuarkAPIError("原上传文件不在目标目录，无法改名")
        current_name = row.get("file_name")
        if current_name not in {name, new_name}:
            raise QuarkAPIError("云端文件名已被其他操作更改，请人工核对")
        self._verify(row, current_name, folder, size, hashes)
        if current_name != new_name:
            duplicate = self._find(new_name, folder)
            if duplicate and str(duplicate.get("fid")) != fid:
                raise QuarkAPIError("目标目录已有该中文名称，请修改名称后重试")
            self._remote(rename_pending_name=new_name)
            try:
                self._api("POST", "file/rename", data={"fid": fid, "file_name": new_name})
            except QuarkAPIError:
                # A lost response may follow a successful rename. Verify before retrying it.
                renamed = self._find(new_name, folder, fid=fid)
                if renamed is None or renamed.get("file_name") != new_name:
                    raise
            self._confirm(new_name, folder, size, hashes, fid)
        self._remote(remote_filename=new_name, rename_pending_name="", naming_status="completed", naming_error="")
        return new_name

    def _publish(self, fid, name, folder, size, hashes, new_name=""):
        self._remote(remote_filename=name, rename_pending_name="")
        if new_name:
            name = self._rename(fid, name, new_name, folder, size, hashes)
        return self._share(fid, name)

    def upload(self, local_path: Path, remote_folder: str = "", *, new_name: str = "") -> str:
        source = Path(local_path).resolve()
        if not source.is_file():
            raise ValueError("上传文件不存在")
        initial = source.stat()
        size = initial.st_size
        if size <= 0:
            raise ValueError("不能上传空文件")
        folder = self._folder(remote_folder)
        if new_name:
            validate_name(new_name, source.name)
        self._stage("uploading")
        self._phase("hashing")
        hashes = self._hashes(source)
        current = source.stat()
        if (current.st_size, current.st_mtime_ns) != (initial.st_size, initial.st_mtime_ns):
            raise QuarkAPIError("本地文件在计算哈希期间发生变化，已停止上传")
        self._remote(upload_md5=hashes["md5"], upload_sha1=hashes["sha1"])
        with self._clients():
            self._phase("checking")
            existing = self._find(source.name, folder)
            if existing:
                fid = self._verify(existing, source.name, folder, size, hashes, require_hash=True)
                self._remote(upload_remote_fid=fid)
                logging.info("夸克目标目录已存在相同内容，复用文件：%s", source.name)
                return self._publish(fid, source.name, folder, size, hashes, new_name)
            mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            result = self._api("POST", "file/upload/pre", data={
                "ccp_hash_update": True, "pdir_fid": folder, "dir_name": "",
                "file_name": source.name, "size": size, "format_type": mime,
                "l_created_at": int(initial.st_ctime * 1000), "l_updated_at": int(initial.st_mtime * 1000),
            })
            pre = result.get("data")
            if not isinstance(pre, dict) or not pre.get("task_id"):
                raise QuarkAPIError("预上传未返回有效任务 ID")
            task_id = str(pre["task_id"])
            self._remote(upload_task_id=task_id)
            if pre.get("fid"):
                self._remember_fid(pre["fid"])
            # Hash submission can complete an instant upload; persist uncertainty first.
            self._stage("link_pending")
            hashed = self._data("POST", "file/update/hash", data={"task_id": pre["task_id"], **hashes})
            if hashed.get("fid"):
                self._remember_fid(hashed["fid"])
            if hashed.get("finish") is not True:
                self._stage("uploading")
                for key in ("auth_info", "upload_id", "obj_key", "bucket", "callback"):
                    if not pre.get(key):
                        raise QuarkAPIError("预上传响应缺少分片授权信息")
                part_size = result.get("metadata", {}).get("part_size", pre.get("part_size", 4 * 1024 * 1024))
                if type(part_size) is not int or part_size < 100 * 1024 or part_size > 5 * 1024**3:
                    raise QuarkAPIError("预上传返回的分片大小无效")
                count = math.ceil(size / part_size)
                if count > 10000:
                    raise QuarkAPIError("文件超出本次上传的分片数量限制")
                self._phase("uploading")
                self._started = time.monotonic()
                self._last_report = self._high_water = 0
                self._progress(0, size, force=True)
                logging.info("夸克 API 开始上传：%s，%d 字节，%d 个分片", source.name, size, count)
                etags = []
                for number in range(1, count + 1):
                    offset = (number - 1) * part_size
                    etags.append(self._part(source, pre, mime, number, offset, min(part_size, size - offset), size))
                current = source.stat()
                if (current.st_size, current.st_mtime_ns) != (initial.st_size, initial.st_mtime_ns):
                    raise QuarkAPIError("本地文件在上传期间发生变化，已停止合并")
                self._phase("merging")
                finished = self._merge(pre, etags)
            else:
                logging.info("夸克文件秒传命中：%s", source.name)
                finished = self._data("POST", "file/upload/finish", data={"task_id": task_id})
            self._stage("link_pending")
            self._phase("checking")
            fid = self._remember_fid(self._receipt_fid(finished, task_id, size, hashes))
            fid = self._confirm(source.name, folder, size, hashes, fid)
            return self._publish(fid, source.name, folder, size, hashes, new_name)

    def share_existing(self, local_path: Path, remote_folder: str = "", *,
                       remote_fid: str = "", size: int | None = None, md5: str = "", sha1: str = "",
                       upload_task_id: str = "", remote_name: str = "", rename_to: str = "",
                       pending_name: str = "") -> str:
        folder = self._folder(remote_folder)
        name = Path(local_path).name
        if remote_fid and not FID.fullmatch(remote_fid):
            raise ValueError("原上传文件 FID 无效，请人工核对")
        if upload_task_id and not FID.fullmatch(upload_task_id):
            raise ValueError("原上传任务 ID 无效，请人工核对")
        self._stage("link_pending")
        self._phase("checking")
        with self._clients():
            hashes = {"md5": md5, "sha1": sha1}
            if upload_task_id:
                receipt = self._data("POST", "file/upload/finish", data={"task_id": upload_task_id})
                fid = self._receipt_fid(receipt, upload_task_id, size, hashes)
                if remote_fid and remote_fid != fid:
                    raise QuarkAPIError("夸克完成回执的文件 FID 与原上传记录不符，请人工核对")
                remote_fid = self._remember_fid(fid)
                logging.info("夸克已通过原上传任务找回完成文件：%s", name)
            expected_name = remote_name or name
            if remote_fid and (rename_to or pending_name):
                for candidate in (rename_to, pending_name):
                    if candidate:
                        validate_name(candidate, name)
                row = self._find(name, folder, fid=remote_fid)
                if row and row.get("file_name") in {rename_to, pending_name} - {""}:
                    expected_name = row["file_name"]
            fid = self._confirm(expected_name, folder, size, hashes, remote_fid)
            self._remote(upload_remote_fid=fid)
            return self._publish(fid, expected_name, folder, size, hashes, rename_to)

    def delete_existing(self, name: str, remote_folder: str, *, remote_fid: str,
                        size: int, md5: str = "", sha1: str = "",
                        delete_task_id: str = "", pending: bool = False) -> None:
        """Delete only the bound file; retry a known task by reading its receipt."""
        if (not isinstance(remote_fid, str) or not FID.fullmatch(remote_fid)
                or remote_fid == "0"):
            raise ValueError("缺少有效的原上传文件 FID，无法删除")
        if (not isinstance(remote_folder, str) or not FID.fullmatch(remote_folder)
                or remote_folder == remote_fid):
            raise ValueError("缺少有效的原上传目录，无法删除")
        if not isinstance(name, str) or not name or type(size) is not int or size <= 0:
            raise ValueError("原上传文件名称或大小缺失，无法删除")
        if delete_task_id and (not isinstance(delete_task_id, str) or not FID.fullmatch(delete_task_id)):
            raise ValueError("原删除任务 ID 无效，请人工核对")
        with self._clients():
            if not delete_task_id:
                row = self._find(name, remote_folder, fid=remote_fid)
                if row is None:
                    message = "原上传文件不在原目录，请先在夸克核对是否已移动或删除"
                    if pending:
                        raise QuarkDeletePending(message)
                    raise QuarkAPIError(message)
                if str(row.get("pdir_fid", "")) != remote_folder or row.get("file_type") == 0:
                    raise QuarkAPIError("原上传文件目录或类型不符，已停止删除")
                self._verify(row, name, remote_folder, size, {"md5": md5, "sha1": sha1})
                self._check_stop()
                # Persist intent before the mutation, including when its response is lost.
                self._remote(cloud_delete_status="pending", cloud_delete_error="")
                try:
                    result = self._data("POST", "file/delete", data={
                        "action_type": 2, "filelist": [remote_fid], "exclude_fids": [],
                    })
                except QuarkAPIError as exc:
                    raise QuarkDeletePending(str(exc)) from exc
                delete_task_id = result.get("task_id")
                if not isinstance(delete_task_id, str) or not FID.fullmatch(delete_task_id):
                    raise QuarkDeletePending("夸克未返回有效删除任务 ID，请在网盘核对后重试")
                self._remote(cloud_delete_task_id=delete_task_id)
            task_failed = False
            try:
                for attempt in range(30):
                    result = self._data("GET", "task", params={
                        "task_id": delete_task_id, "retry_index": attempt,
                    })
                    if result.get("task_id", delete_task_id) != delete_task_id:
                        raise QuarkDeletePending("夸克删除回执与原任务不符，请人工核对")
                    if result.get("status") == 3:
                        task_failed = True
                        self._remote(cloud_delete_status="failed", cloud_delete_task_id="")
                        raise QuarkAPIError("夸克删除任务失败，可核对文件后重试")
                    if result.get("status") == 2:
                        for check in range(5):
                            if self._find(name, remote_folder, fid=remote_fid) is None:
                                return
                            if check < 4:
                                self._wait(0.5)
                        raise QuarkDeletePending("删除任务已完成，但原目录仍有该文件，请稍后重新确认")
                    self._wait(0.5)
                raise QuarkDeletePending("夸克删除任务尚未完成，请稍后重新确认")
            except (QuarkAPIError, DownloadCancelled) as exc:
                if isinstance(exc, QuarkDeletePending):
                    raise
                if task_failed:
                    raise
                raise QuarkDeletePending(str(exc)) from exc
