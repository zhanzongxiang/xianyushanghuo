"""Direct database storage; all application logic executes on the local computer."""
from __future__ import annotations

import hashlib
import json
import re
import ssl
from uuid import uuid4

from sqlalchemy import (BigInteger, Column, LargeBinary, MetaData, String, Table,
                        Text, create_engine, event, select, text)
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from .data_protocol import DIGEST, MAX_ASSET, PROTOCOL_VERSION, encoded, validate_exchange
from .data_server import image_mime


def database_engine(config):
    backend = config.data_server_backend
    arguments = {"connect_timeout": 5}
    if backend == "postgresql":
        arguments.update(sslmode=config.data_server_db_tls,
                         options="-c statement_timeout=15000 -c lock_timeout=10000")
        if config.data_server_db_ca:
            arguments["sslrootcert"] = config.data_server_db_ca
        driver = "postgresql+psycopg"
    elif backend == "mysql":
        driver = "mysql+pymysql"
        arguments.update(read_timeout=15, write_timeout=15, charset="utf8mb4")
        if config.data_server_db_tls != "disable":
            context = ssl.create_default_context(cafile=config.data_server_db_ca or None)
            if config.data_server_db_tls == "require":
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            arguments["ssl"] = context
    else:
        raise ValueError("请选择 PostgreSQL 或 MySQL 数据库")
    url = URL.create(driver, username=config.data_server_db_user, password=config.data_server_db_password,
                     host=config.data_server_db_host, port=config.data_server_db_port,
                     database=config.data_server_db_name)
    engine = create_engine(url, connect_args=arguments, poolclass=NullPool, hide_parameters=True)
    if backend == "mysql" and config.data_server_db_tls != "disable":
        @event.listens_for(engine, "connect")
        def require_tls(connection, _):
            # PyMySQL can otherwise fall back to plaintext when the server lacks TLS.
            if not isinstance(getattr(connection, "_sock", None), ssl.SSLSocket):
                connection.close()
                raise ValueError("数据库未建立 TLS 加密连接，请配置服务器 TLS 或使用本机 SSH 隧道")
    return engine


class SQLDatabase:
    def __init__(self, engine, workspace: str):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", workspace):
            raise ValueError("工作空间无效")
        self.engine, self.workspace = engine, workspace
        self.metadata = MetaData()
        large_text = Text().with_variant(mysql.MEDIUMTEXT(), "mysql")
        binary = LargeBinary().with_variant(mysql.MEDIUMBLOB(), "mysql")

        def table(name, *columns):
            return Table("xy_" + name, self.metadata, *columns,
                         mysql_charset="utf8mb4", mysql_collate="utf8mb4_bin", mysql_engine="InnoDB")

        self.meta = table("meta", Column("key", String(64), primary_key=True),
                          Column("value", String(128), nullable=False))
        self.workspaces = table("workspaces", Column("workspace", String(64), primary_key=True),
                                Column("seq", BigInteger, nullable=False))
        self.records = table("records", Column("workspace", String(64), primary_key=True),
                             Column("kind", String(16), primary_key=True),
                             Column("key", String(256), primary_key=True),
                             Column("revision", BigInteger, nullable=False),
                             Column("value", large_text, nullable=False),
                             Column("seq", BigInteger, nullable=False))
        self.changes = table("changes", Column("workspace", String(64), primary_key=True),
                             Column("seq", BigInteger, primary_key=True),
                             Column("kind", String(16), nullable=False),
                             Column("key", String(256), nullable=False),
                             Column("revision", BigInteger, nullable=False),
                             Column("value", large_text, nullable=False))
        self.operations = table("operations", Column("workspace", String(64), primary_key=True),
                                Column("id", String(32), primary_key=True),
                                Column("request", large_text, nullable=False),
                                Column("result", large_text, nullable=False))
        self.assets = table("assets", Column("workspace", String(64), primary_key=True),
                            Column("digest", String(64), primary_key=True),
                            Column("body", binary, nullable=False))
        self._initialize()

    def _initialize(self):
        with self.engine.begin() as db:
            dialect = db.dialect.name
            if dialect == "postgresql":
                db.execute(text("SELECT pg_advisory_xact_lock(7867140101)"))
            elif dialect == "mysql":
                if db.execute(text("SELECT GET_LOCK('xianyu_schema_v1', 10)")).scalar_one() != 1:
                    raise ValueError("数据库初始化正在进行，请稍后重试")
            try:
                self.metadata.create_all(db)
                for key, value in (("server_id", uuid4().hex), ("schema_version", str(PROTOCOL_VERSION))):
                    self._insert_once(db, self.meta, {"key": key, "value": value}, ["key"])
                version = db.execute(select(self.meta.c.value).where(
                    self.meta.c.key == "schema_version")).scalar_one()
                if version != str(PROTOCOL_VERSION):
                    raise ValueError("数据库结构版本不兼容")
            finally:
                if dialect == "mysql":
                    db.execute(text("SELECT RELEASE_LOCK('xianyu_schema_v1')"))

    @staticmethod
    def _insert_once(db, table, values, keys):
        dialect = db.dialect.name
        if dialect == "postgresql":
            statement = postgresql.insert(table).values(**values).on_conflict_do_nothing(index_elements=keys)
        elif dialect == "mysql":
            statement = mysql.insert(table).values(**values).on_duplicate_key_update(**{keys[0]: values[keys[0]]})
        else:
            statement = sqlite.insert(table).values(**values).on_conflict_do_nothing(index_elements=keys)
        db.execute(statement)

    @staticmethod
    def record(row):
        return {"kind": row["kind"], "key": row["key"], "revision": row["revision"],
                "value": json.loads(row["value"]), "seq": row["seq"]}

    def health(self):
        with self.engine.connect() as db:
            values = dict(db.execute(select(self.meta)).tuples().all())
        if values.get("schema_version") != str(PROTOCOL_VERSION) or not values.get("server_id"):
            raise ValueError("数据库结构版本或身份无效")
        return {"protocol": PROTOCOL_VERSION, "server_id": values["server_id"]}

    def exchange(self, payload):
        workspace, cursor, operations = validate_exchange(payload)
        if workspace != self.workspace:
            raise ValueError("工作空间不匹配")
        results = []
        with self.engine.begin() as db:
            if db.dialect.name == "sqlite":
                db.exec_driver_sql("BEGIN IMMEDIATE")
            self._insert_once(db, self.workspaces, {"workspace": workspace, "seq": 0}, ["workspace"])
            # Serialize commits within a workspace so clients never skip a later-committing change.
            seq = db.execute(select(self.workspaces.c.seq).where(
                self.workspaces.c.workspace == workspace).with_for_update()).scalar_one()
            for operation in operations:
                op_id, kind, key = operation["id"], operation["kind"], operation["key"]
                request = encoded(operation)
                previous = db.execute(select(self.operations).where(
                    self.operations.c.workspace == workspace, self.operations.c.id == op_id)).mappings().first()
                if previous:
                    if previous["request"] != request:
                        raise ValueError("同步操作 ID 已用于其他内容")
                    results.append(json.loads(previous["result"]))
                    continue
                identity = (self.records.c.workspace == workspace, self.records.c.kind == kind,
                            self.records.c.key == key)
                row = db.execute(select(self.records).where(*identity)).mappings().first()
                revision, value = (row["revision"] if row else 0), encoded(operation["value"])
                if row and row["value"] == value:
                    result = {"id": op_id, "status": "applied", "record": self.record(row)}
                elif revision != operation["base_revision"]:
                    result = {"id": op_id, "status": "conflict", "record": self.record(row) if row else
                              {"kind": kind, "key": key, "value": None, "revision": 0, "seq": 0}}
                else:
                    seq += 1
                    changes = dict(workspace=workspace, kind=kind, key=key, revision=revision + 1,
                                   value=value, seq=seq)
                    db.execute(self.changes.insert().values(**changes))
                    if row:
                        db.execute(self.records.update().where(*identity).values(**changes))
                    else:
                        db.execute(self.records.insert().values(**changes))
                    result = {"id": op_id, "status": "applied",
                              "record": {"kind": kind, "key": key, "revision": revision + 1,
                                         "value": operation["value"], "seq": seq}}
                db.execute(self.operations.insert().values(
                    workspace=workspace, id=op_id, request=request, result=encoded(result)))
                results.append(result)
            db.execute(self.workspaces.update().where(self.workspaces.c.workspace == workspace).values(seq=seq))
            rows = db.execute(select(self.changes).where(
                self.changes.c.workspace == workspace, self.changes.c.seq > cursor
            ).order_by(self.changes.c.seq).limit(251)).mappings().all()
            identity = db.execute(select(self.meta.c.value).where(self.meta.c.key == "server_id")).scalar_one()
            return {"protocol": PROTOCOL_VERSION, "server_id": identity, "results": results,
                    "changes": [self.record(row) for row in rows[:250]],
                    "cursor": rows[min(len(rows), 250) - 1]["seq"] if rows else cursor, "has_more": len(rows) > 250}

    def put_asset(self, digest, body):
        if (not re.fullmatch(DIGEST, digest) or not 0 < len(body) <= MAX_ASSET
                or hashlib.sha256(body).hexdigest() != digest or not image_mime(body)):
            raise ValueError("图片内容校验失败或过大")
        with self.engine.begin() as db:
            self._insert_once(db, self.assets, {"workspace": self.workspace, "digest": digest, "body": body},
                              ["workspace", "digest"])

    def get_asset(self, digest):
        if not re.fullmatch(DIGEST, digest):
            raise ValueError("图片标识无效")
        with self.engine.connect() as db:
            body = db.execute(select(self.assets.c.body).where(
                self.assets.c.workspace == self.workspace, self.assets.c.digest == digest)).scalar_one_or_none()
        if body is None:
            raise ValueError("数据库中尚无此预览图片")
        return bytes(body)

    def close(self):
        self.engine.dispose()
