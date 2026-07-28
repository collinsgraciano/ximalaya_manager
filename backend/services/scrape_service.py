"""采集服务 — 喜马拉雅分类采集 + 章节列表获取。"""

from __future__ import annotations

import json
import logging
from psycopg import sql
from psycopg.types.json import Jsonb
from datetime import datetime

from ..database import fetch_one, fetch_all, execute, execute_returning, execute_batch
from ...pipeline.ximalaya_api import (
    scrape_category as _scrape_category,
    get_all_tracks as _get_all_tracks,
    get_album_info as _get_album_info,
    normalize_album_record,
    CATEGORIES,
)

logger = logging.getLogger(__name__)


def get_xm_cookie() -> str:
    """从全局设置读取喜马拉雅 Cookie。"""
    row = fetch_one("SELECT setting_value FROM public.global_settings WHERE setting_key = %s", ("XM_COOKIE",))
    return (row or {}).get("setting_value", "")


def _build_headers(cookie: str = "") -> dict:
    """构建请求头。"""
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    return headers


# ═══════════════════════════════════════════════════════════
# 分类采集
# ═══════════════════════════════════════════════════════════

def scrape_and_save_category(
    category: str,
    max_pages: int = 0,
    sort: str = "default",
    free_only: bool = False,
    max_albums: int = 0,
) -> dict:
    """采集分类专辑并保存到数据库，返回统计信息。"""
    # 创建采集任务记录
    task = execute_returning(
        sql.SQL("""
            INSERT INTO public.xm_scrape_tasks (category, category_name, status, created_at)
            VALUES (%s, %s, 'running', now())
            RETURNING task_id
        """),
        (category, CATEGORIES.get(category, category)),
    )
    task_id = task["task_id"] if task else 0

    cookie = get_xm_cookie()
    headers = _build_headers(cookie)

    # 采集
    albums = _scrape_category(
        category, max_pages=max_pages, sort=sort,
        free_only=free_only, max_albums=max_albums,
        headers=headers,
    )

    # 写入数据库（批量 INSERT）
    batch_params = []
    for album in albums:
        book_id = f"xm_{album['albumId']}"
        book_data = {
            "albumId": album["albumId"],
            "albumCover": album.get("cover", ""),
            "albumCoverPath": album.get("albumCoverPath", ""),
            "intro": album.get("intro", ""),
            "albumPlayCount": album.get("albumPlayCount", 0),
            "isPaid": album.get("isPaid", False),
            "isFinished": album.get("isFinished", 0),
            "anchorId": album.get("anchorId", 0),
            "anchorName": album.get("anchorName", ""),
            "vipType": album.get("vipType", 0),
            "albumUrl": album.get("albumUrl", ""),
        }
        batch_params.append((
            book_id, album["albumTitle"], album.get("anchorName", ""),
            category, album.get("albumTrackCount", 0), Jsonb(book_data),
        ))

    if batch_params:
        execute_batch(
            sql.SQL("""
                INSERT INTO public.books (book_id, book_name, author, category, total_chapters, book_data, book_status, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', now())
                ON CONFLICT (book_id) DO UPDATE SET
                    book_name = EXCLUDED.book_name,
                    author = EXCLUDED.author,
                    category = EXCLUDED.category,
                    total_chapters = EXCLUDED.total_chapters,
                    book_data = EXCLUDED.book_data,
                    updated_at = now()
            """),
            batch_params,
        )

    saved_count = len(batch_params)

    # 更新任务记录
    execute(
        sql.SQL("UPDATE public.xm_scrape_tasks SET status = 'done', total_albums = %s, processed_albums = %s, finished_at = now() WHERE task_id = %s"),
        (len(albums), saved_count, task_id),
    )

    free_count = sum(1 for a in albums if not a.get("isPaid"))
    paid_count = len(albums) - free_count

    return {
        "task_id": task_id,
        "category": category,
        "total_albums": len(albums),
        "saved_albums": saved_count,
        "free": free_count,
        "paid": paid_count,
    }


def scrape_album_tracks(book_id: str) -> dict:
    """获取专辑的所有章节列表并保存到数据库。

    book_id 格式: xm_{albumId}
    """
    # 从 book_id 提取 album_id
    if not book_id.startswith("xm_"):
        return {"ok": False, "error": "book_id 格式错误，应为 xm_{albumId}"}
    album_id = book_id[3:]

    cookie = get_xm_cookie()
    headers = _build_headers(cookie)

    # 获取章节列表
    tracks, album_title = _get_all_tracks(album_id, headers)

    if not tracks:
        return {"ok": False, "error": "未获取到章节列表"}

    # 批量写入 audiobook_chapters
    batch_params = []
    for track in tracks:
        chapter_id = str(track["trackId"])
        audio_url = f"https://www.ximalaya.com/sound/{track['trackId']}"
        batch_params.append((
            book_id, chapter_id, album_title, track["title"],
            audio_url, track.get("orderNo", 0), track.get("duration", 0),
        ))

    if batch_params:
        execute_batch(
            sql.SQL("""
                INSERT INTO public.audiobook_chapters
                    (book_id, chapter_id, book_name, chapter_name, audio_url,
                     upload_status, chapter_order, duration)
                VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s)
                ON CONFLICT (book_id, chapter_id) DO UPDATE SET
                    chapter_name = EXCLUDED.chapter_name,
                    audio_url = EXCLUDED.audio_url,
                    chapter_order = EXCLUDED.chapter_order,
                    duration = EXCLUDED.duration
            """),
            batch_params,
        )

    saved_count = len(batch_params)

    # 更新 books 表的 total_chapters 和 book_data.chapters
    book_row = fetch_one("SELECT book_data FROM public.books WHERE book_id = %s", (book_id,))
    if book_row and book_row.get("book_data"):
        book_data = book_row["book_data"]
        if isinstance(book_data, str):
            import json
            book_data = json.loads(book_data)
        book_data["chapters"] = [
            {
                "chapterId": str(t["trackId"]),
                "chapterName": t["title"],
                "mp3Url": f"https://www.ximalaya.com/sound/{t['trackId']}",
                "orderNo": t.get("orderNo", 0),
                "duration": t.get("duration", 0),
            }
            for t in tracks
        ]
        execute(
            sql.SQL("UPDATE public.books SET total_chapters = %s, book_data = %s, updated_at = now() WHERE book_id = %s"),
            (len(tracks), Jsonb(book_data), book_id),
        )
    else:
        execute(
            sql.SQL("UPDATE public.books SET total_chapters = %s, updated_at = now() WHERE book_id = %s"),
            (len(tracks), book_id),
        )

    return {
        "ok": True,
        "book_id": book_id,
        "album_title": album_title,
        "total_tracks": len(tracks),
        "saved_tracks": saved_count,
    }


# ═══════════════════════════════════════════════════════════
# 查询
# ═══════════════════════════════════════════════════════════

def get_albums(page: int = 1, page_size: int = 20, category: str = "",
               search: str = "", status: str = "") -> dict:
    """分页查询专辑列表。"""
    conditions = []
    params: list = []

    if category:
        conditions.append("category = %s")
        params.append(category)
    if search:
        conditions.append("(book_name ILIKE %s OR author ILIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])
    if status:
        conditions.append("book_status = %s")
        params.append(status)

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    # 总数
    total = fetch_val(f"SELECT COUNT(*) FROM public.books{where_clause}", tuple(params) if params else None)
    total = int(total or 0)

    # 分页
    offset = (page - 1) * page_size
    rows = fetch_all(
        sql.SQL("SELECT book_id, book_name, author, category, total_chapters, book_data, book_status, updated_at "
                "FROM public.books{} ORDER BY updated_at DESC LIMIT %s OFFSET %s").format(
            sql.SQL(where_clause)
        ),
        tuple(params + [page_size, offset]),
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "albums": rows or [],
    }


def get_album_detail(book_id: str) -> dict | None:
    """获取专辑详情。"""
    return fetch_one(
        "SELECT book_id, book_name, author, category, total_chapters, book_data, book_status, status, tags, note, created_at, updated_at "
        "FROM public.books WHERE book_id = %s",
        (book_id,),
    )


def get_album_chapters(book_id: str, page: int = 1, page_size: int = 50,
                       status: str = "") -> dict:
    """分页查询专辑章节列表。"""
    conditions = ["book_id = %s"]
    params: list = [book_id]

    if status:
        conditions.append("upload_status = %s")
        params.append(status)

    where_clause = " AND ".join(conditions)

    total = fetch_val(
        f"SELECT COUNT(*) FROM public.audiobook_chapters WHERE {where_clause}",
        tuple(params),
    )
    total = int(total or 0)

    offset = (page - 1) * page_size
    rows = fetch_all(
        sql.SQL("SELECT book_id, chapter_id, book_name, chapter_name, audio_url, "
                "telegram_file_id, telegram_message_id, telegram_bot_id, telegram_bot_user_id, "
                "upload_status, uploaded_at, worker_id, claimed_at, error_message, chapter_order, duration "
                "FROM public.audiobook_chapters WHERE {} ORDER BY chapter_order LIMIT %s OFFSET %s").format(
            sql.SQL(where_clause)
        ),
        tuple(params + [page_size, offset]),
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "chapters": rows or [],
    }


def delete_album(book_id: str) -> int:
    """删除专辑及其章节。"""
    execute(sql.SQL("DELETE FROM public.audiobook_chapters WHERE book_id = %s"), (book_id,))
    return execute(sql.SQL("DELETE FROM public.books WHERE book_id = %s"), (book_id,))


def get_categories() -> dict:
    """返回可用分类列表。"""
    return CATEGORIES
