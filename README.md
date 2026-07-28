# 喜马拉雅有声书管理系统

基于 Docker 的喜马拉雅有声书采集、处理、Telegram 缓存管理系统。

## 功能

1. **分类采集**：从喜马拉雅抓取分类专辑信息（封面、标题、简介、章节列表）
2. **分布式处理**：将专辑处理任务分派给多个 Colab 实例并行执行
3. **TG 缓存**：每个章节经下载 → DeepFilter 降噪 → 上传到 Telegram，缓存 file_id
4. **DB 兼容**：表结构与 `yt_aduio_book_one_to_all_v2` 完全兼容，pipeline 可直接查询 TG 缓存

## 技术栈

- **Web**: FastAPI + Jinja2 SSR + Bootstrap 5
- **DB**: PostgreSQL 16-alpine + psycopg3 连接池
- **部署**: Docker Compose 一键启动
- **处理**: Colab Worker 轮询 VPS API 认领任务（FOR UPDATE SKIP LOCKED）

## 快速部署

### 1. 准备配置

```bash
cp .env.example .env
# 编辑 .env 修改密码等
```

### 2. 启动

```bash
docker-compose up -d --build
```

### 3. 访问

- Web 界面: http://localhost:59388
- API 文档: http://localhost:59388/api/docs
- 默认密码: inriynisse

## 使用流程

### Step 1: 采集专辑

1. 打开 Web 界面 → 专辑管理 → 采集分类
2. 选择分类（如有声书 youshengshu）、排序、页数
3. 点击「开始采集」，专辑信息保存到数据库

### Step 2: 获取章节列表

1. 点击专辑标题进入详情页
2. 点击「获取章节列表」按钮
3. 系统从喜马拉雅 API 获取所有章节并保存

### Step 3: 配置全局设置

在「全局设置」页面配置：
- `TG_BOT_TOKEN`: Telegram Bot Token（多个用逗号分隔）
- `TG_CHAT_ID`: Telegram 聊天/频道 ID
- `XM_COOKIE`: 喜马拉雅 Cookie（下载付费内容需要）

### Step 4: 创建处理任务

1. 专辑详情页 → 点击「创建处理任务」
2. 或在「任务队列」页面批量创建

### Step 5: 运行 Colab Worker

在 Colab 中运行 Worker 脚本：

```python
# 安装依赖
!pip install requests pycryptodome tqdm pydub

# 克隆项目
!git clone https://github.com/your-repo/ximalaya_manager.git
%cd ximalaya_manager

# 运行 Worker
!python colab/ximalaya_colab_worker.py \
    --vps-url http://your-vps:59388 \
    --worker-id colab_001 \
    --worker-token YOUR_WORKER_TOKEN \
    --install-deps
```

## 数据库表结构

### 兼容表（与 yt_aduio_book_one_to_all_v2 一致）

| 表名 | 用途 |
|------|------|
| `books` | 专辑信息 (book_id = `xm_{albumId}`) |
| `audiobook_chapters` | 章节级 TG 缓存 (book_id + chapter_id) |
| `global_settings` | 全局共享设置 |

### 新增表

| 表名 | 用途 |
|------|------|
| `xm_jobs` | 任务队列（Colab Worker FOR UPDATE SKIP LOCKED 认领） |
| `xm_worker_stats` | Worker 业绩统计 |
| `xm_scrape_tasks` | 采集任务记录 |

## Colab Worker API

Worker 通过 VPS API 轮询认领任务：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/jobs/claim?worker_id=xxx` | GET | 认领任务 |
| `/api/jobs/{job_id}/chapter` | POST | 上报章节结果 |
| `/api/jobs/{job_id}/complete` | POST | 标记任务完成 |
| `/api/jobs/{job_id}/fail` | POST | 标记任务失败 |
| `/api/config?worker_id=xxx` | GET | 获取配置（TG token 等） |
| `/api/worker/heartbeat` | POST | 心跳 |

所有 Worker API 需在请求中携带 `worker_token` 参数（Header `X-Worker-Token` 或 Query `worker_token`）。

## 与参考项目兼容

`books` + `audiobook_chapters` 表结构与 `yt_aduio_book_one_to_all_v2` 完全一致：
- `book_id` 格式: `xm_{albumId}`（参考项目用 `bili_` 前缀）
- `audiobook_chapters.audio_url`: `https://www.ximalaya.com/sound/{trackId}`
- `book_data.chapters[].mp3Url`: 同 audio_url

参考项目的 `fetch_tg_cache_map(book_id, audio_urls)` 可直接匹配。

如需共用同一个 PostgreSQL 数据库，将 `DATABASE_URL` 指向同一实例即可。
