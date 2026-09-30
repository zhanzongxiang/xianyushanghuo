"""Regression for Python-embedded JavaScript. Uses mocked APIs; no account/network actions."""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xianyu_pipeline.web_ui import HTML
from playwright.sync_api import sync_playwright

calls = []
errors = []
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=os.environ["BROWSER_TEST_PATH"])
    try:
        page = browser.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        def handle(route):
            path = route.request.url.split('127.0.0.1:8879')[-1]
            calls.append((route.request.method, path))
            if path == '/':
                return route.fulfill(content_type='text/html', body=HTML)
            if path == '/api/config' and route.request.method == 'GET':
                body = {'source_channel_url': '', 'fetch_mode': 'recent', 'upload_provider': 'local', 'recent_message_limit': 20, 'telegram_browser_headless': False, 'quark_folder_url': '', 'quark_headless': False, 'download_dir': 'D:/Materials/inbox', 'unverified_dir': 'D:/Materials/inbox/.unverified', 'log_path': 'D:/Materials/output/logs/pipeline.log', 'browser_version': '142.0.7444.176', 'browser_profile_dir': 'C:/Users/Administrator/AppData/Local/XianYuMaterialPipeline/telegram_browser_profile_browsers/1f9876/profile-142.0.7444.176', 'browser_profile_status': '复用兼容专用目录'}
            elif path == '/api/logs':
                body = {'running': False, 'logs': ['line1', 'line2']}
            else:
                body = {'message': 'ok'}
            route.fulfill(content_type='application/json', body=json.dumps(body))
        page.route('**/*', handle)
        page.goto('http://127.0.0.1:8879/')
        page.wait_for_function("document.getElementById('logs').textContent === 'line1\\nline2'")
        assert page.evaluate("['saveConfig','runPipeline','loginTelegram','loginQuark','refreshLogs'].every(k=>typeof window[k]==='function')")
        page.on('dialog', lambda dialog: dialog.accept())
        page.get_by_role('button', name='保存配置', exact=True).click()
        assert ('POST', '/api/config') not in calls
        assert page.locator('#browser_executable_path').get_attribute('aria-required') == 'true'
        page.get_by_role('button', name='刷新日志', exact=True).click()
        page.locator('#source_channel_url').fill('not-a-channel-url')
        page.get_by_role('button', name='启动流水线', exact=True).click()
        assert page.locator('#form-error').inner_text()
        assert ('POST', '/api/run') not in calls
        page.locator('#source_channel_url').fill('')
        page.locator('#recent_message_limit').fill('20')
        page.get_by_role('button', name='启动流水线', exact=True).click()
        assert ('POST', '/api/run') not in calls
        page.get_by_role('button', name='首次登录 Telegram', exact=True).click()
        assert ('POST', '/api/telegram-login') not in calls
        page.locator('#upload_provider').select_option('quark_browser')
        page.get_by_role('button', name='首次登录夸克', exact=True).click()
        assert ('POST', '/api/quark-login') not in calls
        page.locator('#browser_executable_path').fill(os.environ["BROWSER_TEST_PATH"])
        with page.expect_response('**/api/browser-check'):
            page.get_by_role('button', name='检查浏览器', exact=True).click()
        assert ('POST', '/api/browser-check') in calls
        assert '142.0.7444.176' in page.locator('#browser-info').inner_text()
        page.locator('#source_channel_url').fill('https://t.me/test_channel')
        page.locator('#upload_provider').select_option('local')
        assert not page.locator('#quark-fields').is_visible()
        page.get_by_role('button', name='保存配置', exact=True).click()
        assert ('POST', '/api/config') in calls
        with page.expect_response('**/api/run'):
            page.get_by_role('button', name='启动流水线', exact=True).click()
        assert ('POST', '/api/run') in calls
        with page.expect_response('**/api/open-downloads'):
            page.get_by_role('button', name='打开下载目录', exact=True).click()
        assert ('POST', '/api/open-downloads') in calls
        artifact_dir = Path(__file__).resolve().parent / "artifacts"
        artifact_dir.mkdir(exist_ok=True)
        for name, width, height in (("desktop", 1366, 900), ("mobile", 390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(artifact_dir / f"settings-{name}.png"), full_page=True)
        assert not errors, errors
        print('PASS: simplified browser-mode UI loads; handlers defined; required validation blocks incomplete run/login; zero page errors')
    finally:
        browser.close()
