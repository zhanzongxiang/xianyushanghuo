# 跨电脑测试说明

## 服务器数据同步

服务端部署和 SSH 隧道配置见 `deploy/README.md`。启用前保留本机数据副本；
新工作空间首次连接会导入本机频道、消息及任务。两台电脑使用同一工作空间，
检查频道和名称同步、图片展示、其他设备任务只读、本机下载上传仍可用。
断开隧道后修改频道名，检查离线状态和待同步数；恢复隧道后确认自动补传。
在两台电脑同时修改同一频道名称，确认冲突数和明确选择两端版本的行为。
令牌在读取配置接口中不可见，便携包模板中为空且同步关闭。

## 统一来源频道列表

在“来源频道”点击“获取账号频道”，扫描完成后频道自动加入已保存列表，显示本轮获取、新增、更新数量。
重复扫描不重复添加，不覆盖自定义名称；空结果、失败和停止保留原列表及当前频道。
频道消息选择器、设置中的来源频道和下载/上传任务的频道筛选均使用已保存列表。
可先编辑频道名称，确认各处同步更新；切换频道后重启应用，确认无需重新扫描即可读取该频道缓存。
列表保存在输出目录 `channels.json`，不包含账号密钥；私有频道访问仍依赖当前已授权会话。

## v55：解密加速与双文件下载

完整解压 `XianYuMaterialPipeline_v55_download_acceleration.zip`，使用包内 `start_ui.cmd`。
启动新版前让旧版任务结束并退出，避免不同版本同时操作相同的 Telegram session 和任务目录。

本版包含 `cryptg` 原生加解密组件，设置页“下载解密组件”应显示 `cryptg · 已启用`。
也可运行包内 EXE 的 `--download-check`，预期返回 `crypto_backend=cryptg` 和 `self_test=passed`；
此检查不读取账号配置、不连接 Telegram。源码环境先安装 `requirements.txt`。

设置默认同时下载 **2 个文件**、每个大文件 **4 路分块**；文件分块、后台配图和主动高清图共用最多
**8 个媒体请求**，不会因两个文件都设为 8 就无限增加连接。下载期间新后台图片降至 1 路，
已开始的图片继续完成，主动查看大图仍可加载。设同时文件数为 1 可回退串行批次，
再设分块为 1 可回退单请求下载；配置仅对下一轮任务生效。

频道列表下载中单个文件失败不影响其他已提交文件；“扫描并下载”及命令行批次在失败时中止该批次，
保留已经完成的文件，取消其他写入后隔离非空片段。停止、ZIP CRC、RAR 校验和同名文件保护仍然生效。
日志新增首字节等待、传输耗时及平均 MiB/s，用同一文件和代理线路对比实际效果；
原生组件自检和离线测试不等同于真实网络提速。本版尚未实现断点续传。

## v53：Telegram 单文件分块并发试用版

完整解压 `XianYuMaterialPipeline_v53_parallel_download.zip`，使用包内 `start_ui.cmd`，不要单独移动 EXE 或删除 `_internal`。保留旧包；切换版本前让旧版任务结束并正常退出，不要让两个版本同时使用同一个 Telegram session 或任务目录。

设置页“Telegram 单文件下载工人”对应 `download_workers`，默认 **4**，范围 **1–8**。已知大小且至少 **8 MiB** 的 ZIP/RAR 使用同一 Telethon client/session 的 asyncio 并发，按不重叠区间下载，单次请求 512 KiB；不是多个账号、多个 session 或下载线程。小文件、未知大小和不支持公开 `iter_download` 的客户端继续走原 `download_media`；设为 **1** 可回退原顺序下载，只对下一次新传输生效。

进度是各块成功写入字节的总和，速度和剩余时间由实际进度计算。停止、无进展超时、总传输超时或任一工人异常会取消并等待全部工人和迭代器收尾，关闭暂存文件后才隔离；不会自动顺序重传。失败文件可能含未下载区间，保留在 `.unverified` 待检查，不能视为完整成品或断点续传文件。

每块长度、尾块、总接收长度和最终文件大小必须一致，之后仍执行 ZIP CRC 或 RAR 完整性校验，全部通过才发布成功；已有同名文件不覆盖。RAR 仍需要可用的 UnRAR/7-Zip 校验工具。

这是试用版，离线回归和 EXE 验收不等于真实 Telegram 测速。本次不安装包，不新增 `cryptg`，不改代理或 DC 设置，也不对真实频道批量下载；**尚未证实实际提速**。同一网络/代理条件下可人工对比工人 1 与 4；并发可能仍受网络、服务端限速或加密开销影响。公开 `iter_download` 的文件引用过期或 CDN 重定向限制会报告失败并保留待检查文件，不调用私有下载 API 或盲目自动重试，可在旧任务结束后设为 1 手动重试。

## v49：图片加载加速试用版

点击封面优先获取约 960 像素以上的最小可用大图，显示“清晰大图（非最大尺寸）”；按“加载最大尺寸图”再请求最大图。关联图片优先使用已缓存的图片消息 ID，失效时回退历史关联。列表空闲时仅对前三张有封面的结果逐张预加载，文件任务运行时不启动预加载。已提交的后台图片下载不会强行取消，前台请求优先进入尚未取出的队列。真实网络速度需要在用户频道实测。


## v46：最新源码整合版

完整解压 `XianYuMaterialPipeline_v46_latest.zip`，双击包内 `start_ui.cmd`，不要单独移动 EXE 或删除 `_internal`。
本版重新构建当前源码，包含账号已加入频道扫描、可搜索的频道选择器、公开与私有广播频道选择、渐进加载和图片/文件并行队列。
在当前源码中，设置页完成 Telegram API 授权后，到“来源频道”点击“获取账号频道”，自动保存后切换频道，再到“频道消息”获取消息、勾选 ZIP/RAR 下载。
账号频道含归档频道，不含群组、已退出、停用或禁止访问的频道；私有频道使用账号会话缓存的频道 ID，不能绕过权限。
v46 原版的账号频道选择仅保存在服务内存；当前源码已改为持久保存列表与当前来源，重启后可继续使用。频道消息缓存和下载记录独立持久化。
RAR 需要本机 7-Zip/UnRAR 完整性校验；失败的非空文件保留到下载目录 `.unverified`，不视为可上传成品。
便携包不含个人凭证、会话、素材及历史任务；另一台电脑需要自行登录并配置代理、校验工具及夸克上传环境。
本地回归和 EXE 冒烟验证不代表真实 Telegram 大文件及真实夸克上传已经通过端到端验收。

## v44：预览文件路径兼容修复

使用 `outputs/XianYuMaterialPipeline_v44_preview_path_fix/start_ui.cmd`。
频道封面和点击后的高清图不再强制要求 Telethon 返回值必须等于请求路径；
兼容 Telethon 返回空值但文件已写入、或实际文件扩展名被调整的情况。
只有确实没有生成图片文件时才判定失败，并在 `pipeline.log` 记录请求路径、返回值和同前缀文件。
旧 v43 的频道消息缓存、预览图和下载任务可以继续使用，切换到 v44 后点击失败图片的重试按钮即可重新获取。

## v43：多频道保存与切换

使用 `outputs/XianYuMaterialPipeline_v43_multi_channel/start_ui.cmd`。频道消息页顶部的频道选择框
用于切换当前频道；切换只读取该频道已经缓存的消息，不会自动连接 Telegram。点击“读取频道”后，
才会读取当前频道的新消息。

“来源频道”页可添加、重命名、切换和移除公开频道。每个频道独立保存消息列表、读取更早消息的游标、
中等预览图和高清图；旧版本的 `output/channel-catalog.json` 会在首次访问对应频道时自动迁移。
移除频道只移除保存的频道入口，不删除该频道的缓存、预览图、下载文件或历史任务。当前频道需要先切换
到其他频道后才能移除。

读取、下载或高清图任务运行期间禁止切换频道，避免正在使用的 Telegram API 请求与新频道混用。
历史下载任务仍按原频道和消息 ID保存，切换频道不会影响重试或上传。

## v42：清晰预览与图片/文件并行

使用 `outputs/XianYuMaterialPipeline_v42_parallel_media/start_ui.cmd`。
列表优先选择约 320 像素等级的照片，排除视频缩略图，不再退回 `thumb=0` 的极小图。
点击放大后按需获取更大尺寸，界面显示“正在加载高清图”、成功或失败及重试入口。
没有前置照片时仍可以显示附件自带预览，明确标注为缩略图而不冒充高清照片。
旧小图缓存不作为新预览复用；重新读取频道可更新，图片地址随实际缓存文件变化。

扫描历史与预览请求共用一个 Telegram 客户端及事件循环，不等消息扫描全部结束才获取图片。
列表消息出现后即可勾选并下载；文件字节传输、列表图片和按需高清图同时处理，
不再需要等所有封面结束才启动文件。文件仍需人工提交，不会自动下载整轮 100 个压缩包。
文件 1 路、列表预览最多 4 路、按需高清图 1 路，配图最多 2 组并行；
每次下载选择 1-20 条，队列最多保留 40 个未完成文件/高清请求。
重复请求、停止后的新请求及队列关闭时的新请求会被拒绝，避免遗漏或重复下载。

列表图片最多等待 30 秒，按需高清图最多 90 秒；具体失败原因记录在日志中。
文件继续检查真实大小与 ZIP CRC/RAR 完整性，校验不会阻塞消息扫描。
单张图片失败不判定文件失败，停止会取消本轮媒体任务并保留可恢复的数据。
原“扫描并下载”与命令行下载仍保持既有批次行为；以上并行队列用于频道消息页。
便携 ZIP 不包含个人配置、会话、历史任务或素材；本机已保存的 API 和代理可自动恢复。

## v41：频道消息渐进加载

使用 `outputs/XianYuMaterialPipeline_v41_channel_loading/start_ui.cmd`。
频道消息在扫描过程中分批出现，不再等全部封面下载完才显示。封面最多 4 路并行获取，
优先使用扫描时已读到的前置图片消息和已缓存的高清封面，仍支持原图放大与缩放。
单张封面失败不影响消息列表，停止加载保留已经读取的消息与成功缓存的图片。

点击“读取频道”立即显示连接状态，随后显示已检查的原始消息数、找到的压缩包数、
封面处理数量和耗时；完成、停止和失败会保留明确的状态提示。
加载期间可以浏览、搜索和勾选，加载结束后提交下载。
刷新进度不重建已有消息行或图片，不清空勾选，也不重置滚动位置。
每轮数量仍为设置页的 1-100 条；首次获取大量高清图仍受代理和 Telegram 速度影响。
测试使用离线模拟消息验证并发、分批返回、停止与界面状态，不等同于真实网络测速。

## v39：保存并恢复 Telegram 设置

使用 `outputs/XianYuMaterialPipeline_v39_saved_settings/start_ui.cmd`。设置保存后，
API ID 显示原值，API Hash 显示密码掩码，代理地址不再因换版本丢失。
重复保存其他设置或将 Hash 输入框留空不会清除密钥；填写新 Hash 才替换。
默认 `test.local.json` 保存的 API ID、Hash、代理和 DC 2 选择同步到当前 Windows 用户的
`%LOCALAPPDATA%\XianYuMaterialPipeline\telegram-settings.json`。
首次启动干净新版时恢复这些设置；如果没有本机备份，从同一项目的源码或旧便携版配置迁移。
已有账号或部分填写的账号配置不被覆盖，自定义配置文件名不参与自动恢复。
下载目录、其他设置与历史任务不自动复制。分发 ZIP 不包含凭证和本机备份文件。

恢复代理地址不等于代理已启动。如果 `socks5://127.0.0.1:7890` 连接被拒绝，
需要先在桌面代理中启用核心并确认该端口监听，再尝试 Telegram API。

## v40：频道消息批量读取

设置页新增“频道消息每轮读取数量”，范围为 1-100，默认 100。
它只控制频道消息列表每次读取和读取更早消息的压缩包消息数量；
“扫描下载数量”仍单独控制直接扫描并下载的数量。
当设置为 100 时，频道列表最多检查 1000 条原始 Telegram 消息，
v40 逐条获取预览封面，可能比读取 12 条耗时更长；v41 已改为分批显示和并行获取封面。

## v38：夸克 CLI / WSL 上传

使用 `outputs/XianYuMaterialPipeline_v38_quark_cli_wsl/start_ui.cmd`。设置页选择
“夸克 CLI / WSL”，填写 WSL 发行版、Node 命令和官方 `quark-drive.cjs` 路径，
先点击“夸克 CLI 登录”完成授权，再点击“检查夸克 CLI”。上传任务会通过 CLI
上传本地 ZIP/RAR，显示进度、速度和剩余时间，上传成功后自动创建分享链接。
分享链接获取失败时保留“结果待确认”状态，重试只查询已有文件，不重复上传。
当前电脑若没有 WSL Linux 发行版，需要先安装并初始化 Ubuntu/Debian；本机检测到
`wsl.exe` 不代表已经具备可运行的发行版。

## v37：频道主帖筛选

使用 `XianYuMaterialPipeline_v37_channel_posts/start_ui.cmd`。频道消息列表和普通下载
只读取 Telegram API 返回的频道主帖，`post=False` 的评论不会进入列表或下载。
转发帖默认保留；在设置中打开“排除转发帖”后，列表、预览和下载入口都会同步排除
带有 `fwd_from` 标记的消息。旧版缓存没有转发字段时按非转发处理，不会被升级后隐藏。
扫描数量是满足筛选条件的压缩包消息数量，不是原始历史消息条数。

## v36：API-only 频道预览

源码本机配置在 `config/test.local.json`；便携版运行
`XianYuMaterialPipeline_v36_api_previews/start_ui.cmd`，在设置中填写自己的 API ID/Hash 和必要的代理。
频道消息封面可点击放大和缩放；新扫描优先保存高清照片，失败时回退小图。重新读取频道可升级旧缓存。
Telegram Web 获取入口已移除；旧配置可在设置页保存后迁移至 API。夸克网页上传仍需配置浏览器路径。
便携 ZIP 不包含本机凭证、会话及历史任务；正式下载前请完成 Telegram API 登录。

## v35：频道消息列表

使用 `XianYuMaterialPipeline_v35_channel_messages/start_ui.cmd`。在“设置”中选择 Telegram API，
配置频道、API ID/Hash 和必要的代理，并完成账号登录。进入“频道消息”点击“读取频道”，
列表只获取 ZIP/RAR 消息元数据和一张封面，不会直接下载压缩包或上传夸克；
可以搜索消息、读取更早消息、勾选后下载。下载完成后到“下载任务”查看校验结果。
本机代理未监听时需先启动桌面代理。便携 ZIP 不包含本机凭证、会话及历史任务。

## v34：可配置下载目录

使用 `XianYuMaterialPipeline_v34_download_directory/start_ui.cmd`。在“设置”中的
“浏览器与下载”填写“下载保存目录”（完整绝对路径）并保存。保存前检查目标目录可创建且可写；
不能选磁盘根目录，任务进行时不能更改设置。新下载的素材、暂存与预览使用新目录，
“下载目录”按钮随之打开新位置。

已下载的素材与预览不会自动移动，历史任务仍使用原路径并可打开、预览或上传；
目录切换后重启也保持原位置。若新目录已有素材，工作台会导入；与旧本地任务同名时
保留旧任务并写警告日志，避免静默覆盖。便携分发 ZIP 仍不包含本机配置或下载文件。

## v33：前置图集预览

使用 `XianYuMaterialPipeline_v33_preview_album/start_ui.cmd`。API 下载优先读取压缩包之前
最近的一条实际消息：照片属于图集时，只取紧邻且同组的最多 5 张；单张照片只取这一张。
消息 ID 可不连续，不跨过文字、其他文件、视频或另一图集。图片按消息顺序展示，
详情中显示“前置图集（自动关联）”及图片消息 ID，便于人工核对归属。

历史查询最多等待 5 秒，每张图片最多 8 秒，整个前置配图阶段最多 25 秒。
部分图片失败时保留成功的图片；全部失败时回退到附件缩略图及 ZIP 内图片，
不会因为预览失败将压缩包判定为下载失败。配图等待不占用压缩包的传输超时。
RAR 的前置图集不依赖解压。图片仅用于工作台展示，不会自动上传为商品主图。

本功能仅用于 API 工作台的新扫描或指定消息重试；Web 模式保持原行为。
没有 Telegram 消息 ID 的历史本地文件不会按文件名猜测补图。旧版运行任务需先停止，
不要让多个版本同时下载或修改同一份任务记录。保留个人配置和会话；分发 ZIP 仍为干净配置。

## v32：素材工作台

使用 `XianYuMaterialPipeline_v32_workbench/start_ui.cmd`。主界面分为下载任务、上传任务、
素材库、任务记录和设置，保留原来的 Telegram API/Web、浏览器路径、代理、超时和登录配置。
“扫描并下载”只下载与校验素材；在上传任务中勾选文件后手动上传原始 ZIP/RAR，不再因缺少展示图阻止上传。
命令行原流水线和 `/api/run` 保持兼容。

下载列表显示真实字节、速度、剩余时间与错误。没有总大小时显示不定进度；夸克网页未提供可靠
字节进度时只显示阶段。展示图取自对应 Telegram 附件缩略图或 ZIP 内有大小限制的图片，
不猜测相邻消息图片的归属。RAR 无缩略图时显示文件图标，不解压不可信预览内容。

已下载素材进入上传列表。选择“本地模拟”仅复制到本地，不代表已上传夸克。
可能已发出上传但未获取分享链接的任务标记为结果待确认，重试只查询原目标中的文件分享链接，
不会重复上传。若网盘中实际没有文件，请人工核对目标和上传结果后处理。
夸克选择器仍需按实际账号网页配置，程序不会猜测。

任务状态保存在输出目录 `workbench-tasks.json`，预览保存在下载目录 `.previews`；刷新或重启后保留，
中断任务显示需处理。旧下载目录中的 ZIP/RAR 可导入，上传前重新校验。
便携 ZIP 只包含干净配置，个人凭证、会话、日志和素材不会被打包。升级时先停止旧版下载任务。

## v31：修复 API 下载进度监控

v30 在首个数据块等待期间会因 `last_report` 变量作用域错误抛出
`UnboundLocalError`，与代理或 RAR 文件无关。v31 修复该错误并增加正常等待首块的
回归测试。使用 `XianYuMaterialPipeline_v31_api_progress_fix/start_ui.cmd`；
不要继续使用 v30 进行 API 下载。真实跨 DC 传输仍需在代理网络中验证。

## v30：API 跨 DC 下载监控

选择 DC 2 生产节点时，跨 DC 下载连接也使用 `149.154.167.50:443`；已有
账号会话所在 DC 不会被改写。Telegram API 下载每隔一段时间记录已接收字节。
`download_start_timeout_seconds` 在 API 模式下是连续无进展上限，默认 180 秒；
`download_transfer_timeout_seconds` 是单个文件总时限，默认 3600 秒。页面上可调整。
超时或点击“停止监测”会取消当前 API 下载；非空文件保留在 `.unverified`，
零字节暂存文件直接清理。网络代理中断、节点路由错误仍需在 FlClash 中解决。
旧 v29 正在进行的 API 下载不受新版本的停止按钮控制；先结束旧任务，再启动
`XianYuMaterialPipeline_v30_api_download_watchdog/start_ui.cmd`。

## v29：API 生产节点与代理

Telegram API 模式可选填写 `socks5://主机:端口` 或 `http://主机:端口` 作为
`telegram_api_proxy_url`，不支持在 URL 中填写代理账号密码。DC 2 节点可选择图中
的生产地址 `149.154.167.50:443`，v29 只在当前会话没有授权密钥、且属于 DC 2
时应用；已有会话的节点与密钥不覆盖，登录后跨数据中心迁移仍由 Telethon 处理。
测试地址 `149.154.167.40:443` 不用于正式账号。当前电脑直连生产地址、测试地址及
Telethon 默认地址均超时，因此仅切换生产 IP 并不能解决网络问题，需配置实际可用
的代理或其他能够让 API 进程连通的网络。不要把截图里的公钥粘贴进程序。
使用 `XianYuMaterialPipeline_v29_api_network/start_ui.cmd`，便携包不含个人配置和会话。

## v28：Telegram API 获取压缩包

当前界面只支持 Telegram API，需在本机配置 API ID/Hash，点击
“首次登录 Telegram”后在新终端完成手机号、验证码及可能的二次验证；旧 Web 登录态不能
代替 API 会话。API 模式按频道最新消息扫描上限查找 ZIP/RAR 文件名，先下载到暂存目录，
校验大小和 ZIP CRC 或 RAR 测试命令后才放进下载目录。失败文件留在 `.unverified`。
目前 API 模式支持抓取一次，不支持持续监测。缺少 RAR 校验工具时需安装 7-Zip/WinRAR。
使用 `XianYuMaterialPipeline_v28_telegram_api/start_ui.cmd`。便携 ZIP 不包含凭证、会话和下载内容。

## v27：延长 Telegram 消息等待

Telegram Web 页面导航及首条频道消息出现的单步等待使用配置项
`telegram_browser_timeout_seconds`，默认 180 秒，可在界面设为 30–600 秒。
之前首条消息等待固定最多 30 秒，页面导航也可能被默认导航超时提前中断。
网络或代理不可用、登录失效以及频道页面未加载时，延长等待不会自动解决连接问题。
使用 `XianYuMaterialPipeline_v27_message_wait/start_ui.cmd` 启动，旧版目录和登录态保留。

## v26：按文件卡片扫描 ZIP/RAR

默认频道为 `https://t.me/free_3d_models_download`。扫描消息时只把文件卡片中的
ZIP/RAR 文件名或带文件名的下载链接计为候选；正文单独提到压缩包不再计数。
下载前重新确认消息 ID、文件名和唯一的卡片控件；遇到不明确的卡片会跳过并记日志，
不会猜测并点击通用下载按钮。虚拟列表回滚定位最多按扫描上限继续查找。
使用 `XianYuMaterialPipeline_v26_archive_card_scan/start_ui.cmd`，浏览器专用登录目录
仍可复用；便携 ZIP 不包含本机浏览器路径、用户配置和登录会话。真实频道的扫描和下载
仍需在本人已登录的浏览器及网络环境下验证。

## v25：频道消息列表动态更新

默认频道为 `https://t.me/free_3d_models_download`，可在界面改为其他公开频道。
修复 Telegram Web 滚动刷新节点时，旧扫描方式在 `nth(index).get_attribute()`
超时导致流水线失败的问题。新版一次读取当前 DOM 消息快照，按稳定消息 ID 重定位下载控件。
使用 `XianYuMaterialPipeline_v25_message_snapshot/start_ui.cmd` 启动，脚本自动选择空闲本地端口；升级沿用兼容的
浏览器专用登录目录。构建后的 ZIP 默认不附带浏览器路径或用户会话数据。

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

### 3.1 Telegram Web 模式（历史版本）

v36 及之后的界面已经移除 Telegram Web 获取入口，当前版本只使用 Telegram API。
下面的浏览器模式仅用于阅读旧版本测试记录；不要在 v37 配置中设置
`telegram_access_mode: "browser"`，也不要再使用 `--telegram-login`。

### 3.2 Telegram API 模式

可直接填写公开频道链接，在最多 `telegram_scan_limit` 条最新消息中查找
`recent_message_limit` 条 ZIP/RAR 文件消息。程序只接受 Telegram 标记为频道主帖的消息，
`post=False` 的评论不会进入扫描结果、频道列表或下载；默认保留转发帖，可通过
`exclude_forwarded_posts: true` 排除带 `fwd_from` 标记的转发帖：

```json
{
  "source_channel_url": "https://t.me/example_channel",
  "fetch_mode": "recent",
  "recent_message_limit": 20,
  "exclude_forwarded_posts": false
}
```

程序使用单独的本机 Telegram API 会话读取频道消息，仅下载 ZIP/RAR 文件。公开链接不会绕过登录权限。私有频道需要账号本身有访问权限，私有链接不能仅凭 URL 推断聊天 ID。

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

## 4. 夸克网盘上传测试

支持 `upload_provider: "quark_api"`：通过 QuarkPan 所用的网页接口批量上传，
在设置页填写夸克 Cookie、目标目录 FID，点击“检查夸克 API”验证登录。
默认同时上传 2 个文件，可设置 1 到 3；列表支持多选上传、进度、分片重试、自动分享。
分享失败进入“结果待确认”，后续补取链接，不重复传输。
接口为网页内部接口，真实账号兼容性需验证，详见 README 的夸克 API 批量上传说明。

同时支持 `upload_provider: "quark_browser"`：通过夸克网页完成上传。

### 4.1 夸克 CLI / WSL 上传

当前支持 `upload_provider: "quark_cli"`。Windows 下建议使用 WSL 执行官方夸克 CLI，设置页可配置 WSL 发行版、Node 命令、`quark-drive.cjs` 路径和目标目录 FID。本项目不自动安装 WSL、Node 或夸克 CLI，也不把 CLI 登录态打包进便携版。

首次使用流程：

1. 在 WSL 中安装并授权夸克 CLI。
2. 在设置页选择“夸克 CLI / WSL”，填写 `quark-drive.cjs` 的 WSL 路径。
3. 如需指定网盘文件夹，填写该文件夹的目录 FID；留空则使用 CLI 默认目录。
4. 点击“检查夸克 CLI”确认授权，再从上传任务列表提交素材。

CLI 上传会读取进度并显示已上传字节、速度和剩余时间。上传完成后程序再创建公开分享链接；如果上传已经完成但分享步骤失败，任务会保留“结果待确认 / 链接待获取”，重试时先浏览目标目录并按完整文件名确认，不会自动重复提交上传。

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

### 4.2 便携包浏览器运行时

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
