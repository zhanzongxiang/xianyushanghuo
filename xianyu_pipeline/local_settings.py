"""Keep local Telegram settings across clean portable upgrades."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path

from .adapters import parse_telegram_api_proxy, parse_telegram_dc2_server


TELEGRAM_SETTINGS = (
    "telegram_api_id",
    "telegram_api_hash",
    "telegram_api_proxy_url",
    "telegram_api_dc2_server",
)


def _defaults_path(config_path: Path) -> Path | None:
    app_data = os.environ.get("LOCALAPPDATA")
    if config_path.name != "test.local.json" or not app_data:
        return None
    return Path(app_data) / "XianYuMaterialPipeline" / "telegram-settings.json"


def _read_settings(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logging.warning("本机 Telegram 配置不可读取，保留当前配置：%s", path)
        return {}
    return value if isinstance(value, dict) else {}


def _telegram_values(payload: dict) -> dict | None:
    api_id = payload.get("telegram_api_id")
    api_hash = payload.get("telegram_api_hash")
    if (type(api_id) is not int or not 1 <= api_id <= 2147483647
            or not isinstance(api_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash)):
        return None
    values = {key: payload.get(key, "") for key in TELEGRAM_SETTINGS}
    try:
        parse_telegram_api_proxy(values["telegram_api_proxy_url"])
        parse_telegram_dc2_server(values["telegram_api_dc2_server"])
    except (ValueError, TypeError):
        return None
    return values


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def remember_telegram_settings(config_path: Path, payload: dict) -> None:
    defaults = _defaults_path(config_path)
    values = _telegram_values(payload)
    if defaults is None or values is None:
        return
    try:
        _write_json(defaults, values)
    except OSError:
        logging.warning("配置文件已保存，但本机 Telegram 升级配置未同步：%s", defaults)


def _legacy_configs(config_path: Path) -> list[Path]:
    directory = config_path.parent
    if directory.name == "config":
        root = directory.parent
        outputs = root / "outputs"
    elif re.fullmatch(r"XianYuMaterialPipeline_v[0-9]+_[A-Za-z0-9_]+", directory.name):
        outputs = directory.parent
        root = outputs.parent
    else:
        return []
    candidates = [root / "config" / "test.local.json", outputs / "workbench.local.json"]
    candidates.extend(outputs.glob("XianYuMaterialPipeline_v*/test.local.json"))
    available = []
    for candidate in candidates:
        try:
            if candidate.resolve() != config_path and candidate.is_file():
                available.append((candidate.stat().st_mtime, candidate))
        except OSError:
            continue
    return [path for _, path in sorted(available, key=lambda item: item[0], reverse=True)]


def restore_telegram_settings(config_path: str | Path) -> None:
    """Restore only a clean template; never overwrite an existing account or direct mode."""
    config_path = Path(config_path).resolve()
    defaults = _defaults_path(config_path)
    if defaults is None:
        return
    current = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(current, dict):
        return
    if current.get("telegram_api_id") or current.get("telegram_api_hash"):
        if not defaults.exists():
            remember_telegram_settings(config_path, current)
        return
    values = _telegram_values(_read_settings(defaults)) if defaults.exists() else None
    if values is None:
        for candidate in _legacy_configs(config_path):
            values = _telegram_values(_read_settings(candidate))
            if values is not None:
                break
    if values is None:
        return
    current.update(values)
    _write_json(config_path, current)
    remember_telegram_settings(config_path, current)
    logging.info("已恢复本机保存的 Telegram API 与网络配置：%s", config_path)
