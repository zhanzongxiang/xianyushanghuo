from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import re
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .runtime import validate_browser_path

METADATA = "pipeline-browser.json"


def parse_version(value: str) -> tuple[int, int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value.strip()):
        raise ValueError(f"无法识别浏览器版本: {value}")
    return tuple(int(part) for part in value.strip().split("."))


def version_text(version: tuple[int, ...]) -> str:
    return ".".join(map(str, version))


def executable_version(executable: Path) -> tuple[int, int, int, int]:
    """Read PE version resources without starting the user's browser."""
    if os.name != "nt":
        raise RuntimeError("当前系统浏览器版本检测仅支持 Windows")

    class FixedFileInfo(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in (
            "signature", "structure_version", "file_ms", "file_ls", "product_ms", "product_ls",
            "flags_mask", "flags", "os", "type", "subtype", "date_ms", "date_ls",
        )]

    api = ctypes.WinDLL("version", use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    api.GetFileVersionInfoW.restype = wintypes.BOOL
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                  ctypes.POINTER(wintypes.UINT)]
    api.VerQueryValueW.restype = wintypes.BOOL
    size = api.GetFileVersionInfoSizeW(str(executable), None)
    if not size:
        raise ValueError(f"无法读取浏览器 EXE 版本信息，请确认选择的是 Chrome/Edge 主程序: {executable}")
    buffer = ctypes.create_string_buffer(size)
    value = ctypes.c_void_p()
    length = wintypes.UINT()
    if (not api.GetFileVersionInfoW(str(executable), 0, size, buffer)
            or not api.VerQueryValueW(buffer, "\\", ctypes.byref(value), ctypes.byref(length))
            or length.value < ctypes.sizeof(FixedFileInfo)):
        raise ValueError(f"读取浏览器版本资源失败: {executable}")
    info = ctypes.cast(value, ctypes.POINTER(FixedFileInfo)).contents
    if info.signature != 0xFEEF04BD:
        raise ValueError("浏览器 EXE 版本资源签名无效")
    return (info.file_ms >> 16, info.file_ms & 0xFFFF, info.file_ls >> 16, info.file_ls & 0xFFFF)


def recorded_profile_version(directory: Path) -> tuple[int, int, int, int] | None:
    """A failed downgrade may rewrite Last Version, so also check creation version."""
    versions = []
    try:
        versions.append(parse_version((directory / "Last Version").read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeError):
        pass
    try:
        preferences = json.loads((directory / "Default/Preferences").read_text(encoding="utf-8"))
        versions.append(parse_version(preferences["profile"]["created_by_version"]))
    except (OSError, ValueError, UnicodeError, KeyError, TypeError):
        pass
    return max(versions) if versions else None


@dataclass(frozen=True)
class BrowserProfile:
    executable: Path
    version: tuple[int, int, int, int]
    directory: Path
    is_new: bool
    legacy_version: tuple[int, int, int, int] | None

    @property
    def status(self) -> str:
        return "新专用目录，需首次登录" if self.is_new else "复用兼容专用目录"


def resolve_browser_profile(base_dir: Path, browser_path: str) -> BrowserProfile:
    executable = validate_browser_path(browser_path)
    version = executable_version(executable)
    identity = os.path.normcase(str(executable))
    fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    base_dir = base_dir.resolve()
    root = base_dir.parent / f"{base_dir.name}_browsers" / fingerprint
    legacy_version = recorded_profile_version(base_dir)
    compatible = []
    if root.is_dir():
        for candidate in root.iterdir():
            if not candidate.is_dir() or candidate.is_symlink() or candidate.resolve().parent != root.resolve():
                continue
            try:
                metadata = json.loads((candidate / METADATA).read_text(encoding="utf-8"))
                if metadata.get("executable") != identity:
                    continue
                previous = parse_version(metadata["highest_file_version"])
                # Chrome/Edge PE versions match their profile versions. Other
                # Chromium products may use an independent product version.
                if executable.name.lower() in {"chrome.exe", "msedge.exe", "chromium.exe"}:
                    on_disk = recorded_profile_version(candidate)
                    if on_disk:
                        previous = max(previous, on_disk)
                if previous <= version:
                    compatible.append((previous, candidate))
            except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError):
                continue
    if compatible:
        _, directory = max(compatible, key=lambda item: (item[0], str(item[1])))
        return BrowserProfile(executable, version, directory, False, legacy_version)
    directory = root / f"profile-{version_text(version)}"
    if directory.exists():
        # Unknown/corrupted metadata is never overwritten to force a reuse.
        directory = root / f"profile-{version_text(version)}-{uuid4().hex[:8]}"
    return BrowserProfile(executable, version, directory, True, legacy_version)


class ProfileLease:
    """Prevent two application processes from launching the same managed profile."""

    def __init__(self, directory: Path):
        directory.parent.mkdir(parents=True, exist_ok=True)
        self.file = (directory.parent / f"{directory.name}.lock").open("a+b")
        try:
            if self.file.seek(0, os.SEEK_END) == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError(f"该浏览器专用目录正在被另一个任务使用，请结束对应任务后重试: {directory}") from exc

    def close(self) -> None:
        if not self.file.closed:
            self.file.close()


def write_profile_metadata(profile: BrowserProfile) -> None:
    profile.directory.mkdir(parents=True, exist_ok=True)
    metadata = {
        "executable": os.path.normcase(str(profile.executable)),
        "highest_file_version": version_text(profile.version),
    }
    temporary = profile.directory / f".{METADATA}.{uuid4().hex}.tmp"
    temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(profile.directory / METADATA)


def launch_browser_context(playwright, base_dir: Path, browser_path: str, *, headless: bool,
                           accept_downloads: bool = True, downloads_path: Path | None = None):
    profile = resolve_browser_profile(base_dir, browser_path)
    lease = ProfileLease(profile.directory)
    try:
        write_profile_metadata(profile)
        log_dir = profile.directory / "logs"
        log_dir.mkdir(exist_ok=True)
        chrome_log = log_dir / f"chromium-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.log"
        options = {
            "headless": headless, "accept_downloads": accept_downloads,
            "executable_path": str(profile.executable), "timeout": 30000,
            "args": ["--enable-logging", f"--log-file={chrome_log}"],
        }
        if downloads_path is not None:
            downloads_path.mkdir(parents=True, exist_ok=True)
            options["downloads_path"] = str(downloads_path.resolve())
        logging.info("浏览器 EXE=%s; 文件版本=%s; headless=%s",
                     profile.executable, version_text(profile.version), headless)
        logging.info("实际浏览器登录目录: %s; %s", profile.directory, profile.status)
        if profile.legacy_version is not None:
            logging.info("旧目录原样保留: %s; 旧目录版本=%s（不加载、不复制）",
                         base_dir.resolve(), version_text(profile.legacy_version))
        logging.info("Chromium 诊断日志路径: %s", chrome_log)
        context = playwright.chromium.launch_persistent_context(str(profile.directory), **options)
    except Exception as exc:
        lease.close()
        logging.exception("浏览器启动失败；请核对 EXE、浏览器策略及独立目录权限")
        raise RuntimeError(
            f"浏览器启动失败（文件版本 {version_text(profile.version)}）。"
            f"实际专用目录: {profile.directory}。旧登录目录未改动，请查看 Chromium 诊断日志。"
        ) from exc
    context.on("close", lambda _: lease.close())
    context._pipeline_profile_dir = profile.directory
    if context.browser is not None:
        logging.info("浏览器实际版本: %s", context.browser.version)
        context.browser.on("disconnected", lambda _: (lease.close(), logging.info("浏览器事件: 连接已断开")))
    return context


def check_browser(base_dir: Path, browser_path: str, *, headless: bool = False) -> dict[str, str]:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        context = launch_browser_context(playwright, base_dir, browser_path, headless=headless,
                                         accept_downloads=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("about:blank")
            result = {"version": context.browser.version, "profile_dir": str(context._pipeline_profile_dir)}
            logging.info("浏览器检查通过: 版本=%s; 登录目录=%s", result["version"], result["profile_dir"])
            return result
        finally:
            context.close()
