# 本地运行、服务器仅提供数据库

程序、界面、Telegram 登录、下载校验和夸克上传都在本机运行。
`47.113.126.29` 仅需提供 PostgreSQL 或 MySQL 数据库，不部署项目源码、
Python 服务、工作台页面、Telegram 或浏览器。

本机通过数据库驱动读写，现有 JSON 文件和本地 SQLite 同步队列用于离线缓存。
启用后自动导入本机已保存频道、已缓存消息及任务记录；断网时继续工作，恢复后补传。
默认不开启同步，不会自动连接或修改服务器数据库。

## 准备数据库

先确认服务器已有数据库的类型、端口、库名、应用账号及连接方式。
设置里的 `47.113.126.29:5432`、`xianyu`、`xianyu_app` 是预填值，
不表示服务器已安装 PostgreSQL 或已创建对应账号。

可使用已有独立数据库。应用账号应仅有该数据库的建表、查询、插入、更新权限，
不要使用数据库超级用户。第一次启用同步会在目标库创建 `xy_` 前缀的表。
同一工作空间共享频道和消息；工作空间不是数据库账号之间的权限隔离机制。
不互相信任的用户应使用独立数据库和账号。

PostgreSQL 管理员可按需创建：

```sql
CREATE ROLE xianyu_app LOGIN;
-- 在 psql 使用 \password xianyu_app 交互设置密码。
CREATE DATABASE xianyu OWNER xianyu_app;
```

MySQL 管理员可按需创建，替换来源地址和密码：

```sql
CREATE DATABASE xianyu CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
CREATE USER 'xianyu_app'@'本机出口IP' IDENTIFIED BY '替换为独立强密码';
GRANT SELECT, INSERT, UPDATE, CREATE, INDEX, REFERENCES
  ON xianyu.* TO 'xianyu_app'@'本机出口IP';
```

MySQL 的 `max_allowed_packet` 应至少为 32 MiB，以支持最大 12 MiB 的预览图片。
数据库须允许该账号从实际连接来源登录。使用 SSH 隧道时，连接来源是服务器本机。

## 本机设置

安装本机依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

进入工作台“设置 → 在线数据库”：

| 设置 | 内容 |
|---|---|
| 存储方式 | PostgreSQL 或 MySQL，与服务器一致 |
| 数据库地址 | `47.113.126.29`，或 SSH 隧道的 `127.0.0.1` |
| 数据库端口 | 实际端口；常用 PostgreSQL `5432`、MySQL `3306` |
| 数据库名、用户、密码 | 服务器实际创建的应用账号 |
| 连接加密 | 直接远程连接选择 TLS；验证证书时填写本机 CA 路径 |
| 工作空间 | 默认 `default`，共享同一份数据的电脑填写相同值 |
| 同步间隔 | 默认 10 秒，范围 5 至 300 秒 |

先保持同步关闭，填写并保存配置，点击“测试连接”。该按钮只连接并执行 `SELECT 1`，
不会创建业务表或导入数据；它不验证所有建表及读写权限。
测试成功后启用同步并保存，状态会显示连接情况、待同步数量、冲突和最近成功时间。
密码保存后显示掩码，读取配置接口不返回密码。密码不跟随业务数据同步或进入发布包。

没有数据库 TLS 时，使用已有 SSH 访问建立隧道。PostgreSQL 示例：

```powershell
ssh -N -L 127.0.0.1:15432:127.0.0.1:5432 你的SSH账号@47.113.126.29
```

此时设置地址 `127.0.0.1`、端口 `15432`、连接加密“本机 SSH 隧道”。
MySQL 可将本地端口改为 `13306`、目标端口改为 `3306`。
隧道关闭后本机继续离线工作，重连后自动同步。远程裸 TCP 不允许关闭 TLS；
不需要额外开放 HTTP 数据服务端口。

已为此电脑配置受限 SSH 密钥时，`start_ui.cmd` 会调用 `start_database_tunnel.ps1`
启动隧道，并在网络中断后自动重连。启动器读取当前 Windows 用户目录下的
`%LOCALAPPDATA%\XianYuMaterialPipeline\database-tunnel.json`；
该文件和私钥不会进入发布包。未配置此电脑密钥的其他电脑仍需单独配置 SSH 访问。
关闭在线同步后，后台隧道会自动退出。
本次服务器恢复记录见 [MySQL 恢复记录](server-mysql-status.md)。

## 入库内容

| 数据表 | 内容 |
|---|---|
| `xy_records` | 频道、消息 ID、文件名和大小、正文、游标、任务状态、中文名称、分享链接 |
| `xy_assets` | 按工作空间和 SHA256 去重的头像、封面与预览图片二进制 |
| `xy_changes` | 按工作空间递增的变更记录，用于增量同步 |
| `xy_operations` | 已处理操作，用于断网重试时避免重复写入 |
| `xy_workspaces` | 工作空间变更序号及并发写入锁 |
| `xy_meta` | 数据库身份及结构版本 |

ZIP/RAR 文件、Telegram 会话、API 凭证、代理、夸克 Cookie、本机绝对路径、
系统设置和自定义术语表留在本机。下载上传使用本机网络。
任务按设备区分，其他设备任务只读，不会在本机自动重试、上传或打开磁盘路径。
应用账号需要定期备份数据库；图片包含在数据库备份中，无额外资源目录。

本地 `.server-sync/outbox.sqlite3` 保存设备 ID、游标、待同步记录和冲突；
不要将该目录复制到另一台电脑作为运行目录。频道消息在本机每频道缓存最近 300 条，
数据库保留已同步历史记录，本版不提供完整历史数据库分页查询。
同时编辑同一记录时，在设置页对比双方内容并选择采用服务器或本机版本。

切换到新的数据库或工作空间时，会将本机缓存导入目标；先核对目标是否正确。
重建数据库会改变数据库身份，本机会停止同步，避免继续使用旧版本号和游标。
密码轮换不会重置已有数据库的同步进度。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_database_storage.py tests/test_server_sync.py -q
.\.venv\Scripts\python.exe tests/check_channel_picker_ui.py
```

默认验证使用隔离的本地 SQLite 模拟远程存储，覆盖幂等、事务回滚、冲突、
并发写入、分页、图片、断网重启和敏感字段过滤，不连接真实账号或远程数据库。
可在独立测试库设置 `XIANYU_TEST_DATABASE_URL`，运行
`tests/test_database_storage.py::test_real_database_contract` 验证 PostgreSQL/MySQL；
该用例会创建 `xy_` 表并清理自己的临时工作空间，不应指向生产库。

之前已使用 HTTP 数据服务的配置继续兼容，维护文档见
[旧版数据服务](legacy-data-service.md)。新配置默认使用数据库直连。
Electron 桌面版方案另见 [暂存方案](../docs/desktop-packaging-plan.md)，尚未实施。
