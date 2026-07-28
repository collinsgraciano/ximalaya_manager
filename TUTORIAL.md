# 喜马拉雅有声书管理系统 — 部署与使用教程

> 本文档详细说明系统的部署方式、功能使用、配置管理和运维操作。

---

## 目录

1. [系统架构](#1-系统架构)
2. [环境要求](#2-环境要求)
3. [Docker 一键部署](#3-docker-一键部署)
4. [服务器部署脚本](#4-服务器部署脚本)
5. [首次配置](#5-首次配置)
6. [使用流程](#6-使用流程)
7. [Colab Worker 使用](#7-colab-worker-使用)
8. [代理池配置](#8-代理池配置)
9. [全局设置说明](#9-全局设置说明)
10. [API 接口文档](#10-api-接口文档)
11. [数据库表结构](#11-数据库表结构)
12. [运维操作](#12-运维操作)
13. [与参考项目兼容](#13-与参考项目兼容)

---

## 1. 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                         VPS 服务器                            │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  Docker Compose                                     │   │
│  │  ┌──────────────────┐  ┌────────────────────────┐   │   │
│  │  │  PostgreSQL 16   │  │  FastAPI Web App        │   │   │
│  │  │  (端口 5433)     │←→│  (端口 59388)           │   │   │
│  │  │  数据持久化       │  │  Jinja2 SSR + REST API │   │   │
│  │  └──────────────────┘  └──────────┬─────────────┘   │   │
│  └──────────────────────────────────────┼─────────────────┘   │
└─────────────────────────────────────────┼───────────────────┘
                                          │ Worker API
                           ┌──────────────┼──────────────┐
                           │              │              │
                    ┌──────▼──┐   ┌──────▼──┐   ┌──────▼──┐
                    │ Colab   │   │ Colab   │   │ Colab   │
                    │ Worker 1│   │ Worker 2│   │ Worker N│
                    └─────────┘   └─────────┘   └─────────┘
```

**数据流**：

```
喜马拉雅网站 ──采集──→ VPS数据库 ──创建任务──→ 任务队列
                                                │
                                    Colab Worker 轮询认领
                                                │
                                    下载音频 → DeepFilter降噪
                                                │
                                    上传Telegram → 缓存file_id → 数据库
```

---

## 2. 环境要求

| 组件 | 要求 |
|------|------|
| 服务器 | Linux VPS（推荐 Ubuntu 22.04+），2GB+ 内存 |
| Docker | 24.0+ |
| Docker Compose | v2+ |
| 网络 | VPS 需访问喜马拉雅（可配置代理）和 Telegram API |
| Colab | Google Colab 账号（免费版可用，Pro 更佳） |

---

## 3. Docker 一键部署

### 3.1 获取代码

```bash
git clone https://github.com/collinsgraciano/ximalaya_manager.git
cd ximalaya_manager
```

### 3.2 配置环境变量

```bash
cp .env.example .env
nano .env
```

`.env` 文件内容：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `POSTGRES_PASSWORD` | PostgreSQL 数据库密码 | `changeme_strong_password` |
| `SECRET_KEY` | Cookie 签名密钥 | `dev_secret_key_change_in_production` |
| `APP_PASSWORD` | Web 界面登录密码 | `inriynisse` |
| `BASE_URL` | VPS 外部访问地址 | `http://localhost:59388` |
| `WORKER_AUTH_TOKEN` | Colab Worker 认证 Token | `changeme_worker_token` |

**生产环境务必修改所有默认密码和 Token。**

### 3.3 启动服务

```bash
docker-compose up -d --build
```

启动后包含两个容器：

| 容器 | 端口 | 用途 |
|------|------|------|
| `xm_postgres` | 5433 → 5432 | PostgreSQL 数据库 |
| `xm_web` | 59388 → 59388 | FastAPI Web 应用 |

### 3.4 访问

| 地址 | 说明 |
|------|------|
| `http://<服务器IP>:59388` | Web 管理界面 |
| `http://<服务器IP>:59388/api/docs` | Swagger API 文档 |
| `http://<服务器IP>:59388/api/system/health` | 健康检查 |

首次访问需输入密码登录（默认 `inriynisse`）。

---

## 4. 服务器部署脚本

项目提供一键部署脚本 `scripts/deploy/git-server-deploy.sh`，在服务器上执行：

```bash
cd /root/ximalaya_manager
bash scripts/deploy/git-server-deploy.sh
```

脚本自动完成：

1. `git pull` 拉取最新代码（脚本自身更新后会自动重新执行）
2. 检查 `.env` 是否存在（不存在则从模板创建）
3. 下载 DeepFilter 二进制到宿主机持久化缓存
4. 检查 5433 端口占用（自动停止系统 PostgreSQL 或残留容器）
5. 智能构建（通过哈希比对判断是否需要重建镜像）
6. 重启 Docker 服务
7. 健康检查（Web + PostgreSQL）

---

## 5. 首次配置

登录 Web 界面后，进入 **全局设置** 页面配置以下内容：

### 5.1 必须配置

| 设置项 | 说明 | 示例 |
|--------|------|------|
| `TG_BOT_TOKEN` | Telegram Bot Token（多个用英文逗号分隔） | `123456:ABC-DEF...,789012:GHI-JKL...` |
| `TG_CHAT_ID` | Telegram 聊天/频道 ID（带 `-100` 前缀） | `-1001234567890` |

### 5.2 可选配置

| 设置项 | 说明 | 默认值 |
|--------|------|--------|
| `XM_COOKIE` | 喜马拉雅 Cookie（下载付费内容需要） | 空 |
| `ENABLE_DEEPFILTER` | 是否启用降噪 | `true` |
| `DEEPFILTER_SEGMENT_MINUTES` | 降噪分片时长（分钟） | `60` |
| `DOWNLOAD_INTERVAL` | 下载间隔秒数 | `1.5` |
| `TG_SERIAL_UPLOAD` | 串行上传到 TG | `true` |
| `TG_UPLOAD_INTERVAL` | TG 上传间隔秒数 | `3` |

### 5.3 代理配置（可选）

见 [第 8 节](#8-代理池配置)。

---

## 6. 使用流程

### Step 1：采集专辑

1. 打开 Web 界面 → **专辑管理** → 点击「采集分类」
2. 选择分类（如有声书 `youshengshu`）、排序方式
3. 设置最大页数 / 最大专辑数（0 = 不限）
4. 勾选「仅免费专辑」（可选）
5. 点击「开始采集」

系统会从喜马拉雅抓取分类下所有专辑，保存以下信息到数据库：

- 专辑标题、主播名、分类
- 封面图片 URL、简介
- 播放量、是否付费、是否完结
- 总章节数

### Step 2：获取章节列表

1. 在专辑管理页面点击专辑标题进入详情页
2. 点击「获取章节」按钮
3. 系统调用喜马拉雅移动端 API 获取所有章节

每个章节保存以下信息：

- 章节标题、章节 ID（trackId）
- 章节序号、时长
- 音频 URL 标识（`https://www.ximalaya.com/sound/{trackId}`）

### Step 3：创建处理任务

**单个专辑**：在专辑详情页点击「创建任务」

**批量创建**：在任务队列页面输入多个 book_id（逗号分隔）

任务创建后状态为 `pending`，等待 Colab Worker 认领。

### Step 4：运行 Colab Worker

在 Google Colab 中运行 Worker 脚本（详见下一节）。

### Step 5：查看进度

- **仪表盘**：总览专辑数、章节数、任务状态、Worker 在线数
- **任务队列**：查看任务进度、认领 Worker、完成时间
- **Worker 统计**：查看各 Worker 的任务数、成功率、章节数
- **专辑详情**：查看每个章节的上传状态、TG file_id

---

## 7. Colab Worker 使用

### 7.1 在 Colab 中安装依赖

```python
!pip install requests pycryptodome tqdm pydub
```

### 7.2 克隆项目

```python
!git clone https://github.com/collinsgraciano/ximalaya_manager.git
%cd ximalaya_manager
```

### 7.3 运行 Worker

```python
!python colab/ximalaya_colab_worker.py \
    --vps-url http://your-vps-ip:59388 \
    --worker-id colab_001 \
    --worker-token YOUR_WORKER_TOKEN \
    --install-deps
```

### 7.4 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--vps-url` | VPS API 地址（必填） | — |
| `--worker-id` | Worker ID（不填则自动生成） | `colab_{随机}` |
| `--worker-token` | Worker 认证 Token（必填） | — |
| `--poll-interval` | 无任务时等待秒数 | `10` |
| `--max-jobs` | 最大处理任务数（0 = 不限） | `0` |
| `--install-deps` | 自动安装缺失依赖 | 不安装 |

### 7.5 Worker 工作流程

```
启动 → 获取配置（TG Token、Cookie、代理等） → 启动心跳线程
  ↓
轮询认领任务（GET /api/jobs/claim）
  ↓
对每个章节：
  1. 下载音频（喜马拉雅 mobile-playpage API + AES 解密）
  2. DeepFilter 降噪（如启用）
  3. 上传到 Telegram（sendAudio，多 Bot Token 轮换）
  4. 上报结果（POST /api/jobs/{job_id}/chapter）
  ↓
全部完成 → 标记任务完成（POST /api/jobs/{job_id}/complete）
  ↓
继续轮询下一个任务
```

### 7.6 多 Worker 并行

可同时运行多个 Colab Worker，系统使用 `FOR UPDATE SKIP LOCKED` 确保不会重复认领。每个 Worker 使用不同的 `--worker-id`：

```python
# Worker 1
!python colab/ximalaya_colab_worker.py --vps-url ... --worker-id colab_001 --worker-token ...

# Worker 2
!python colab/ximalaya_colab_worker.py --vps-url ... --worker-id colab_002 --worker-token ...
```

---

## 8. 代理池配置

系统支持为采集和音频下载配置代理池，按延迟自动排序，优先使用速度最快的代理。

### 8.1 配置方法

在 Web 界面 → **全局设置** → **代理** 分组：

| 设置项 | 说明 | 默认值 |
|--------|------|--------|
| `PROXY_ENABLED` | 是否启用代理 | `false` |
| `PROXY_LIST` | 代理列表（每行一个） | 空 |
| `PROXY_TEST_URL` | 健康检测 URL | `https://www.ximalaya.com` |
| `PROXY_DEAD_RETRY_MINUTES` | 死亡代理重试间隔（分钟） | `5` |
| `PROXY_TIMEOUT` | 代理请求超时秒数 | `10` |

### 8.2 代理格式

```
http://ip:port
http://user:pass@ip:port
socks5://ip:port
socks5://user:pass@ip:port
```

> 使用 SOCKS5 代理需要安装 `pip install requests[socks]`

### 8.3 工作机制

1. **初始化时健康检测**：对每个代理发请求测延迟，按延迟升序排序
2. **优先使用最快代理**：`get()` 返回延迟最低的可用代理
3. **死亡标记**：请求失败时标记代理死亡，N 分钟后自动恢复
4. **多代理轮换**：多个可用代理间做 round-robin，避免总打同一个
5. **全死降级**：所有代理不可用时自动直连

### 8.4 适用范围

| 场景 | 是否使用代理 |
|------|------------|
| VPS 采集分类专辑 | ✅ |
| VPS 获取章节列表 | ✅ |
| Colab 下载音频 | ✅ |
| VPS Web 界面访问 | ❌（直连） |
| Colab 上传到 Telegram | ❌（直连） |

---

## 9. 全局设置说明

所有设置存储在 `global_settings` 表中，通过 Web 界面管理。

### Telegram 设置

| 设置项 | 类型 | 说明 |
|--------|------|------|
| `TG_BOT_TOKEN` | Secret | Bot Token（多个用英文逗号分隔，支持多 Bot 轮换上传） |
| `TG_CHAT_ID` | 明文 | 音频上传目标聊天/频道 ID |
| `TG_SERIAL_UPLOAD` | 布尔 | 是否串行上传（避免限流） |
| `TG_UPLOAD_INTERVAL` | 数字 | TG 上传间隔秒数 |

### 喜马拉雅设置

| 设置项 | 类型 | 说明 |
|--------|------|------|
| `XM_COOKIE` | Secret | 喜马拉雅 Cookie（下载付费内容需要，需含 `1&_token`） |
| `DOWNLOAD_INTERVAL` | 数字 | 下载间隔秒数 |

### 代理设置

| 设置项 | 类型 | 说明 |
|--------|------|------|
| `PROXY_ENABLED` | 布尔 | 是否启用代理 |
| `PROXY_LIST` | 多行文本 | 代理列表（每行一个） |
| `PROXY_TEST_URL` | 明文 | 健康检测 URL |
| `PROXY_DEAD_RETRY_MINUTES` | 数字 | 死亡代理重试间隔（分钟） |
| `PROXY_TIMEOUT` | 数字 | 代理请求超时秒数 |

### DeepFilter 设置

| 设置项 | 类型 | 说明 |
|--------|------|------|
| `ENABLE_DEEPFILTER` | 布尔 | 是否启用降噪 |
| `DEEPFILTER_SEGMENT_MINUTES` | 数字 | 音频分片时长（分钟） |

---

## 10. API 接口文档

启动后访问 `http://<服务器IP>:59388/api/docs` 查看交互式 Swagger 文档。

### 10.1 Web UI API

以下 API 需要登录 Cookie 认证：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/dashboard/stats` | GET | 仪表盘统计数据 |
| `/api/categories` | GET | 获取分类列表 |
| `/api/albums` | GET | 分页查询专辑列表 |
| `/api/albums/{book_id}` | GET | 获取专辑详情 |
| `/api/albums/{book_id}` | PUT | 更新专辑信息 |
| `/api/albums/{book_id}` | DELETE | 删除专辑 |
| `/api/albums/{book_id}/chapters` | GET | 分页查询章节列表 |
| `/api/albums/{book_id}/chapters/{chapter_id}` | PUT | 更新章节信息 |
| `/api/albums/scrape` | POST | 触发分类采集 |
| `/api/albums/{book_id}/scrape-tracks` | POST | 获取章节列表 |
| `/api/albums/{book_id}/reset-chapters` | POST | 重置章节状态 |
| `/api/jobs` | GET | 分页查询任务列表 |
| `/api/jobs/{job_id}` | GET | 获取任务详情 |
| `/api/jobs/create` | POST | 创建单个任务 |
| `/api/jobs/create-batch` | POST | 批量创建任务 |
| `/api/workers` | GET | 获取 Worker 统计 |
| `/api/settings` | GET | 获取全局设置 |
| `/api/settings` | POST | 更新设置项 |
| `/api/settings/{key}` | DELETE | 删除设置项 |
| `/api/system/health` | GET | 健康检查（公开） |

### 10.2 Colab Worker API

以下 API 需要 Worker Token 认证（`X-Worker-Token` Header 或 `worker_token` Query 参数）：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/jobs/claim?worker_id=xxx` | GET | 认领任务 |
| `/api/jobs/{job_id}/chapter?worker_id=xxx` | POST | 上报章节结果 |
| `/api/jobs/{job_id}/complete?worker_id=xxx` | POST | 标记任务完成 |
| `/api/jobs/{job_id}/fail?worker_id=xxx` | POST | 标记任务失败 |
| `/api/config?worker_id=xxx` | GET | 获取配置（TG Token、Cookie、代理等） |
| `/api/worker/heartbeat?worker_id=xxx` | POST | 心跳 |

**安全机制**：`/chapter`、`/complete`、`/fail` 端点会验证 `worker_id` 是否为该任务的认领者，防止越权操作。

---

## 11. 数据库表结构

### 11.1 兼容表（与 yt_aduio_book_one_to_all_v2 一致）

**`books` — 专辑信息**

| 列名 | 类型 | 说明 |
|------|------|------|
| `book_id` | text PK | 格式 `xm_{albumId}` |
| `book_name` | text | 专辑标题 |
| `author` | text | 主播名 |
| `category` | text | 分类 |
| `total_chapters` | integer | 总章节数 |
| `book_data` | jsonb | 完整专辑数据（封面、简介、章节列表等） |
| `tags` | text[] | 标签 |
| `note` | text | 备注 |
| `book_status` | varchar(50) | `pending` / `success` / `partial` / `failed` |
| `created_at` | timestamptz | 创建时间 |
| `updated_at` | timestamptz | 更新时间 |

**`audiobook_chapters` — 章节级 TG 缓存**

| 列名 | 类型 | 说明 |
|------|------|------|
| `book_id` | text | 专辑 ID（联合主键） |
| `chapter_id` | text | 章节 ID = trackId（联合主键） |
| `chapter_name` | text | 章节标题 |
| `audio_url` | text | `https://www.ximalaya.com/sound/{trackId}` |
| `telegram_file_id` | text | TG 文件 ID |
| `telegram_message_id` | bigint | TG 消息 ID |
| `telegram_bot_user_id` | bigint | 上传 Bot 的 User ID |
| `upload_status` | varchar(50) | `pending` / `uploaded` / `failed` |
| `uploaded_at` | timestamptz | 上传时间 |
| `worker_id` | varchar(100) | 认领此章节的 Worker |
| `chapter_order` | integer | 章节序号 |
| `duration` | integer | 章节时长（秒） |
| `error_message` | text | 错误信息 |

**`global_settings` — 全局设置**

| 列名 | 类型 | 说明 |
|------|------|------|
| `setting_key` | text PK | 设置键名 |
| `setting_value` | text | 设置值 |
| `description` | text | 描述 |
| `is_secret` | boolean | 是否为敏感信息 |
| `updated_at` | timestamptz | 更新时间 |

### 11.2 新增表

**`xm_jobs` — 任务队列**

| 列名 | 类型 | 说明 |
|------|------|------|
| `job_id` | serial PK | 任务 ID |
| `job_type` | varchar(50) | 任务类型（`process_album`） |
| `book_id` | text | 关联专辑 |
| `book_name` | text | 专辑名称 |
| `status` | varchar(50) | `pending` / `processing` / `done` / `failed` |
| `worker_id` | varchar(100) | 认领 Worker |
| `total_chapters` | integer | 总章节数 |
| `done_chapters` | integer | 已完成章节数 |
| `retry_count` | integer | 重试次数 |
| `result` | jsonb | 任务结果 |
| `error_message` | text | 错误信息 |
| `created_at` | timestamptz | 创建时间 |
| `claimed_at` | timestamptz | 认领时间 |
| `finished_at` | timestamptz | 完成时间 |

**`xm_worker_stats` — Worker 统计**

| 列名 | 类型 | 说明 |
|------|------|------|
| `worker_id` | varchar(100) PK | Worker ID |
| `total_jobs` | integer | 总任务数 |
| `success_jobs` | integer | 成功数 |
| `failed_jobs` | integer | 失败数 |
| `total_chapters` | integer | 处理章节总数 |
| `last_job_at` | timestamptz | 最后任务时间 |
| `last_seen_at` | timestamptz | 最后在线时间 |

**`xm_scrape_tasks` — 采集任务记录**

| 列名 | 类型 | 说明 |
|------|------|------|
| `task_id` | serial PK | 任务 ID |
| `category` | text | 采集分类 |
| `status` | varchar(50) | `running` / `done` / `failed` |
| `total_albums` | integer | 采集专辑数 |
| `processed_albums` | integer | 已保存数 |

---

## 12. 运维操作

### 12.1 查看日志

```bash
# Web 应用日志
docker logs -f xm_web --tail 50

# PostgreSQL 日志
docker logs -f xm_postgres --tail 50
```

### 12.2 重启服务

```bash
docker-compose restart web
docker-compose restart postgres
```

### 12.3 重建镜像

```bash
docker-compose up -d --build
```

### 12.4 更新代码

```bash
git pull
bash scripts/deploy/git-server-deploy.sh
```

### 12.5 数据库备份

```bash
docker exec xm_postgres pg_dump -U xm_app ximalaya > backups/ximalaya_$(date +%Y%m%d).sql
```

### 12.6 数据库恢复

```bash
docker exec -i xm_postgres psql -U xm_app ximalaya < backups/ximalaya_20260728.sql
```

### 12.7 进入数据库

```bash
docker exec -it xm_postgres psql -U xm_app -d ximalaya
```

### 12.8 超时任务自动回收

系统在每次 Worker 认领任务前自动检查：超过 30 分钟仍处于 `processing` 状态的任务会被重置为 `pending`，防止 Worker 崩溃导致任务卡死。

### 12.9 重置章节状态

在专辑详情页点击「重置章节」按钮，可将所有失败/待处理章节重置为 `pending`，清除 Worker 认领记录，便于重新处理。

---

## 13. 与参考项目兼容

### 13.1 表结构兼容

`books` + `audiobook_chapters` 表结构与 `yt_aduio_book_one_to_all_v2` 完全一致：

| 项目 | book_id 前缀 | audio_url 格式 |
|------|-------------|----------------|
| ximalaya_manager | `xm_{albumId}` | `https://www.ximalaya.com/sound/{trackId}` |
| bili_tg_audiobook | `bili_{BV号}` | `https://www.bilibili.com/video/BVxxx?p=N` |

### 13.2 共用数据库

将 `DATABASE_URL` 指向同一 PostgreSQL 实例即可共用。两个项目的数据通过 `book_id` 前缀区分，互不冲突。

参考项目的 `fetch_tg_cache_map(book_id, audio_urls)` 可直接匹配本项目写入的 TG 缓存数据。

### 13.3 book_data.chapters 格式

```json
{
  "chapters": [
    {
      "chapterId": "123456",
      "chapterName": "第一章",
      "mp3Url": "https://www.ximalaya.com/sound/123456",
      "orderNo": 1,
      "duration": 1800
    }
  ]
}
```

---

## 附录：项目文件结构

```
ximalaya_manager/
├── .env.example              # 环境变量模板
├── .gitignore
├── README.md
├── requirements.txt          # Python 依赖
├── docker-compose.yml        # Docker Compose 配置
│
├── docker/
│   ├── Dockerfile.web        # Web 应用镜像（多阶段构建）
│   ├── entrypoint.sh         # 容器启动入口
│   └── init-db.sql           # 数据库初始化 SQL
│
├── backend/
│   ├── __init__.py
│   ├── main.py               # FastAPI 应用入口
│   ├── auth.py               # 认证中间件
│   ├── database.py           # 数据库连接池
│   ├── settings.py           # 应用配置
│   ├── api/
│   │   ├── __init__.py
│   │   ├── albums.py         # 专辑管理 API
│   │   ├── dashboard.py      # 仪表盘 API
│   │   ├── jobs.py           # 任务队列 API
│   │   ├── workers.py        # Worker 统计 API
│   │   └── settings_api.py   # 全局设置 API
│   ├── services/
│   │   ├── __init__.py
│   │   ├── job_service.py    # 任务队列逻辑
│   │   └── scrape_service.py # 采集服务
│   └── templates/
│       ├── base.html         # 基础模板
│       ├── login.html        # 登录页
│       ├── dashboard.html    # 仪表盘
│       ├── albums.html       # 专辑管理
│       ├── album_detail.html # 专辑详情
│       ├── jobs.html         # 任务队列
│       ├── workers.html      # Worker 统计
│       └── settings.html     # 全局设置
│
├── pipeline/                 # 核心处理管道（VPS + Colab 共用）
│   ├── __init__.py
│   ├── ximalaya_api.py       # 喜马拉雅 API（采集 + 下载 + AES 解密）
│   ├── tg_upload.py          # Telegram 上传（多 Bot 轮换）
│   ├── deepfilter.py         # DeepFilter 降噪
│   └── proxy_pool.py         # 代理池（延迟排序 + 健康检测）
│
├── colab/
│   └── ximalaya_colab_worker.py  # Colab Worker 脚本
│
└── scripts/
    └── deploy/
        └── git-server-deploy.sh  # 服务器一键部署脚本
```
