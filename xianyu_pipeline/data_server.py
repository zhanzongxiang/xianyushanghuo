"""Authenticated metadata service. Runs independently of Telegram and Quark."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from uuid import uuid4

from .data_protocol import DIGEST, MAX_ASSET, MAX_JSON, PROTOCOL_VERSION, encoded, validate_record


class DataDatabase:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records (
                    workspace TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
                    revision INTEGER NOT NULL, value TEXT NOT NULL, seq INTEGER NOT NULL,
                    PRIMARY KEY(workspace, kind, key));
                CREATE TABLE IF NOT EXISTS changes (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL,
                    kind TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL, value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS changes_workspace ON changes(workspace, seq);
                CREATE TABLE IF NOT EXISTS operations (
                    workspace TEXT NOT NULL, id TEXT NOT NULL, request TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(workspace, id));
            """)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('server_id', ?)", (uuid4().hex,))
            self.server_id = db.execute("SELECT value FROM meta WHERE key='server_id'").fetchone()[0]

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def record(row):
        return {"kind": row["kind"], "key": row["key"], "revision": row["revision"],
                "value": json.loads(row["value"]), "seq": row["seq"]}

    def exchange(self, payload: dict) -> dict:
        workspace, device = payload.get("workspace"), payload.get("device_id")
        cursor, operations = payload.get("cursor"), payload.get("operations")
        if (not isinstance(workspace, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", workspace)
                or not isinstance(device, str) or not re.fullmatch(r"[a-f0-9]{32}", device)
                or type(cursor) is not int or cursor < 0
                or not isinstance(operations, list) or len(operations) > 50):
            raise ValueError("同步请求格式无效")
        for operation in operations:
            if (not isinstance(operation, dict) or set(operation) != {"id", "kind", "key", "base_revision", "value"}
                    or not isinstance(operation["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", operation["id"])
                    or type(operation["base_revision"]) is not int or operation["base_revision"] < 0):
                raise ValueError("同步操作格式无效")
            validate_record(operation["kind"], operation["key"], operation["value"], device)
        results = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for operation in operations:
                op_id, kind, key = operation["id"], operation["kind"], operation["key"]
                request = encoded(operation)
                previous = db.execute("SELECT request, result FROM operations WHERE workspace=? AND id=?",
                                      (workspace, op_id)).fetchone()
                if previous:
                    if previous["request"] != request:
                        raise ValueError("同步操作 ID 已用于其他内容")
                    results.append(json.loads(previous["result"]))
                    continue
                row = db.execute("SELECT * FROM records WHERE workspace=? AND kind=? AND key=?",
                                 (workspace, kind, key)).fetchone()
                value = encoded(operation["value"])
                revision = row["revision"] if row else 0
                if row and row["value"] == value:
                    result = {"id": op_id, "status": "applied", "record": self.record(row)}
                elif operation["base_revision"] != revision:
                    result = {"id": op_id, "status": "conflict", "record": self.record(row) if row else
                              {"kind": kind, "key": key, "value": None, "revision": 0, "seq": 0}}
                else:
                    seq = db.execute(
                        "INSERT INTO changes(workspace,kind,key,revision,value) VALUES(?,?,?,?,?)",
                        (workspace, kind, key, revision + 1, value)).lastrowid
                    db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?)",
                               (workspace, kind, key, revision + 1, value, seq))
                    result = {"id": op_id, "status": "applied",
                              "record": {"kind": kind, "key": key, "value": operation["value"],
                                         "revision": revision + 1, "seq": seq}}
                db.execute("INSERT INTO operations VALUES(?,?,?,?)",
                           (workspace, op_id, request, encoded(result)))
                results.append(result)
            rows = db.execute("SELECT * FROM changes WHERE workspace=? AND seq>? ORDER BY seq LIMIT 251",
                              (workspace, cursor)).fetchall()
            return {"protocol": PROTOCOL_VERSION, "server_id": self.server_id, "results": results,
                    "changes": [self.record(row) for row in rows[:250]],
                    "cursor": rows[min(len(rows), 250) - 1]["seq"] if rows else cursor,
                    "has_more": len(rows) > 250}


def image_mime(body: bytes) -> str | None:
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "image/webp"
    return None


def create_server(address, root: Path, token: str) -> ThreadingHTTPServer:
    if not isinstance(token, str) or len(token) < 32 or not token.isascii() or any(char.isspace() for char in token):
        raise ValueError("DATA_SERVER_TOKEN 必须是至少 32 位、不含空白的 ASCII 随机令牌")
    root = Path(root).resolve()
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    database = DataDatabase(root / "business.sqlite3")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, fmt, *args):
            pass  # Request URLs and headers never enter logs.

        def respond(self, status, value):
            body = encoded(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def authenticated(self):
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
                self.respond(401, {"message": "数据服务认证失败"})
                return False
            return True

        def read_body(self, limit):
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("不支持分块请求体")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= limit:
                raise ValueError("请求体大小无效")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("请求体不完整")
            return body

        def do_GET(self):
            if not self.authenticated():
                return
            path = urlsplit(self.path).path
            if path == "/v1/health":
                self.respond(200, {"protocol": PROTOCOL_VERSION, "server_id": database.server_id})
            elif re.fullmatch("/v1/assets/" + DIGEST, path):
                asset = assets / path.rsplit("/", 1)[1]
                if not asset.is_file():
                    self.respond(404, {"message": "图片不存在"})
                    return
                body = asset.read_bytes()
                mime = image_mime(body)
                if not mime:
                    self.respond(404, {"message": "图片无效"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.respond(404, {"message": "接口不存在"})

        def do_POST(self):
            if not self.authenticated():
                return
            if urlsplit(self.path).path != "/v1/sync":
                self.respond(404, {"message": "接口不存在"})
                return
            try:
                payload = json.loads(self.read_body(MAX_JSON))
                if not isinstance(payload, dict):
                    raise ValueError("请求必须是 JSON 对象")
                self.respond(200, database.exchange(payload))
            except (ValueError, TypeError, KeyError, UnicodeError):
                self.respond(400, {"message": "同步数据格式无效"})
            except sqlite3.Error:
                self.respond(503, {"message": "数据库暂时不可用"})

        def do_PUT(self):
            if not self.authenticated():
                return
            path = urlsplit(self.path).path
            if not re.fullmatch("/v1/assets/" + DIGEST, path):
                self.respond(404, {"message": "图片标识无效"})
                return
            temporary = assets / (uuid4().hex + ".tmp")
            try:
                body = self.read_body(MAX_ASSET)
                digest = path.rsplit("/", 1)[1]
                if hashlib.sha256(body).hexdigest() != digest or not image_mime(body):
                    raise ValueError("图片内容校验失败")
                temporary.write_bytes(body)
                temporary.replace(assets / digest)
                self.respond(200, {"digest": digest})
            except (ValueError, UnicodeError):
                self.respond(400, {"message": "图片内容无效或过大"})
            finally:
                temporary.unlink(missing_ok=True)

    return ThreadingHTTPServer(address, Handler)


def main():
    parser = argparse.ArgumentParser(description="频道与任务数据服务，不运行下载上传")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--data-dir", type=Path, default=Path("server_data"))
    args = parser.parse_args()
    server = create_server((args.host, args.port), args.data_dir, os.environ.get("DATA_SERVER_TOKEN", ""))
    print(f"Data service: {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
