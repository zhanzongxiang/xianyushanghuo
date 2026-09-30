# 跨电脑测试说明

## v24：修复启动闪退，可运行版本

本节优先于下方历史版本说明。解压完整的 `XianYuMaterialPipeline_v24_profile_isolation`
文件夹，双击 `start_ui.cmd`；不要单独移动 EXE 或删除 `_internal`。无需安装 Python。

1. 填写本机 Chrome / Edge 主程序的绝对 EXE 路径并保存，这是必填项。
2. 点击“检查浏览器”。程序实际启动后自动关闭，日志应出现“浏览器检查通过”。
3. 填写真实 Telegram 公开频道链接，点击“首次登录 Telegram”，在程序打开的窗口中完成登录。
4. 上传方式先保持“本地模拟（先验证下载）”，点击“启动流水线”。成品在“打开下载目录”中查看。
5. 夸克上传仍需真实文件夹地址和经过账号页面验证的选择器，未配置不能自动上传。
   首次登录夸克后关闭程序打开的窗口即可结束，无需在终端按回车；最长等待 5 分钟。

本次确认的 v23 闪退原因：Chromium 153 创建的旧目录被系统 Chrome 142 加载，进程立即退出。
v24 根据浏览器 EXE 路径隔离专用目录，读取文件版本及目录记录，正常升级复用兼容目录，
降级或切换浏览器时另建目录；首次切换需要重新登录，不复制或删除旧登录目录。
界面显示的是保存配置对应的浏览器版本、专用目录和复用状态，实际启动结果以检查日志为准。
使用同一 EXE 不等于接管手动打开的浏览器窗口；自动化仍保持独立进程和登录数据。
独立目录有跨进程使用锁，不能同时进行登录、检查及下载任务。

浏览器检查命令（不访问 Telegram、不下载资源）：

```powershell
.\XianYuMaterialPipeline_v24_profile_isolation.exe --config test.local.json --browser-check
```

保留 v23 的下载超时、持久化复制、RAR 完整性校验和失败文件留存。
真实 Telegram 登录、频道权限、网络连通性及大文件下载需要在使用者账号环境确认。

## v23：系统浏览器路径与可靠下载

本节优先于下方旧版本说明。新包使用 `XianYuMaterialPipeline_v23_system_browser.exe`，
通过 `start_ui.cmd` 启动，不再分发或自动选择随包 Chromium。

- 在配置页填写**浏览器程序路径**，例如 `C:\Program Files\Google\Chrome\Application\chrome.exe`
  或 `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`。保存、登录和启动时均校验，
  必须是现存的绝对 EXE 路径，不接受快捷方式或附加命令参数。支持 Chromium 内核，不支持 Firefox/IE。
- 程序仍使用独立登录目录，不接管个人浏览器窗口，不共享个人 Profile。切换浏览器版本或产品时
  可能需要重新登录；若提示 Profile 不兼容，应配置新的专用 `telegram_browser_profile`，
  不要把路径指向日常浏览器用户目录，也不要删除旧登录目录。
- 下载启动等待默认 180 秒，另有 30 秒观察宽限；只点击一次，不因短暂未出现下载事件而点击下一个附件。
  传输独立限时默认 3600 秒，记录实际接收字节，超时明确取消。点击停止时，已开始传输的当前附件会先处理完。
- 浏览器暂存文件在 `inbox/.browser-downloads/<任务ID>/`；收到完成事件后才复制、校验和正式保存到 `inbox`。
  文件与日志区显示绝对路径，按钮打开固定配置的下载目录，而不是浏览器下载历史的 UUID 路径。
- ZIP 保留 CRC 校验。RAR 使用系统 UnRAR/7-Zip 的 **test** 命令，不解压或执行包内内容。
  可选字段 `archive_test_executable_path` 可指定 `UnRAR.exe` 或 `7z.exe`；
  留空则查找 PATH 和常见安装目录。不随包分发这些工具。
- RAR 加密、损坏、分卷缺失、校验工具缺失或校验超时时，不标记成功；已下载文件保存在
  `inbox/.unverified/<ID>/原文件名.rar`，日志记录具体分类和位置。
  当前不支持自动输入解压密码或自动归并分卷，需人工处理。失败的浏览器暂存文件也不计为成品。
- 运行日志为 `output/logs/pipeline.log`，每份 5 MiB、保留 5 个备份，包含时间和错误堆栈；
  浏览器诊断日志位于专用 Profile 的 `logs/chromium-时间-ID.log`，不会每次覆盖。
  分享日志前应检查频道信息、文件名和路径等隐私。
- 前端与本机 API 均拒绝缺失浏览器路径，运行期间禁止修改配置；修改及打开目录接口拒绝跨站请求。

可配置的高级字段：

```json
{
  "browser_executable_path": "",
  "download_start_timeout_seconds": 180,
  "download_start_grace_seconds": 30,
  "download_transfer_timeout_seconds": 3600,
  "archive_test_executable_path": "",
  "archive_test_timeout_seconds": 600
}
```

本地测试（不会连接真实 Telegram 或上传网盘）：

```powershell
$env:BROWSER_TEST_PATH = "C:\Program Files\Google\Chrome\Application\chrome.exe"
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe tests\check_ui_browser.py
```

浏览器集成测试需要 `BROWSER_TEST_PATH`，生成真实 RAR 样本的测试需要本机 WinRAR；
缺少时对应集成测试会跳过，其余单元测试照常运行。真实频道 GB 级文件仍需要账号环境复测。

## 参数与日志界面

**必须运行 `dist\XianYuMaterialPipeline\XianYuMaterialPipeline.exe`，不要运行 `build\XianYuMaterialPipeline\` 下的文件。**

`build` 是 PyInstaller 的临时构建目录，不是可分发程序目录；直接复制或运行 build 里的 EXE 会出现 `Failed to load Python DLL`，并且中文路径可能在错误信息中显示为问号。

推荐双击最新版本：

```text
dist\XianYuMaterialPipeline_v4\启动界面.bat
```

如果使用其他版本，批处理文件中的 EXE 名称必须与文件夹内实际 EXE 完全一致。不要把 v3 的批处理复制到 v4 文件夹。 

或运行：

```powershell
cd dist\XianYuMaterialPipeline
.\XianYuMaterialPipeline.exe --config test.local.json --web-ui
```

浏览器打开 `http://127.0.0.1:8765/`，界面只保留 Telegram 频道链接、最新消息数量、后台运行开关和夸克目标文件夹等必要参数；高级浏览器路径和网页选择器继续保存在配置文件中，保存配置后可启动首次登录流程并查看日志。界面只监听本机回环地址，不对局域网开放。

首次登录 Telegram 时点击“首次登录 Telegram”，在弹出的可见浏览器中完成登录；登录后再尝试将 Telegram“后台运行”设为“是”。首次登录夸克时点击“首次登录夸克”，在弹出的可见浏览器中完成登录；如果选择器不正确，日志会显示失败原因，程序不会写入伪造分享链接。

## 1. 推荐测试方式：Python 便携运行

如果使用源码模式，在目标 Windows 电脑安装 Python 3.11 或更高版本，然后在项目目录执行：

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe run_pipeline.py --config config/test.local.json
```

当前测试配置使用本地模拟模式，不连接 Telegram，也不上传夸克网盘。运行后查看：

- `sample_data/output/manifest.json`
- `sample_data/output/cloud_fixture/test-folder/`

## 2. 准备本地测试素材

把测试用的 ZIP、PDF、JPG 等文件放入：

```text
sample_data/inbox/
```

建议至少准备：

- 一个 ZIP 压缩包
- 压缩包内包含一个 PDF 或普通文档
- 一张 JPG/PNG 展示图

不要把真实账号密钥、Telegram session 或夸克 Cookie 放进代码包或 Git 仓库。

## 3. Telegram 测试

### 3.1 浏览器/屏幕自动化模式（无需 API ID/API Hash）

在界面中选择“浏览器/屏幕自动化（无需 API）”，填写公开频道链接、抓取模式 `recent` 和最新消息数量。首次点击“首次登录 Telegram”，程序会打开可见 Telegram Web 窗口，人工完成登录后按提示保存会话；登录态保存在 `telegram_browser_profile`。后续可将 `telegram_browser_headless` 设为 `true` 后台运行。该模式当前只支持公开频道链接和频道整体最新 N 条发言，不支持指定消息 ID；页面结构变化时可在界面中补充实际确认的消息/附件选择器。

命令行首次登录方式：

```powershell
XianYuMaterialPipeline.exe --config test.local.json --telegram-login
```

浏览器模式不需要 Telegram API ID/API Hash，但仍需要用户本人登录 Telegram Web；程序不会绕过验证码、二次验证或频道权限。

### 3.2 Telegram API 模式

现在可以直接填写公开频道链接，检索频道整体最新的 N 条发言（不按某个发言人筛选），再从这 N 条中处理含附件的消息：

```json
{
  "source_channel_url": "https://t.me/example_channel",
  "fetch_mode": "recent",
  "recent_message_limit": 20
}
```

程序会使用本机 Telegram 登录会话按频道整体顺序读取最新消息，不读取或筛选发言人；在这 N 条中只下载带附件的消息。公开链接不会绕过登录权限。私有频道需要账号本身有访问权限，私有链接不能仅凭 URL 推断聊天 ID。

如果仍要指定消息，使用：

```json
{
  "source_channel_url": "https://t.me/example_channel",
  "fetch_mode": "message_ids",
  "message_ids": [12345, 12346]
}
```

在 API 模式下，配置以下字段后，程序会选择 Telegram 实际适配器：

```json
{
  "source_chat": "聊天用户名或 ID",
  "message_ids": [12345],
  "telegram_api_id": 123456,
  "telegram_api_hash": "你的 api_hash",
  "telegram_session": "sample_data/telegram.session"
}
```

然后安装依赖并运行。首次运行可能需要在终端完成 Telegram 登录验证。不要把生成的 `.session` 文件分享给他人。

## 4. 夸克网盘浏览器自动化测试

当前支持 `upload_provider: "quark_browser"`：通过夸克网页完成上传，不调用未公开的内部 API。

首次建议使用可见浏览器（`quark_headless: false`）登录并确认目标文件夹；可以运行：

```powershell
XianYuMaterialPipeline.exe --config test.local.json --quark-login
```

登录态保存在独立目录 `quark_browser_profile`。确认会话可用、网页控件选择器已经在目标账号页面实测后，再切换 `quark_headless: true` 后台运行。

必须填写以下实际页面选择器，不要凭空猜测：

```json
{
  "quark_folder_url": "https://pan.quark.cn/...",
  "quark_browser_profile": "sample_data/quark_browser_profile",
  "quark_headless": true,
  "quark_upload_input_selector": "...",
  "quark_file_row_selector": "...",
  "quark_share_button_selector": "...",
  "quark_share_link_selector": "..."
}
```

如果遇到扫码、验证码、二次验证或页面结构变化，应先改为可见模式人工处理；程序不会绕过验证，也不会把未确认的文本当作分享链接。

当前仍建议第一轮先用 `upload_provider: "local"` 做离线验收，再在目标电脑配置真实夸克文件夹地址和选择器。

### 4.1 便携包浏览器运行时

v12 测试包（`XianYuMaterialPipeline_v12_test.zip`）已将 Playwright Chromium 一并放入 `_internal/playwright/driver/package/.local-browsers/`。请解压完整的 `XianYuMaterialPipeline_v12_test` 文件夹，不能只复制 EXE，也不要删除 `_internal`。首次登录时程序打开 `web.telegram.org/k/` 而非 `t.me` 频道介绍页；请在**程序打开的浏览器**中登录，桌面版客户端的登录状态不会自动复用，完成后关闭登录浏览器窗口。抓取时会进入 `web.telegram.org/k/#@频道用户名` 并在日志中报告当前 URL、标题、消息节点数与下载结果。若未读取到消息，会报错而非假装成功；图片在 Web 端可能只打开预览，网页不同版本的附件控件仍需按实际登录页面验证。请先用“本地模拟”上传方式验收下载。

v14 测试版把默认 Telegram Web 登录态保存在当前 Windows 用户固定目录 `%LOCALAPPDATA%\XianYuMaterialPipeline\telegram_browser_profile`，更新/解压新版后仍复用同一浏览器会话；首次换用 v14 需登录一次，后续不要删除此目录，也不要与其他人分享。若 Telegram 本身撤销会话、退出登录或在不同 Windows 账户/电脑使用，仍可能要求重新扫码。自定义 `telegram_browser_profile` 时仍按自定义路径运行。旧版登录态留在原包的 `sample_data/telegram_browser_profile`，不会被自动复制或删除。

v13 测试版针对“点击附件后长时间没有日志”缩短点击和下载事件等待时间，优先明确文档卡片/下载链接，不再点击泛化的媒体父节点。日志区分“开始点击”“下载已开始（正在传输）”“下载已完成/保存失败”。大文件在下载已开始后仍需等待传输完成，不能据浏览器出现文件卡片判断已保存到本地。

日志窗口现在固定高度并带滚动条，最多保留最近 500 条记录，刷新时自动滚动到最新日志。

### 4.2 最新 N 条与文件校验

v15 测试版按频道消息 ID 去重后取已渲染的最新 N 条**消息**，并非 N 个文件。日志列出 DOM 节点数、唯一消息数、每条消息的内容摘要与有效附件数；`sample_data/output/manifest.json` 的 `source_text` 保存整条已处理消息的可见文本，文件路径可在 `files` 和 `images` 查看。Web 端消息可能虚拟滚动，因此消息列表未加载足够历史时，不能保证覆盖频道实际最新 N 条；出现不足会记录，不会伪称完整。下载的 ZIP 将在保存前验证完整性，并在日志显示 ZIP 内文件数量与前 10 项文件名；失败不生成可用候选且不会覆盖同名已有文件。RAR/7z 目前只能验证非空，仍需外部校验工具确认压缩包完整性；不要运行来源不明的可执行内容。

## 5. 当前安全边界

- 默认不制作 EXE 自解压包
- 默认拒绝可执行文件
- 默认保留人工审核
- 不自动发布闲鱼商品
- 不在配置中保存账号密码
