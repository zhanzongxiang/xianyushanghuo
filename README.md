# 闲鱼素材流水线

从 Telegram API 或 Telegram Web 读取频道压缩包，校验并整理本地素材；可选择本地模拟上传或通过夸克网页上传。输出待人工审核，不自动发布闲鱼。

## 素材工作台

下载页展示素材图片、文件名、真实下载进度、速度和失败原因；上传页勾选已下载的 ZIP/RAR
后再上传。右侧可查看大图、来源、校验结果和本地目录，设置、日志、来源频道独立管理。
任务记录刷新后保留，重启时将未结束的任务标记为中断，不显示虚构进度。
图片来自对应附件缩略图或 ZIP 内的图片；没有可用图片时显示文件图标。
夸克网页上传仅显示可确认的阶段，分享链接待确认时不重复发送文件。

## 源码运行（Windows）

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config\test.example.json config\test.local.json
.\.venv\Scripts\python.exe run_pipeline.py --config config\test.local.json --web-ui
```

浏览器打开本地界面后可选择 Telegram Web 或 Telegram API。Web 模式需填写 Chrome/Edge/Chromium 的 EXE 路径并先登录浏览器；API 模式需填写 API ID/Hash，点击“首次登录 Telegram”在新终端完成账号授权。只有 Web 模式或夸克网页上传需要浏览器程序路径。API Hash 和账号会话只保存在本机，不会从界面读取接口返回。

运行配置 `config/test.local.json`、账号会话、下载数据、构建产物和日志只保存在本机，不提交到仓库。`config/test.example.json` 仅包含空凭证和示例值。

## 构建便携版

```powershell
.\build_portable.ps1
```

产物位于 `outputs/XianYuMaterialPipeline_v34_download_directory/`，运行其中的 `start_ui.cmd`。此目录需整体保留，不要单独移动 EXE。构建脚本包含离线界面资源，不会捆绑 Chromium，Web 模式仍需自行安装受支持的浏览器。API 模式可选配置 SOCKS5/HTTP 代理和 DC 2 生产地址；后者也用于跨 DC 文件下载，但不能代替可用的网络连接。API 下载连续无进展或超过总时限时中断并保留非空暂存文件；“停止任务”也会中断当前 API 下载。原来的命令行流水线保持兼容。

设置页的“下载保存目录”接受绝对路径，保存时检查目录可写，后续下载与预览写入新目录。
原文件不搬迁，历史任务继续按保存时的原目录读取文件与图片；任务运行中不可更改配置。

API 工作台优先获取压缩包紧邻前一条消息的照片或同组图集，最多 5 张，按消息顺序展示。
图片来源和消息 ID 会保存；相邻位置仅是自动关联规则，不代表已确认图片属于该文件。
中间出现文字、文件、视频、链接预览或另一图集时不跨越；图片失败不阻止下载，依次回退到附件缩略图及 ZIP 内图片。
RAR 无需解压即可显示前置图集。Web 模式及没有 Telegram 消息 ID 的历史本地文件暂不支持该关联。

```powershell
$env:BROWSER_TEST_PATH = "C:\Program Files\Google\Chrome\Application\chrome.exe"
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe tests\check_ui_browser.py
```

更多操作说明、下载故障和 RAR 校验行为见 [README_TEST.md](README_TEST.md)。真实频道下载和夸克上传需在使用者账号与网络环境中验证。
