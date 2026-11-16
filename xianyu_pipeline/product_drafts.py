"""Editable product records, separate from transfer execution state."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from .models import utc_now
from .workbench import image_type

EDIT_FIELDS = {"title", "price", "stock", "category", "format", "variant", "description", "image_indices"}
DRAFT_STATUSES = {"editing", "ready", "saving", "saved", "attention", "pending"}


def price_value(value: str) -> str:
    if not isinstance(value, str) or len(value) > 16:
        raise ValueError("价格请填写金额，最多保留两位小数")
    if not value.strip():
        return ""
    try:
        amount = Decimal(value)
        if not amount.is_finite() or not Decimal("0.01") <= amount <= Decimal("99999999.99") or amount.as_tuple().exponent < -2:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise ValueError("价格须为 0.01 至 99999999.99，最多两位小数") from None
    return format(amount, ".2f")


def validate_qianniu_url(value: str, *, required=False) -> str:
    from urllib.parse import urlsplit
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("千牛页面地址无效")
    if not value and not required:
        return ""
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.netloc != hostname or re.search(r"[\s\\]", value)
            or not any(hostname == root or hostname.endswith("." + root) for root in ("taobao.com", "tmall.com"))):
        raise ValueError("千牛页面须为淘宝或天猫的 HTTPS 地址")
    return value


def missing_fields(row: dict, *, require_shop=False) -> list[str]:
    result = []
    if require_shop and not row.get("shop_name"):
        result.append("目标店铺")
    for key, label in (("title", "商品标题"), ("price", "商品价格"),
                       ("category", "商品类目"), ("description", "商品详情")):
        if not row.get(key):
            result.append(label)
    if not row.get("image_indices"):
        result.append("商品图片")
    return result


class DraftConflict(ValueError):
    pass


class ProductDraftStore:
    def __init__(self, output_dir: Path):
        self.root = output_dir / ".product-drafts"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "records.json"
        self.lock = threading.RLock()
        self.remote_drafts = []
        self.rows = {}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("商品草稿记录格式无效，原文件已保留")
            self.rows = {row["id"]: row for row in data}
            for row in self.rows.values():
                if row["status"] == "saving":
                    row.update(status="pending", error="上次千牛保存中断，请核对千牛中的草稿，避免重复创建",
                               updated_at=utc_now(), revision=row["revision"] + 1)
            self.flush()

    def flush(self):
        with self.lock:
            temporary = self.path.with_name("records-" + uuid4().hex + ".tmp")
            try:
                temporary.write_text(json.dumps(list(self.rows.values()), ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def get(self, identity: str) -> dict:
        with self.lock:
            if not isinstance(identity, str) or identity not in self.rows:
                raise ValueError("商品草稿不存在，或属于其他设备")
            return copy.deepcopy(self.rows[identity])

    def create(self, task: dict, config) -> dict:
        if (task.get("remote_device") or task.get("record_removed_at")
                or task.get("upload_status") != "completed" or not task.get("share_url")
                or task.get("upload_provider") not in {"quark_api", "quark_browser", "quark_cli"}):
            raise ValueError("请选择本机已上传夸克并取得分享链接的素材")
        shop = config.qianniu_shop_name.strip()
        identity = hashlib.sha256(f"{shop}:{task['id']}".encode()).hexdigest()[:24]
        with self.lock:
            existing = next((row for row in self.rows.values()
                             if row["task_id"] == task["id"] and row["shop_name"] == shop), None)
            if existing:
                return self.get(existing["id"])
            if identity in self.rows:
                # A repeated upload or link recovery must not overwrite editorial work.
                return self.get(identity)
            title = Path(task.get("remote_filename") or task.get("rename_target") or task["filename"]).stem
            title = re.sub(r"[\r\n\t]+", " ", title).strip()[:60]
            images = []
            for value in task.get("image_paths", [])[:10]:
                source = Path(value)
                if not image_type(source):
                    continue
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                target = self.root / "assets" / (digest + source.suffix.lower())
                target.parent.mkdir(exist_ok=True)
                if not target.exists():
                    shutil.copyfile(source, target)
                images.append(str(target.resolve()))
            description = config.qianniu_default_description.replace("{title}", title).replace(
                "{filename}", task["filename"]).replace("{format}", config.qianniu_default_format)
            now = utc_now()
            row = dict(id=identity, task_id=task["id"], shop_name=config.qianniu_shop_name.strip(),
                       filename=task["filename"], chat=task.get("chat", ""), message_id=task.get("message_id", 0),
                       source_url=task.get("source_url", ""), share_url=task["share_url"],
                       upload_remote_fid=task.get("upload_remote_fid", ""), title=title,
                       price=config.qianniu_default_price, stock=config.qianniu_default_stock,
                       category=config.qianniu_default_category, format=config.qianniu_default_format,
                       variant=config.qianniu_default_variant, description=description[:12000],
                       image_paths=images, image_indices=list(range(min(len(images), 5))),
                       status="editing", error="", remote_url="", saved_at="", confirmed_by="",
                       revision=1, created_at=now, updated_at=now)
            row["status"] = "editing" if missing_fields(row) else "ready"
            self.rows[identity] = row
            self.flush()
            return copy.deepcopy(row)

    def bind_unassigned_shop(self, name: str) -> int:
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("目标店铺名称无效")
        name = name.strip()
        with self.lock:
            assigned = {row["task_id"] for row in self.rows.values() if row["shop_name"] == name}
            updated = {}
            for identity, original in self.rows.items():
                if (original.get("shop_name") or original["status"] in {"saving", "saved", "pending"}
                        or original.get("remote_url") or original["task_id"] in assigned):
                    continue
                row = copy.deepcopy(original)
                row.update(shop_name=name, revision=row["revision"] + 1, updated_at=utc_now())
                updated[identity] = row
                assigned.add(row["task_id"])
            previous = self.rows
            self.rows = {**self.rows, **updated}
            try:
                if updated:
                    self.flush()
            except Exception:
                self.rows = previous
                raise
            return len(updated)

    def edit(self, identity: str, revision: int, values: dict) -> dict:
        with self.lock:
            row = self.get(identity)
            if type(revision) is not int or row["revision"] != revision:
                raise DraftConflict("资料已发生变化，请重新读取后编辑")
            if row["status"] in {"saving", "pending", "saved"} or row.get("remote_url"):
                raise DraftConflict("千牛草稿已保存或结果待确认，请先到千牛核对和编辑")
            if not isinstance(values, dict) or set(values) - EDIT_FIELDS:
                raise ValueError("商品资料字段无效")
            for key, value in values.items():
                if key == "price":
                    value = price_value(value)
                elif key == "stock":
                    if type(value) is not int or not 1 <= value <= 999999999:
                        raise ValueError("库存须为 1 至 999999999 的整数")
                elif key == "image_indices":
                    if (not isinstance(value, list) or len(value) > 5 or len(set(map(str, value))) != len(value)
                            or any(type(i) is not int or not 0 <= i < len(row["image_paths"]) for i in value)):
                        raise ValueError("请选择最多 5 张不重复的商品图片")
                else:
                    limit = {"title": 60, "category": 200, "format": 80, "variant": 200, "description": 12000}[key]
                    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
                        raise ValueError(f"商品资料长度或格式无效：{key}")
                    value = value.strip()
                row[key] = value
            row.update(status="editing" if missing_fields(row) else "ready", error="",
                       revision=row["revision"] + 1, updated_at=utc_now())
            self.rows[identity] = row
            self.flush()
            return copy.deepcopy(row)

    def update_status(self, identity: str, status: str, **values):
        if status not in DRAFT_STATUSES or set(values) - {"error", "remote_url", "saved_at", "confirmed_by"}:
            raise ValueError("草稿状态无效")
        with self.lock:
            row = self.get(identity)
            row.update(values, status=status, revision=row["revision"] + 1, updated_at=utc_now())
            self.rows[identity] = row
            self.flush()
            return copy.deepcopy(row)

    def preview(self, identity: str, index: int):
        row = self.get(identity)
        if not 0 <= index < len(row["image_paths"]):
            raise ValueError("商品图片不存在")
        path = Path(row["image_paths"][index]).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("商品图片路径无效")
        mime = image_type(path)
        if not mime:
            raise ValueError("商品图片已丢失")
        return path, mime

    def snapshot(self):
        with self.lock:
            result = []
            for original in self.rows.values():
                row = copy.deepcopy(original)
                paths = row.pop("image_paths", [])
                row["images"] = [{"url": f"/api/product-drafts/{row['id']}/images/{i}", "index": i}
                                 for i in range(len(paths))]
                row["missing"] = missing_fields(row)
                result.append(row)
            result.extend(copy.deepcopy(self.remote_drafts))
            return {"drafts": sorted(result, key=lambda row: row["updated_at"], reverse=True)}
