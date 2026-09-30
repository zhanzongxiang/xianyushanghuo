from __future__ import annotations

import json
import logging
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event
from urllib.parse import parse_qs, urlparse

from .adapters import LocalCloudStorageAdapter, LocalTelegramAdapter, TelegramBrowserAdapter, TelethonTelegramAdapter
from .config import PipelineConfig
from .pipeline import process_messages, run_pipeline
from .quark_browser import QuarkBrowserStorageAdapter
from .runtime import configure_logging, validate_browser_path
from .archive_validation import find_archive_tester
from .browser_profiles import check_browser, resolve_browser_profile, version_text


class MemoryLogHandler(logging.Handler):
    def __init__(self, state: "WebState"):
        super().__init__()
        self.state = state

    def emit(self, record: logging.LogRecord) -> None:
        self.state.add_log(self.format(record))


class WebState:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.logs: list[str] = []
        self.running = False
        self.stop_event = Event()
        self.lock = threading.Lock()

    def add_log(self, message: str) -> None:
        with self.lock:
            self.logs.append(message)
            self.logs = self.logs[-500:]

    def snapshot(self) -> dict:
        with self.lock:
            return {"running": self.running, "logs": list(self.logs)}


HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="icon" href="data:,"><title>闲鱼素材流水线 v24</title>
<style>
[hidden]{display:none!important}
body{font-family:Segoe UI,Microsoft YaHei,sans-serif;background:#f5f7fb;color:#172033;margin:0}
main{max-width:1100px;margin:28px auto;padding:0 20px}h1{margin-bottom:6px}.sub{color:#667085}
.card{border-top:1px solid #d0d5dd;padding:20px 0;margin:16px 0}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.wide{grid-column:1/-1}
label{display:flex;flex-direction:column;gap:5px;font-weight:600;font-size:13px;min-width:0;position:relative}input,select{padding:9px;border:1px solid #d0d5dd;border-radius:8px;font:inherit;min-width:0;max-width:100%;box-sizing:border-box;margin-top:auto}.required-star{position:absolute;right:0;top:0}button{border:0;border-radius:8px;padding:10px 15px;margin-right:8px;margin-bottom:8px;cursor:pointer;background:#155eef;color:#fff}button.secondary{background:#667085}button.warn{background:#b54708}#paths{overflow-wrap:anywhere;font-size:13px;color:#475467}
pre{background:#101828;color:#d1fadf;border-radius:8px;padding:14px;height:260px;max-height:260px;white-space:pre-wrap;overflow-y:auto;overflow-x:auto}.hint{font-size:12px;color:#667085;font-weight:400}.danger,.required-star{color:#b42318}.required-star{font-weight:700}.field-error{border-color:#b42318!important}.field-muted{opacity:.55}#form-error{min-height:1.4em}
@media(max-width:760px){.grid{grid-template-columns:1fr}.wide{grid-column:auto}}
</style></head><body><main>
<h1>闲鱼素材流水线 <small>v24</small></h1><div class="sub">Telegram 获取素材 → 本地处理 → 夸克网页上传。当前不自动发布闲鱼。</div>
<div class="card"><h2>运行参数</h2><p class="hint">当前仅使用 Telegram Web 获取频道最新发言；红色 * 为启动所需。登录态由程序保存在本机，无需填写 API 凭证、消息 ID 或会话路径。</p><div class="grid">
<label>Telegram 频道公开链接 <span class="hint">例如 https://t.me/example_channel</span><input id="source_channel_url" placeholder="https://t.me/example_channel"></label>
<label>浏览器程序路径（Chrome / Edge / Chromium）<input id="browser_executable_path" required placeholder="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"></label>
<label class="wide">浏览器版本与 Telegram 专用目录<output id="browser-info" style="overflow-wrap:anywhere;font-weight:400">未配置</output></label>
<label>ZIP/RAR 消息数量 <span class="hint">抓取一次时向前查找最新 N 条 ZIP/RAR 消息（扫描上限 200 条）；持续监测时只接收启动后新消息</span><input id="recent_message_limit" type="number" min="1" max="100" value="20"></label>
<label>运行方式<select id="telegram_run_mode"><option value="once">抓取一次</option><option value="watch">持续监测新 ZIP/RAR</option></select></label>
<label>Telegram 浏览器后台运行<select id="telegram_browser_headless"><option value="false">否（可见，建议先调试）</option><option value="true">是（登录后再尝试）</option></select></label>
<label>上传方式<select id="upload_provider"><option value="local">本地模拟（先验证下载）</option><option value="quark_browser">夸克网页上传</option></select></label>
<label>下载启动等待（秒）<input id="download_start_timeout_seconds" type="number" min="1" max="86400" value="180"></label>
<label>下载传输时限（秒）<input id="download_transfer_timeout_seconds" type="number" min="1" max="604800" value="3600"></label>
<label>RAR 校验程序路径（可选）<input id="archive_test_executable_path" placeholder="C:\\Program Files\\WinRAR\\UnRAR.exe"></label>
<div id="quark-fields" class="wide grid" hidden>
<label>夸克目标文件夹 URL<input id="quark_folder_url" placeholder="https://pan.quark.cn/..."></label>
<label>夸克浏览器后台运行<select id="quark_headless"><option value="false">否（可见，建议先调试）</option><option value="true">是（登录后再尝试）</option></select></label>
</div>
</div><p class="hint">夸克网页上传仍需要事先确认该页面的上传、文件行和分享控件；这些高级选择器保留在配置文件中，不会在这里猜测或生成假分享链接。</p><p class="danger">请勿填写或分享 Telegram/夸克密码、验证码、Cookie 或浏览器会话目录。</p>
<p id="form-error" class="danger" role="alert"></p>
  <button onclick="saveConfig()">保存配置</button><button onclick="checkBrowser()" class="secondary">检查浏览器</button><button onclick="loginTelegram()" class="secondary">首次登录 Telegram</button><button onclick="loginQuark()" class="secondary">首次登录夸克</button><button onclick="runPipeline()" class="warn">启动流水线</button><button onclick="stopPipeline()" class="secondary">停止监测</button></div>
<div class="card"><h2>文件与日志</h2><div id="paths"></div><button onclick="openDownloads()" class="secondary">打开下载目录</button><button onclick="refreshLogs()" class="secondary">刷新日志</button><pre id="logs">尚未运行</pre></div>
</main><script>
const ids=['source_channel_url','browser_executable_path','download_start_timeout_seconds','download_transfer_timeout_seconds','archive_test_executable_path','recent_message_limit','telegram_run_mode','telegram_browser_headless','upload_provider','quark_folder_url','quark_headless'];
function requiredFields(){const result=['browser_executable_path','source_channel_url','recent_message_limit','download_start_timeout_seconds','download_transfer_timeout_seconds'];if(document.getElementById('upload_provider').value==='quark_browser')result.push('quark_folder_url');return result}
function markRequired(){
 const required=new Set(requiredFields());
 ids.forEach(id=>{const input=document.getElementById(id),label=input.closest('label');let star=label.querySelector('.required-star');if(!star){star=document.createElement('span');star.className='required-star';star.textContent=' *';label.insertBefore(star,input)}star.hidden=!required.has(id);input.setAttribute('aria-required',required.has(id)?'true':'false')});
 document.getElementById('quark-fields').hidden=document.getElementById('upload_provider').value!=='quark_browser';
}
function showBrowser(c){document.getElementById('browser-info').textContent=c.browser_profile_error||('版本 '+c.browser_version+' | '+c.browser_profile_status+' | '+c.browser_profile_dir)}
async function refreshBrowser(){const r=await fetch('/api/config');if(r.ok)showBrowser(await r.json())}
async function load(){let r=await fetch('/api/config');let c=await r.json();ids.forEach(id=>{if(c[id]!==undefined)document.getElementById(id).value=String(c[id])});document.getElementById('paths').textContent='下载目录：'+(c.download_dir||'')+' | 待检查：'+(c.unverified_dir||'')+' | 日志：'+(c.log_path||'');showBrowser(c);markRequired();refreshLogs()}
function values(){return {browser_executable_path:document.getElementById('browser_executable_path').value.trim(),download_start_timeout_seconds:Number(document.getElementById('download_start_timeout_seconds').value),download_transfer_timeout_seconds:Number(document.getElementById('download_transfer_timeout_seconds').value),archive_test_executable_path:document.getElementById('archive_test_executable_path').value.trim(),source_channel_url:document.getElementById('source_channel_url').value.trim(),recent_message_limit:Number(document.getElementById('recent_message_limit').value),telegram_run_mode:document.getElementById('telegram_run_mode').value,telegram_scan_limit:200,telegram_browser_headless:document.getElementById('telegram_browser_headless').value==='true',upload_provider:document.getElementById('upload_provider').value,quark_folder_url:document.getElementById('quark_folder_url').value.trim(),quark_headless:document.getElementById('quark_headless').value==='true',telegram_access_mode:'browser',fetch_mode:'recent',source_chat:'',message_ids:[],cloud_folder:''}}
function validate(fields=requiredFields()){
 const error=document.getElementById('form-error');error.textContent='';document.querySelectorAll('.field-error').forEach(el=>el.classList.remove('field-error'));
 for(const id of fields){const input=document.getElementById(id),value=input.value.trim();if(!value||(input.type==='number'&&(!Number.isInteger(Number(value))||Number(value)<Number(input.min)||Number(value)>Number(input.max)))||(id==='source_channel_url'&&!/^https:[/][/]t[.]me[/][A-Za-z0-9_]+[/]?$/.test(value))){input.classList.add('field-error');input.focus();error.textContent='请填写或检查：'+input.closest('label').textContent.trim();return false}}
 return true;
}
async function saveConfig(showMessage=true){if(!validate(['browser_executable_path','download_start_timeout_seconds','download_transfer_timeout_seconds']))return false;const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values())});const x=await r.json();if(!r.ok){document.getElementById('form-error').textContent=x.message;return false}await refreshBrowser();if(showMessage)alert(x.message);return true}
async function checkBrowser(){if(!await saveConfig(false))return;const r=await fetch('/api/browser-check',{method:'POST'});if(!r.ok)document.getElementById('form-error').textContent=(await r.json()).message;refreshLogs()}
async function runPipeline(){if(!validate())return;if(!await saveConfig(false))return;let r=await fetch('/api/run',{method:'POST'});if(!r.ok)document.getElementById('form-error').textContent=(await r.json()).message;refreshLogs()}
async function stopPipeline(){let r=await fetch('/api/stop',{method:'POST'});if(!r.ok)document.getElementById('form-error').textContent=(await r.json()).message;refreshLogs()}
async function loginTelegram(){if(!validate(['browser_executable_path','source_channel_url']))return;if(!await saveConfig(false))return;let r=await fetch('/api/telegram-login',{method:'POST'});if(!r.ok)document.getElementById('form-error').textContent=(await r.json()).message;refreshLogs()}
async function loginQuark(){if(!validate(['browser_executable_path','quark_folder_url']))return;if(!await saveConfig(false))return;let r=await fetch('/api/quark-login',{method:'POST'});if(!r.ok)document.getElementById('form-error').textContent=(await r.json()).message;refreshLogs()}
async function openDownloads(){let r=await fetch('/api/open-downloads',{method:'POST'});if(!r.ok)document.getElementById('form-error').textContent=(await r.json()).message}
let logTimer;
async function refreshLogs(){clearTimeout(logTimer);let r=await fetch('/api/logs');let x=await r.json();let box=document.getElementById('logs');box.textContent=x.logs.join('\\n')||'尚未运行';box.scrollTop=box.scrollHeight;if(x.running)logTimer=setTimeout(refreshLogs,1000);else await refreshBrowser()}
document.getElementById('upload_provider').addEventListener('change',markRequired);
load();
</script></body></html>"""


def config_payload(config: PipelineConfig) -> dict:
    return {
        "source_chat": "",
        "browser_executable_path": config.browser_executable_path,
        "download_start_timeout_seconds": config.download_start_timeout_seconds,
        "download_transfer_timeout_seconds": config.download_transfer_timeout_seconds,
        "archive_test_executable_path": config.archive_test_executable_path,
        "download_dir": str(config.inbox_dir.resolve()),
        "unverified_dir": str((config.inbox_dir / ".unverified").resolve()),
        "log_path": str((config.output_dir / "logs" / "pipeline.log").resolve()),
        "source_channel_url": config.source_channel_url,
        "fetch_mode": "recent",
        "recent_message_limit": config.recent_message_limit,
        "telegram_run_mode": config.telegram_run_mode,
        "message_ids": [],
        "telegram_access_mode": "browser",
        "telegram_browser_headless": config.telegram_browser_headless,
        "upload_provider": config.upload_provider,
        "quark_folder_url": config.quark_folder_url,
        "quark_headless": config.quark_headless,
    }


def browser_payload(config: PipelineConfig) -> dict:
    try:
        profile = resolve_browser_profile(config.telegram_browser_profile, config.browser_executable_path)
        return {"browser_version": version_text(profile.version), "browser_profile_dir": str(profile.directory),
                "browser_profile_status": profile.status, "browser_profile_error": ""}
    except (ValueError, RuntimeError, OSError) as exc:
        return {"browser_version": "", "browser_profile_dir": "", "browser_profile_status": "",
                "browser_profile_error": str(exc)}


def write_config(path: Path, payload: dict) -> None:
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not payload.get("telegram_api_hash"):
        payload.pop("telegram_api_hash", None)
    existing.update(payload)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def validate_browser_settings(payload: dict) -> None:
    if not isinstance(payload.get("browser_executable_path", ""), str):
        raise ValueError("浏览器程序路径必须为字符串")
    validate_browser_path(payload.get("browser_executable_path", ""))
    for key, maximum in (("download_start_timeout_seconds", 86400),
                         ("download_transfer_timeout_seconds", 604800)):
        value = payload.get(key)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"{key} 必须为 1 到 {maximum} 的整数秒")
    if not isinstance(payload.get("archive_test_executable_path", ""), str):
        raise ValueError("RAR 校验程序路径必须为字符串")
    if payload.get("archive_test_executable_path"):
        find_archive_tester(payload["archive_test_executable_path"])


def start_web_ui(config_path: str | Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    config_path = Path(config_path).resolve()
    state = WebState(config_path)
    configure_logging(PipelineConfig.from_json(config_path).output_dir / "logs", MemoryLogHandler(state))
    logging.info("v24 浏览器目录隔离版; 配置文件=%s", config_path)

    class Handler(BaseHTTPRequestHandler):
        def json_response(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                body = HTML.encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            elif path == "/api/config":
                config = PipelineConfig.from_json(config_path)
                self.json_response({**config_payload(config), **browser_payload(config)})
            elif path == "/api/logs":
                self.json_response(state.snapshot())
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            expected_hosts = {f"{host}:{port}", f"localhost:{port}"}
            if (self.headers.get("Host") not in expected_hosts
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"
                    or (self.headers.get("Origin") and self.headers["Origin"] not in
                        {f"http://{value}" for value in expected_hosts})):
                self.json_response({"message": "仅允许本机同源请求"}, 403)
                return
            length = int(self.headers.get("Content-Length", 0))
            if length < 0 or length > 65536:
                self.json_response({"message": "请求体过大"}, 413)
                return
            body = self.rfile.read(length)
            path = urlparse(self.path).path
            if path == "/api/config":
                try:
                    payload = json.loads(body or b"{}")
                    if not isinstance(payload, dict):
                        raise ValueError("配置必须为 JSON 对象")
                    allowed = set(config_payload(PipelineConfig.from_json(config_path))) - {"download_dir", "log_path", "unverified_dir"}
                    allowed.update({"telegram_scan_limit", "cloud_folder"})
                    payload = {key: value for key, value in payload.items() if key in allowed}
                    validate_browser_settings(payload)
                except (ValueError, RuntimeError, OSError) as exc:
                    self.json_response({"message": str(exc)}, 400)
                    return
                payload.update({
                    "telegram_access_mode": "browser",
                    "fetch_mode": "recent",
                    "source_chat": "",
                    "message_ids": [],
                })
                with state.lock:
                    busy = state.running
                    if not busy:
                        write_config(config_path, payload)
                if busy:
                    self.json_response({"message": "任务运行中，不能修改配置"}, 409)
                    return
                self.json_response({"message": "配置已保存"})
            elif path == "/api/open-downloads":
                try:
                    directory = PipelineConfig.from_json(config_path).inbox_dir.resolve()
                    directory.mkdir(parents=True, exist_ok=True)
                    os.startfile(str(directory))
                    self.json_response({"message": "已打开下载目录"})
                except (OSError, AttributeError) as exc:
                    self.json_response({"message": f"打开目录失败: {exc}"}, 400)
            elif path == "/api/stop":
                if not state.running:
                    self.json_response({"message": "当前没有运行中的任务"}, 409); return
                state.stop_event.set()
                logging.info("已请求停止；当前附件下载完成后退出监测")
                self.json_response({"message": "已请求停止"})
            elif path in {"/api/run", "/api/quark-login", "/api/telegram-login", "/api/browser-check"}:
                try:
                    validate_browser_settings(config_payload(PipelineConfig.from_json(config_path)))
                except (ValueError, RuntimeError, OSError) as exc:
                    self.json_response({"message": str(exc)}, 400)
                    return
                with state.lock:
                    busy = state.running
                    if not busy:
                        state.stop_event.clear()
                        state.running = True
                if busy:
                    self.json_response({"message": "已有任务运行中"}, 409)
                    return
                threading.Thread(target=worker, args=(path,), daemon=True).start()
                self.json_response({"message": "任务已启动"})
            else:
                self.send_error(404)

        def log_message(self, format: str, *args) -> None:
            if args and isinstance(args[0], str) and "/api/logs" in args[0]:
                return
            logging.info("Web %s", format % args)

    def worker(action: str) -> None:
        try:
            config = PipelineConfig.from_json(config_path)
            logging.info("开始任务，上传方式=%s", config.upload_provider)
            if action == "/api/browser-check":
                check_browser(config.telegram_browser_profile, config.browser_executable_path)
                return
            if action == "/api/quark-login":
                from .quark_browser import login_and_inspect
                login_and_inspect(config.quark_folder_url, config.quark_browser_profile, config.browser_executable_path)
                logging.info("夸克登录流程结束")
                return
            if action == "/api/telegram-login":
                from .adapters import telegram_browser_login
                telegram_browser_login(config.telegram_browser_profile, config.source_channel_url, config.browser_executable_path)
                logging.info("Telegram Web 登录流程结束")
                return
            if config.source_channel_url or config.source_chat:
                if config.telegram_access_mode == "browser":
                    if config.telegram_run_mode not in {"once", "watch"} or not 1 <= config.telegram_scan_limit <= 1000:
                        raise ValueError("运行模式或扫描上限无效")
                    telegram = TelegramBrowserAdapter(config.telegram_browser_profile, config.inbox_dir, config.telegram_browser_headless, config.telegram_browser_timeout_seconds, config.telegram_message_selector, config.telegram_attachment_selector, watch_mode=config.telegram_run_mode == "watch", stop_event=state.stop_event, scan_limit=config.telegram_scan_limit, **config.browser_download_options())
                    logging.info("使用 Telegram Web 浏览器抓取（无需 API ID/API Hash）")
                else:
                    if not config.telegram_api_id or not config.telegram_api_hash:
                        raise ValueError("API 模式需要 Telegram API ID 和 API Hash；或将 telegram_access_mode 改为 browser")
                    telegram = TelethonTelegramAdapter(config.telegram_api_id, config.telegram_api_hash, config.telegram_session, config.inbox_dir)
                    logging.info("使用 Telegram API 实际抓取")
            else:
                telegram = LocalTelegramAdapter(config.inbox_dir)
                logging.info("使用本地素材模拟抓取")
            if config.upload_provider == "quark_browser":
                cloud = QuarkBrowserStorageAdapter(folder_url=config.quark_folder_url, profile_dir=config.quark_browser_profile, headless=config.quark_headless, upload_input_selector=config.quark_upload_input_selector, file_row_selector=config.quark_file_row_selector, share_button_selector=config.quark_share_button_selector, share_link_selector=config.quark_share_link_selector, timeout_seconds=config.quark_timeout_seconds, browser_executable_path=config.browser_executable_path)
            else:
                cloud = LocalCloudStorageAdapter(config.output_dir / "cloud_fixture")
            if config.telegram_run_mode == "watch" and isinstance(telegram, TelegramBrowserAdapter):
                telegram.on_message = lambda message: process_messages(config, [message], cloud)
                telegram.fetch_messages(config.source_chat, config.message_ids, config.source_channel_url, "recent", config.recent_message_limit)
                logging.info("持续监测已停止")
                return
            candidates = run_pipeline(config, telegram, cloud)
            logging.info("任务结束，候选数=%d", len(candidates))
            for item in candidates:
                logging.info("%s status=%s share=%s warnings=%s", item.candidate_id, item.status, item.share_url, item.warnings)
        except Exception:
            logging.exception("任务失败")
        finally:
            with state.lock:
                state.running = False

    server = ThreadingHTTPServer((host, port), Handler)
    if open_browser:
        webbrowser.open(f"http://{host}:{port}/")
    logging.info("界面已启动：http://%s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
