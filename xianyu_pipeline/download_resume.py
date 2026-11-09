"""Local checkpoints for verified Telegram file chunks, without account secrets."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path


class DownloadCheckpoint:
    def __init__(self, root: Path, *, session: str, peer: str, message_id: int,
                 document_id: int | None, filename: str, size: int, chunk_size: int):
        identity = json.dumps([str(Path(session).resolve()), peer, message_id, document_id,
                               filename, size, chunk_size], ensure_ascii=True)
        key = hashlib.sha256(identity.encode()).hexdigest()
        self.directory = root / ".unverified" / f"telegram-{key}"
        self.source = self.directory / filename
        self.path = self.directory / "resume.json"
        self.metadata = {"version": 1, "identity": key, "size": size, "chunk_size": chunk_size}
        self.size, self.chunk_size = size, chunk_size
        self.completed: dict[int, str] = {}
        self.received = 0
        self.dirty = 0
        self.last_flush = time.monotonic()
        self.lease = None

    def acquire(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lease = (self.directory / "download.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if not self.lease.seek(0, os.SEEK_END):
                    self.lease.write(b"\0")
                    self.lease.flush()
                self.lease.seek(0)
                msvcrt.locking(self.lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lease.close()
            self.lease = None
            raise RuntimeError("同一文件正在其他任务中下载，请等待该任务结束") from exc

    def release(self) -> None:
        if self.lease is not None:
            self.lease.close()
            self.lease = None

    def restore(self) -> int:
        """Only trust saved chunks whose bytes still match their checkpoint hash."""
        self.completed.clear()
        self.received = 0
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(stored, dict)
                    or any(stored.get(key) != value for key, value in self.metadata.items())
                    or not isinstance(stored.get("chunks"), dict)):
                return 0
            with self.source.open("rb") as source:
                length = source.seek(0, os.SEEK_END)
                for value, digest in stored["chunks"].items():
                    if (len(value) > 20 or not value.isascii() or not value.isdecimal()
                            or not isinstance(digest, str)):
                        continue
                    index = int(value)
                    offset = index * self.chunk_size
                    count = min(self.chunk_size, self.size - offset)
                    if count <= 0 or offset + count > length:
                        continue
                    source.seek(offset)
                    content = source.read(count)
                    if len(content) == count and hashlib.sha256(content).hexdigest() == digest:
                        self.completed[index] = digest
                        self.received += count
        except (OSError, ValueError, TypeError):
            self.completed.clear()
            self.received = 0
        return self.received

    def record(self, index: int, content) -> None:
        if index not in self.completed:
            self.received += len(content)
        self.completed[index] = hashlib.sha256(content).hexdigest()
        self.dirty += 1

    def flush(self, output, *, force: bool = False) -> None:
        if not self.dirty:
            return
        now = time.monotonic()
        if not force and self.dirty < 16 and now - self.last_flush < 1:
            return
        # Commit the bytes before the map; a crash may re-fetch a chunk, never skip a hole.
        output.flush()
        os.fsync(output.fileno())
        temporary = self.path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump({**self.metadata, "chunks": self.completed}, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)
        self.dirty = 0
        self.last_flush = now

    def discard(self) -> None:
        self.path.unlink(missing_ok=True)
