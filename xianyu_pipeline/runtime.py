from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def telegram_crypto_backend() -> str:
    from telethon.crypto import aes

    if aes.cryptg is not None:
        return "cryptg"
    if aes.libssl.encrypt_ige and aes.libssl.decrypt_ige:
        return "OpenSSL"
    return "Python"


def check_download_acceleration() -> dict:
    from telethon.crypto.aes import AES

    data, key, iv = bytes(range(256)) * 16, bytes(range(32)), bytes(range(32, 64))
    encrypted = AES.encrypt_ige(data, key, iv)
    if AES.decrypt_ige(encrypted, key, iv) != data:
        raise RuntimeError("Telegram 下载加速组件加解密自检失败")
    return {"crypto_backend": telegram_crypto_backend(), "self_test": "passed"}


def validate_browser_path(value: str) -> Path:
    value = str(value).strip().strip('"')
    if not value:
        raise ValueError("浏览器程序路径为必填项，请指定 Chrome、Edge 等 Chromium 浏览器")
    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"浏览器程序路径必须是已存在的绝对文件路径: {path}")
    if os.name == "nt" and path.suffix.lower() != ".exe":
        raise ValueError("浏览器程序路径必须指向 .exe 文件，不能使用快捷方式或命令参数")
    if path.name.lower() in {"firefox.exe", "firefox", "iexplore.exe"}:
        raise ValueError("当前自动化仅支持 Chromium 内核浏览器，不支持 Firefox 或 IE")
    return path.resolve()


def configure_logging(log_dir: Path, memory_handler: logging.Handler | None = None) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = (log_dir / "pipeline.log").resolve()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Re-entering the UI in tests must not duplicate or retain old file handlers.
    for handler in list(root.handlers):
        if getattr(handler, "_pipeline_handler", False):
            root.removeHandler(handler)
            handler.close()
    file_handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handlers = [file_handler]
    if memory_handler is not None:
        handlers.append(memory_handler)
    else:
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler._pipeline_handler = True
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
        root.addHandler(handler)
    from .download_diagnostics import configure_diagnostics
    configure_diagnostics(log_dir)
    logging.info("运行日志: %s", log_path)
    return log_path
