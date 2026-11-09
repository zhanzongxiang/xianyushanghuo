# Telegram 下载引擎对照测试

本轮先独立测试 tdl，再决定是否增加正式下载引擎。现有工作台仍使用配置中的
Telethon 或 TDLib；测速工具不改配置、不建立素材记录，也不调用夸克上传。

## 已核实的边界

- 已按官方 Windows 发布包安装并校验 tdl v0.20.4，程序保留在 `.cache/tools/tdl/v0.20.4/`。
- 本轮核对了该版本的 `cmd/dl.go`、`app/dl/dl.go`、`app/dl/progress.go` 和 `app/dl/serve.go`。
- `--continue` 记录批次中已完成的项目，不代表恢复单个未完成文件的字节进度。
  原生下载错误处理还可能删除未完成的临时文件。
- 暂不把 tdl 输出文件的长度当作实时进度：并行随机写入可能提前扩展文件长度。
  本次 tdl 只报告成功文件的整体有效速度，首字节延迟留空。
- tdl 的 HTTP 服务模式可作为后续 aria2 试验的桥接入口；这一版没有启用它。
  上游服务绑定全部网卡且没有访问认证，需要先处理访问范围，再验证 Range 和跨重启恢复。
- tdl 使用独立授权。现有 Telethon、TDLib 登录状态不会自动变成 tdl 登录状态。
  登录时必须使用同一账号；当前测速工具不能自动核实 tdl 与 Telethon 的账号一致性，报告明确标注此限制。

tdl 的上游地址为 `https://github.com/iyear/tdl`，Windows 发布地址为
`https://github.com/iyear/tdl/releases/tag/v0.20.4`。许可证随官方安装包保留。
安装器没有修改上游程序，也没有把它纳入本项目的便携包。

## 运行步骤

所有命令在项目根目录的 PowerShell 中执行。先启动系统设置里的代理，暂停其他下载和图片扫描，
避免代理或带宽争用干扰比较。以当前正式配置为例：

```powershell
# 新机器安装；已安装的目录不会被覆盖。
powershell -NoProfile -ExecutionPolicy Bypass -File tools/install_tdl.ps1

# 预检，不连接 Telegram、不启动登录。
.\.venv\Scripts\python.exe tools/benchmark_downloads.py --config config/test.local.json --check

# tdl 首次扫码授权，在终端进行；两步验证密码只在终端交互输入。
.\.venv\Scripts\python.exe tools/benchmark_downloads.py --config config/test.local.json --login-tdl

# 将链接换成同一个 ZIP/RAR 文件所在的消息链接。
.\.venv\Scripts\python.exe tools/benchmark_downloads.py --config config/test.local.json --url "https://t.me/频道名/消息ID" --engines telethon tdl --runs 3

# 加入现有 TDLib。先退出占用同一 TDLib 登录数据库的应用实例。
.\.venv\Scripts\python.exe tools/benchmark_downloads.py --config config/test.local.json --url "https://t.me/频道名/消息ID" --engines telethon tdlib tdl --runs 3
```

若实际使用预览版配置，将 `--config` 换成该预览实例的配置文件，例如
`.cache/image-loading-preview/config.json`。工具直接读取指定文件，不向它写入任何配置。
如果提示代理连接被拒绝，请先确认代理软件已启动且实际监听的端口与配置相符。
预检只确认代理 TCP 可连接，不代表代理已能访问 Telegram。

## 结果与判定

每次运行创建独立的 `outputs/download-benchmarks/时间-随机标识/`，其中包含：

- `report.json`：预检结果、文件 ID/大小/DC、逐轮引擎耗时、速度、恢复字节数和文件 SHA256。
- `logs/`：当前项目下载引擎的诊断日志。
- `轮次-引擎/`：该轮试验的下载文件；tdl 另有 `tdl-console.log`。

速度单位为 **MiB/s**（字节/秒除以 1,048,576）。`download_seconds` 包括下载引擎初始化、
跨 DC 准备和文件传输，排除压缩包校验；`total_seconds` 包括校验和 SHA256 计算。
共同的 Telethon 消息查询不计入引擎下载时间；tdl 自身重新解析消息的时间计入 tdl 下载时间。
因此这反映候选引擎接入现有 API 列表之后的耗时，不代表每个引擎的纯网络峰值。
已恢复或命中缓存的下载不会生成可用于全新下载对比的速度值。

每轮使用一个文件，沿用相同代理和单文件并发设置，按轮次轮换测试顺序。
所有结果先检查消息文件大小和 ZIP CRC/RAR 校验，再对比 SHA256。
失败、取消、空文件和内容不一致均不会计作成功测速；失败文件只留在试验目录。
Ctrl+C 或下载总超时会终止本次启动的 tdl 进程及其 Windows 子进程。

真正采用新引擎前还需要：用相同账号完成真实文件测试，观察多个时段和不同大小文件，
并单独验证中断恢复。单次测速或文件已缓存不能证明持续提速。
