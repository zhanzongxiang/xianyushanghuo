"""Create a distributable ZIP from allowlisted files and a clean config sample."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile


def package(folder: Path, example: Path, archive: Path) -> None:
    folder = folder.resolve()
    example = example.resolve()
    archive = archive.resolve()
    sample = json.loads(example.read_text(encoding="utf-8"))
    if any(sample.get(key) for key in (
        "browser_executable_path", "telegram_api_id", "telegram_api_hash", "quark_cookie"
    )):
        raise ValueError("Sample configuration contains local or account values")
    executable = folder / f"{folder.name}.exe"
    files = [executable, folder / "start_ui.cmd", folder / "README_TEST.md"]
    internal = folder / "_internal"
    if not internal.is_dir():
        raise FileNotFoundError(internal)
    files.extend(sorted(path for path in internal.rglob("*") if path.is_file()))
    if any(not path.is_file() or path.is_symlink() for path in files):
        raise ValueError("Distributable files missing or linked")
    temporary = archive.with_name(f".{archive.name}.{uuid4().hex}.tmp")
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=6) as output:
            for path in files:
                output.write(path, f"{folder.name}/{path.relative_to(folder).as_posix()}")
            output.write(example, f"{folder.name}/test.local.json")
        with ZipFile(temporary) as result:
            if result.testzip() is not None:
                raise ValueError("Portable ZIP failed CRC check")
            entries = result.namelist()
            if any("/.local-browsers/" in name or "/sample_data/" in name
                   or name.endswith((".session", "pipeline-browser.json", "telegram-settings.json")) for name in entries):
                raise ValueError("Portable ZIP contains runtime or browser state")
            if json.loads(result.read(f"{folder.name}/test.local.json")) != sample:
                raise ValueError("Portable ZIP configuration mismatch")
        temporary.replace(archive)
    finally:
        if temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("example", type=Path)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    package(args.folder, args.example, args.archive)
