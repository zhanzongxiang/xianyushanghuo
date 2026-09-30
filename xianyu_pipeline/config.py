from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class PipelineConfig:
    """流水线配置；默认仅生成待审核结果，不直接发布闲鱼。"""

    source_chat: str = ""
    source_channel_url: str = ""
    fetch_mode: str = "message_ids"
    recent_message_limit: int = 20
    telegram_run_mode: str = "once"
    telegram_scan_limit: int = 200
    message_ids: list[int] = field(default_factory=list)
    inbox_dir: Path = Path("data/inbox")
    work_dir: Path = Path("data/work")
    output_dir: Path = Path("data/output")
    cloud_folder: str = ""
    upload_provider: str = "local"
    telegram_access_mode: str = "api"
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_session: str = "data/telegram.session"
    telegram_browser_profile: Path = Path("data/telegram_browser_profile")
    browser_executable_path: str = ""
    telegram_browser_headless: bool = False
    telegram_browser_timeout_seconds: int = 180
    download_start_timeout_seconds: int = 180
    download_start_grace_seconds: int = 30
    download_transfer_timeout_seconds: int = 3600
    archive_test_executable_path: str = ""
    archive_test_timeout_seconds: int = 600
    telegram_message_selector: str = ""
    telegram_attachment_selector: str = ""
    quark_cookie: str = ""
    quark_share_expire_days: int = 0
    quark_folder_url: str = ""
    quark_browser_profile: Path = Path("data/quark_browser_profile")
    quark_headless: bool = True
    quark_timeout_seconds: int = 180
    quark_upload_input_selector: str = ""
    quark_file_row_selector: str = ""
    quark_share_button_selector: str = ""
    quark_share_link_selector: str = ""
    make_sfx: bool = False
    allow_executable: bool = False
    require_manual_review: bool = True
    allowed_file_extensions: tuple[str, ...] = (
        ".zip",
        ".7z",
        ".rar",
        ".pdf",
        ".doc",
        ".docx",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
    )

    @classmethod
    def from_json(cls, path: str | Path) -> "PipelineConfig":
        config_path = Path(path).resolve()
        payload: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
        # 源码配置通常位于项目 config/；便携包配置位于程序根目录。
        base_dir = config_path.parent.parent if config_path.parent.name == "config" else config_path.parent
        for key in ("inbox_dir", "work_dir", "output_dir", "telegram_session", "telegram_browser_profile", "quark_browser_profile"):
            if key in payload:
                value = Path(payload[key])
                payload[key] = value if value.is_absolute() else base_dir / value
        # 配置随便携包升级，但网页登录态应留在当前 Windows 用户的固定目录。
        # 只迁移默认相对路径；显式自定义或绝对路径继续遵循用户配置。
        if payload.get("telegram_browser_profile") == base_dir / "sample_data/telegram_browser_profile":
            app_data = os.environ.get("LOCALAPPDATA")
            if app_data:
                payload["telegram_browser_profile"] = Path(app_data) / "XianYuMaterialPipeline" / "telegram_browser_profile"
        if "allowed_file_extensions" in payload:
            payload["allowed_file_extensions"] = tuple(payload["allowed_file_extensions"])
        return cls(**payload)

    def ensure_directories(self) -> None:
        for directory in (self.inbox_dir, self.work_dir, self.output_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def browser_download_options(self) -> dict[str, Any]:
        return {
            "browser_executable_path": self.browser_executable_path,
            "start_timeout_seconds": self.download_start_timeout_seconds,
            "start_grace_seconds": self.download_start_grace_seconds,
            "transfer_timeout_seconds": self.download_transfer_timeout_seconds,
            "archive_test_executable_path": self.archive_test_executable_path,
            "archive_test_timeout_seconds": self.archive_test_timeout_seconds,
        }
