# 47.113.126.29 MySQL 恢复记录

操作日期：2026-10-22。此文档不包含密码或私钥。

## 恢复方式

- 原数据卷：`wordpress_wordpress_db_data`，恢复时约 498 MiB。
- 从旧二进制日志确认 MySQL 版本为 `8.0.42`。
- 先备份并校验 240 个原文件，再创建副本恢复；原数据卷未挂载到新服务。
- 备份目录：`/root/xianyu-db-recovery-20261022-092846`。
- 压缩备份：该目录下的 `mysql-original.tar.gz`，约 45 MiB。
- 备份 SHA256：`b5c2504c8e3630bea7375ead413cfa4a711db501520ff7d8219f215bce3915bf`。
- 恢复副本卷：`xianyu_mysql_restored_20261022092846`。
- 正式容器：`xianyu-mysql-restored`，镜像固定为 `mysql:8.0.42`。
- 镜像摘要：`sha256:63823b8e2cbe4ae0c558155e02d00beba56130fbc3d147efccbdb328ae2dbb9e`。
- 容器配置 `unless-stopped` 自动启动，内存上限 640 MiB。

原 WordPress 数据库保留，57 张表通过 `CHECK TABLE ... QUICK`。
保留原 root 和 WordPress 账号的认证信息。恢复副本新增本机管理账号 `xianyu_admin`，
凭据仅在上述 root 目录中的 `admin.cnf` 和 `credentials.json`，权限为 600。
临时恢复容器、验收库和验收账号已在验证后移除。

## 应用连接

- 本项目使用独立数据库 `xianyu`、专用账号 `xianyu_app`。
- 应用账号仅有 `xianyu` 库的 SELECT、INSERT、UPDATE、CREATE、INDEX、REFERENCES 权限。
- 数据库容器只发布服务器本机 `127.0.0.1:3306`。
- 此电脑经 SSH 隧道连接本机 `127.0.0.1:13306`，应用设置选择 MySQL 和“本机 SSH 隧道”。
- 应用数据库密码已保存到本机忽略文件 `config/test.local.json`，配置接口仅返回“已配置”。
- SSH 使用单独生成的受限密钥，仅允许连接服务器 `127.0.0.1:3306`，
  已验证不能执行远程命令或转发 SSH 22 端口。服务器 root 登录密码不写入应用配置。
- `%LOCALAPPDATA%\XianYuMaterialPipeline\database-tunnel.json` 记录此电脑的隧道参数，
  私钥位于同目录下的 `ssh` 子目录，仅当前 Windows 用户有读取权限。
- `start_ui.cmd` 自动启动隧道维护进程；运行期间断线后自动重连。
  关闭在线同步后维护进程停止。应用本身和下载上传仍在本机运行。

真实 MySQL 验收覆盖幂等写入、冲突、并发变更游标、1 MiB 图片二进制存储。
验收在独立临时库完成，未向 WordPress 数据库写入业务测试记录。

2026-10-22 本机应用验收：在线同步状态为 `connected`，待同步项和冲突均为 0。
服务器已保存 1 个频道、100 条频道消息、1 个消息游标、1 条任务记录和 88 张预览图。
已主动中断本项目的 SSH 隧道进程，维护进程约 5 秒后重建连接，后续同步成功。

## 日常检查

```sh
docker ps --filter name=xianyu-mysql-restored
docker logs --tail 50 xianyu-mysql-restored
docker exec xianyu-mysql-restored \
  mysql --defaults-extra-file=/run/secrets/xianyu-admin.cnf \
  -e 'SHOW DATABASES; SELECT kind, COUNT(*) FROM xianyu.xy_records GROUP BY kind;'
```

定期通过该管理凭据执行 MySQL 备份；原始恢复备份不会包含后续新增的应用数据。
不要直接替换正在使用的数据目录，也不要让两个容器同时加载恢复卷。
