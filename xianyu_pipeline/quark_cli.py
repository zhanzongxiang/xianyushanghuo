"""Quark CLI/WSL upload adapter.

The adapter talks to the official command line entry point through argv
arguments and consumes its newline-delimited JSON output.  No shell command
string is constructed, so Windows paths and file names are passed as data.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


class QuarkCLIError(RuntimeError):
    pass


def validate_quark_cli_settings(values: dict, existing=None) -> None:
    """Validate settings without checking whether WSL/Node is installed."""
    def value_of(key: str, default=""):
        if key in values:
            return values[key]
        return getattr(existing, key, default) if existing is not None else default

    mode = value_of("quark_cli_mode", "wsl")
    if mode not in {"wsl", "native"}:
        raise ValueError("夸克 CLI 执行模式必须是 wsl 或 native")

    executable = value_of("quark_cli_executable_path", "wsl.exe")
    if not isinstance(executable, str) or not executable.strip() or "\x00" in executable:
        raise ValueError("夸克 CLI 执行程序不能为空")

    node_command = value_of("quark_cli_node_command", "node")
    if not isinstance(node_command, str) or not node_command.strip() or "\x00" in node_command:
        raise ValueError("夸克 CLI Node 命令不能为空")

    script_path = value_of("quark_cli_script_path", "")
    if not isinstance(script_path, str) or "\x00" in script_path:
        raise ValueError("夸克 CLI 脚本路径无效")
    if mode == "wsl" and not script_path.strip() and node_command.strip().lower() != "quarkclouddrive":
        raise ValueError("WSL 模式请填写 quark-drive.cjs 路径，或将 Node 命令改为 quarkclouddrive")

    distribution = value_of("quark_cli_wsl_distribution", "")
    if not isinstance(distribution, str) or "\x00" in distribution:
        raise ValueError("WSL 发行版名称无效")

    parent_fid = value_of("quark_cli_parent_fid", "")
    if not isinstance(parent_fid, str) or "\x00" in parent_fid:
        raise ValueError("夸克 CLI 目标目录 FID 无效")
    if parent_fid and not re.fullmatch(r"[A-Za-z0-9_-]+", parent_fid):
        raise ValueError("夸克 CLI 目标目录 FID 只能包含字母、数字、下划线或连字符")

    timeout = value_of("quark_cli_timeout_seconds", 3600)
    if type(timeout) is not int or not 1 <= timeout <= 604800:
        raise ValueError("夸克 CLI 超时必须为 1 到 604800 的整数秒")


class QuarkCLIStorageAdapter:
    """Upload one archive through the official Quark CLI and create a share."""

    def __init__(
        self,
        *,
        mode: str = "wsl",
        executable_path: str = "wsl.exe",
        wsl_distribution: str = "",
        node_command: str = "node",
        script_path: str = "",
        parent_fid: str = "",
        timeout_seconds: int = 3600,
        share_expired_type: int = 1,
        share_url_type: int = 1,
        on_stage: Callable[[str], None] | None = None,
        on_progress: Callable[[int, int, float, float | None], None] | None = None,
        stop_event: threading.Event | None = None,
    ):
        validate_quark_cli_settings({
            "quark_cli_mode": mode,
            "quark_cli_executable_path": executable_path,
            "quark_cli_wsl_distribution": wsl_distribution,
            "quark_cli_node_command": node_command,
            "quark_cli_script_path": script_path,
            "quark_cli_parent_fid": parent_fid,
            "quark_cli_timeout_seconds": timeout_seconds,
        })
        if type(share_expired_type) is not int or not 1 <= share_expired_type <= 7:
            raise ValueError("夸克 CLI 分享过期类型必须为 1 到 7")
        if share_url_type not in {1, 2}:
            raise ValueError("夸克 CLI 分享链接类型必须为 1 或 2")
        self.mode = mode
        self.executable_path = executable_path.strip()
        self.wsl_distribution = wsl_distribution.strip()
        self.node_command = node_command.strip()
        self.script_path = script_path.strip()
        self.parent_fid = parent_fid.strip()
        self.timeout_seconds = timeout_seconds
        self.share_expired_type = share_expired_type
        self.share_url_type = share_url_type
        self.on_stage = on_stage
        self.on_progress = on_progress
        self.stop_event = stop_event

    def _report_stage(self, stage: str) -> None:
        if callable(self.on_stage):
            try:
                self.on_stage(stage)
            except Exception:
                logging.warning("夸克 CLI 阶段回调失败：%s", stage, exc_info=True)

    def _report_progress(self, current: int, total: int, speed: float, eta: float | None) -> None:
        if callable(self.on_progress):
            try:
                self.on_progress(current, total, speed, eta)
            except Exception:
                logging.warning("夸克 CLI 进度回调失败", exc_info=True)

    @staticmethod
    def _windows_to_wsl_path(path: Path) -> str:
        value = str(path.resolve())
        match = re.match(r"^([A-Za-z]):[\\/](.*)$", value)
        if match:
            drive, remainder = match.groups()
            return f"/mnt/{drive.lower()}/{remainder.replace(chr(92), '/')}"
        if value.startswith("\\\\"):
            return value.replace("\\", "/")
        return value.replace("\\", "/")

    def _command(self, args: list[str], *, local_paths: set[int] | None = None) -> list[str]:
        if self.mode == "wsl":
            command = [self.executable_path]
            if self.wsl_distribution:
                command += ["-d", self.wsl_distribution]
            command += ["--", self.node_command]
            if self.script_path:
                command.append(self.script_path)
        else:
            command = [self.executable_path]
            if self.script_path:
                command.append(self.script_path)
        local_paths = local_paths or set()
        command.extend(
            self._windows_to_wsl_path(Path(value)) if self.mode == "wsl" and index in local_paths else value
            for index, value in enumerate(args)
        )
        return command

    @staticmethod
    def _display_command(command: list[str]) -> str:
        return " ".join(f'"{value}"' if re.search(r"\s", value) else value for value in command)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    def _run(self, args: list[str], *, local_paths: set[int] | None = None,
             timeout_seconds: int | None = None) -> list[dict[str, Any]]:
        command = self._command(args, local_paths=local_paths)
        logging.info("夸克 CLI 执行: %s", self._display_command(command))
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as exc:
            raise QuarkCLIError(f"无法启动夸克 CLI：{exc}") from exc

        events: list[dict[str, Any]] = []
        output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def pump(name: str, stream) -> None:
            try:
                for line in stream:
                    output_queue.put((name, line))
            finally:
                output_queue.put((f"{name}_eof", None))

        stdout_thread = threading.Thread(target=pump, args=("stdout", process.stdout), daemon=True)
        stderr_thread = threading.Thread(target=pump, args=("stderr", process.stderr), daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        finished_streams: set[str] = set()
        result: dict[str, Any] | None = None
        last_current = 0
        last_sample = time.monotonic()
        deadline = time.monotonic() + (timeout_seconds or self.timeout_seconds)
        try:
            while True:
                if self.stop_event is not None and self.stop_event.is_set():
                    self._terminate(process)
                    raise QuarkCLIError("已请求停止夸克 CLI 上传")
                if time.monotonic() > deadline:
                    self._terminate(process)
                    raise QuarkCLIError(f"夸克 CLI 超过 {timeout_seconds or self.timeout_seconds} 秒未完成")
                try:
                    stream_name, raw = output_queue.get(timeout=0.2)
                except queue.Empty:
                    if process.poll() is not None and len(finished_streams) == 2:
                        break
                    continue
                if stream_name.endswith("_eof"):
                    finished_streams.add(stream_name.removesuffix("_eof"))
                    continue
                if raw is None:
                    continue
                line = raw.strip()
                if not line:
                    continue
                if stream_name == "stderr":
                    logging.info("夸克 CLI: %s", line)
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logging.warning("夸克 CLI 输出了非 JSON 行：%s", line[:500])
                    continue
                if not isinstance(event, dict):
                    logging.warning("夸克 CLI 输出格式无效：%s", line[:500])
                    continue
                events.append(event)
                if event.get("type") == "progress":
                    data = event.get("data") if isinstance(event.get("data"), dict) else {}
                    current = max(0, int(data.get("current") or 0))
                    total = max(0, int(data.get("total") or 0))
                    now = time.monotonic()
                    elapsed = now - last_sample
                    speed = max(0, current - last_current) / elapsed if elapsed > 0 else 0
                    eta = max(0, total - current) / speed if total > 0 and speed > 0 else None
                    last_current, last_sample = current, now
                    self._report_progress(current, total, speed, eta)
                elif event.get("type") == "result":
                    result = event
            return_code = process.wait(timeout=3)
        finally:
            if process.poll() is None:
                self._terminate(process)
        if result is None:
            raise QuarkCLIError(f"夸克 CLI 未返回最终结果（退出码 {return_code}）")
        code = result.get("code", 0)
        if code not in (0, None):
            message = str(result.get("msg") or "未知错误")
            if self._looks_unauthorized(message, code):
                raise QuarkCLIError(f"夸克 CLI 未授权：{message}；请先执行夸克 CLI 登录")
            raise QuarkCLIError(f"夸克 CLI 执行失败（code={code}）：{message}")
        if return_code != 0:
            raise QuarkCLIError(f"夸克 CLI 异常退出（退出码 {return_code}）")
        return events

    @staticmethod
    def _looks_unauthorized(message: str, code: Any) -> bool:
        text = message.casefold()
        return any(token in text for token in ("未授权", "授权", "认证", "token", "unauthor")) or code == -1408

    @staticmethod
    def _result(events: list[dict[str, Any]], action: str) -> dict[str, Any]:
        for event in reversed(events):
            if event.get("type") == "result" and event.get("action") in {action, None}:
                return event
        raise QuarkCLIError(f"夸克 CLI {action} 没有返回结果")

    @staticmethod
    def _file_id(events: list[dict[str, Any]], filename: str) -> str:
        result = QuarkCLIStorageAdapter._result(events, "upload")
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        fids = data.get("fids")
        if isinstance(fids, list) and len(fids) == 1 and isinstance(fids[0], str) and fids[0]:
            return fids[0]
        for event in events:
            if event.get("type") != "list":
                continue
            item = event.get("data") if isinstance(event.get("data"), dict) else {}
            item_name = item.get("fileName") or item.get("filename") or item.get("name")
            fid = item.get("fileId") or item.get("fid")
            if fid and (not item_name or item_name == filename):
                return str(fid)
        raise QuarkCLIError(f"夸克 CLI 上传成功但未返回文件 FID：{filename}")

    @staticmethod
    def _share_url(events: list[dict[str, Any]]) -> str:
        result = QuarkCLIStorageAdapter._result(events, "share")
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        value = data.get("share_url")
        if not isinstance(value, str) or not re.fullmatch(r"https://pan\.quark\.cn/s/[A-Za-z0-9_-]+", value):
            raise QuarkCLIError("夸克 CLI 未返回有效的分享链接")
        return value

    def _share(self, file_id: str, *, report_stage: bool = True) -> str:
        if report_stage:
            self._report_stage("link_pending")
        events = self._run([
            "share", file_id, "--url-type", str(self.share_url_type),
            "--expired-type", str(self.share_expired_type),
        ])
        return self._share_url(events)

    def upload(self, local_path: Path, remote_folder: str) -> str:
        local_path = Path(local_path)
        if not local_path.is_file():
            raise FileNotFoundError(local_path)
        target = str(remote_folder or self.parent_fid or "").strip()
        if target and self.parent_fid and target != self.parent_fid:
            raise QuarkCLIError("夸克 CLI 上传目标 FID 与当前配置不一致")
        self._report_stage("uploading")
        args = ["upload", str(local_path)]
        local_index = {1}
        if self.parent_fid:
            args += ["--parent-fid", self.parent_fid]
        events = self._run(args, local_paths=local_index)
        try:
            file_id = self._file_id(events, local_path.name)
            self._report_progress(local_path.stat().st_size, local_path.stat().st_size, 0, None)
            return_url = self._share(file_id)
        except Exception:
            # A completed upload followed by a share failure must be recoverable
            # through share_existing; never report it as a clean upload.
            self._report_stage("link_pending")
            raise
        self._report_stage("completed")
        return return_url

    def share_existing(self, local_path: Path, remote_folder: str) -> str:
        filename = Path(local_path).name
        target = str(remote_folder or self.parent_fid or "").strip()
        if target and self.parent_fid and target != self.parent_fid:
            raise QuarkCLIError("夸克 CLI 原上传目标已变化，不能直接恢复分享链接")
        self._report_stage("link_pending")
        args = ["browse"]
        if self.parent_fid:
            args += ["--parent-fid", self.parent_fid]
        events = self._run(args)
        matches: list[dict[str, Any]] = []
        for event in events:
            if event.get("type") != "list":
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            item_name = data.get("fileName") or data.get("filename") or data.get("name")
            file_id = data.get("fileId") or data.get("fid")
            if item_name == filename and file_id:
                matches.append(data)
        if len(matches) != 1:
            raise QuarkCLIError(f"夸克 CLI 目标目录中找不到唯一的同名文件：{filename}")
        result = self._share(str(matches[0].get("fileId") or matches[0].get("fid")), report_stage=False)
        self._report_stage("completed")
        return result

    def login(self) -> None:
        events = self._run(["login"], timeout_seconds=max(self.timeout_seconds, 600))
        result = self._result(events, "login")
        logging.info("夸克 CLI 登录命令完成: %s", result.get("msg") or "成功")

    def check(self) -> None:
        events = self._run(["get-user-info"], timeout_seconds=min(self.timeout_seconds, 300))
        result = self._result(events, "get-user-info")
        logging.info("夸克 CLI 授权检查通过: %s", result.get("msg") or "成功")
