"""专辑管理 API。"""

from __future__ import annotations

import logging
from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..services.scrape_service import (
    start_scrape,
    get_scrape_status,
    stop_scrape,
    scrape_album_tracks,
    get_albums,
    get_album_detail,
    get_album_chapters,
    delete_album,
    delete_all_albums,
    start_scrape_all_tracks,
    get_tracks_status,
    stop_tracks_scrape,
    get_categories,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["albums"])


# ═══════════════════════════════════════
# 分类列表
# ═══════════════════════════════════════

@router.get("/categories")
def api_categories():
    return {"categories": get_categories()}


# ═══════════════════════════════════════
# 专辑列表
# ═══════════════════════════════════════

@router.get("/albums")
def api_albums(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    category: str = Query(""),
    search: str = Query(""),
    status: str = Query(""),
):
    return get_albums(page, page_size, category, search, status)


# ═══════════════════════════════════════
# 专辑详情
# ═══════════════════════════════════════

@router.get("/albums/{book_id}")
def api_album_detail(book_id: str):
    album = get_album_detail(book_id)
    if not album:
        return {"ok": False, "error": "专辑不存在"}
    return {"ok": True, "album": album}


# ═══════════════════════════════════════
# 专辑章节列表
# ═══════════════════════════════════════

@router.get("/albums/{book_id}/chapters")
def api_album_chapters(
    book_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    status: str = Query(""),
):
    return get_album_chapters(book_id, page, page_size, status)


# ═══════════════════════════════════════
# 触发分类采集（多分类 + 后台运行）
# ═══════════════════════════════════════

class ScrapeRequest(BaseModel):
    categories: list[str]
    max_pages: int = 0
    sort: str = "default"
    free_only: bool = False
    max_albums: int = 0


@router.post("/albums/scrape")
def api_scrape(req: ScrapeRequest):
    if not req.categories:
        return {"ok": False, "error": "请至少选择一个分类"}
    ok = start_scrape(req.categories, req.max_pages, req.sort, req.free_only,
                      req.max_albums)
    if not ok:
        return {"ok": False, "error": "已有采集任务正在运行"}
    return {"ok": True, "message": "采集已启动"}


@router.get("/albums/scrape/status")
def api_scrape_status():
    return get_scrape_status()


@router.post("/albums/scrape/stop")
def api_scrape_stop():
    stopped = stop_scrape()
    return {"ok": stopped}


# ═══════════════════════════════════════
# 触发章节列表采集
# ═══════════════════════════════════════

@router.post("/albums/{book_id}/scrape-tracks")
def api_scrape_tracks(book_id: str):
    try:
        result = scrape_album_tracks(book_id)
        return result
    except Exception as e:
        logger.error(f"章节采集失败: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


# ═══════════════════════════════════════
# 编辑专辑
# ═══════════════════════════════════════

class UpdateAlbum(BaseModel):
    book_name: str | None = None
    author: str | None = None
    category: str | None = None
    note: str | None = None
    book_status: str | None = None
    tags: list[str] | None = None


@router.put("/albums/{book_id}")
def api_update_album(book_id: str, req: UpdateAlbum):
    """更新专辑信息。"""
    from psycopg import sql as pg_sql
    from ..database import execute as db_execute, fetch_one

    updates = {}
    if req.book_name is not None:
        updates["book_name"] = req.book_name
    if req.author is not None:
        updates["author"] = req.author
    if req.category is not None:
        updates["category"] = req.category
    if req.note is not None:
        updates["note"] = req.note
    if req.book_status is not None:
        updates["book_status"] = req.book_status
    if req.tags is not None:
        updates["tags"] = req.tags

    if not updates:
        return {"ok": False, "error": "无更新字段"}

    set_parts = pg_sql.SQL(", ").join(
        pg_sql.SQL("{} = {}").format(pg_sql.Identifier(k), pg_sql.Placeholder())
        for k in updates.keys()
    )
    set_parts = pg_sql.SQL(", ").join([set_parts, pg_sql.SQL("updated_at = now()")])

    rowcount = db_execute(
        pg_sql.SQL("UPDATE public.books SET {} WHERE book_id = %s").format(set_parts),
        tuple(list(updates.values()) + [book_id]),
    )
    if rowcount == 0:
        return {"ok": False, "error": "专辑不存在"}
    return {"ok": True, "updated": rowcount}


# ═══════════════════════════════════════
# 编辑章节
# ═══════════════════════════════════════

class UpdateChapter(BaseModel):
    chapter_name: str | None = None
    upload_status: str | None = None
    error_message: str | None = None


@router.put("/albums/{book_id}/chapters/{chapter_id}")
def api_update_chapter(book_id: str, chapter_id: str, req: UpdateChapter):
    """更新章节信息。"""
    from psycopg import sql as pg_sql
    from ..database import execute as db_execute

    updates = {}
    if req.chapter_name is not None:
        updates["chapter_name"] = req.chapter_name
    if req.upload_status is not None:
        updates["upload_status"] = req.upload_status
    if req.error_message is not None:
        updates["error_message"] = req.error_message

    if not updates:
        return {"ok": False, "error": "无更新字段"}

    set_parts = pg_sql.SQL(", ").join(
        pg_sql.SQL("{} = {}").format(pg_sql.Identifier(k), pg_sql.Placeholder())
        for k in updates.keys()
    )

    rowcount = db_execute(
        pg_sql.SQL("UPDATE public.audiobook_chapters SET {} WHERE book_id = %s AND chapter_id = %s").format(set_parts),
        tuple(list(updates.values()) + [book_id, chapter_id]),
    )
    if rowcount == 0:
        return {"ok": False, "error": "章节不存在"}
    return {"ok": True, "updated": rowcount}


# ═══════════════════════════════════════
# 重置章节状态（重试失败章节）
# ═══════════════════════════════════════

@router.post("/albums/{book_id}/reset-chapters")
def api_reset_chapters(book_id: str):
    """重置专辑下所有失败/待处理章节为 pending，清除 worker 认领。"""
    from psycopg import sql as pg_sql
    from ..database import execute as db_execute

    rowcount = db_execute(
        pg_sql.SQL("""
            UPDATE public.audiobook_chapters
            SET upload_status = 'pending', worker_id = NULL, claimed_at = NULL,
                error_message = NULL
            WHERE book_id = %s AND upload_status IN ('failed', 'pending')
        """),
        (book_id,),
    )
    # 同时重置 book_status
    db_execute(
        pg_sql.SQL("UPDATE public.books SET book_status = 'pending', updated_at = now() WHERE book_id = %s"),
        (book_id,),
    )
    return {"ok": True, "reset": rowcount}


# ═══════════════════════════════════════
# 删除所有专辑
# ═══════════════════════════════════════

@router.delete("/albums/all")
def api_delete_all_albums():
    deleted = delete_all_albums()
    return {"ok": True, "deleted": deleted}


# ═══════════════════════════════════════
# 批量获取所有专辑章节（后台线程）
# ═══════════════════════════════════════

@router.post("/albums/scrape-all-tracks")
def api_scrape_all_tracks(max_workers: int = Query(5, ge=1, le=20)):
    ok = start_scrape_all_tracks(max_workers)
    if not ok:
        return {"ok": False, "error": "已有章节采集任务正在运行"}
    return {"ok": True, "message": "批量章节采集已启动"}


@router.get("/albums/scrape-all-tracks/status")
def api_scrape_all_tracks_status():
    return get_tracks_status()


@router.post("/albums/scrape-all-tracks/stop")
def api_scrape_all_tracks_stop():
    stopped = stop_tracks_scrape()
    return {"ok": stopped}


# ═══════════════════════════════════════
# 一键为所有待处理专辑创建任务
# ═══════════════════════════════════════

class CreateAllJobsRequest(BaseModel):
    categories: list[str] | None = None


@router.post("/albums/create-all-jobs")
def api_create_all_jobs(req: CreateAllJobsRequest | None = None):
    from ..services.job_service import create_jobs_for_all_pending
    categories = req.categories if req else None
    jobs = create_jobs_for_all_pending(categories)
    return {"ok": True, "count": len(jobs), "jobs": jobs}


# ═══════════════════════════════════════
# 删除单个专辑
# ═══════════════════════════════════════

@router.delete("/albums/{book_id}")
def api_delete_album(book_id: str):
    deleted = delete_album(book_id)
    return {"ok": True, "deleted": deleted}
