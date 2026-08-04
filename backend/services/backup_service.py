"""B2 云备份服务 — 从 DB 读取配置，后台线程执行备份/恢复。

备份/恢复操作在后台线程中运行，通过模块级状态变量轮询进度。
定时备份使用独立线程 + threading.Event 控制。

Web 容器与 PostgreSQL 容器在同一 Docker 网络，直接用 subprocess 调 pg_dump/psql，
配置文件通过 Docker volume 挂载直接读写，无需 SSH。
"""

from __future__ import annotations

import sys
import os
import io
import time
import gzip
import shutil
import subprocess
import threading
import logging
from datetime import datetime, timedelta
from pathlib import Path

from ..database import fetch_one, fetch_all, execute
from psycopg import sql as pg_sql

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════
# 备份配置键（存于 global_settings 表）
# ═══════════════════════════════════════════

BACKUP_CONFIG_KEYS = [
    ("B2_ENDPOINT",          "Backblaze B2 S3 Endpoint (如 s3.us-west-004.backblazeb2.com)", False),
    ("B2_ACCESS_KEY_ID",     "B2 App Key ID",                                                True),
    ("B2_SECRET_ACCESS_KEY", "B2 App Key (applicationKey)",                                  True),
    ("B2_BUCKET",            "B2 Bucket 名称",                                               False),
    ("BACKUP_KEEP",          "保留备份数量",                                                 False),
    ("BACKUP_INTERVAL_HOURS","定时备份间隔 (小时)",                                          False),
]

# 默认值
BACKUP_CONFIG_DEFAULTS = {
    "B2_ENDPOINT": "",
    "B2_ACCESS_KEY_ID": "",
    "B2_SECRET_ACCESS_KEY": "",
    "B2_BUCKET": "xm-backups",
    "BACKUP_KEEP": "7",
    "BACKUP_INTERVAL_HOURS": "24",
}

# ═══════════════════════════════════════════
# 状态管理
# ═══════════════════════════════════════════

_backup_lock = threading.Lock()
_backup_running = False
_backup_progress = ""       # 当前进度描述
_backup_result: dict | None = None  # 最后一次备份结果

_restore_lock = threading.Lock()
_restore_running = False
_restore_progress = ""
_restore_result: dict | None = None

# 定时备份
_schedule_thread: threading.Thread | None = None
_schedule_stop_event = threading.Event()
_schedule_interval = 24.0   # 小时
_schedule_last_run: str | None = None
_schedule_next_run: str | None = None


# ═══════════════════════════════════════════
# 配置读写
# ═══════════════════════════════════════════

def get_backup_config() -> dict:
    """从 DB 读取备份配置，返回 {key: value} 字典。"""
    result = {}
    for key, desc, is_secret in BACKUP_CONFIG_KEYS:
        row = fetch_one(
            "SELECT setting_value FROM public.global_settings WHERE setting_key = %s",
            (key,),
        )
        val = (row or {}).get("setting_value", "") if row else ""
        if not val:
            val = BACKUP_CONFIG_DEFAULTS.get(key, "")
        # secret 字段如果已配置则返回 ****** 给前端
        if is_secret and val:
            result[key] = "******"
            result[f"_{key}_configured"] = True
        else:
            result[key] = val
        result[f"_{key}_desc"] = desc
        result[f"_{key}_secret"] = is_secret
    return result


def save_backup_config(config: dict) -> dict:
    """保存备份配置到 DB。

    值为 "******" 的 secret 字段跳过不修改。
    """
    saved = []
    for key, desc, is_secret in BACKUP_CONFIG_KEYS:
        val = config.get(key, "")
        if val == "******":
            continue  # 不修改已存在的 secret
        if not val:
            continue  # 空值跳过
        # 检查是否已存在
        existing = fetch_one(
            "SELECT setting_key FROM public.global_settings WHERE setting_key = %s",
            (key,),
        )
        if existing:
            execute(
                "UPDATE public.global_settings SET setting_value = %s, updated_at = now() WHERE setting_key = %s",
                (val, key),
            )
        else:
            execute(
                pg_sql.SQL("""
                    INSERT INTO public.global_settings (setting_key, setting_value, description, is_secret, updated_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (setting_key) DO UPDATE SET
                        setting_value = EXCLUDED.setting_value,
                        updated_at = now()
                """),
                (key, val, desc, is_secret),
            )
        saved.append(key)

    return {"ok": True, "saved": saved}


def _build_runtime_config() -> dict:
    """从 DB 构建运行时配置字典（用于 init_config）。"""
    config = {}
    for key, desc, is_secret in BACKUP_CONFIG_KEYS:
        row = fetch_one(
            "SELECT setting_value FROM public.global_settings WHERE setting_key = %s",
            (key,),
        )
        val = (row or {}).get("setting_value", "") if row else ""
        if not val:
            val = BACKUP_CONFIG_DEFAULTS.get(key, "")
        config[key] = val
    return config


def _init_common_config():
    """从 DB 加载配置并注入到 common 模块。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "supabase_backup"))
    import common
    config = _build_runtime_config()
    common.init_config(config)
    return common


# ═══════════════════════════════════════════
# 本地操作（Web 容器内直接执行，无需 SSH）
# ═══════════════════════════════════════════

# 数据库连接信息（从 DATABASE_URL 解析，与 Web 应用使用同一连接）
from urllib.parse import urlparse as _urlparse
_dsn = os.environ.get("DATABASE_URL", "")
if _dsn.startswith("postgresql+psycopg://"):
    _dsn = _dsn.replace("postgresql+psycopg://", "postgresql://", 1)
_parsed = _urlparse(_dsn)
DB_HOST = _parsed.hostname or "postgres"
DB_PORT = str(_parsed.port or 5432)
DB_USER = _parsed.username or "xm_app"
DB_PASSWORD = _parsed.password or ""
DB_NAME = _parsed.path.lstrip("/") or "ximalaya"

# pg_dump / psql 需要 PGPASSWORD 环境变量
_pg_env = {**os.environ, "PGPASSWORD": DB_PASSWORD}

# 配置文件路径（通过 Docker volume 挂载）
ENV_FILE = "/app/.env"
COMPOSE_FILE = "/app/docker-compose.yml"
# backups volume 挂载点，用于临时文件和恢复的配置文件
TMP_DIR = Path("/app/backups")
TMP_DIR.mkdir(parents=True, exist_ok=True)


def _pg_dump_to_file(filepath: str) -> int:
    """直接在 Web 容器内执行 pg_dump，输出到文件。返回文件大小（字节）。"""
    cmd = [
        "pg_dump",
        "-h", DB_HOST,
        "-p", DB_PORT,
        "-U", DB_USER,
        "-d", DB_NAME,
        "--no-owner", "--no-privileges",
        "--clean", "--if-exists",
        "-f", filepath,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, env=_pg_env)
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump 失败: {result.stderr[:300]}")
    return os.path.getsize(filepath)


def _psql_restore(filepath: str) -> tuple[int, str, str]:
    """直接在 Web 容器内执行 psql 恢复。返回 (exit_code, stdout, stderr)。"""
    with open(filepath, "rb") as f:
        result = subprocess.run(
            ["psql", "-h", DB_HOST, "-p", DB_PORT, "-U", DB_USER, "-d", DB_NAME],
            stdin=f,
            capture_output=True,
            timeout=3600,
            env=_pg_env,
        )
    return result.returncode, result.stdout.decode("utf-8", errors="replace"), result.stderr.decode("utf-8", errors="replace")


def _count_table(table: str) -> int:
    """查询表行数。"""
    row = fetch_one(f"SELECT count(*) AS cnt FROM {table}")
    return int((row or {}).get("cnt", 0)) if row else 0


# ═══════════════════════════════════════════
# 备份状态
# ═══════════════════════════════════════════

def get_backup_status() -> dict:
    """获取备份操作状态。"""
    return {
        "running": _backup_running,
        "progress": _backup_progress,
        "result": _backup_result,
    }


def get_restore_status() -> dict:
    """获取恢复操作状态。"""
    return {
        "running": _restore_running,
        "progress": _restore_progress,
        "result": _restore_result,
    }


# ═══════════════════════════════════════════
# 备份执行（后台线程）
# ═══════════════════════════════════════════

def run_backup_async():
    """启动后台备份线程。如果已有备份在运行则返回错误。"""
    global _backup_running, _backup_progress, _backup_result

    with _backup_lock:
        if _backup_running:
            return {"ok": False, "error": "已有备份任务在运行中"}

        _backup_running = True
        _backup_progress = "初始化..."
        _backup_result = None

    thread = threading.Thread(target=_backup_worker, daemon=True)
    thread.start()
    return {"ok": True, "message": "备份已启动"}


def _backup_worker():
    """备份工作线程。直接在 Web 容器内执行，无需 SSH。"""
    global _backup_running, _backup_progress, _backup_result

    try:
        common = _init_common_config()

        if not common.check_b2_config():
            _backup_result = {"ok": False, "error": "B2 配置不完整"}
            return

        _backup_progress = "检查 B2 Bucket..."
        if not common.ensure_bucket():
            _backup_result = {"ok": False, "error": "B2 Bucket 不可访问"}
            return

        ts = common.timestamp_str()

        # pg_dump 直接导出到本地持久化目录
        _backup_progress = "导出数据库 (pg_dump)..."
        dump_path = str(TMP_DIR / "xm_dump.sql")
        dump_size = _pg_dump_to_file(dump_path)

        # 超过 1GB 则压缩（B2 免费版每日下载 1GB 限额）
        COMPRESS_THRESHOLD = 1024 * 1024 * 1024  # 1GB
        if dump_size > COMPRESS_THRESHOLD:
            _backup_progress = f"dump {common.format_size(dump_size)} > 1GB，压缩中..."
            with open(dump_path, "rb") as f_in:
                with gzip.open(dump_path + ".gz", "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            os.remove(dump_path)
            final_path = dump_path + ".gz"
            b2_name = "db_dump.sql.gz"
            b2_ct = "application/gzip"
        else:
            final_path = dump_path
            b2_name = "db_dump.sql"
            b2_ct = "application/sql"

        final_size = os.path.getsize(final_path)

        # 读取 dump 文件
        _backup_progress = f"读取 dump ({common.format_size(final_size)})..."
        with open(final_path, "rb") as f:
            dump_data = f.read()
        os.remove(final_path)

        # 读取配置文件（通过 Docker volume 挂载）
        _backup_progress = "读取配置文件..."
        config_files = {}
        if os.path.exists(ENV_FILE):
            with open(ENV_FILE, "rb") as f:
                config_files[".env"] = f.read()
        if os.path.exists(COMPOSE_FILE):
            with open(COMPOSE_FILE, "rb") as f:
                config_files["docker-compose.yml"] = f.read()

        # 上传到 B2
        _backup_progress = f"上传到 B2 (dump {common.format_size(len(dump_data))})..."
        storage_prefix = ts
        uploaded_files = []
        all_ok = True

        if common.upload_to_b2(f"{storage_prefix}/{b2_name}", dump_data, b2_ct):
            uploaded_files.append({"name": b2_name, "size": len(dump_data)})
        else:
            all_ok = False

        for name, data in config_files.items():
            ct = "text/plain" if name == ".env" else "text/yaml"
            if common.upload_to_b2(f"{storage_prefix}/{name}", data, ct):
                uploaded_files.append({"name": name, "size": len(data)})
            else:
                all_ok = False

        # 清理旧备份
        backup_keep = common.get_backup_keep()
        _backup_progress = f"清理旧备份 (保留 {backup_keep} 份)..."
        folders = common.list_backup_folders()
        deleted = []
        if len(folders) > backup_keep:
            for folder in folders[backup_keep:]:
                common.delete_backup_folder(folder)
                deleted.append(folder)

        _backup_result = {
            "ok": all_ok,
            "timestamp": ts,
            "files": uploaded_files,
            "deleted_old": deleted,
            "total_size": sum(f["size"] for f in uploaded_files),
        }
        _backup_progress = "完成"

    except Exception as e:
        _backup_result = {"ok": False, "error": str(e)}
        _backup_progress = "失败"
    finally:
        _backup_running = False


# ═══════════════════════════════════════════
# 恢复执行（后台线程）
# ═══════════════════════════════════════════

def run_restore_async(folder: str, mode: str = "full"):
    """启动后台恢复线程。

    Args:
        folder: 备份文件夹名 (时间戳)，空字符串表示最新
        mode: "full" | "db" | "config"
    """
    global _restore_running, _restore_progress, _restore_result

    with _restore_lock:
        if _restore_running:
            return {"ok": False, "error": "已有恢复任务在运行中"}

        _restore_running = True
        _restore_progress = "初始化..."
        _restore_result = None

    thread = threading.Thread(target=_restore_worker, args=(folder, mode), daemon=True)
    thread.start()
    return {"ok": True, "message": "恢复已启动"}


def _restore_worker(folder: str, mode: str):
    """恢复工作线程。直接在 Web 容器内执行，无需 SSH。"""
    global _restore_running, _restore_progress, _restore_result

    try:
        common = _init_common_config()

        if not common.check_b2_config():
            _restore_result = {"ok": False, "error": "B2 配置不完整"}
            return

        # 确定备份文件夹
        if not folder:
            folders = common.list_backup_folders()
            if not folders:
                _restore_result = {"ok": False, "error": "B2 中没有备份"}
                return
            folder = folders[0]

        _restore_progress = f"从 B2 下载备份 {folder}..."
        files = common.list_backup_files(folder)
        if not files:
            _restore_result = {"ok": False, "error": f"备份 {folder} 中没有文件"}
            return

        dump_data = None
        dump_is_gz = False
        env_data = None
        compose_data = None

        for fname in files:
            data = common.download_from_b2(f"{folder}/{fname}")
            if data is None:
                continue
            if fname == "db_dump.sql":
                dump_data = data
            elif fname == "db_dump.sql.gz":
                dump_data = data
                dump_is_gz = True
            elif fname == ".env":
                env_data = data
            elif fname == "docker-compose.yml":
                compose_data = data

        table_counts = {}
        restored_items = []

        # 恢复数据库
        if mode in ("full", "db") and dump_data:
            _restore_progress = f"写入 dump 文件 ({common.format_size(len(dump_data))})..."
            restore_path = str(TMP_DIR / "xm_restore.sql")

            if dump_is_gz:
                # 解压 gzip
                _restore_progress = "解压 dump..."
                decompressed = gzip.decompress(dump_data)
                with open(restore_path, "wb") as f:
                    f.write(decompressed)
            else:
                with open(restore_path, "wb") as f:
                    f.write(dump_data)

            _restore_progress = "执行 psql 恢复..."
            exit_code, out, err = _psql_restore(restore_path)
            os.remove(restore_path)

            # 验证表行数
            _restore_progress = "验证数据..."
            for table in ["books", "audiobook_chapters", "global_settings", "xm_jobs", "xm_worker_stats", "xm_scrape_tasks"]:
                table_counts[table] = _count_table(table)

            restored_items.append("数据库")

        # 恢复配置文件
        if mode in ("full", "config") and (env_data or compose_data):
            _restore_progress = "恢复配置文件..."
            backup_ts = common.timestamp_str()

            if env_data:
                # .env 通过 Docker volume 挂载为 ro，需要写入宿主机路径
                # 但 /app/.env 是 ro 挂载，写不了。写入 backups 目录作为备份。
                env_backup = str(TMP_DIR / f".env.restored_{backup_ts}")
                with open(env_backup, "wb") as f:
                    f.write(env_data)
                restored_items.append(f".env (已保存到 {env_backup}，需手动替换)")

            if compose_data:
                compose_backup = str(TMP_DIR / f"docker-compose.yml.restored_{backup_ts}")
                with open(compose_backup, "wb") as f:
                    f.write(compose_data)
                restored_items.append(f"docker-compose.yml (已保存到 {compose_backup}，需手动替换)")

        _restore_result = {
            "ok": True,
            "folder": folder,
            "restored": restored_items,
            "table_counts": table_counts,
        }
        _restore_progress = "完成"

    except Exception as e:
        _restore_result = {"ok": False, "error": str(e)}
        _restore_progress = "失败"
    finally:
        _restore_running = False


# ═══════════════════════════════════════════
# 列出备份
# ═══════════════════════════════════════════

def list_backups() -> list[dict]:
    """列出 B2 中的所有备份，返回结构化数据。"""
    try:
        common = _init_common_config()
        if not common.check_b2_config():
            return []

        folders = common.list_backup_folders()
        result = []
        for idx, folder in enumerate(folders):
            objs = common.list_b2_objects(prefix=f"{folder}/")
            files = []
            total_size = 0
            for obj in objs:
                fname = obj["key"].split("/")[-1]
                files.append({"name": fname, "size": obj["size"]})
                total_size += obj["size"]
            result.append({
                "folder": folder,
                "is_latest": idx == 0,
                "files": files,
                "total_size": total_size,
                "total_size_text": common.format_size(total_size),
            })
        return result
    except Exception as e:
        logger.warning(f"列出备份失败: {e}")
        return []


# ═══════════════════════════════════════════
# 定时备份
# ═══════════════════════════════════════════

def start_scheduled_backup(interval_hours: float) -> dict:
    """启动定时备份线程。"""
    global _schedule_thread, _schedule_interval, _schedule_next_run

    if _schedule_thread and _schedule_thread.is_alive():
        return {"ok": False, "error": "定时备份已在运行中"}

    _schedule_stop_event.clear()
    _schedule_interval = interval_hours

    # 持久化到 DB（重启后自动恢复）
    _save_schedule_state(True, interval_hours)

    from datetime import timedelta
    next_dt = datetime.now() + timedelta(hours=interval_hours)
    _schedule_next_run = next_dt.strftime("%Y-%m-%d %H:%M:%S")

    _schedule_thread = threading.Thread(target=_schedule_worker, args=(interval_hours,), daemon=True)
    _schedule_thread.start()

    logger.info(f"定时备份已启动，间隔 {interval_hours} 小时，首次将在 {next_dt.strftime('%H:%M')} 执行")
    return {"ok": True, "message": f"定时备份已启动，每 {interval_hours} 小时一次，首次将在 {next_dt.strftime('%Y-%m-%d %H:%M')} 执行"}


def stop_scheduled_backup() -> dict:
    """停止定时备份线程。"""
    global _schedule_next_run

    # 持久化关闭状态
    _save_schedule_state(False, _schedule_interval)

    if not _schedule_thread or not _schedule_thread.is_alive():
        _schedule_next_run = None
        return {"ok": False, "error": "定时备份未在运行"}

    _schedule_stop_event.set()
    _schedule_next_run = None
    return {"ok": True, "message": "定时备份已停止"}


def get_schedule_status() -> dict:
    """获取定时备份状态。"""
    running = _schedule_thread is not None and _schedule_thread.is_alive()
    return {
        "running": running,
        "interval_hours": _schedule_interval,
        "last_run": _schedule_last_run,
        "next_run": _schedule_next_run,
    }


def _save_schedule_state(enabled: bool, interval_hours: float):
    """将定时备份开关状态持久化到 DB。"""
    import json
    state = json.dumps({"enabled": enabled, "interval_hours": interval_hours})
    execute(
        pg_sql.SQL("""
            INSERT INTO public.global_settings (setting_key, setting_value, description, is_secret, updated_at)
            VALUES ('BACKUP_SCHEDULE_STATE', %s, '定时备份开关状态（内部使用，JSON）', false, now())
            ON CONFLICT (setting_key) DO UPDATE SET
                setting_value = EXCLUDED.setting_value,
                updated_at = now()
        """),
        (state,),
    )


def restore_schedule_on_startup():
    """应用启动时检查 DB 中的定时备份状态，如果之前是开启的则自动恢复。

    应在 FastAPI lifespan 中调用。
    """
    global _schedule_interval

    try:
        row = fetch_one(
            "SELECT setting_value FROM public.global_settings WHERE setting_key = 'BACKUP_SCHEDULE_STATE'"
        )
        if not row:
            return

        import json
        state = json.loads(row.get("setting_value", "{}"))
        if state.get("enabled"):
            interval = float(state.get("interval_hours", 24))
            _schedule_interval = interval
            logger.info(f"恢复定时备份状态：开启，间隔 {interval} 小时")
            _schedule_stop_event.clear()
            global _schedule_thread
            from datetime import timedelta
            next_dt = datetime.now() + timedelta(hours=interval)
            global _schedule_next_run
            _schedule_next_run = next_dt.strftime("%Y-%m-%d %H:%M:%S")
            _schedule_thread = threading.Thread(target=_schedule_worker, args=(interval,), daemon=True)
            _schedule_thread.start()
    except Exception as e:
        logger.warning(f"恢复定时备份状态失败: {e}")


def _schedule_worker(interval_hours: float):
    """定时备份工作线程。先等待间隔时间，再执行备份。"""
    global _schedule_last_run, _schedule_next_run

    while not _schedule_stop_event.is_set():
        # 先等待间隔时间，再执行备份
        wait_seconds = interval_hours * 3600
        if _schedule_stop_event.wait(timeout=wait_seconds):
            break  # stop_event 被 set

        if _schedule_stop_event.is_set():
            break

        # 执行备份
        if not _backup_running:
            logger.info("定时备份触发")
            run_backup_async()

            # 等待备份完成
            while _backup_running and not _schedule_stop_event.is_set():
                _schedule_stop_event.wait(timeout=5)

            _schedule_last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 计算下次运行时间
        from datetime import timedelta
        next_dt = datetime.now() + timedelta(hours=interval_hours)
        _schedule_next_run = next_dt.strftime("%Y-%m-%d %H:%M:%S")

    logger.info("定时备份线程已退出")
