from __future__ import annotations

import locale
import logging
import os
import shutil
import subprocess
from pathlib import Path


class ArchiveValidationError(RuntimeError):
    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


def find_archive_tester(configured: str = "") -> Path:
    if configured.strip():
        path = Path(os.path.expandvars(configured.strip().strip('"'))).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ArchiveValidationError("tool_missing", f"压缩包校验工具不存在: {path}")
        candidates = [path]
    else:
        candidates = [Path(found) for name in ("UnRAR.exe", "unrar", "7z.exe", "7z", "7zz")
                      if (found := shutil.which(name))]
        for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if root:
                candidates.extend([Path(root) / "WinRAR" / "UnRAR.exe", Path(root) / "7-Zip" / "7z.exe"])
    for path in candidates:
        if path.is_file() and path.stem.lower() in {"unrar", "7z", "7zz", "7za"}:
            return path.resolve()
    raise ArchiveValidationError("tool_missing", "RAR 尚未校验：请安装 7-Zip/WinRAR，或配置 7z.exe/UnRAR.exe 路径")


def validate_rar(path: Path, tester: str = "", timeout_seconds: int = 600) -> None:
    with path.open("rb") as source:
        header = source.read(8)
    if not (header.startswith(b"Rar!\x1a\x07\x00") or header == b"Rar!\x1a\x07\x01\x00"):
        raise ArchiveValidationError("invalid_format", "文件不是有效的 RAR 格式")
    executable = find_archive_tester(tester)
    if executable.stem.lower() == "unrar":
        args = [str(executable), "t", "-p-", "-idq", "-y", str(path.resolve())]
    else:
        args = [str(executable), "t", "-y", "-bd", "-bso0", "-bsp0", str(path.resolve())]
    logging.info("RAR 完整性校验开始: 工具=%s; 文件=%s", executable, path.resolve())
    try:
        result = subprocess.run(
            args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout_seconds,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise ArchiveValidationError("timeout", "RAR 校验超时，文件保留待检查") from exc
    except OSError as exc:
        raise ArchiveValidationError("tool_error", f"RAR 校验工具启动失败: {exc}") from exc
    raw_output = result.stdout + result.stderr
    try:
        output = raw_output.decode("utf-8")
    except UnicodeDecodeError:
        output = raw_output.decode(locale.getencoding(), errors="replace")
    normalized = output.lower()
    if result.returncode == 0:
        logging.info("RAR 完整性校验通过: %s", path.name)
        return
    if any(text in normalized for text in ("volume", "分卷", "无法找到卷", "找不到卷", "無法找到卷", "next archive")):
        category, reason = "missing_volume", "RAR 缺少分卷或分卷不可读"
    elif result.returncode == 11 or any(text in normalized for text in ("password", "encrypted", "密码", "加密")):
        category, reason = "password_required", "RAR 需要密码，尚未通过完整性校验"
    elif result.returncode == 3 or any(text in normalized for text in ("crc", "checksum", "corrupt", "损坏", "unexpected end")):
        category, reason = "corrupt", "RAR 损坏或内容校验失败"
    else:
        category, reason = "test_failed", "RAR 校验未通过"
    raise ArchiveValidationError(category, f"{reason} (exit={result.returncode}): {output.strip()[-1500:]}")
