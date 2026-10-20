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
    source_channel_url: str = "https://t.me/free_3d_models_download"
    fetch_mode: str = "message_ids"
    recent_message_limit: int = 20
    channel_message_limit: int = 100
    telegram_run_mode: str = "once"
    telegram_scan_limit: int = 200
    message_ids: list[int] = field(default_factory=list)
    inbox_dir: Path = Path("data/inbox")
    work_dir: Path = Path("data/work")
    output_dir: Path = Path("data/output")
    data_server_enabled: bool = False
    data_server_url: str = ""
    data_server_token: str = ""
    data_server_workspace: str = "default"
    data_server_interval_seconds: int = 10
    cloud_folder: str = ""
    upload_provider: str = "local"
    auto_upload_after_download: bool = False
    rename_translation_provider: str = "mymemory"
    rename_glossary: str = ""
    telegram_access_mode: str = "api"
    exclude_forwarded_posts: bool = False
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_api_proxy_url: str = ""
    telegram_api_dc2_server: str = ""
    telegram_session: str = "data/telegram.session"
    telegram_browser_profile: Path = Path("data/telegram_browser_profile")
    browser_executable_path: str = ""
    telegram_browser_headless: bool = False
    telegram_browser_timeout_seconds: int = 180
    download_start_timeout_seconds: int = 180
    download_start_grace_seconds: int = 30
    download_transfer_timeout_seconds: int = 3600
    download_workers: int = 4
    download_file_concurrency: int = 2
    archive_test_executable_path: str = ""
    archive_test_timeout_seconds: int = 600
    telegram_message_selector: str = ""
    telegram_attachment_selector: str = ""
    quark_cookie: str = ""
    quark_api_parent_fid: str = "0"
    quark_api_timeout_seconds: int = 60
    quark_api_file_concurrency: int = 2
    quark_share_expire_days: int = 7
    quark_folder_url: str = ""
    quark_browser_profile: Path = Path("data/quark_browser_profile")
    quark_headless: bool = True
    quark_timeout_seconds: int = 180
    quark_upload_input_selector: str = ""
    quark_file_row_selector: str = ""
    quark_share_button_selector: str = ""
    quark_share_link_selector: str = ""
    quark_share_create_button_selector: str = ""
    quark_folder_ready_selector: str = ""
    quark_cli_mode: str = "wsl"
    quark_cli_executable_path: str = "wsl.exe"
    quark_cli_wsl_distribution: str = ""
    quark_cli_node_command: str = "node"
    quark_cli_script_path: str = ""
    quark_cli_parent_fid: str = ""
    quark_cli_timeout_seconds: int = 3600
    quark_cli_share_expired_type: int = 1
    quark_cli_share_url_type: int = 1
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

    def __post_init__(self) -> None:
        from .data_protocol import validate_endpoint
        import re
        if type(self.data_server_enabled) is not bool:
            raise ValueError("data_server_enabled 必须为布尔值")
        self.data_server_url = validate_endpoint(self.data_server_url)
        if (not isinstance(self.data_server_token, str) or
                (self.data_server_token and (len(self.data_server_token) < 32
                 or not self.data_server_token.isascii() or any(char.isspace() for char in self.data_server_token)))):
            raise ValueError("数据服务令牌须为至少 32 位、不含空白的 ASCII 字符")
        if not isinstance(self.data_server_workspace, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.data_server_workspace):
            raise ValueError("工作空间须为 1 至 64 位字母、数字、下划线或连字符")
        if type(self.data_server_interval_seconds) is not int or not 5 <= self.data_server_interval_seconds <= 300:
            raise ValueError("同步间隔须为 5 至 300 秒")
        if self.data_server_enabled and (not self.data_server_url or not self.data_server_token):
            raise ValueError("启用服务器同步时必须填写数据服务地址和令牌")
        if type(self.download_workers) is not int or not 1 <= self.download_workers <= 8:
            raise ValueError("download_workers 必须为 1 到 8 的整数")
        if type(self.download_file_concurrency) is not int or not 1 <= self.download_file_concurrency <= 2:
            raise ValueError("download_file_concurrency 必须为 1 到 2 的整数")
        if type(self.quark_api_file_concurrency) is not int or not 1 <= self.quark_api_file_concurrency <= 3:
            raise ValueError("quark_api_file_concurrency 必须为 1 到 3 的整数")
        if type(self.quark_api_timeout_seconds) is not int or not 10 <= self.quark_api_timeout_seconds <= 600:
            raise ValueError("quark_api_timeout_seconds 必须为 10 到 600 的整数秒")

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
        if payload.get("telegram_session") == base_dir / "sample_data/telegram.session":
            app_data = os.environ.get("LOCALAPPDATA")
            if app_data:
                payload["telegram_session"] = str(Path(app_data) / "XianYuMaterialPipeline" / "telegram_api.session")
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

    def cloud_upload_target(self) -> str:
        if self.upload_provider == "quark_api":
            return self.quark_api_parent_fid
        if self.upload_provider == "quark_browser":
            return self.quark_folder_url
        if self.upload_provider == "quark_cli":
            return self.quark_cli_parent_fid
        return self.cloud_folder
