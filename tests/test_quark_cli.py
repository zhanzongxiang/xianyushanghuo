from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from xianyu_pipeline.quark_cli import (
    QuarkCLIError,
    QuarkCLIStorageAdapter,
    validate_quark_cli_settings,
)


def fake_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake_quark_cli.py"
    script.write_text(
        """
import json
import sys

args = sys.argv[1:]
action = args[0] if args else ""
if action == "upload":
    print(json.dumps({"type": "progress", "action": "upload", "data": {"current": 5, "total": 10, "percent": 50}}), flush=True)
    print(json.dumps({"type": "list", "action": "upload", "code": 0, "data": {"fileId": "fid-upload", "fileName": "asset.zip"}}), flush=True)
    print(json.dumps({"type": "result", "action": "upload", "code": 0, "data": {"fids": ["fid-upload"]}}), flush=True)
elif action == "share":
    print(json.dumps({"type": "result", "action": "share", "code": 0, "data": {"share_url": "https://pan.quark.cn/s/Fixture"}}), flush=True)
elif action == "browse":
    print(json.dumps({"type": "list", "action": "browse", "code": 0, "data": {"fid": "fid-existing", "filename": "asset.zip"}}), flush=True)
    print(json.dumps({"type": "result", "action": "browse", "code": 0, "data": {"total": 1}}), flush=True)
elif action == "get-user-info":
    print(json.dumps({"type": "result", "action": "get-user-info", "code": 0, "msg": "ok", "data": {}}), flush=True)
elif action == "login":
    print(json.dumps({"type": "result", "action": "login", "code": 0, "msg": "ok", "data": {}}), flush=True)
""".strip(),
        encoding="utf-8",
    )
    return script


def adapter(tmp_path: Path, **values) -> QuarkCLIStorageAdapter:
    return QuarkCLIStorageAdapter(
        mode="native",
        executable_path=sys.executable,
        script_path=str(fake_cli(tmp_path)),
        parent_fid="folder-1",
        **values,
    )


def test_cli_upload_consumes_progress_uploads_then_shares(tmp_path):
    source = tmp_path / "asset with spaces.zip"
    source.write_bytes(b"fixture")
    stages = []
    progress = []
    cloud = adapter(tmp_path, on_stage=stages.append,
                    on_progress=lambda current, total, speed, eta: progress.append((current, total)))

    assert cloud.upload(source, "folder-1") == "https://pan.quark.cn/s/Fixture"
    assert stages == ["uploading", "link_pending", "completed"]
    assert progress[0] == (5, 10)
    assert progress[-1] == (source.stat().st_size, source.stat().st_size)


def test_cli_recovery_browses_exact_filename_without_resubmitting(tmp_path):
    stages = []
    cloud = adapter(tmp_path, on_stage=stages.append)
    missing = tmp_path / "asset.zip"

    assert cloud.share_existing(missing, "folder-1") == "https://pan.quark.cn/s/Fixture"
    assert stages == ["link_pending", "completed"]


def test_cli_wsl_path_conversion():
    assert QuarkCLIStorageAdapter._windows_to_wsl_path(Path(r"C:\Downloads\asset.zip")) == "/mnt/c/Downloads/asset.zip"


@pytest.mark.parametrize(
    "values",
    [
        {"quark_cli_mode": "other"},
        {"quark_cli_executable_path": ""},
        {"quark_cli_mode": "wsl", "quark_cli_node_command": "node", "quark_cli_script_path": ""},
        {"quark_cli_parent_fid": "bad fid"},
        {"quark_cli_timeout_seconds": 0},
    ],
)
def test_cli_settings_are_validated(values):
    base = {
        "quark_cli_mode": "wsl",
        "quark_cli_executable_path": "wsl.exe",
        "quark_cli_wsl_distribution": "",
        "quark_cli_node_command": "node",
        "quark_cli_script_path": "/home/user/.quarkclouddrive/scripts/quark-drive.cjs",
        "quark_cli_parent_fid": "",
        "quark_cli_timeout_seconds": 3600,
    }
    base.update(values)
    with pytest.raises(ValueError):
        validate_quark_cli_settings(base)


def test_cli_unauthorized_result_is_actionable(tmp_path):
    script = tmp_path / "unauthorized.py"
    script.write_text(
        "import json; print(json.dumps({'type':'result','action':'browse','code':-1408,'msg':'未完成授权认证','data':{}}), flush=True)",
        encoding="utf-8",
    )
    cloud = QuarkCLIStorageAdapter(mode="native", executable_path=sys.executable,
                                   script_path=str(script), timeout_seconds=10)
    with pytest.raises(QuarkCLIError, match="未授权"):
        cloud.share_existing(tmp_path / "asset.zip", "")
