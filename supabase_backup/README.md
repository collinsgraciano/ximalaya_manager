# ximalaya_manager B2 云备份系统

定时将项目所有数据（PostgreSQL 数据库 + 配置文件）备份到 [Backblaze B2](https://www.backblaze.com/cloud-storage)，支持一键恢复。

## 两种使用方式

1. **Web 界面**（推荐）：打开网页 → 侧边栏「数据备份」→ 填写 B2 配置 → 点击「马上备份」「开启定时备份」「恢复数据」
2. **命令行**：本文档下方介绍的使用方法

## 为什么选 Backblaze B2？

| 对比项 | Backblaze B2 免费版 | Supabase 免费版 |
|--------|-------------------|----------------|
| 存储空间 | **10 GB** | 1 GB |
| 单文件大小限制 | **无限制** | 50 MB |
| 每日下载流量 | 1 GB/天 | 5 GB/月 |
| S3 兼容 API | ✅ | ❌ (自有 API) |
| 可存备份数 | **~45 份** (205MB/份) | ~4 份 |

> 当前数据库 dump 约 205 MB，B2 的 10GB 免费额度可存放约 45 份备份，完全够用。

## 目录结构

```
supabase_backup/
├── common.py          # 共享工具模块 (配置、SSH、B2 S3 API)
├── backup.py          # 备份脚本 (备份 + 定时守护 + 清理)
├── restore.py         # 恢复脚本 (下载 + 恢复到 VPS)
├── requirements.txt   # Python 依赖
├── .env.example       # 配置模板
├── README.md          # 本文件
└── downloads/         # 恢复时下载的文件 (自动生成)
```

## 备份内容

| 内容 | 说明 |
|------|------|
| `db_dump.sql.gz` | PostgreSQL 完整数据库导出 (gzip 压缩)，包含所有表结构和数据 |
| `.env` | VPS 上的环境变量配置文件 |
| `docker-compose.yml` | Docker Compose 编排文件 |

数据库包含的表：`books`、`audiobook_chapters`、`global_settings`、`xm_jobs`、`xm_worker_stats`、`xm_scrape_tasks`

---

## 一、前置准备

### 1. 注册 Backblaze B2

1. 访问 [https://www.backblaze.com/](https://www.backblaze.com/) 注册账号
2. 登录后进入 **B2 Cloud Storage** 控制台

### 2. 创建 Bucket

1. 点击 **Buckets** → **Create a Bucket**
2. 设置：
   - **Bucket Name**: `xm-backups`（或自定义，需与 .env 中一致）
   - **Files in Bucket are**: **Private**
   - 其他默认
3. 创建后记录 **Endpoint** 信息（如 `s3.us-west-004.backblazeb2.com`）

### 3. 创建 App Key

1. 点击 **App Keys** → **Add a New Application Key**
2. 设置：
   - **Name**: `xm-backup-key`
   - **Allow access to Bucket(s)**: 选择刚创建的 `xm-backups`
   - **Capabilities**: Read & Write
3. 创建后**立即记录**以下信息（只显示一次）：
   - **keyID** → 填入 `B2_ACCESS_KEY_ID`
   - **applicationKey** → 填入 `B2_SECRET_ACCESS_KEY`

### 4. 安装依赖

```bash
cd H:\2026_main_project\ximalaya_manager\supabase_backup
pip install -r requirements.txt
```

### 5. 配置环境变量

```bash
copy .env.example .env
```

编辑 `.env` 文件：

```ini
# VPS SSH
VPS_HOST=117.55.234.219
VPS_PORT=22
VPS_USER=root
VPS_PASS=你的VPS密码

# Backblaze B2
B2_ENDPOINT=s3.us-west-004.backblazeb2.com
B2_ACCESS_KEY_ID=你的keyID
B2_SECRET_ACCESS_KEY=你的applicationKey
B2_BUCKET=xm-backups

# 备份设置
BACKUP_KEEP=7
BACKUP_INTERVAL_HOURS=24
```

---

## 二、备份使用方法

### 手动执行一次备份

```bash
cd H:\2026_main_project\ximalaya_manager\supabase_backup
python backup.py backup
```

输出示例：
```
[20260802_030000] === 开始 B2 备份 ===
  VPS: 117.55.234.219
  B2 Bucket: xm-backups

连接 SSH ...
SSH 连接成功

=== 1/3 导出数据库 ===
  OK: 远程 dump 205.0 MB
  下载中 ...
  OK: 已下载 (205.0 MB)

=== 2/3 下载配置文件 ===
  OK: .env (0.2 KB)
  OK: docker-compose.yml (2.5 KB)

=== 3/3 上传到 Backblaze B2 ===
  OK: db_dump.sql.gz (205.0 MB)
  OK: .env (0.2 KB)
  OK: docker-compose.yml (2.5 KB)

=== 清理旧备份 (保留最近 7 份) ===
  无需清理

=== 备份完成 ===
  时间戳: 20260802_030000
  B2 路径: xm-backups/20260802_030000/
  数据库 dump: 205.0 MB
  配置文件: .env, docker-compose.yml

Done!
```

### 查看已有备份

```bash
python backup.py list
```

### 清理旧备份

```bash
# 保留最近 7 份（默认）
python backup.py clean

# 保留最近 3 份
python backup.py clean --keep 3
```

### 定时守护进程模式

```bash
# 每 24 小时备份一次（默认）
python backup.py daemon

# 每 12 小时备份一次
python backup.py daemon -i 12
```

按 `Ctrl+C` 停止。

---

## 三、设置定时任务

### 方式一：Windows 任务计划程序

```powershell
# 创建每天凌晨 3:00 执行的定时备份任务
schtasks /create /tn "XmB2Backup" /tr "python H:\2026_main_project\ximalaya_manager\supabase_backup\backup.py backup" /sc daily /st 03:00

# 查看任务
schtasks /query /tn "XmB2Backup"

# 删除任务
schtasks /delete /tn "XmB2Backup" /f
```

### 方式二：Linux crontab（VPS 上运行）

```bash
crontab -e

# 添加每天凌晨 3:00 备份
0 3 * * * cd /opt/ximalaya_manager && python3 supabase_backup/backup.py backup >> /var/log/xm_b2_backup.log 2>&1
```

### 方式三：Python 守护进程（配合 nohup / screen）

```bash
# 后台运行，每 6 小时备份
nohup python backup.py daemon -i 6 >> backup_daemon.log 2>&1 &
```

---

## 四、恢复教程

### 场景一：完整恢复（数据库 + 配置文件）

适用于 VPS 数据丢失、需要完整恢复的情况。

```bash
cd H:\2026_main_project\ximalaya_manager\supabase_backup

# 1. 查看可用备份
python restore.py list

# 2. 恢复最新备份（自动下载 + SSH 恢复到 VPS）
python restore.py restore

# 或恢复指定时间点的备份
python restore.py restore 20260802_030000
```

恢复过程：
1. 从 B2 下载备份文件到本地
2. SSH 上传 db_dump.sql.gz 到 VPS
3. 在 VPS 上 `gunzip` 解压 + `psql` 导入数据库
4. 恢复 `.env` 和 `docker-compose.yml`（原文件自动备份为 `.bak`）
5. 验证各表数据行数

恢复后需要重启服务：
```bash
ssh root@117.55.234.219
cd /opt/ximalaya_manager
docker compose up -d --build web
```

### 场景二：仅恢复数据库

```bash
python restore.py restore --db-only

# 指定备份
python restore.py restore 20260802_030000 --db-only
```

### 场景三：仅恢复配置文件

```bash
python restore.py restore --config-only
```

### 场景四：仅下载备份到本地（不恢复）

```bash
python restore.py download 20260802_030000
```

文件下载到 `supabase_backup/downloads/20260802_030000/` 目录。

### 场景五：手动恢复（不使用脚本）

如果脚本无法使用，可以手动恢复：

```bash
# 1. 从 B2 网页端下载 db_dump.sql.gz
#    B2 控制台 → Buckets → xm-backups → 选择备份文件夹 → 下载 db_dump.sql.gz

# 2. 上传到 VPS
scp db_dump.sql.gz root@117.55.234.219:/tmp/

# 3. SSH 到 VPS 恢复
ssh root@117.55.234.219
gunzip /tmp/db_dump.sql.gz
docker exec -i xm_postgres psql -U xm_app -d ximalaya < /tmp/db_dump.sql

# 4. 重启服务
cd /opt/ximalaya_manager
docker compose up -d --build web
```

---

## 五、B2 免费额度说明

| 资源 | 免费额度 | 你的使用量 | 说明 |
|------|---------|-----------|------|
| 存储空间 | 10 GB | ~205 MB/份 × 7 = ~1.4 GB | 充足 |
| 每日下载 | 1 GB/天 | 每次恢复 ~205 MB | 每天 可恢复 ~4 次 |
| 单文件大小 | 无限制 | dump 205 MB | 无问题 |
| 上传流量 | 免费 | — | 上传不收费 |
| API 调用 | 2,500 次/天 (Class B) | 每次备份 ~5 次 | 无问题 |

> 如果后续数据增长超过 10 GB，B2 收费仅 $0.005/GB/月（约 ¥0.035/GB/月），非常便宜。

---

## 六、故障排除

### 问题：SSH 连接失败

```
FAIL: SSH 连接失败: [Errno -2] Name or service not known
```

**解决**：检查 `.env` 中的 `VPS_HOST`、`VPS_PORT`、`VPS_USER`、`VPS_PASS` 是否正确。

### 问题：B2 上传失败 (AccessDenied)

```
FAIL: 上传 ... 失败: An error occurred (AccessDenied) ...
```

**解决**：
1. 检查 `B2_ENDPOINT` 是否正确（在 B2 控制台 Buckets 页面查看）
2. 检查 `B2_ACCESS_KEY_ID` 和 `B2_SECRET_ACCESS_KEY` 是否正确
3. 确认 App Key 有目标 bucket 的 Read & Write 权限

### 问题：B2 bucket 不存在

```
ERROR: B2 bucket 'xm-backups' 不存在，请在 B2 网页端创建
```

**解决**：在 B2 控制台手动创建 bucket，脚本不会自动创建。

### 问题：pg_dump 在 VPS 上失败

```
FAIL: pg_dump 退出码 1
```

**解决**：SSH 到 VPS 手动测试：
```bash
ssh root@117.55.234.219
docker exec xm_postgres pg_dump -U xm_app -d ximalaya --no-owner --no-privileges | head -20
```

### 问题：恢复后 Web 服务无法访问

**解决**：恢复后需要重启服务：
```bash
ssh root@117.55.234.219
cd /opt/ximalaya_manager
docker compose up -d --build web
```

### 问题：恢复 psql 出现 ERROR

pg_dump 使用了 `--clean --if-exists`，恢复时会先 DROP 再 CREATE。部分 `DROP TABLE IF EXISTS` 在表不存在时会产生 NOTICE（非错误），可以忽略。如果出现真正的 ERROR，检查 SQL dump 文件是否完整。

### 问题：boto3 未安装

```
ModuleNotFoundError: No module named 'boto3'
```

**解决**：
```bash
pip install boto3
```

---

## 七、命令速查

| 操作 | 命令 |
|------|------|
| 执行一次备份 | `python backup.py backup` |
| 启动定时守护 | `python backup.py daemon` |
| 每 12 小时备份 | `python backup.py daemon -i 12` |
| 列出所有备份 | `python backup.py list` |
| 清理旧备份 | `python backup.py clean --keep 7` |
| 恢复最新备份 | `python restore.py restore` |
| 恢复指定备份 | `python restore.py restore 20260802_030000` |
| 仅恢复数据库 | `python restore.py restore --db-only` |
| 仅恢复配置文件 | `python restore.py restore --config-only` |
| 下载备份到本地 | `python restore.py download 20260802_030000` |
| 列出可用备份 | `python restore.py list` |
