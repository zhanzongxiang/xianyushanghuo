from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path

from .adapters import CloudStorageAdapter, TelegramAdapter
from .config import PipelineConfig
from .models import ProductCandidate, SourceMessage

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar"}
EXECUTABLE_EXTENSIONS = {".exe", ".bat", ".cmd", ".scr", ".js", ".vbs", ".ps1"}


class PipelineError(RuntimeError):
    pass


def safe_name(value: str) -> str:
    value = re.sub(r"[^\w\-\u4e00-\u9fff ]+", "", value, flags=re.UNICODE).strip()
    return value[:80] or "未命名商品"


def derive_title(message: SourceMessage, files: list[Path]) -> str:
    first_line = next((line.strip() for line in message.text.splitlines() if line.strip()), "")
    if first_line and first_line != "Local inbox fixture":
        return safe_name(first_line)
    return safe_name(files[0].stem if files else f"消息-{message.message_id}")


def expand_zip(source: Path, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    with zipfile.ZipFile(source) as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise PipelineError(f"检测到不安全的压缩包路径: {member.filename}")
        archive.extractall(destination)
        extracted = [path for path in destination.rglob("*") if path.is_file()]
    return extracted


def prepare_files(message: SourceMessage, config: PipelineConfig) -> tuple[list[Path], list[str]]:
    warnings: list[str] = []
    prepared: list[Path] = []
    message_dir = config.work_dir / f"message_{message.message_id}"
    message_dir.mkdir(parents=True, exist_ok=True)

    for source in message.attachment_paths:
        if not source.exists() or not source.is_file():
            warnings.append(f"附件不存在: {source}")
            continue
        if source.suffix.lower() in EXECUTABLE_EXTENSIONS and not config.allow_executable:
            warnings.append(f"拒绝可执行文件: {source.name}")
            continue
        if source.suffix.lower() not in config.allowed_file_extensions:
            warnings.append(f"跳过未允许的文件类型: {source.name}")
            continue
        destination = message_dir / source.name
        shutil.copy2(source, destination)
        if source.suffix.lower() == ".zip":
            extracted_dir = message_dir / f"{source.stem}_extracted"
            prepared.extend(expand_zip(destination, extracted_dir))
        else:
            prepared.append(destination)

    # 默认不制作 EXE；即使配置要求，也必须显式开启安全开关。
    if config.make_sfx and not config.allow_executable:
        warnings.append("已阻止制作 EXE：allow_executable=false。建议交付 ZIP/7z。")
    return prepared, warnings


def match_assets(files: list[Path]) -> tuple[list[Path], list[Path]]:
    images = [path for path in files if path.suffix.lower() in IMAGE_EXTENSIONS]
    payload = [path for path in files if path.suffix.lower() not in IMAGE_EXTENSIONS]
    return images, payload


def fingerprint(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: str(item).lower()):
        digest.update(path.name.encode("utf-8"))
        digest.update(str(path.stat().st_size).encode("ascii"))
    return digest.hexdigest()[:12]


def process_messages(config: PipelineConfig, messages: list[SourceMessage], cloud: CloudStorageAdapter) -> list[ProductCandidate]:
    config.ensure_directories()
    manifest = config.output_dir / "manifest.json"
    previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
    candidates: list[ProductCandidate] = []
    for message in messages:
        files, warnings = prepare_files(message, config)
        images, payload = match_assets(files)
        if not images:
            warnings.append("未匹配到展示图")
        if not payload:
            warnings.append("未匹配到交付文件")

        candidate_id = f"msg-{message.message_id}-{fingerprint(files)}"
        candidate = ProductCandidate(
            candidate_id=candidate_id,
            source_message_id=message.message_id,
            title=derive_title(message, payload or files),
            source_text=message.text,
            files=payload,
            images=images,
            warnings=warnings,
        )
        if payload and not warnings:
            upload_source = payload[0]
            candidate.delivery_path = upload_source
            try:
                candidate.share_url = cloud.upload(upload_source, config.cloud_folder)
            except Exception as exc:
                candidate.status = "needs_review"
                candidate.warnings.append(f"上传未完成：{exc}")
        else:
            candidate.status = "needs_review"
        candidates.append(candidate)

    manifest.write_text(json.dumps(previous + [item.to_dict() for item in candidates], ensure_ascii=False, indent=2), encoding="utf-8")
    return candidates


def run_pipeline(config: PipelineConfig, telegram: TelegramAdapter, cloud: CloudStorageAdapter) -> list[ProductCandidate]:
    config.ensure_directories()
    messages = telegram.fetch_messages(
        config.source_chat, config.message_ids, config.source_channel_url,
        config.fetch_mode, config.recent_message_limit,
    )
    return process_messages(config, messages, cloud)
