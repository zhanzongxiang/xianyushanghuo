# 闲鱼素材流水线

从 Telegram Web 读取频道压缩包，校验并整理本地素材；可选择本地模拟上传或通过夸克网页上传。输出待人工审核，不自动发布闲鱼。

## 源码运行（Windows）

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config\test.example.json config\test.local.json
.\.venv\Scripts\python.exe run_pipeline.py --config config\test.local.json --web-ui
```

浏览器打开 `http://127.0.0.1:8765/`，在配置页填写本机 Chrome/Edge/Chromium 主程序的绝对 EXE 路径（必填），保存后点击“检查浏览器”。填写真实 Telegram 公开频道链接，首次登录后再启动流水线。自动化浏览器使用与日常浏览器分开的登录目录；切换到不兼容版本时需要重新登录，原会话不会被删除。

运行配置 `config/test.local.json`、账号会话、下载数据、构建产物和日志只保存在本机，不提交到仓库。`config/test.example.json` 仅包含空凭证和示例值。

## 构建便携版

```powershell
.\build_portable.ps1
```

产物位于 `outputs/XianYuMaterialPipeline_v24_profile_isolation/`，运行其中的 `start_ui.cmd`。此目录需整体保留，不要单独移动 EXE。构建脚本不会捆绑 Chromium，目标电脑需要自行安装受支持的浏览器。

```powershell
$env:BROWSER_TEST_PATH = "C:\Program Files\Google\Chrome\Application\chrome.exe"
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe tests\check_ui_browser.py
```

更多操作说明、下载故障和 RAR 校验行为见 [README_TEST.md](README_TEST.md)。真实频道下载和夸克上传需在使用者账号与网络环境中验证。
