"""仪表盘统计 API。"""

from __future__ import annotations

from fastapi import APIRouter
from ..database import fetch_one

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard/stats")
def dashboard_stats():
    """仪表盘统计数据 — 单条 SQL 合并所有查询，减少 DB 往返和连接占用。"""
    row = fetch_one("""
        WITH counts AS (
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
        ),
        recent_jobs AS (
            SELECT json_agg(row_to_json(t)) AS jobs
            FROM (
                SELECT job_id, book_id, book_name, status, worker_id,
                       total_chapters, done_chapters, created_at, claimed_at, finished_at
                FROM public.xm_jobs ORDER BY created_at DESC LIMIT 10
            ) t
        ),
        category_stats AS (
            SELECT json_agg(row_to_json(t)) AS categories
            FROM (
                SELECT category, COUNT(*) AS album_count
                FROM public.books GROUP BY category ORDER BY album_count DESC
            ) t
        )
        SELECT
            (SELECT row_to_json(counts))  AS counts,
            (SELECT jobs FROM recent_jobs) AS recent_jobs,
            (SELECT categories FROM category_stats) AS category_stats
    """)
    row = row or {}
    counts = row.get("counts") or {}
    recent_jobs = row.get("recent_jobs") or []
    category_stats = row.get("category_stats") or []

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
        "recent_jobs": recent_jobs,
        "category_stats": category_stats,
    }
