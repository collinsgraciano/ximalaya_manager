"""任务队列管理 — 创建/认领/完成/失败。"""

from __future__ import annotations

import logging
from psycopg import sql
from psycopg.types.json import Jsonb

from ..database import fetch_one, fetch_all, execute, execute_returning, fetch_val

logger = logging.getLogger(__name__)

# 超时任务自动回收阈值：心跳 30s 一次，90s 没心跳 = worker 已死
STALE_JOB_HEARTBEAT_SECONDS = 90
# 兜底：认领超过此分钟数无论如何回收（防止 worker_stats 表丢失记录）
STALE_JOB_FALLBACK_MINUTES = 60


# ═══════════════════════════════════════════════════════════
# 创建任务
# ═══════════════════════════════════════════════════════════

def create_job(book_id: str, job_type: str = "process_album") -> dict | None:
    """为专辑创建处理任务。"""
    # 获取专辑信息
    book = fetch_one(
        "SELECT book_id, book_name, total_chapters FROM public.books WHERE book_id = %s",
        (book_id,),
    )
    if not book:
        return None

    # 获取章节数
    chapter_count = fetch_one(
        "SELECT COUNT(*) as cnt FROM public.audiobook_chapters WHERE book_id = %s AND upload_status = 'pending'",
        (book_id,),
    )
    total_chapters = int((chapter_count or {}).get("cnt", 0))
    if total_chapters == 0:
        total_chapters = book.get("total_chapters", 0)

    return execute_returning(
        sql.SQL("""
            INSERT INTO public.xm_jobs (job_type, book_id, book_name, status, total_chapters, created_at)
            VALUES (%s, %s, %s, 'pending', %s, now())
            RETURNING *
        """),
        (job_type, book_id, book.get("book_name", ""), total_chapters),
    )


def create_jobs_batch(book_ids: list[str]) -> list[dict]:
    """批量为多个专辑创建任务。"""
    results = []
    for book_id in book_ids:
        job = create_job(book_id)
        if job:
            results.append(job)
    return results


def create_jobs_for_all_pending() -> list[dict]:
    """为未完成、不在任务队列且已获取章节的专辑创建任务。"""
    # 筛选条件：
    # 1. book_status != 'success'（未完成）
    # 2. 已有章节记录（已获取章节信息）
    # 3. 有 pending 章节
    # 4. 没有 pending/processing 任务（不在任务队列）
    rows = fetch_all(
        sql.SQL("""
            SELECT DISTINCT b.book_id
            FROM public.books b
            WHERE b.book_status != 'success'
              AND EXISTS (
                  SELECT 1 FROM public.audiobook_chapters c
                  WHERE c.book_id = b.book_id
              )
              AND EXISTS (
                  SELECT 1 FROM public.audiobook_chapters c
                  WHERE c.book_id = b.book_id AND c.upload_status = 'pending'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM public.xm_jobs j
                  WHERE j.book_id = b.book_id
                    AND j.status IN ('pending', 'processing')
              )
            ORDER BY b.book_id
        """),
    )

    results = []
    for row in (rows or []):
        job = create_job(row["book_id"])
        if job:
            results.append(job)

    return results


# ═══════════════════════════════════════════════════════════
# 超时任务回收
# ═══════════════════════════════════════════════════════════

def _reset_stale_jobs():
    """将超时的 processing 任务重置为 pending。

    判断依据：worker 心跳超时（last_seen_at 超过 90s）或认领超过 60 分钟（兜底）。
    """
    execute(
        sql.SQL("""
            UPDATE public.xm_jobs
            SET status = 'pending', worker_id = NULL, claimed_at = NULL
            WHERE status = 'processing'
              AND (
                -- worker 心跳超时（主判断）
                NOT EXISTS (
                    SELECT 1 FROM public.xm_worker_stats s
                    WHERE s.worker_id = xm_jobs.worker_id
                      AND s.last_seen_at > now() - make_interval(secs => %s)
                )
                -- 兜底：认领超过 60 分钟无论如何回收
                OR claimed_at < now() - make_interval(mins => %s)
              )
        """),
        (STALE_JOB_HEARTBEAT_SECONDS, STALE_JOB_FALLBACK_MINUTES),
    )


# ═══════════════════════════════════════════════════════════
# 认领任务（FOR UPDATE SKIP LOCKED）
# ═══════════════════════════════════════════════════════════

def claim_job(worker_id: str) -> dict | None:
    """原子认领一个待处理任务。

    优先找回自己之前未完成的 processing 任务（worker 重启场景）。
    否则用 FOR UPDATE SKIP LOCKED 认领一个新的 pending 任务。
    """
    # 先回收超时任务
    _reset_stale_jobs()

    # 优先找回自己 processing 的任务（worker 重启后恢复）
    job = fetch_one(
        sql.SQL("""
            SELECT * FROM public.xm_jobs
            WHERE worker_id = %s AND status = 'processing'
            ORDER BY claimed_at DESC
            LIMIT 1
        """),
        (worker_id,),
    )
    reclaimed = False

    if not job:
        # 认领新的 pending 任务（无重试次数限制）
        job = execute_returning(
            sql.SQL("""
                UPDATE public.xm_jobs
                SET status = 'processing', worker_id = %s, claimed_at = now()
                WHERE job_id IN (
                    SELECT job_id FROM public.xm_jobs
                    WHERE status = 'pending'
                    ORDER BY created_at
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
            """),
            (worker_id,),
        )

    if not job:
        return None

    if reclaimed or job.get("worker_id") == worker_id:
        reclaimed = True

    # 获取该专辑的待处理章节列表
    book_id = job["book_id"]
    chapters = fetch_all(
        sql.SQL("""
            SELECT chapter_id, chapter_name, audio_url, chapter_order, duration
            FROM public.audiobook_chapters
            WHERE book_id = %s AND upload_status = 'pending'
            ORDER BY chapter_order
        """),
        (book_id,),
    )

    # 认领章节（标记 worker_id + claimed_at）
    if chapters:
        execute(
            sql.SQL("""
                UPDATE public.audiobook_chapters
                SET worker_id = %s, claimed_at = now()
                WHERE book_id = %s AND upload_status = 'pending'
            """),
            (worker_id, book_id),
        )

    job["chapters"] = chapters or []
    job["reclaimed"] = reclaimed
    return job


# ═══════════════════════════════════════════════════════════
# 章节进度上报
# ═══════════════════════════════════════════════════════════

def update_chapter_result(
    job_id: int,
    chapter_id: str,
    upload_status: str,
    telegram_file_id: str = "",
    telegram_message_id: int = 0,
    telegram_bot_id: int | None = None,
    telegram_bot_user_id: int | None = None,
    error_message: str = "",
    original_telegram_file_id: str = "",
    original_telegram_message_id: int = 0,
    original_telegram_bot_id: int | None = None,
    original_telegram_bot_user_id: int | None = None,
) -> dict:
    """更新章节处理结果。"""
    # 获取 job 的 book_id
    job = fetch_one("SELECT book_id FROM public.xm_jobs WHERE job_id = %s", (job_id,))
    if not job:
        return {"ok": False, "error": "任务不存在"}

    book_id = job["book_id"]

    # 更新章节状态
    update_data = {
        "upload_status": upload_status,
        "error_message": error_message,
    }
    if telegram_file_id:
        update_data["telegram_file_id"] = telegram_file_id
    if telegram_message_id:
        update_data["telegram_message_id"] = telegram_message_id
    if telegram_bot_id is not None:
        update_data["telegram_bot_id"] = telegram_bot_id
    if telegram_bot_user_id is not None:
        update_data["telegram_bot_user_id"] = telegram_bot_user_id
    # 原始音频 TG 缓存
    if original_telegram_file_id:
        update_data["original_telegram_file_id"] = original_telegram_file_id
    if original_telegram_message_id:
        update_data["original_telegram_message_id"] = original_telegram_message_id
    if original_telegram_bot_id is not None:
        update_data["original_telegram_bot_id"] = original_telegram_bot_id
    if original_telegram_bot_user_id is not None:
        update_data["original_telegram_bot_user_id"] = original_telegram_bot_user_id
    if upload_status == "uploaded":
        from datetime import datetime
        update_data["uploaded_at"] = datetime.now().isoformat()
    else:
        # 失败回退为 pending：清除 worker 归属，使下次可被重新认领
        update_data["worker_id"] = None
        update_data["claimed_at"] = None

    set_parts = sql.SQL(", ").join(
        sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder())
        for k in update_data.keys()
    )
    execute(
        sql.SQL("UPDATE public.audiobook_chapters SET {} WHERE book_id = %s AND chapter_id = %s").format(set_parts),
        tuple(list(update_data.values()) + [book_id, str(chapter_id)]),
    )

    # 更新 job 的 done_chapters 计数（仅统计已上传的）
    if upload_status == "uploaded":
        execute(
            sql.SQL("""
                UPDATE public.xm_jobs
                SET done_chapters = (
                    SELECT COUNT(*) FROM public.audiobook_chapters
                    WHERE book_id = %s AND upload_status = 'uploaded'
                )
                WHERE job_id = %s
            """),
            (book_id, job_id),
        )

    # 检查是否全部完成
    remaining = fetch_one(
        "SELECT COUNT(*) as cnt FROM public.audiobook_chapters WHERE book_id = %s AND upload_status = 'pending'",
        (book_id,),
    )
    remaining_count = int((remaining or {}).get("cnt", 0))

    return {
        "ok": True,
        "chapter_id": chapter_id,
        "upload_status": upload_status,
        "remaining": remaining_count,
    }


# ═══════════════════════════════════════════════════════════
# 任务完成/失败
# ═══════════════════════════════════════════════════════════

def complete_job(job_id: int, result: dict | None = None) -> dict:
    """标记任务完成。

    - 全部章节已上传 → status='done', book_status='success'
    - 仍有 pending → 重新入队 (status='pending', retry_count++)，无限重试直到全部完成
    """
    job = fetch_one("SELECT book_id, retry_count FROM public.xm_jobs WHERE job_id = %s", (job_id,))
    if not job:
        return {"ok": False, "error": "任务不存在"}

    book_id = job["book_id"]
    retry_count = int(job.get("retry_count", 0))

    # 检查是否还有 pending 章节
    remaining = fetch_val(
        "SELECT COUNT(*) FROM public.audiobook_chapters WHERE book_id = %s AND upload_status = 'pending'",
        (book_id,),
    )
    remaining = int(remaining or 0)

    if remaining > 0:
        # 重新入队，无限重试直到全部完成
        execute(
            sql.SQL("""
                UPDATE public.xm_jobs
                SET status = 'pending', worker_id = NULL, claimed_at = NULL,
                    retry_count = retry_count + 1, result = %s
                WHERE job_id = %s
            """),
            (Jsonb(result) if result else None, job_id),
        )
        # 释放 pending 章节的 worker 归属
        execute(
            sql.SQL("""
                UPDATE public.audiobook_chapters
                SET worker_id = NULL, claimed_at = NULL
                WHERE book_id = %s AND upload_status = 'pending'
            """),
            (book_id,),
        )
        execute(
            sql.SQL("UPDATE public.books SET book_status = 'pending', updated_at = now() WHERE book_id = %s"),
            (book_id,),
        )
        logger.info(f"任务 #{job_id} 还有 {remaining} 个 pending 章节，重新入队 (retry={retry_count + 1})")
        return {"ok": True, "job_id": job_id, "requeued": True, "remaining": remaining,
                "retry_count": retry_count + 1}

    # 全部完成 → 标记 done, success
    execute(
        sql.SQL("""
            UPDATE public.xm_jobs
            SET status = 'done', finished_at = now(), result = %s
            WHERE job_id = %s
        """),
        (Jsonb(result) if result else None, job_id),
    )
    execute(
        sql.SQL("UPDATE public.books SET book_status = 'success', updated_at = now() WHERE book_id = %s"),
        (book_id,),
    )

    # 更新 Worker 统计
    job_row = fetch_one("SELECT worker_id FROM public.xm_jobs WHERE job_id = %s", (job_id,))
    if job_row and job_row.get("worker_id"):
        _update_worker_stats(job_row["worker_id"], success=True, chapters=0)

    return {"ok": True, "job_id": job_id, "requeued": False,
            "book_status": "success", "remaining": 0}


def fail_job(job_id: int, error_message: str) -> dict:
    """标记任务失败。"""
    execute(
        sql.SQL("""
            UPDATE public.xm_jobs
            SET status = 'failed', finished_at = now(), error_message = %s,
                retry_count = retry_count + 1
            WHERE job_id = %s
        """),
        (error_message, job_id),
    )

    # 更新 books.book_status
    job = fetch_one("SELECT book_id FROM public.xm_jobs WHERE job_id = %s", (job_id,))
    if job:
        execute(
            sql.SQL("UPDATE public.books SET book_status = 'failed', updated_at = now() WHERE book_id = %s"),
            (job["book_id"],),
        )
        # 重置未完成的章节状态
        execute(
            sql.SQL("""
                UPDATE public.audiobook_chapters
                SET upload_status = 'pending', worker_id = NULL, claimed_at = NULL
                WHERE book_id = %s AND upload_status != 'uploaded'
            """),
            (job["book_id"],),
        )

    # 更新 Worker 统计
    job_row = fetch_one("SELECT worker_id FROM public.xm_jobs WHERE job_id = %s", (job_id,))
    if job_row and job_row.get("worker_id"):
        _update_worker_stats(job_row["worker_id"], success=False, chapters=0)

    return {"ok": True, "job_id": job_id, "error": error_message}


# ═══════════════════════════════════════════════════════════
# Worker 优雅退出释放任务
# ═══════════════════════════════════════════════════════════

def release_job(worker_id: str) -> dict:
    """Worker 退出时释放自己 processing 的任务回 pending。

    保留已上传的章节，未完成的章节重置为 pending。
    """
    jobs = fetch_all(
        sql.SQL("""
            SELECT job_id, book_id FROM public.xm_jobs
            WHERE worker_id = %s AND status = 'processing'
        """),
        (worker_id,),
    )

    for job in (jobs or []):
        execute(
            sql.SQL("""
                UPDATE public.xm_jobs
                SET status = 'pending', worker_id = NULL, claimed_at = NULL
                WHERE job_id = %s
            """),
            (job["job_id"],),
        )
        execute(
            sql.SQL("""
                UPDATE public.audiobook_chapters
                SET upload_status = 'pending', worker_id = NULL, claimed_at = NULL
                WHERE book_id = %s AND upload_status != 'uploaded'
            """),
            (job["book_id"],),
        )
        logger.info(f"任务 #{job['job_id']} 已由 {worker_id} 释放")

    count = len(jobs or [])
    return {"ok": True, "released": count}


# ═══════════════════════════════════════════════════════════
# 手动重置任务
# ═══════════════════════════════════════════════════════════

def reset_job(job_id: int) -> dict:
    """手动重置任务为 pending，将未完成的章节重置为 pending。

    用于处理卡在 processing 状态的任务（worker 崩溃/断线）。
    """
    job = fetch_one("SELECT book_id, retry_count FROM public.xm_jobs WHERE job_id = %s", (job_id,))
    if not job:
        return {"ok": False, "error": "任务不存在"}

    book_id = job["book_id"]

    # 重置任务状态（递增 retry_count）
    execute(
        sql.SQL("""
            UPDATE public.xm_jobs
            SET status = 'pending', worker_id = NULL, claimed_at = NULL,
                finished_at = NULL, error_message = NULL,
                retry_count = retry_count + 1
            WHERE job_id = %s
        """),
        (job_id,),
    )

    # 重置未完成的章节（保留已上传的）
    execute(
        sql.SQL("""
            UPDATE public.audiobook_chapters
            SET upload_status = 'pending'
            WHERE book_id = %s AND upload_status != 'uploaded'
        """),
        (book_id,),
    )

    return {"ok": True, "job_id": job_id}


# ═══════════════════════════════════════════════════════════
# Worker 统计
# ═══════════════════════════════════════════════════════════

def _update_worker_stats(worker_id: str, success: bool, chapters: int = 0):
    """更新 Worker 业绩统计。"""
    from datetime import datetime
    execute(
        sql.SQL("""
            INSERT INTO public.xm_worker_stats
                (worker_id, total_jobs, success_jobs, failed_jobs, total_chapters, last_job_at, last_seen_at, updated_at)
            VALUES (%s, 1, %s, %s, %s, now(), now(), now())
            ON CONFLICT (worker_id) DO UPDATE SET
                total_jobs = public.xm_worker_stats.total_jobs + 1,
                success_jobs = public.xm_worker_stats.success_jobs + %s,
                failed_jobs = public.xm_worker_stats.failed_jobs + %s,
                total_chapters = public.xm_worker_stats.total_chapters + %s,
                last_job_at = now(),
                last_seen_at = now(),
                updated_at = now()
        """),
        (worker_id,
         1 if success else 0, 0 if success else 1, chapters,
         1 if success else 0, 0 if success else 1, chapters),
    )


def heartbeat(worker_id: str) -> dict:
    """Worker 心跳，更新 last_seen_at。"""
    execute(
        sql.SQL("""
            INSERT INTO public.xm_worker_stats (worker_id, last_seen_at, updated_at)
            VALUES (%s, now(), now())
            ON CONFLICT (worker_id) DO UPDATE SET
                last_seen_at = now(),
                updated_at = now()
        """),
        (worker_id,),
    )
    return {"ok": True, "worker_id": worker_id}


def add_chapter_to_worker(worker_id: str, count: int = 1):
    """累加 Worker 处理章节数。"""
    execute(
        sql.SQL("""
            UPDATE public.xm_worker_stats
            SET total_chapters = total_chapters + %s
            WHERE worker_id = %s
        """),
        (count, worker_id),
    )


# ═══════════════════════════════════════════════════════════
# 查询
# ═══════════════════════════════════════════════════════════

def get_jobs(page: int = 1, page_size: int = 20, status: str = "") -> dict:
    """分页查询任务列表。"""
    conditions = []
    params: list = []

    if status:
        conditions.append("status = %s")
        params.append(status)

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    total = fetch_val(
        f"SELECT COUNT(*) FROM public.xm_jobs{where_clause}",
        tuple(params) if params else None,
    )
    total = int(total or 0)

    offset = (page - 1) * page_size
    rows = fetch_all(
        sql.SQL("SELECT * FROM public.xm_jobs{} ORDER BY created_at DESC LIMIT %s OFFSET %s").format(
            sql.SQL(where_clause)
        ),
        tuple(params + [page_size, offset]),
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "jobs": rows or [],
    }


def get_job_detail(job_id: int) -> dict | None:
    """获取任务详情。"""
    return fetch_one("SELECT * FROM public.xm_jobs WHERE job_id = %s", (job_id,))


def get_worker_stats() -> list[dict]:
    """获取所有 Worker 统计。"""
    return fetch_all(
        "SELECT * FROM public.xm_worker_stats ORDER BY updated_at DESC"
    ) or []


def get_pending_jobs_count() -> int:
    """获取待处理任务数。"""
    result = fetch_one("SELECT COUNT(*) as cnt FROM public.xm_jobs WHERE status = 'pending'")
    return int((result or {}).get("cnt", 0))


def delete_job(job_id: int) -> dict:
    """删除任务（不删除专辑和已上传的章节）。"""
    job = fetch_one("SELECT book_id FROM public.xm_jobs WHERE job_id = %s", (job_id,))
    if not job:
        return {"ok": False, "error": "任务不存在"}

    execute(
        sql.SQL("DELETE FROM public.xm_jobs WHERE job_id = %s"),
        (job_id,),
    )
    # 清除章节的 worker 归属（保留已上传的章节状态）
    execute(
        sql.SQL("""
            UPDATE public.audiobook_chapters
            SET worker_id = NULL, claimed_at = NULL
            WHERE book_id = %s AND upload_status != 'uploaded'
        """),
        (job["book_id"],),
    )
    return {"ok": True, "job_id": job_id}


def delete_jobs_batch(job_ids: list[int]) -> dict:
    """批量删除任务。"""
    if not job_ids:
        return {"ok": True, "deleted": 0}
    rows = fetch_all(
        sql.SQL("SELECT DISTINCT book_id FROM public.xm_jobs WHERE job_id = ANY(%s)"),
        (job_ids,),
    )
    execute(
        sql.SQL("DELETE FROM public.xm_jobs WHERE job_id = ANY(%s)"),
        (job_ids,),
    )
    for row in (rows or []):
        execute(
            sql.SQL("""
                UPDATE public.audiobook_chapters
                SET worker_id = NULL, claimed_at = NULL
                WHERE book_id = %s AND upload_status != 'uploaded'
            """),
            (row["book_id"],),
        )
    return {"ok": True, "deleted": len(job_ids)}


def reset_jobs_batch(job_ids: list[int]) -> dict:
    """批量重置任务为 pending。"""
    if not job_ids:
        return {"ok": True, "reset": 0}
    rows = fetch_all(
        sql.SQL("SELECT job_id, book_id FROM public.xm_jobs WHERE job_id = ANY(%s)"),
        (job_ids,),
    )
    for row in (rows or []):
        execute(
            sql.SQL("""
                UPDATE public.xm_jobs
                SET status = 'pending', worker_id = NULL, claimed_at = NULL,
                    finished_at = NULL, error_message = NULL,
                    retry_count = retry_count + 1
                WHERE job_id = %s
            """),
            (row["job_id"],),
        )
        execute(
            sql.SQL("""
                UPDATE public.audiobook_chapters
                SET upload_status = 'pending', worker_id = NULL, claimed_at = NULL
                WHERE book_id = %s AND upload_status != 'uploaded'
            """),
            (row["book_id"],),
        )
    return {"ok": True, "reset": len(job_ids)}


def delete_jobs_by_status(status: str) -> dict:
    """按状态删除所有匹配的任务。"""
    rows = fetch_all(
        sql.SQL("SELECT job_id FROM public.xm_jobs WHERE status = %s"),
        (status,),
    )
    job_ids = [r["job_id"] for r in (rows or [])]
    if not job_ids:
        return {"ok": True, "deleted": 0}
    return delete_jobs_batch(job_ids)
