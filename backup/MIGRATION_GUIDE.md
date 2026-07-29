# 备份与迁移指南

## 目录结构

```
backup/
├── backup_vps.py          # 定时备份脚本 (本地运行, SSH 进 VPS)
├── migrate_vps.py         # 一键迁移脚本 (旧 VPS → 新 VPS)
├── MIGRATION_GUIDE.md     # 本文件
└── archives/              # 压缩备份归档 (自动生成)
    └── xm_backup_20260728_030000.tar.gz
        ├── db_dump.sql.gz     # PostgreSQL 压缩导出 (schema + data)
        ├── .env                # 环境变量 (含密码)
        └── docker-compose.yml  # Docker Compose 配置快照
```

---

## 一、定时备份

### 1.1 手动执行一次备份

```bash
python backup/backup_vps.py
```

输出示例:
```
[20260728_030000] 开始备份 ximalaya_manager
  VPS: 117.55.234.219

=== 1/3 导出数据库 (gzip 压缩) ===
  OK: 远程压缩 dump 42 KB
  下载中 ...
  OK: db_dump.sql.gz (42 KB)

=== 2/3 下载 .env ===
  OK: .env (185 bytes)

=== 3/3 下载 docker-compose.yml ===
  OK: docker-compose.yml

=== 打包归档 ===
  OK: xm_backup_20260728_030000.tar.gz (44 KB)

=== 备份完成 ===
  文件: backup/archives/xm_backup_20260728_030000.tar.gz
  大小: 0.04 MB
```

### 1.2 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--keep N` | 7 | 保留最近 N 份备份, 超出的自动删除 |
| `--no-cleanup` | - | 本次备份后不清理旧备份 |

### 1.3 备份格式

备份为单个 `.tar.gz` 压缩包，内含：
- `db_dump.sql.gz` — PostgreSQL 全量导出 (gzip 压缩, 恢复时 `gunzip -c | psql`)
- `.env` — 环境变量 (POSTGRES_PASSWORD, WORKER_AUTH_TOKEN, APP_PASSWORD 等)
- `docker-compose.yml` — Docker Compose 配置快照 (git 里也有, 备份仅为对照)

> **注意**: 代码本身在 GitHub 上, 不需要备份。迁移时直接 `git clone` 即可。

### 1.4 设置 Windows 定时任务

以管理员身份打开 PowerShell:

```powershell
schtasks /create /tn "XimalayaBackup" `
  /tr "python H:\2026_main_project\ximalaya_manager\backup\backup_vps.py" `
  /sc daily /st 03:00

# 查看
schtasks /query /tn "XimalayaBackup"

# 删除
schtasks /delete /tn "XimalayaBackup" /f
```

### 1.5 设置 Linux crontab (如在 Linux 上运行)

```bash
crontab -e
0 3 * * * cd /opt/ximalaya_manager && python3 backup/backup_vps.py >> /var/log/xm_backup.log 2>&1
```

---

## 二、一键迁移到新 VPS

### 2.1 用法

```bash
python backup/migrate_vps.py --dst-host NEW_IP --dst-pass NEW_ROOT_PASSWORD
```

可选参数:
- `--dst-port` SSH 端口 (默认 22)
- `--dst-user` SSH 用户 (默认 root)
- `--git-url` Git 仓库地址 (默认内置地址)
- `--dst-dir` 新 VPS 项目路径 (默认 /opt/ximalaya_manager)

### 2.2 脚本自动完成 7 步

| Step | 说明 |
|------|------|
| 1 | SSH 进旧 VPS, `pg_dump \| gzip` 导出数据库 + 下载 `.env` |
| 2 | 连接新 VPS, SFTP 上传备份文件 |
| 3 | 在新 VPS 上 `git clone` (已存在则 `git pull`) |
| 4 | 恢复 `.env`, 自动改写 `BASE_URL` 为新 VPS IP |
| 5 | `docker compose up -d --build` 启动容器 |
| 6 | `gunzip -c \| psql` 恢复数据库 |
| 7 | 健康检查 + 数据行数校验 |

### 2.3 迁移后手动操作

1. **更新 Colab Worker URL**: Colab 运行时 `--vps-url` 改为新 VPS 地址
2. **开放防火墙端口**:
   ```bash
   ufw allow 59388/tcp  # Web
   ufw allow 22/tcp     # SSH
   # 5433 (PostgreSQL) 不建议对公网开放
   ```
3. **验证**: 浏览器访问 `http://NEW_IP:59388`, 用 APP_PASSWORD 登录
4. **关停旧 VPS**: 确认新 VPS 稳定运行后手动关停

### 2.4 前置条件

- 新 VPS: Ubuntu 22.04+ (或其他 Linux)
- 新 VPS 已安装 Docker + Docker Compose
- 本机能 SSH 到新 VPS

---

## 三、手动迁移 (不使用脚本)

如需手动迁移, 解压备份包后按以下步骤操作:

```bash
# 1. 解压备份
tar xzf backup/archives/xm_backup_XXXXXXXX_XXXXXX.tar.gz -C /tmp/xm_backup

# 2. 上传到新 VPS
scp -r /tmp/xm_backup root@NEW_IP:/tmp/xm_backup

# 3. 在新 VPS 上
ssh root@NEW_IP
git clone https://github.com/your-repo/ximalaya_manager.git /opt/ximalaya_manager
cp /tmp/xm_backup/.env /opt/ximalaya_manager/.env
# 修改 BASE_URL
sed -i 's|^BASE_URL=.*|BASE_URL=http://NEW_IP:59388|' /opt/ximalaya_manager/.env
cd /opt/ximalaya_manager
docker compose up -d --build
# 等待 15s 后恢复数据库
gunzip -c /tmp/xm_backup/db_dump.sql.gz | docker exec -i xm_postgres psql -U xm_app -d ximalaya
```

---

## 四、回滚

迁移期间两台 VPS 可以并行运行, 确认新 VPS 无误后再关停旧 VPS:

1. **旧 VPS 未关停**: 直接改回 Colab Worker 的 `--vps-url` 指向旧 VPS
2. **旧 VPS 已关停**: 重新启动旧 VPS 的 Docker 容器 (`docker compose up -d`), 再切换回来
