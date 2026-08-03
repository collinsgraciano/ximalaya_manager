-- docker/init-db.sql
-- 在 PostgreSQL 首次启动时自动执行

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ═══════════════════════════════════════════════════════════
-- 兼容表（与 yt_aduio_book_one_to_all_v2 完全一致）
-- ═══════════════════════════════════════════════════════════

-- 1. books — 专辑信息 (book_id = xm_{albumId})
CREATE TABLE IF NOT EXISTS public.books (
    book_id          text        PRIMARY KEY,
    book_name        text,
    author           text,
    category         text,
    total_chapters   integer,
    book_data        jsonb,
    tags             text[],
    note             text,
    status           text        DEFAULT '',
    created_at       timestamptz DEFAULT now(),
    updated_at       timestamptz DEFAULT now(),
    book_status      varchar(50) DEFAULT 'pending'
);
ALTER TABLE public.books ADD COLUMN IF NOT EXISTS category text;
ALTER TABLE public.books ADD COLUMN IF NOT EXISTS total_chapters integer;
ALTER TABLE public.books ADD COLUMN IF NOT EXISTS note text;
ALTER TABLE public.books ADD COLUMN IF NOT EXISTS book_status varchar(50) DEFAULT 'pending';
CREATE INDEX IF NOT EXISTS idx_books_category    ON public.books(category);
CREATE INDEX IF NOT EXISTS idx_books_status      ON public.books(status);
CREATE INDEX IF NOT EXISTS idx_books_book_status ON public.books(book_status);
CREATE INDEX IF NOT EXISTS idx_books_tags_gin    ON public.books USING gin(tags);
CREATE INDEX IF NOT EXISTS idx_books_updated_at  ON public.books(updated_at DESC);

-- 2. audiobook_chapters — 章节级 TG 缓存 (与参考项目完全一致)
CREATE TABLE IF NOT EXISTS public.audiobook_chapters (
    book_id               text NOT NULL,
    chapter_id            text NOT NULL,
    book_name             text,
    chapter_name          text,
    audio_url             text,
    telegram_file_id      text,
    telegram_message_id   bigint,
    telegram_bot_id       integer,
    telegram_bot_user_id  bigint,
    upload_status         varchar(50) DEFAULT 'pending',
    uploaded_at           timestamptz,
    worker_id             varchar(100),
    claimed_at            timestamptz,
    error_message         text,
    CONSTRAINT audiobook_chapters_pkey PRIMARY KEY (book_id, chapter_id)
);
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS telegram_file_id text;
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS telegram_message_id bigint;
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS upload_status varchar(50) DEFAULT 'pending';
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS uploaded_at timestamptz;
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS telegram_bot_id integer;
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS telegram_bot_user_id bigint;
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS worker_id varchar(100);
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS claimed_at timestamptz;
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS error_message text;
-- 本项目新增列
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS chapter_order integer;
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS duration integer;
-- 原始音频（降噪前）的 TG 缓存信息
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS original_telegram_file_id text;
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS original_telegram_message_id bigint;
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS original_telegram_bot_id integer;
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS original_telegram_bot_user_id bigint;

COMMENT ON COLUMN public.audiobook_chapters.telegram_bot_id IS '上传此文件的 Bot 编号（对应 BOT_TOKENS 数组索引）';
COMMENT ON COLUMN public.audiobook_chapters.telegram_bot_user_id IS '上传此文件的 Bot 的永久 Telegram User ID';
COMMENT ON COLUMN public.audiobook_chapters.worker_id IS '认领此章节的 Worker ID';
COMMENT ON COLUMN public.audiobook_chapters.claimed_at IS 'Worker 认领此章节的时间戳';
COMMENT ON COLUMN public.audiobook_chapters.error_message IS '上传失败时的错误信息记录';

CREATE INDEX IF NOT EXISTS idx_audiobook_chapters_book_id ON public.audiobook_chapters(book_id);
CREATE INDEX IF NOT EXISTS idx_audiobook_chapters_audio_url ON public.audiobook_chapters(book_id, audio_url);
CREATE INDEX IF NOT EXISTS idx_audiobook_chapters_upload_status ON public.audiobook_chapters(upload_status);
CREATE INDEX IF NOT EXISTS idx_chapters_book_status ON public.audiobook_chapters(book_id, upload_status);

-- 3. global_settings — 全局共享设置
CREATE TABLE IF NOT EXISTS public.global_settings (
    setting_key   text PRIMARY KEY,
    setting_value text NOT NULL DEFAULT '',
    description   text,
    is_secret     boolean DEFAULT false,
    updated_at    timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.global_settings ALTER COLUMN is_secret DROP NOT NULL;

-- ═══════════════════════════════════════════════════════════
-- 本项目新增表
-- ═══════════════════════════════════════════════════════════

-- 4. xm_jobs — 任务队列（Colab Worker 认领模式）
CREATE TABLE IF NOT EXISTS public.xm_jobs (
    job_id        serial PRIMARY KEY,
    job_type      varchar(50) NOT NULL DEFAULT 'process_album',
    book_id       text,
    book_name     text,
    status        varchar(50) DEFAULT 'pending',
    worker_id     varchar(100),
    claimed_at    timestamptz,
    result        jsonb,
    error_message text,
    retry_count   integer NOT NULL DEFAULT 0,
    total_chapters integer NOT NULL DEFAULT 0,
    done_chapters  integer NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);
CREATE INDEX IF NOT EXISTS idx_xm_jobs_status      ON public.xm_jobs(status);
CREATE INDEX IF NOT EXISTS idx_xm_jobs_book        ON public.xm_jobs(book_id);
CREATE INDEX IF NOT EXISTS idx_xm_jobs_created_at ON public.xm_jobs(created_at DESC);

COMMENT ON TABLE public.xm_jobs IS 'Colab Worker 任务队列：通过 FOR UPDATE SKIP LOCKED 原子认领';
COMMENT ON COLUMN public.xm_jobs.job_type IS 'process_album=处理专辑章节（下载→降噪→上传TG）';
COMMENT ON COLUMN public.xm_jobs.status IS 'pending=待处理；processing=处理中；done=成功；failed=失败';

-- 5. xm_worker_stats — Worker 业绩统计
CREATE TABLE IF NOT EXISTS public.xm_worker_stats (
    worker_id      varchar(100) PRIMARY KEY,
    total_jobs     integer NOT NULL DEFAULT 0,
    success_jobs   integer NOT NULL DEFAULT 0,
    failed_jobs    integer NOT NULL DEFAULT 0,
    total_chapters integer NOT NULL DEFAULT 0,
    total_seconds  bigint NOT NULL DEFAULT 0,
    last_job_at    timestamptz,
    last_seen_at   timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.xm_worker_stats IS 'Colab Worker 业绩统计';

-- 6. xm_scrape_tasks — 采集任务记录
CREATE TABLE IF NOT EXISTS public.xm_scrape_tasks (
    task_id         serial PRIMARY KEY,
    category        text NOT NULL,
    category_name   text,
    status          varchar(50) DEFAULT 'pending',
    total_albums    integer NOT NULL DEFAULT 0,
    processed_albums integer NOT NULL DEFAULT 0,
    error_message   text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);
CREATE INDEX IF NOT EXISTS idx_xm_scrape_tasks_status ON public.xm_scrape_tasks(status);

-- ═══════════════════════════════════════════════════════════
-- 初始化全局设置
-- ═══════════════════════════════════════════════════════════

INSERT INTO public.global_settings (setting_key, setting_value, description, is_secret) VALUES
    ('TG_BOT_TOKEN', '', 'Telegram Bot Token（多个Token用英文逗号分隔，支持多Bot轮换上传）', true),
    ('TG_CHAT_ID', '', 'Telegram Chat ID（音频上传目标聊天/频道ID）', false),
    ('XM_COOKIE', '', '喜马拉雅 Cookie（用于下载付费专辑，需含 1&_token）', true),
    ('ENABLE_DEEPFILTER', 'true', '是否启用 DeepFilter 降噪（Colab 端使用）', false),
    ('DEEPFILTER_MODEL', 'DeepFilterNet2', 'DeepFilter 降噪模型。可选: DeepFilterNet2(v2, 推荐速度快), DeepFilterNet3(v3, 质量最高), GTCRN(超轻量最快, 16kHz)', false),
    ('DEEPFILTER_SEGMENT_MINUTES', '60', 'DeepFilter 音频分片时长（分钟）', false),
    ('DOWNLOAD_INTERVAL', '1.5', '下载间隔秒数', false),
    ('TG_SERIAL_UPLOAD', 'true', '是否串行上传到TG（避免限流）', false),
    ('TG_UPLOAD_INTERVAL', '3', 'TG上传间隔秒数', false),
    ('PROXY_ENABLED', 'false', '是否启用代理（采集和下载均通过代理）', false),
    ('PROXY_LIST', '', '代理列表（手动填写）。每行一个，格式 http://ip:port 或 socks5://ip:port。留空则自动从下方URL获取', false),
    ('PROXY_LIST_URL', 'https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text&country=cn,https://raw.githubusercontent.com/HankNovic/ProxyClean/refs/heads/main/SOCKS5.txt', '自动获取代理列表的URL（当 PROXY_LIST 为空时使用）。多个URL用英文逗号分隔', false),
    ('PROXY_VERIFY_COUNTRY', '中国', '验证代理所在国家（通过 ip-api.com 检测）。填国家名如 中国/China，留空则不验证国家', false),
    ('PROXY_MAX_TESTS', '100', '自动发现时代理最大测试数量', false),
    ('PROXY_REFRESH_HOURS', '2', '定时自动刷新代理池的间隔（小时）。每隔此时长从URL获取新代理去重合并', false),
    ('PROXY_VERIFIED_CACHE', '', '系统内部使用：已验证代理列表（JSON，永久保存，失效即删）。请勿手动修改', false),
    ('PROXY_TEST_URL', 'https://www.ximalaya.com', '代理健康检测URL', false),
    ('PROXY_DEAD_RETRY_MINUTES', '5', '死亡代理重试间隔（分钟）', false),
    ('PROXY_TIMEOUT', '10', '代理请求超时秒数', false),
    ('XM_AUDIO_QUALITY', 'M4A_24', '下载音质优先级（从左到右依次尝试）。可选值：M4A_24, MP3_32, MP3_64, M4A_64, M4A_128。多个用逗号分隔，如 M4A_24,MP3_32,M4A_128', false),
    -- B2 云备份配置
    ('B2_ENDPOINT', '', 'Backblaze B2 S3 Endpoint (如 s3.us-west-004.backblazeb2.com)', false),
    ('B2_ACCESS_KEY_ID', '', 'B2 App Key ID', true),
    ('B2_SECRET_ACCESS_KEY', '', 'B2 App Key (applicationKey)', true),
    ('B2_BUCKET', 'xm-backups', 'B2 Bucket 名称', false),
    ('VPS_HOST', '117.55.234.219', 'VPS SSH 主机地址', false),
    ('VPS_PORT', '22', 'VPS SSH 端口', false),
    ('VPS_USER', 'root', 'VPS SSH 用户名', false),
    ('VPS_PASS', '', 'VPS SSH 密码', true),
    ('BACKUP_KEEP', '7', '保留备份数量', false),
    ('BACKUP_INTERVAL_HOURS', '24', '定时备份间隔（小时）', false)
ON CONFLICT (setting_key) DO NOTHING;
