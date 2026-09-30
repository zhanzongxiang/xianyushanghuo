from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class SourceMessage:
    message_id: int
    chat: str
    text: str = ""
    attachment_paths: list[Path] = field(default_factory=list)
    received_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class ProductCandidate:
    candidate_id: str
    source_message_id: int
    title: str
    source_text: str = ""
    files: list[Path] = field(default_factory=list)
    images: list[Path] = field(default_factory=list)
    delivery_path: Path | None = None
    share_url: str = ""
    status: str = "pending_review"
    warnings: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["files"] = [str(item) for item in self.files]
        data["images"] = [str(item) for item in self.images]
        data["delivery_path"] = str(self.delivery_path) if self.delivery_path else ""
        return data
