"""仪表盘统计 API。"""

from __future__ import annotations

from fastapi import APIRouter
from ..database import fetch_one, fetch_all

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard/stats")
def dashboard_stats():
    """仪表盘统计数据。"""
    # 单条 SQL 合并所有 COUNT 查询，减少 DB 往返
    counts = fetch_one("""
        SELECT
            (SELECT COUNT(*) FROM public.books)                                          AS albums_total,
            (SELECT COUNT(*) FROM public.audiobook_chapters)                              AS chapters_total,
            (SELECT COUNT(*) FROM public.audiobook_chapters WHERE upload_status = 'uploaded') AS chapters_uploaded,
            (SELECT COUNT(*) FROM public.audiobook_chapters WHERE upload_status = 'pending')  AS chapters_pending,
            (SELECT COUNT(*) FROM public.audiobook_chapters WHERE upload_status = 'failed')   AS chapters_failed,
            (SELECT COUNT(*) FROM public.xm_jobs WHERE status = 'pending')                AS jobs_pending,
            (SELECT COUNT(*) FROM public.xm_jobs WHERE status = 'processing')             AS jobs_processing,
            (SELECT COUNT(*) FROM public.xm_jobs WHERE status = 'done')                    AS jobs_done,
            (SELECT COUNT(*) FROM public.xm_jobs WHERE status = 'failed')                  AS jobs_failed,
            (SELECT COUNT(*) FROM public.xm_worker_stats WHERE last_seen_at > now() - interval '5 minutes') AS workers_active
    """)
    counts = counts or {}

    # 最近任务
    recent_jobs = fetch_all(
        "SELECT job_id, book_id, book_name, status, worker_id, "
        "total_chapters, done_chapters, created_at, claimed_at, finished_at "
        "FROM public.xm_jobs ORDER BY created_at DESC LIMIT 10"
    )

    # 分类统计
    category_stats = fetch_all(
        "SELECT category, COUNT(*) as album_count FROM public.books GROUP BY category ORDER BY album_count DESC"
    )

    return {
        "albums": {
            "total": int(counts.get("albums_total") or 0),
        },
        "chapters": {
            "total": int(counts.get("chapters_total") or 0),
            "uploaded": int(counts.get("chapters_uploaded") or 0),
            "pending": int(counts.get("chapters_pending") or 0),
            "failed": int(counts.get("chapters_failed") or 0),
        },
        "jobs": {
            "pending": int(counts.get("jobs_pending") or 0),
            "processing": int(counts.get("jobs_processing") or 0),
            "done": int(counts.get("jobs_done") or 0),
            "failed": int(counts.get("jobs_failed") or 0),
        },
        "workers": {
            "active": int(counts.get("workers_active") or 0),
        },
        "recent_jobs": recent_jobs or [],
        "category_stats": category_stats or [],
    }
