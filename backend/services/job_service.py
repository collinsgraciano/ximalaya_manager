"""任务队列管理 — 创建/认领/完成/失败。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from psycopg import sql
from psycopg.types.json import Jsonb

from ..database import fetch_one, fetch_all, execute, execute_returning, fetch_val

logger = logging.getLogger(__name__)

# 超时任务自动回收阈值：心跳 30s 一次，90s 没心跳 = worker 已死
STALE_JOB_HEARTBEAT_SECONDS = 90
# 兜底：认领超过此分钟数无论如何回收（防止 worker_stats 表丢失记录）
STALE_JOB_FALLBACK_MINUTES = 60


def _ensure_chapters(book_id: str) -> bool:
    """确保专辑已获取全部章节列表。如果章节不全则自动获取。

    使用随机代理获取章节，返回 True 表示已有完整章节（或刚获取成功），False 表示获取失败。
    """
    # 延迟导入避免循环依赖
    from .scrape_service import scrape_album_tracks, _init_proxy_pool, get_random_proxy

    book = fetch_one(
        "SELECT total_chapters FROM public.books WHERE book_id = %s",
        (book_id,),
    )
    if not book:
        return False

    expected = int(book.get("total_chapters") or 0)

    chapter_row = fetch_one(
        "SELECT COUNT(*) as cnt FROM public.audiobook_chapters WHERE book_id = %s",
        (book_id,),
    )
    actual = int((chapter_row or {}).get("cnt", 0))

    # 已有章节且数量匹配（或 books.total_chapters 为 0 但已有章节记录）
    if actual > 0 and (expected == 0 or actual >= expected):
        return True

    # 章节不全 → 随机选代理获取章节列表
    proxy_enabled, _ = _init_proxy_pool()
    proxies = get_random_proxy() if proxy_enabled else None
    logger.info(f"专辑 {book_id} 章节不全 (DB:{actual}/{expected or '?'}), 自动获取...{f' [代理]' if proxies else ' [直连]'}")
    try:
        result = scrape_album_tracks(book_id, proxies=proxies or None)
        return bool(result.get("ok"))
    except Exception as e:
        logger.error(f"自动获取章节失败 {book_id}: {e}")
        return False


# ═══════════════════════════════════════════════════════════
# 创建任务
# ═══════════════════════════════════════════════════════════

def create_job(book_id: str, job_type: str = "process_album") -> dict | None:
    """为专辑创建任务。如果专辑未获取章节则自动获取。"""
    # 获取专辑信息
    book = fetch_one(
        "SELECT book_id, book_name, total_chapters FROM public.books WHERE book_id = %s",
        (book_id,),
    )
    if not book:
        return None

    # 确保已获取全部章节
    if not _ensure_chapters(book_id):
        return None

    # 获取待处理章节数
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


def create_jobs_for_all_pending(categories: list[str] | None = None,
                                max_workers: int = 5) -> list[dict]:
    """为未完成、不在任务队列的专辑创建任务（多线程）。

    如果专辑未获取章节或章节不全，会自动先获取章节列表（每个线程随机选代理）再创建任务。

    Args:
        categories: 可选分类列表，仅在这些分类中筛选。None=所有分类。
        max_workers: 最大并发线程数。
    """
    cat_clause = sql.SQL("")
    params: list = []
    if categories:
        cat_clause = sql.SQL(" AND b.category = ANY(%s)")
        params.append(categories)

    # 查找未完成、不在任务队列的专辑（不要求已有章节）
    rows = fetch_all(
        sql.SQL("""
            SELECT DISTINCT b.book_id
            FROM public.books b
            WHERE b.book_status != 'success'
              AND NOT EXISTS (
                  SELECT 1 FROM public.xm_jobs j
                  WHERE j.book_id = b.book_id
                    AND j.status IN ('pending', 'processing')
              )
              {cat_clause}
            ORDER BY b.book_id
        """).format(cat_clause=cat_clause),
        tuple(params) if params else None,
    )

    if not rows:
        return []

    book_ids = [row["book_id"] for row in rows]
    total = len(book_ids)
    results: list[dict] = []
    done_count = 0

    worker_count = max(1, min(max_workers, total))
    logger.info(f"批量创建任务: {total} 个专辑, {worker_count} 线程并发")

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(create_job, bid): bid for bid in book_ids}
        for future in as_completed(futures):
            bid = futures[future]
            try:
                job = future.result()
                if job:
                    results.append(job)
            except Exception as e:
                logger.error(f"创建任务失败 {bid}: {e}")
            done_count += 1
            if done_count % 10 == 0 or done_count == total:
                logger.info(f"批量创建进度: {done_count}/{total}, 已创建 {len(results)} 个任务")

    logger.info(f"批量创建完成: {len(results)}/{total} 个任务创建成功")
    return results


# ═══════════════════════════════════════════════════════════
# 超时任务回收
# ═══════════════════════════════════════════════════════════

def _reset_stale_jobs():
    """回收处理中的任务。

    1. 自动完成：章节全部 uploaded 的 processing 任务 → done
    2. 超时回收：worker 心跳超时或认领超过 60 分钟的 processing → pending
    """
    # 1. 自动完成：所有章节已上传但任务还是 processing
    execute(
        sql.SQL("""
            UPDATE public.xm_jobs
            SET status = 'done', finished_at = now(),
                result = jsonb_build_object('auto_completed', true)
            WHERE status = 'processing'
              AND NOT EXISTS (
                  SELECT 1 FROM public.audiobook_chapters c
                  WHERE c.book_id = xm_jobs.book_id
                    AND c.upload_status = 'pending'
              )
        """),
    )
    # 同步更新 book_status
    execute(
        sql.SQL("""
            UPDATE public.books
            SET book_status = 'success', updated_at = now()
            WHERE book_id IN (
                SELECT book_id FROM public.xm_jobs
                WHERE status = 'done' AND result->>'auto_completed' = 'true'
            )
              AND book_status != 'success'
        """),
    )

    # 2. 超时回收
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
                      AND s.last_seen_at > now() - (%s * interval '1 second')
                )
                -- 兜底：认领超过 60 分钟无论如何回收
                OR claimed_at < now() - (%s * interval '1 minute')
              )
        """),
        (STALE_JOB_HEARTBEAT_SECONDS, STALE_JOB_FALLBACK_MINUTES),
    )


# ═══════════════════════════════════════════════════════════
# 认领任务（FOR UPDATE SKIP LOCKED）
# ═══════════════════════════════════════════════════════════

def claim_job(worker_id: str) -> dict | None:
    """原子认领一个待处理任务（单一事务，防止竞态）。

    流程：
    1. 回收超时任务
    2. 优先找回自己 processing 的任务（worker 重启恢复）
    3. 原子认领新 pending 任务（防止同一 worker 认领多个、防止两个 job 处理同一专辑）
    4. 获取 pending 章节列表
    5. 章节未入库检查（total_chapters > 0 但无章节 → 放弃）
    6. 标记章节归属
    """
    from ..database import transaction
    from psycopg.rows import dict_row

    with transaction() as conn:
        cur = conn.cursor(row_factory=dict_row)

        # 1a. 自动完成：章节全部 uploaded 的 processing 任务 → done
        cur.execute(
            """
            UPDATE public.xm_jobs
            SET status = 'done', finished_at = now(),
                result = jsonb_build_object('auto_completed', true)
            WHERE status = 'processing'
              AND NOT EXISTS (
                  SELECT 1 FROM public.audiobook_chapters c
                  WHERE c.book_id = xm_jobs.book_id
                    AND c.upload_status = 'pending'
              )
            """,
        )
        cur.execute(
            """
            UPDATE public.books
            SET book_status = 'success', updated_at = now()
            WHERE book_id IN (
                SELECT book_id FROM public.xm_jobs
                WHERE status = 'done' AND result->>'auto_completed' = 'true'
            )
              AND book_status != 'success'
            """,
        )

        # 1b. 回收超时任务
        cur.execute(
            """
            UPDATE public.xm_jobs
            SET status = 'pending', worker_id = NULL, claimed_at = NULL
            WHERE status = 'processing'
              AND (
                NOT EXISTS (
                    SELECT 1 FROM public.xm_worker_stats s
                    WHERE s.worker_id = xm_jobs.worker_id
                      AND s.last_seen_at > now() - (%s * interval '1 second')
                )
                OR claimed_at < now() - (%s * interval '1 minute')
              )
            """,
            (STALE_JOB_HEARTBEAT_SECONDS, STALE_JOB_FALLBACK_MINUTES),
        )

        # 2. 优先找回自己 processing 的任务
        cur.execute(
            "SELECT * FROM public.xm_jobs WHERE worker_id = %s AND status = 'processing' ORDER BY claimed_at DESC LIMIT 1",
            (worker_id,),
        )
        row = cur.fetchone()
        reclaimed = row is not None

        # 3. 原子认领新 pending 任务
        if not row:
            cur.execute(
                """
                UPDATE public.xm_jobs
                SET status = 'processing', worker_id = %s, claimed_at = now()
                WHERE job_id IN (
                    SELECT j.job_id FROM public.xm_jobs j
                    WHERE j.status = 'pending'
                      AND NOT EXISTS (
                          SELECT 1 FROM public.xm_jobs j2
                          WHERE j2.worker_id = %s AND j2.status = 'processing'
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM public.xm_jobs j3
                          WHERE j3.book_id = j.book_id
                            AND j3.status = 'processing'
                            AND j3.job_id != j.job_id
                      )
                    ORDER BY j.created_at
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
                """,
                (worker_id, worker_id),
            )
            row = cur.fetchone()

        if not row:
            return None

        job = dict(row)

        # 4. 获取 pending 章节列表
        book_id = job["book_id"]
        cur.execute(
            """
            SELECT chapter_id, chapter_name, audio_url, chapter_order, duration
            FROM public.audiobook_chapters
            WHERE book_id = %s AND upload_status = 'pending'
            ORDER BY chapter_order
            """,
            (book_id,),
        )
        chapters = [dict(r) for r in cur.fetchall()]

        # 5. 章节未入库检查
        if not chapters:
            cur.execute(
                "SELECT COALESCE(total_chapters, 0) FROM public.books WHERE book_id = %s",
                (book_id,),
            )
            tc_row = cur.fetchone()
            total_chapters = 0
            if tc_row:
                val = list(tc_row.values())[0]
                total_chapters = int(val) if val is not None else 0
            if total_chapters > 0:
                # 章节未采集，放弃认领（rollback 会撤销 UPDATE）
                return None

        # 6. 标记章节归属
        if chapters:
            cur.execute(
                """
                UPDATE public.audiobook_chapters
                SET worker_id = %s, claimed_at = now()
                WHERE book_id = %s AND upload_status = 'pending'
                """,
                (worker_id, book_id),
            )

        job["chapters"] = chapters
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
        # 使用 UTC 时间避免 Colab Worker 与 VPS 时区不一致
        from datetime import datetime, timezone
        update_data["uploaded_at"] = datetime.now(timezone.utc)
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

    使用单一事务，防止 requeue 和章节释放之间的竞态。
    """
    from ..database import transaction
    from psycopg.rows import dict_row

    with transaction() as conn:
        cur = conn.cursor(row_factory=dict_row)

        cur.execute("SELECT book_id, retry_count, worker_id FROM public.xm_jobs WHERE job_id = %s", (job_id,))
        job = cur.fetchone()
        if not job:
            return {"ok": False, "error": "任务不存在"}

        job = dict(job)
        book_id = job["book_id"]
        retry_count = int(job.get("retry_count", 0))

        # 检查是否还有 pending 章节
        cur.execute(
            "SELECT COUNT(*) FROM public.audiobook_chapters WHERE book_id = %s AND upload_status = 'pending'",
            (book_id,),
        )
        remaining_row = cur.fetchone()
        remaining = int(list(remaining_row.values())[0]) if remaining_row else 0

        if remaining > 0:
            # 重新入队 + 释放章节 + 更新 book_status — 原子操作
            cur.execute(
                """
                UPDATE public.xm_jobs
                SET status = 'pending', worker_id = NULL, claimed_at = NULL,
                    retry_count = retry_count + 1, result = %s
                WHERE job_id = %s
                """,
                (Jsonb(result) if result else None, job_id),
            )
            cur.execute(
                """
                UPDATE public.audiobook_chapters
                SET worker_id = NULL, claimed_at = NULL
                WHERE book_id = %s AND upload_status = 'pending'
                """,
                (book_id,),
            )
            cur.execute(
                "UPDATE public.books SET book_status = 'pending', updated_at = now() WHERE book_id = %s",
                (book_id,),
            )
            logger.info(f"任务 #{job_id} 还有 {remaining} 个 pending 章节，重新入队 (retry={retry_count + 1})")
            return {"ok": True, "job_id": job_id, "requeued": True, "remaining": remaining,
                    "retry_count": retry_count + 1}

        # 全部完成 → 标记 done, success
        cur.execute(
            """
            UPDATE public.xm_jobs
            SET status = 'done', finished_at = now(), result = %s
            WHERE job_id = %s
            """,
            (Jsonb(result) if result else None, job_id),
        )
        cur.execute(
            "UPDATE public.books SET book_status = 'success', updated_at = now() WHERE book_id = %s",
            (book_id,),
        )

    # 更新 Worker 统计
    if job.get("worker_id"):
        _update_worker_stats(job["worker_id"], success=True, chapters=0)

    return {"ok": True, "job_id": job_id, "requeued": False,
            "book_status": "success", "remaining": 0}


def fail_job(job_id: int, error_message: str) -> dict:
    """标记任务失败。"""
    job = fetch_one("SELECT book_id, worker_id FROM public.xm_jobs WHERE job_id = %s", (job_id,))

    execute(
        sql.SQL("""
            UPDATE public.xm_jobs
            SET status = 'failed', finished_at = now(), error_message = %s,
                retry_count = retry_count + 1
            WHERE job_id = %s
        """),
        (error_message, job_id),
    )

    # 更新 books.book_status + 重置未完成章节
    if job:
        execute(
            sql.SQL("UPDATE public.books SET book_status = 'failed', updated_at = now() WHERE book_id = %s"),
            (job["book_id"],),
        )
        execute(
            sql.SQL("""
                UPDATE public.audiobook_chapters
                SET upload_status = 'pending', worker_id = NULL, claimed_at = NULL
                WHERE book_id = %s AND upload_status != 'uploaded'
            """),
            (job["book_id"],),
        )

    # 更新 Worker 统计
    if job and job.get("worker_id"):
        _update_worker_stats(job["worker_id"], success=False, chapters=0)

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


def reset_jobs_by_status(status: str) -> dict:
    """按状态重置所有匹配的任务为 pending。"""
    rows = fetch_all(
        sql.SQL("SELECT job_id FROM public.xm_jobs WHERE status = %s"),
        (status,),
    )
    job_ids = [r["job_id"] for r in (rows or [])]
    if not job_ids:
        return {"ok": True, "reset": 0}
    return reset_jobs_batch(job_ids)
