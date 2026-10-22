"""Durable metadata outbox with a local JSON cache and device-owned tasks."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
from uuid import uuid4

import httpx
from sqlalchemy.exc import SQLAlchemyError

from .data_protocol import DIGEST, FIELDS, MAX_ASSET, PROTOCOL_VERSION, encoded, validate_record
from .data_server import image_mime
from .models import utc_now
from .workbench import channel_key, image_type
from .telegram_sources import split_scope


class HTTPDataClient:
    """Compatibility transport for previously configured data services."""
    def __init__(self, config):
        self.client = httpx.Client(base_url=config.data_server_url, headers={
            "Authorization": "Bearer " + config.data_server_token,
        }, timeout=httpx.Timeout(10, connect=5), follow_redirects=False, trust_env=False)

    def health(self):
        response = self.client.get("/v1/health")
        response.raise_for_status()
        return response.json()

    def exchange(self, payload):
        response = self.client.post("/v1/sync", json=payload)
        response.raise_for_status()
        return response.json()

    def put_asset(self, digest, body):
        self.client.put("/v1/assets/" + digest, content=body,
                        headers={"Content-Type": "application/octet-stream"}).raise_for_status()

    def get_asset(self, digest):
        with self.client.stream("GET", "/v1/assets/" + digest) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_ASSET:
                    raise ValueError("服务端图片超过大小限制")
        return bytes(body)

    def close(self):
        self.client.close()


class SyncJournal:
    def __init__(self, root: Path, target: str):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "outbox.sqlite3"
        self.target = hashlib.sha256(target.encode()).hexdigest()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS entries (
                    target TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
                    local_value TEXT NOT NULL, remote_value TEXT NOT NULL DEFAULT 'null',
                    revision INTEGER NOT NULL DEFAULT 0, operation TEXT,
                    conflict INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(target,kind,key));
                CREATE TABLE IF NOT EXISTS assets (
                    target TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(target,digest));
            """)
            db.execute("INSERT OR IGNORE INTO meta VALUES('device_id',?)", (uuid4().hex,))
            self.device_id = db.execute("SELECT value FROM meta WHERE key='device_id'").fetchone()[0]
            db.execute("""UPDATE entries SET local_value='null' WHERE target=? AND kind='tasks'
                AND operation IS NULL AND key NOT LIKE ?""", (self.target, self.device_id + ":%"))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def meta(self, key, value=None):
        key = self.target + ":" + key
        with self.connect() as db:
            if value is not None:
                db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else ""

    def capture(self, records):
        with self.connect() as db:
            existing = {(row["kind"], row["key"]): row for row in db.execute(
                "SELECT * FROM entries WHERE target=?", (self.target,))}
            # Only explicit removal of a previously observed channel creates a tombstone.
            values = dict(records)
            for identity, row in existing.items():
                if identity[0] in {"channels", "topics"} and identity not in values and row["local_value"] != "null":
                    values[identity] = None
            for (kind, key), value in values.items():
                text = encoded(value)
                row = existing.get((kind, key))
                if row is None:
                    db.execute("INSERT INTO entries(target,kind,key,local_value,operation) VALUES(?,?,?,?,?)",
                               (self.target, kind, key, text, uuid4().hex))
                elif row["local_value"] != text:
                    db.execute("UPDATE entries SET local_value=?,operation=? WHERE target=? AND kind=? AND key=?",
                               (text, uuid4().hex, self.target, kind, key))

    def operations(self):
        with self.connect() as db:
            return [{"id": row["operation"], "kind": row["kind"], "key": row["key"],
                     "base_revision": row["revision"], "value": json.loads(row["local_value"])}
                    for row in db.execute(
                        "SELECT * FROM entries WHERE target=? AND operation IS NOT NULL AND conflict=0 LIMIT 50",
                        (self.target,))]

    def accept(self, response):
        with self.connect() as db:
            for result in response["results"]:
                row = result["record"]
                db.execute("""UPDATE entries SET remote_value=?,revision=?,
                    conflict=?,operation=CASE WHEN ?='applied' AND operation=? THEN NULL ELSE operation END
                    WHERE target=? AND kind=? AND key=?""",
                           (encoded(row["value"]), row["revision"], result["status"] == "conflict",
                            result["status"], result["id"], self.target, row["kind"], row["key"]))
            for row in response["changes"]:
                value = encoded(row["value"])
                db.execute("""INSERT INTO entries(target,kind,key,local_value,remote_value,revision)
                    VALUES(?,?,?,'null',?,?) ON CONFLICT(target,kind,key) DO UPDATE SET
                    remote_value=excluded.remote_value,revision=excluded.revision,
                    conflict=CASE WHEN entries.operation IS NOT NULL
                        AND entries.local_value != excluded.remote_value THEN 1 ELSE entries.conflict END
                    WHERE entries.revision < excluded.revision""",
                           (self.target, row["kind"], row["key"], value, row["revision"]))
            db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)",
                       (self.target + ":cursor", str(response["cursor"])))

    def incoming(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                """SELECT * FROM entries WHERE target=? AND operation IS NULL
                   AND local_value != remote_value""", (self.target,))]

    def applied(self, kind, key, value):
        with self.connect() as db:
            db.execute("UPDATE entries SET local_value=? WHERE target=? AND kind=? AND key=? AND operation IS NULL",
                       (encoded(value), self.target, kind, key))

    def resolve(self, choice):
        if choice not in {"local", "server"}:
            raise ValueError("冲突处理方式无效")
        with self.connect() as db:
            if choice == "local":
                for row in db.execute("SELECT kind,key FROM entries WHERE target=? AND conflict=1", (self.target,)).fetchall():
                    db.execute("UPDATE entries SET conflict=0,operation=? WHERE target=? AND kind=? AND key=?",
                               (uuid4().hex, self.target, row["kind"], row["key"]))
            else:
                db.execute("UPDATE entries SET conflict=0,operation=NULL WHERE target=? AND conflict=1", (self.target,))

    def counts(self):
        with self.connect() as db:
            row = db.execute("""SELECT COALESCE(SUM(operation IS NOT NULL),0),COALESCE(SUM(conflict),0)
                FROM entries WHERE target=?""", (self.target,)).fetchone()
            return {"pending": row[0], "conflicts": row[1]}

    def conflicts(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM entries WHERE target=? AND conflict=1 LIMIT 20", (self.target,)).fetchall()
        result = []
        for row in rows:
            local, remote = json.loads(row["local_value"]), json.loads(row["remote_value"])
            fields = sorted(set(local or {}) | set(remote or {}))
            differences = [{"field": field, "local": str((local or {}).get(field, "（无）"))[:200],
                            "server": str((remote or {}).get(field, "（无）"))[:200]}
                           for field in fields if (local or {}).get(field) != (remote or {}).get(field)]
            result.append({"kind": row["kind"], "key": row["key"], "fields": differences[:8],
                           "local_deleted": local is None, "server_deleted": remote is None})
        return result

    def asset_uploaded(self, digest, *, mark=False):
        with self.connect() as db:
            if mark:
                db.execute("INSERT OR IGNORE INTO assets VALUES(?,?)", (self.target, digest))
            return db.execute("SELECT 1 FROM assets WHERE target=? AND digest=?", (self.target, digest)).fetchone() is not None


class ServerSync:
    def __init__(self, config, channels, catalog, transfers, *, control_lock=None, is_busy=None):
        self.config = config
        self.channels, self.catalog, self.transfers = channels, catalog, transfers
        self.control_lock = control_lock or threading.Lock()
        self.is_busy = is_busy or (lambda: False)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.status = {"stage": "disabled", "error": "", "pending": 0, "conflicts": 0, "last_sync": ""}
        self.root = config.output_dir / ".server-sync"
        self.journal = None
        self.digest_cache = {}
        self.assets = {}
        self.database = None
        self.configure(config)

    @staticmethod
    def target(config):
        if config.data_server_backend == "http":
            return encoded(["http", config.data_server_url, config.data_server_workspace])
        return encoded([config.data_server_backend, config.data_server_db_host, config.data_server_db_port,
                        config.data_server_db_name, config.data_server_db_user, config.data_server_workspace])

    def configure(self, config):
        with self.lock:
            previous = self.target(self.config)
            if self.database:
                self.database.close()
                self.database = None
            self.config = config
            if previous != self.target(config):
                self.transfers.remote_tasks = []
            if config.data_server_enabled:
                # Preserve existing HTTP journals; database password rotation keeps its cursor.
                target = (encoded([config.data_server_url, config.data_server_workspace, config.data_server_token])
                          if config.data_server_backend == "http" else self.target(config))
                self.journal = SyncJournal(self.root, target)
                self.status.update(stage="waiting", error="", last_sync="", conflict_rows=[], **self.journal.counts())
            else:
                self.journal = None
                self.status.update(stage="disabled", error="", pending=0, conflicts=0, last_sync="", conflict_rows=[])
                self.transfers.remote_tasks = []
        self.wake.set()

    def snapshot(self):
        # Never wait for an HTTP request to render the local workbench.
        journal = self.journal
        return {**self.status, "enabled": self.config.data_server_enabled,
                "device_id": journal.device_id if journal else ""}

    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self._run, daemon=True, name="data-sync")
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=20)
        if not self.thread or not self.thread.is_alive():
            with self.lock:
                if self.database:
                    self.database.close()
                    self.database = None

    @contextmanager
    def client(self):
        if self.config.data_server_backend == "http":
            client = HTTPDataClient(self.config)
            try:
                yield client
            finally:
                client.close()
        else:
            if self.database is None:
                from .database_storage import SQLDatabase, database_engine
                engine = database_engine(self.config)
                try:
                    self.database = SQLDatabase(engine, self.config.data_server_workspace)
                except Exception:
                    engine.dispose()
                    raise
            yield self.database

    def _run(self):
        while not self.stop_event.is_set():
            self.wake.clear()
            self.sync_once()
            self.wake.wait(self.config.data_server_interval_seconds)

    def _image(self, value, root):
        try:
            path = Path(value).resolve()
            if not path.is_relative_to(Path(root).resolve()) or not image_type(path):
                return ""
            stat = path.stat()
            cache_key = str(path), stat.st_mtime_ns, stat.st_size
            digest = self.digest_cache.get(cache_key)
            if not digest:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                self.digest_cache[cache_key] = digest
            self.assets[digest] = path
            return digest
        except (OSError, ValueError, TypeError):
            return ""

    def collect(self):
        records = {}
        for row in self.channels.list()["channels"]:
            value = {key: row[key] for key in FIELDS["channels"] if key in row}
            digest = self._image(self.config.output_dir / ".channel-avatars" / f"{row.get('id', 0)}.jpg",
                                 self.config.output_dir / ".channel-avatars") if row.get("avatar_url") else ""
            if digest:
                value["avatar_digest"] = digest
            records["channels", row["key"]] = value
            for topic in row.get("topics", []):
                records["topics", topic["key"]] = {field: topic[field] for field in FIELDS["topics"] if field in topic}
        with self.catalog.lock:
            paths = list(self.catalog.catalog_root.glob("*.json"))
            if self.catalog.path.exists():
                paths.append(self.catalog.path)
            seen = set()
            for path in paths:
                current = self.catalog._read(path)
                if not current or current["channel"] in seen:
                    continue
                key = channel_key(current["channel"])
                seen.add(key)
                records["cursors", key] = {field: current[field] for field in FIELDS["cursors"] if field in current}
                for row in current["messages"]:
                    value = {field: row[field] for field in FIELDS["messages"] if field in row}
                    peer, topic_id = split_scope(key)
                    value.update(chat=peer, topic_id=topic_id)
                    for field in ("cover", "full_cover"):
                        digest = self._image(row.get(field + "_path"), self.catalog.preview_root)
                        if digest:
                            value[field + "_digest"] = digest
                    records["messages", f"{key}:{row['message_id']}"] = value
        with self.transfers.lock:
            tasks = copy.deepcopy(list(self.transfers.tasks.values()))
        for row in tasks:
            value = {field: row[field] for field in FIELDS["tasks"] if field in row}
            value["device_id"] = self.journal.device_id
            value["image_digests"] = [digest for path in row.get("image_paths", [])[:10]
                                      if (digest := self._image(path, row.get("storage_root", self.config.inbox_dir)))]
            records["tasks", f"{self.journal.device_id}:{row['id']}"] = value
        for (kind, key), value in records.items():
            validate_record(kind, key, value, self.journal.device_id)
        return records

    def _download_image(self, client, digest):
        if not digest:
            return None
        if not re.fullmatch(DIGEST, digest):
            raise ValueError("服务端图片标识无效")
        root = self.root / "assets"
        root.mkdir(parents=True, exist_ok=True)
        target = root / digest
        if target.is_file():
            body = target.read_bytes()
        else:
            body = client.get_asset(digest)
            if len(body) > MAX_ASSET or hashlib.sha256(body).hexdigest() != digest or not image_mime(body):
                raise ValueError("服务端图片校验失败")
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(body)
            temporary.replace(target)
        if len(body) > MAX_ASSET or hashlib.sha256(body).hexdigest() != digest or not image_mime(body):
            raise ValueError("本地同步图片校验失败")
        extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}[image_mime(body)]
        image = root / (digest + extension)
        if not image.exists():
            image.write_bytes(body)
        return image

    def _materialize(self, client, kind, value):
        value = copy.deepcopy(value)
        if value is None:
            return None
        if kind == "channels":
            digest = value.pop("avatar_digest", "")
            image = self._download_image(client, digest)
            value["avatar_url"] = ""
            if image and image_mime(image.read_bytes()) == "image/jpeg" and value.get("id"):
                target = self.config.output_dir / ".channel-avatars" / f"{value['id']}.jpg"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(image.read_bytes())
                value["avatar_url"] = f"/api/channel-avatar/{value['id']}?v={int(digest[:12], 16)}"
        elif kind == "messages":
            for prefix in ("cover", "full_cover"):
                digest = value.pop(prefix + "_digest", "")
                image = self._download_image(client, digest)
                if image:
                    target = self.catalog.preview_root / "synced" / image.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        target.write_bytes(image.read_bytes())
                    value[prefix + "_path"] = str(target.resolve())
                    value["full_status" if prefix == "full_cover" else "cover_status"] = "ready"
        elif kind == "tasks":
            digests = value.pop("image_digests", [])
            value["images"] = [] if value.get("record_removed_at") else [
                {"url": f"/api/synced-image/{image.name}", "name": image.name}
                for digest in digests if (image := self._download_image(client, digest))]
        return value

    def apply(self, staged):
        with self.channels.lock, self.catalog.lock, self.transfers.lock:
            for row, value in staged:
                kind, key = row["kind"], row["key"]
                if kind == "channels":
                    records = [item for item in self.channels.data["channels"] if item["key"] != key]
                    if value:
                        records.append(value)
                    self.channels._flush({"channels": records})
                elif kind == "topics":
                    records = [item for item in self.channels.data.get("topics", []) if item["key"] != key]
                    if value:
                        records.append(value)
                    self.channels._flush({**self.channels.data, "topics": records})
                elif kind in {"messages", "cursors"}:
                    chat = key.rsplit(":", 1)[0] if kind == "messages" else key
                    current = self.catalog._current(chat)
                    if kind == "messages":
                        messages = {item["message_id"]: item for item in current["messages"]}
                        previous = messages.get(value["message_id"], {})
                        messages[value["message_id"]] = {**previous, **value}
                        current["messages"] = sorted(messages.values(), key=lambda item: item["message_id"], reverse=True)[:300]
                    else:
                        current.update(value)
                    self.catalog.catalog_root.mkdir(parents=True, exist_ok=True)
                    path = self.catalog._path(chat)
                    temporary = path.with_suffix(".json.tmp")
                    temporary.write_text(encoded(current), encoding="utf-8")
                    temporary.replace(path)
                elif kind == "tasks":
                    # Foreign task records are view-only and never enter local execution state.
                    if value["device_id"] != self.journal.device_id or value["id"] not in self.transfers.tasks:
                        identity = "remote-" + key.replace(":", "-")
                        existing = [item for item in self.transfers.remote_tasks if item["id"] != identity]
                        if not value.get("record_removed_at"):
                            existing.append({**value, "id": identity, "remote_device": True, "local_path": ""})
                        self.transfers.remote_tasks = existing
                    else:
                        self.transfers.tasks[value["id"]].update({field: item for field, item in value.items()
                                                                 if field not in {"images", "device_id"}})
                        self.transfers.flush()
                self.journal.applied(kind, key, json.loads(row["remote_value"]))

    def resolve(self, choice):
        with self.lock:
            if not self.journal:
                raise ValueError("尚未启用服务器同步")
            self.journal.resolve(choice)
        self.wake.set()

    def sync_once(self):
        with self.lock:
            if not self.journal:
                return self.snapshot()
            self.status.update(stage="syncing", error="")
            try:
                self.journal.capture(self.collect())
                operations = self.journal.operations()
                config = self.config
                with self.client() as client:
                    health = client.health()
                    if health.get("protocol") != PROTOCOL_VERSION or not isinstance(health.get("server_id"), str):
                        raise ValueError("数据服务协议版本不兼容")
                    previous_server = self.journal.meta("server_id")
                    if previous_server and previous_server != health["server_id"]:
                        raise ValueError("服务器数据库已更换，请使用新的工作空间重新同步")
                    self.journal.meta("server_id", health["server_id"])
                    for operation in operations:
                        value = operation["value"] or {}
                        digests = [value.get(field) for field in ("avatar_digest", "cover_digest", "full_cover_digest")]
                        digests.extend(value.get("image_digests", []))
                        for digest in filter(None, digests):
                            if not self.journal.asset_uploaded(digest):
                                client.put_asset(digest, self.assets[digest].read_bytes())
                                self.journal.asset_uploaded(digest, mark=True)
                    data = client.exchange({
                        "workspace": config.data_server_workspace, "device_id": self.journal.device_id,
                        "cursor": int(self.journal.meta("cursor") or 0), "operations": operations,
                    })
                    if data.get("server_id") != health["server_id"] or type(data.get("cursor")) is not int:
                        raise ValueError("数据服务响应无效")
                    for row in data["changes"] + [result["record"] for result in data["results"]]:
                        validate_record(row["kind"], row["key"], row["value"])
                    # Preserve the base version of edits made while HTTP was in flight.
                    with self.control_lock:
                        self.journal.capture(self.collect())
                        self.journal.accept(data)
                        idle = not self.is_busy()
                    if idle:
                        staged = [(row, self._materialize(client, row["kind"], json.loads(row["remote_value"])))
                                  for row in self.journal.incoming()]
                        with self.control_lock:
                            self.journal.capture(self.collect())
                            ready = {(row["kind"], row["key"]) for row in self.journal.incoming()}
                            if not self.is_busy():
                                self.apply([(row, value) for row, value in staged if (row["kind"], row["key"]) in ready])
                    self.status.update(stage="connected", last_sync=utc_now(), **self.journal.counts(),
                                       conflict_rows=self.journal.conflicts())
                    if data.get("has_more") or self.journal.operations():
                        self.wake.set()
            except Exception as exc:
                # Do not echo HTTP URLs, authorization headers or user configuration.
                if isinstance(exc, httpx.HTTPStatusError):
                    message = f"数据服务返回 HTTP {exc.response.status_code}"
                elif isinstance(exc, httpx.TransportError):
                    message = "无法连接数据服务，变更已保留，稍后自动重试"
                elif isinstance(exc, SQLAlchemyError):
                    message = "数据库连接或读写失败，请检查地址、账号权限与 TLS 配置；变更已保留，将自动重试"
                elif isinstance(exc, ValueError):
                    message = str(exc)
                    for secret in (self.config.data_server_token, self.config.data_server_db_password):
                        if secret:
                            message = message.replace(secret, "[已隐藏]")
                else:
                    message = f"同步未完成（{type(exc).__name__}），本地数据已保留"
                self.status.update(stage="offline", error=message, **self.journal.counts(),
                                   conflict_rows=self.journal.conflicts())
            return self.snapshot()
