"""Optional single-admin authentication for the server workbench."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlparse

COOKIE = "workbench_session"


def hash_password(password: str, salt: str | None = None) -> str:
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError("密码长度必须为12至256个字符")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        return False
    try:
        algorithm, salt, digest = encoded.split("$")
        return algorithm == "scrypt" and hmac.compare_digest(hash_password(password, salt), encoded)
    except (ValueError, TypeError):
        return False


class AdminAuth:
    def __init__(self, credential_file: Path, hosts: set[str], secure: bool = False):
        self.path = credential_file
        self.hosts = hosts
        self.secure = secure
        self.lock = threading.Lock()
        self.sessions: dict[str, float] = {}
        self.attempts: dict[str, list[float]] = {}
        data = json.loads(credential_file.read_text(encoding="utf-8"))
        self.username = data["username"]
        self.password_hash = data["password_hash"]
        if not self.username or not self.password_hash.startswith("scrypt$") or not hosts:
            raise ValueError("管理员认证配置无效")
        # Validate the hash structure at startup, without loading a plaintext password.
        algorithm, salt, digest = self.password_hash.split("$")
        if len(bytes.fromhex(salt)) != 16 or len(bytes.fromhex(digest)) != 64:
            raise ValueError("管理员密码哈希无效")

    @classmethod
    def from_environment(cls, host: str, port: int):
        filename = os.environ.get("WORKBENCH_AUTH_FILE")
        if not filename:
            return None  # Preserve existing desktop behaviour.
        hosts = {f"{host}:{port}", f"localhost:{port}"}
        hosts.update(value.strip() for value in os.environ.get("WORKBENCH_ALLOWED_HOSTS", "").split(",") if value.strip())
        return cls(Path(filename), hosts, os.environ.get("WORKBENCH_SECURE_COOKIE") == "1")

    def authenticated(self, cookie: str) -> bool:
        try:
            parsed = SimpleCookie()
            parsed.load(cookie)
            token = parsed[COOKIE].value if COOKIE in parsed else ""
        except Exception:
            return False
        key = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            now = time.monotonic()
            self.sessions = {k: expiry for k, expiry in self.sessions.items() if expiry > now}
            return bool(token) and key in self.sessions

    def login(self, username, password, address: str):
        with self.lock:
            now = time.monotonic()
            self.attempts = {key: [t for t in times if t > now - 900] for key, times in self.attempts.items()}
            times = self.attempts.setdefault(address, [])
            if len(times) >= 5:
                return None, 429
            times.append(now)
            valid = verify_password(password, self.password_hash)
            if not isinstance(username, str) or not hmac.compare_digest(username.encode(), self.username.encode()) or not valid:
                return None, 401
            if len(self.sessions) >= 32:
                self.sessions.pop(next(iter(self.sessions)))
            token = secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = now + 8 * 3600
            return token, 200

    def logout(self, cookie: str):
        try:
            parsed = SimpleCookie(); parsed.load(cookie)
            token = parsed[COOKIE].value
        except Exception:
            return
        with self.lock:
            self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)

    def cookie_header(self, token: str, clear: bool = False) -> str:
        return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={0 if clear else 28800}" + ("; Secure" if self.secure else "")


LOGIN_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>工作台登录</title><style>body{font-family:system-ui;background:#f5f7fb;color:#17243b;display:grid;place-items:center;min-height:95vh}main{background:white;padding:32px;border:1px solid #dce3ed;border-radius:16px;width:min(340px,80vw)}input,button{box-sizing:border-box;width:100%;padding:12px;margin:8px 0;border:1px solid #c6d1e0;border-radius:8px}button{background:#245cdb;color:white;cursor:pointer}p{font-size:14px;color:#506079}#error{color:#b42318}</style><main><h2>管理员登录</h2><p>个人工作台 · 不开放注册</p><form id="login"><label>账号<input id="username" autocomplete="username" required maxlength="128"></label><label>密码<input id="password" type="password" autocomplete="current-password" required maxlength="256"></label><button>登录工作台</button><p id="error" role="alert"></p></form><p>无域名部署仅通过 SSH 隧道访问。</p></main><script>document.getElementById('login').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;try{const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:document.getElementById('username').value,password:document.getElementById('password').value})});const d=await r.json();if(r.ok)location.replace('/');else document.getElementById('error').textContent=d.message}catch(e){document.getElementById('error').textContent='连接失败，请检查 SSH 隧道'}finally{b.disabled=false;document.getElementById('password').value=''}};</script></html>"""

LOGOUT_HTML = """<button id="admin-logout" style="position:fixed;bottom:16px;right:16px;z-index:1000;padding:10px;border:1px solid #c6d1e0;border-radius:8px;background:white;color:#17243b">退出登录</button><script>document.getElementById('admin-logout').onclick=async()=>{const r=await fetch('/api/auth/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(r.ok)location.replace('/login')};</script>"""


class AuthHandlerMixin:
    """Gate every GET/POST before reading or executing any business operation."""
    auth: AdminAuth | None = None

    def end_headers(self):
        if self.auth:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def auth_gate(self, method: str) -> bool:
        auth = self.auth
        if auth is None:
            return True
        path = urlparse(self.path).path
        if self.headers.get("Host") not in auth.hosts:
            self.json_response({"message": "访问地址未获允许"}, 403)
            return False
        if method == "POST":
            scheme = "https" if auth.secure else "http"
            if (self.headers.get("Origin") != f"{scheme}://{self.headers.get('Host')}"
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"
                    or self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json"):
                self.json_response({"message": "请求必须来自工作台同源页面"}, 403)
                return False
        if method == "GET" and path == "/login":
            body = LOGIN_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
            return False
        if method == "POST" and path == "/api/auth/login":
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError()
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 4096:
                    raise ValueError()
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError()
                token, status = auth.login(data.get("username"), data.get("password"), self.client_address[0])
            except (ValueError, UnicodeDecodeError):
                self.json_response({"message": "登录请求无效"}, 400)
                return False
            if token:
                body = b'{"message":"ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", auth.cookie_header(token))
                self.end_headers(); self.wfile.write(body)
            else:
                self.json_response({"message": "尝试次数过多，请15分钟后重试" if status == 429 else "账号或密码错误"}, status)
            return False
        if not auth.authenticated(self.headers.get("Cookie", "")):
            if method == "GET" and path == "/":
                self.send_response(303); self.send_header("Location", "/login")
                self.send_header("Content-Length", "0"); self.end_headers()
            else:
                self.json_response({"message": "请先登录工作台"}, 401)
            return False
        if method == "POST" and path == "/api/auth/logout":
            auth.logout(self.headers.get("Cookie", ""))
            self.send_response(200); self.send_header("Set-Cookie", auth.cookie_header("", True))
            self.send_header("Content-Length", "0"); self.end_headers()
            return False
        return True
