# 旧版 HTTP 数据服务（兼容方案）

此文档仅供已有 HTTP 数据服务的用户维护旧配置。当前采用
[本地程序直连数据库](README.md)，无需在服务器部署本项目或数据服务。

服务端仅运行 `xianyu_pipeline.data_server`，无需 Telegram、夸克、浏览器或 Python 第三方包。
需要 Python 3.10 或以上。本地工作台继续运行下载、校验、上传。

## 在 47.113.126.29 部署

使用有权限的 SSH 账号登录服务器，先将源码放到 `/opt/xianyu-data`。
创建独立系统用户和持久化目录（已有目录先核对其用途）：

```sh
sudo useradd --system --home /var/lib/xianyu-data --shell /usr/sbin/nologin xianyu-data
sudo install -d -o xianyu-data -g xianyu-data -m 700 /var/lib/xianyu-data
sudo install -m 600 /dev/null /etc/xianyu-data.env
```

生成令牌，在服务器终端查看并保存到 `/etc/xianyu-data.env`：

```sh
python3 -c "import secrets; print('DATA_SERVER_TOKEN=' + secrets.token_urlsafe(48))"
sudoedit /etc/xianyu-data.env
```

该文件应只有一行 `DATA_SERVER_TOKEN=生成的令牌`。不要放进仓库。
安装服务（先核对本机 Python 路径，必要时修改 service 的 ExecStart）：

```sh
cd /opt/xianyu-data
sudo install -m 644 deploy/data-server.service /etc/systemd/system/xianyu-data.service
sudo systemctl daemon-reload
sudo systemctl enable --now xianyu-data
sudo systemctl status xianyu-data
```

## 本地连接

没有配置域名和 HTTPS 时，在本地打开 SSH 隧道，保持终端运行：

```powershell
ssh -N -L 127.0.0.1:8780:127.0.0.1:8780 你的SSH账号@47.113.126.29
```

本地工作台“设置 → 服务器数据”填写：

| 设置 | 内容 |
|---|---|
| 启用服务器同步 | 开启 |
| 数据服务地址 | `http://127.0.0.1:8780` |
| 访问令牌 | 服务器生成的同一令牌 |
| 工作空间 | `default`，同一份数据的电脑使用同一值 |
| 同步间隔 | 默认 10 秒，范围 5–300 秒 |

保存后自动导入现有本地频道、已缓存消息及任务记录。可以点击“立即同步”。
状态显示连接情况、待同步条数、冲突条数和最近成功时间。令牌保存后显示掩码，不从读取配置接口返回。
如果已有 HTTPS 反向代理，可用独立域名将根路径转发至 `127.0.0.1:8780`；
保留 Authorization 请求头，将上传体限制设为至少 12 MiB，并关闭代理缓存。
不要将数据库端口或此服务的裸 HTTP 端口直接开放公网。

## 数据范围与恢复

- `business.sqlite3` 保存频道、消息、游标、各设备任务状态、中文名称和分享链接。
- `assets/` 保存按 SHA256 去重的头像和预览图片；ZIP/RAR 始终留在执行电脑。
- 每台电脑在本地 `.server-sync/outbox.sqlite3` 保存设备 ID、同步游标、待同步记录和冲突。
  不要将该目录复制给另一台电脑作为运行目录，否则两台电脑会共用设备身份。
- Telegram 会话、API 凭证、代理设置、夸克 Cookie、磁盘路径、系统设置不发送到服务器。
- 活动频道仍是每台电脑自己的选择；其他电脑任务只读，不参与本机的上传、重试或中断恢复。
- 本地已有 JSON 是离线缓存，不会被删除。网络中断时继续本地工作，恢复后自动补同步。
- 同一记录在两端同时改动时保留双方版本，设置页明确选择冲突采用服务器或本机版本。
  展开冲突记录可对比字段差异，最多展示 20 条。
  这两个按钮会处理当前工作空间中本机全部冲突记录；处理前先核对频道和任务。
- 同步保留服务端消息记录；每台电脑仍只展示每频道最近 300 条缓存消息。
  第一版没有云端任务调度、断点续传或压缩包云备份。
- 改为新工作空间会将本机缓存导入该空间；切换前确认本机数据属于目标空间。
- 自定义术语表和命名服务配置继续保留本机；任务生成的中文名称随任务同步。

备份时先停止 `xianyu-data` 服务，再完整备份 `/var/lib/xianyu-data`（含数据库和 assets），
恢复到相同位置后再启动。保留数据库的 server_id；
检测到同一地址数据库被重新创建时客户端会停止同步，防止使用失效的版本号和游标。
需要新数据库时使用新的工作空间，并保留旧本地数据副本。

## 离线验收

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_server_sync.py -q
```

测试只运行临时本机服务和虚拟记录，不连接 Telegram、夸克或上述服务器。
实际部署仍需要服务器 SSH 账号、授权登录方式以及可写部署目录。
