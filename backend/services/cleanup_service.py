"""数据清理 — 定期删除已完成/失败的历史任务记录。"""

from __future__ import annotations

import logging

from psycopg import sql

from ..database import execute, fetch_val

logger = logging.getLogger(__name__)


def cleanup_old_jobs(days: int = 30) -> dict:
    """删除 days 天前已完成/失败的任务记录和采集任务记录。

    不删除 audiobook_chapters 或 books 表数据。
    """
    deleted_jobs = execute(
        sql.SQL("DELETE FROM public.xm_jobs "
                "WHERE status IN ('done', 'failed') "
                "AND finished_at IS NOT NULL "
                "AND finished_at < now() - (%s * interval '1 day')"),
        (days,),
    )
    deleted_tasks = execute(
        sql.SQL("DELETE FROM public.xm_scrape_tasks "
                "WHERE status IN ('done', 'cancelled') "
                "AND finished_at IS NOT NULL "
                "AND finished_at < now() - (%s * interval '1 day')"),
        (days,),
    )
    logger.info(f"清理 {days} 天前历史记录: jobs={deleted_jobs}, scrape_tasks={deleted_tasks}")
    return {"jobs": deleted_jobs, "scrape_tasks": deleted_tasks, "days": days}


def get_cleanup_stats() -> dict:
    """获取可清理的历史记录统计。"""
    done_jobs = fetch_val(
        "SELECT count(*) FROM public.xm_jobs "
        "WHERE status IN ('done', 'failed') AND finished_at IS NOT NULL"
    )
    done_tasks = fetch_val(
        "SELECT count(*) FROM public.xm_scrape_tasks "
        "WHERE status IN ('done', 'cancelled') AND finished_at IS NOT NULL"
    )
    return {
        "deletable_jobs": int(done_jobs or 0),
        "deletable_tasks": int(done_tasks or 0),
    }
