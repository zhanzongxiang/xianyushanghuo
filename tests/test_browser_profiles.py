from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from xianyu_pipeline import browser_profiles as profiles


@pytest.fixture
def selection(tmp_path, monkeypatch):
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"MZ")
    version = [142, 0, 7444, 176]
    monkeypatch.setattr(profiles, "executable_version", lambda _: tuple(version))
    return tmp_path / "legacy", executable, version


def record(directory, version):
    (directory / "Default").mkdir(parents=True, exist_ok=True)
    (directory / "Default/Preferences").write_text(
        json.dumps({"profile": {"created_by_version": version}}), encoding="utf-8")
    (directory / "Last Version").write_text(version, encoding="utf-8")


def snapshot(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*") if path.is_file()}


def test_legacy_is_never_reused_or_changed(selection):
    base, executable, _ = selection
    record(base, "153.0.8010.12")
    before = snapshot(base)
    result = profiles.resolve_browser_profile(base, str(executable))
    assert result.directory != base and result.is_new
    assert result.legacy_version == (153, 0, 8010, 12)
    assert not result.directory.exists()
    profiles.write_profile_metadata(result)
    assert snapshot(base) == before
    assert profiles.resolve_browser_profile(base, str(executable)).directory == result.directory


def test_upgrade_reuses_but_downgrade_separates(selection):
    base, executable, version = selection
    first = profiles.resolve_browser_profile(base, str(executable))
    profiles.write_profile_metadata(first)
    version[0] = 153
    upgrade = profiles.resolve_browser_profile(base, str(executable))
    assert upgrade.directory == first.directory and not upgrade.is_new
    profiles.write_profile_metadata(upgrade)
    version[0] = 142
    downgrade = profiles.resolve_browser_profile(base, str(executable))
    assert downgrade.directory != upgrade.directory and downgrade.is_new
    profiles.write_profile_metadata(downgrade)
    assert profiles.resolve_browser_profile(base, str(executable)).directory == downgrade.directory


def test_creation_version_prevents_reuse_after_failed_downgrade(selection):
    base, executable, _ = selection
    first = profiles.resolve_browser_profile(base, str(executable))
    profiles.write_profile_metadata(first)
    record(first.directory, "153.0.8010.12")
    (first.directory / "Last Version").write_text("142.0.7444.176", encoding="utf-8")
    assert profiles.recorded_profile_version(first.directory) == (153, 0, 8010, 12)
    assert profiles.resolve_browser_profile(base, str(executable)).directory != first.directory


def test_different_executables_get_separate_profiles(selection, tmp_path):
    base, executable, _ = selection
    other = tmp_path / "msedge.exe"
    other.write_bytes(b"MZ")
    first = profiles.resolve_browser_profile(base, str(executable))
    profiles.write_profile_metadata(first)
    second = profiles.resolve_browser_profile(base, str(other))
    assert second.directory.parent != first.directory.parent


@pytest.mark.parametrize("metadata", ["invalid-json", "[]", '{"executable": "wrong"}'])
def test_unknown_metadata_is_preserved(selection, metadata):
    base, executable, _ = selection
    first = profiles.resolve_browser_profile(base, str(executable))
    first.directory.mkdir(parents=True)
    (first.directory / profiles.METADATA).write_text(metadata, encoding="utf-8")
    result = profiles.resolve_browser_profile(base, str(executable))
    assert result.directory != first.directory
    assert (first.directory / profiles.METADATA).read_text(encoding="utf-8") == metadata


def test_busy_lease_and_release(tmp_path):
    directory = tmp_path / "profile"
    first = profiles.ProfileLease(directory)
    try:
        with pytest.raises(RuntimeError, match="另一个任务"):
            profiles.ProfileLease(directory)
    finally:
        first.close()
    profiles.ProfileLease(directory).close()


def test_launch_failure_releases_lease(selection):
    base, executable, _ = selection
    def fail(*args, **kwargs):
        raise OSError("fixture launch failure")
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=fail))
    with pytest.raises(RuntimeError, match="旧登录目录未改动"):
        profiles.launch_browser_context(playwright, base, str(executable), headless=True)
    profile = profiles.resolve_browser_profile(base, str(executable))
    profiles.ProfileLease(profile.directory).close()


def test_real_browser_retains_session_and_avoids_newer_legacy(tmp_path):
    executable = os.environ.get("BROWSER_TEST_PATH")
    if not executable:
        pytest.skip("BROWSER_TEST_PATH required")
    from playwright.sync_api import sync_playwright
    base = tmp_path / "legacy"
    newer = os.environ.get("LEGACY_BROWSER_TEST_PATH")
    with sync_playwright() as playwright:
        if newer:
            context = playwright.chromium.launch_persistent_context(str(base), executable_path=newer,
                                                                    headless=True)
            context.close()
            assert profiles.recorded_profile_version(base) > profiles.executable_version(Path(executable))
        else:
            record(base, "999.0.0.0")
        before = snapshot(base)
        actual_dirs = []
        for iteration in range(2):
            context = profiles.launch_browser_context(playwright, base, executable, headless=True)
            try:
                actual_dirs.append(context._pipeline_profile_dir)
                context.route("https://fixture.invalid/**", lambda route: route.fulfill(
                    content_type="text/html", body="<title>Local profile fixture</title>"))
                page = context.pages[0]
                page.goto("https://fixture.invalid/")
                if iteration == 0:
                    page.evaluate("localStorage.setItem('persistent', 'yes')")
                else:
                    assert page.evaluate("localStorage.getItem('persistent')") == "yes"
            finally:
                context.close()
        assert actual_dirs[0] == actual_dirs[1] != base
        assert snapshot(base) == before


def test_quark_login_window_can_close_without_console():
    from playwright.sync_api import Error as PlaywrightError
    from xianyu_pipeline.quark_browser import wait_for_login_window
    state = [False]
    def closed(_):
        state[0] = True
        raise PlaywrightError("fixture closed")
    page = SimpleNamespace(is_closed=lambda: state[0], wait_for_timeout=closed)
    wait_for_login_window(page)


def test_quark_login_window_times_out(monkeypatch, caplog):
    from xianyu_pipeline import quark_browser
    clock = [0]
    monkeypatch.setattr(quark_browser.time, "monotonic", lambda: clock[0])
    def tick(_):
        clock[0] += 1
    page = SimpleNamespace(is_closed=lambda: False, wait_for_timeout=tick)
    quark_browser.wait_for_login_window(page, timeout_seconds=2)
    assert clock[0] == 2 and "超时" in caplog.text
