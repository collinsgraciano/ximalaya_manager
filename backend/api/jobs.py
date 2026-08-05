"""Colab Worker API — 任务认领 + 章节上报 + 配置分发。"""

from __future__ import annotations

import logging
from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..services.job_service import (
    claim_job,
    update_chapter_result,
    complete_job,
    fail_job,
    heartbeat,
    add_chapter_to_worker,
    reset_job,
    release_job,
    delete_job,
    delete_jobs_batch,
    reset_jobs_batch,
    delete_jobs_by_status,
    reset_jobs_by_status,
    delete_all_jobs,
)
from ..database import fetch_all, fetch_one

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["jobs"])


def _get_effective_proxy_list(settings_map: dict) -> list[str]:
    """获取生效的代理列表：优先手动 PROXY_LIST，为空则读 VPS 缓存的已验证代理。"""
    manual = [line.strip() for line in settings_map.get("PROXY_LIST", "").splitlines() if line.strip()]
    if manual:
        return manual

    # 读 VPS 缓存的已验证代理，分发给 Colab
    import json
    cache_raw = settings_map.get("PROXY_VERIFIED_CACHE", "")
    if cache_raw:
        try:
            cache = json.loads(cache_raw)
            return cache.get("proxies", [])
        except Exception:
            pass
    return []


# ═══════════════════════════════════════
# 认领任务
# ═══════════════════════════════════════

@router.get("/jobs/claim")
def api_claim_job(worker_id: str = Query(...)):
    """Colab Worker 认领任务。"""
    job = claim_job(worker_id)
    if not job:
        return {"ok": False, "error": "没有待处理任务"}

    # 更新心跳
    heartbeat(worker_id)

    return {"ok": True, "job": job}


# ═══════════════════════════════════════
# 章节结果上报
# ═══════════════════════════════════════

class ChapterResult(BaseModel):
    chapter_id: str
    upload_status: str  # uploaded / pending
    telegram_file_id: str = ""
    telegram_message_id: int = 0
    telegram_bot_id: int | None = None
    telegram_bot_user_id: int | None = None
    error_message: str = ""
    # 原始音频（降噪前）TG 缓存
    original_telegram_file_id: str = ""
    original_telegram_message_id: int = 0
    original_telegram_bot_id: int | None = None
    original_telegram_bot_user_id: int | None = None


@router.post("/jobs/{job_id}/chapter")
def api_chapter_result(job_id: int, result: ChapterResult,
                       worker_id: str = Query(...)):
    """Colab Worker 上报章节处理结果。"""
    # 验证 worker_id 是否为该任务的认领者
    job_row = fetch_one("SELECT worker_id FROM public.xm_jobs WHERE job_id = %s", (job_id,))
    if not job_row:
        return {"ok": False, "error": "任务不存在"}
    if job_row.get("worker_id") != worker_id:
        return {"ok": False, "error": "无权操作此任务"}

    resp = update_chapter_result(
        job_id=job_id,
        chapter_id=result.chapter_id,
        upload_status=result.upload_status,
        telegram_file_id=result.telegram_file_id,
        telegram_message_id=result.telegram_message_id,
        telegram_bot_id=result.telegram_bot_id,
        telegram_bot_user_id=result.telegram_bot_user_id,
        error_message=result.error_message,
        original_telegram_file_id=result.original_telegram_file_id,
        original_telegram_message_id=result.original_telegram_message_id,
        original_telegram_bot_id=result.original_telegram_bot_id,
        original_telegram_bot_user_id=result.original_telegram_bot_user_id,
    )

    # 累加 Worker 章节数
    if resp.get("ok"):
        add_chapter_to_worker(worker_id)

    return resp


# ═══════════════════════════════════════
# 任务完成/失败
# ═══════════════════════════════════════

class JobComplete(BaseModel):
    result: dict | None = None


@router.post("/jobs/{job_id}/complete")
def api_complete_job(job_id: int, req: JobComplete,
                     worker_id: str = Query(...)):
    """标记任务完成。"""
    # 验证 worker_id
    job_row = fetch_one("SELECT worker_id FROM public.xm_jobs WHERE job_id = %s", (job_id,))
    if not job_row:
        return {"ok": False, "error": "任务不存在"}
    if job_row.get("worker_id") != worker_id:
        return {"ok": False, "error": "无权操作此任务"}

    return complete_job(job_id, req.result)


class JobFail(BaseModel):
    error_message: str


@router.post("/jobs/{job_id}/fail")
def api_fail_job(job_id: int, req: JobFail,
                 worker_id: str = Query(...)):
    """标记任务失败。"""
    # 验证 worker_id
    job_row = fetch_one("SELECT worker_id FROM public.xm_jobs WHERE job_id = %s", (job_id,))
    if not job_row:
        return {"ok": False, "error": "任务不存在"}
    if job_row.get("worker_id") != worker_id:
        return {"ok": False, "error": "无权操作此任务"}

    return fail_job(job_id, req.error_message)


# ═══════════════════════════════════════
# 手动重置任务
# ═══════════════════════════════════════

@router.post("/jobs/{job_id}/reset")
def api_reset_job(job_id: int):
    """手动重置任务为 pending（Web UI 用，无需 worker_id 验证）。"""
    return reset_job(job_id)


@router.delete("/jobs/all")
def api_delete_all_jobs():
    """删除所有任务。"""
    return delete_all_jobs()


@router.delete("/jobs/{job_id}")
def api_delete_job(job_id: int):
    """删除任务（Web UI 用，不删除专辑和已上传章节）。"""
    return delete_job(job_id)


class BatchJobIds(BaseModel):
    job_ids: list[int]


@router.post("/jobs/batch-delete")
def api_batch_delete_jobs(req: BatchJobIds):
    """批量删除任务。"""
    return delete_jobs_batch(req.job_ids)


@router.post("/jobs/batch-reset")
def api_batch_reset_jobs(req: BatchJobIds):
    """批量重置任务为 pending。"""
    return reset_jobs_batch(req.job_ids)


@router.delete("/jobs/status/{status}")
def api_delete_jobs_by_status(status: str):
    """按状态删除所有匹配的任务。"""
    return delete_jobs_by_status(status)


@router.post("/jobs/status/{status}/reset")
def api_reset_jobs_by_status(status: str):
    """按状态批量重置所有匹配的任务为 pending。"""
    return reset_jobs_by_status(status)


@router.post("/jobs/release")
def api_release_job(worker_id: str = Query(...)):
    """Worker 退出时释放自己 processing 的任务。"""
    return release_job(worker_id)


# ═══════════════════════════════════════
# 配置分发
# ═══════════════════════════════════════

@router.get("/config")
def api_config(worker_id: str = Query("")):
    """分发配置给 Colab Worker（TG Token、Cookie 等）。"""
    # 更新心跳
    if worker_id:
        heartbeat(worker_id)

    # 读取全局设置
    settings_rows = fetch_all("SELECT setting_key, setting_value FROM public.global_settings")
    settings_map = {row["setting_key"]: row["setting_value"] for row in (settings_rows or [])}

    return {
        "ok": True,
        "config": {
            "tg_bot_tokens": [t.strip() for t in settings_map.get("TG_BOT_TOKEN", "").split(",") if t.strip()],
            "tg_chat_id": settings_map.get("TG_CHAT_ID", ""),
            "xm_cookie": settings_map.get("XM_COOKIE", ""),
            "enable_deepfilter": settings_map.get("ENABLE_DEEPFILTER", "true").lower() == "true",
            "deepfilter_model": settings_map.get("DEEPFILTER_MODEL", "DeepFilterNet2"),
            "deepfilter_segment_minutes": int(settings_map.get("DEEPFILTER_SEGMENT_MINUTES", "60")),
            "download_interval": float(settings_map.get("DOWNLOAD_INTERVAL", "1.5")),
            "download_retries": int(settings_map.get("DOWNLOAD_RETRIES", "10")),
            "tg_serial_upload": settings_map.get("TG_SERIAL_UPLOAD", "true").lower() == "true",
            "tg_upload_interval": float(settings_map.get("TG_UPLOAD_INTERVAL", "3")),
            "proxy_enabled": settings_map.get("PROXY_ENABLED", "false").lower() == "true",
            "proxy_list": _get_effective_proxy_list(settings_map),
            "proxy_list_url": settings_map.get("PROXY_LIST_URL", ""),
            "proxy_verify_country": settings_map.get("PROXY_VERIFY_COUNTRY", "中国"),
            "proxy_max_tests": int(settings_map.get("PROXY_MAX_TESTS", "100")),
            "proxy_refresh_hours": float(settings_map.get("PROXY_REFRESH_HOURS", "2")),
            "proxy_test_url": settings_map.get("PROXY_TEST_URL", "https://www.ximalaya.com"),
            "proxy_timeout": int(settings_map.get("PROXY_TIMEOUT", "10")),
            "audio_quality": settings_map.get("XM_AUDIO_QUALITY", "M4A_24"),
        },
    }


# ═══════════════════════════════════════
# Worker 心跳
# ═══════════════════════════════════════

@router.post("/worker/heartbeat")
def api_heartbeat(worker_id: str = Query(...)):
    """Worker 心跳。"""
    return heartbeat(worker_id)


# ═══════════════════════════════════════
# 任务列表（Web UI 用）
# ═══════════════════════════════════════

@router.get("/jobs")
def api_jobs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str = Query(""),
):
    from ..services.job_service import get_jobs
    return get_jobs(page, page_size, status)


@router.get("/jobs/{job_id}")
def api_job_detail(job_id: int):
    from ..services.job_service import get_job_detail
    job = get_job_detail(job_id)
    if not job:
        return {"ok": False, "error": "任务不存在"}
    return {"ok": True, "job": job}


# ═══════════════════════════════════════
# 创建任务（Web UI 用）
# ═══════════════════════════════════════

class CreateJobRequest(BaseModel):
    book_id: str


@router.post("/jobs/create")
def api_create_job(req: CreateJobRequest):
    from ..services.job_service import create_job
    job = create_job(req.book_id)
    if not job:
        return {"ok": False, "error": "专辑不存在或无待处理章节"}
    return {"ok": True, "job": job}


class CreateJobsBatchRequest(BaseModel):
    book_ids: list[str]


@router.post("/jobs/create-batch")
def api_create_jobs_batch(req: CreateJobsBatchRequest):
    from ..services.job_service import create_jobs_batch
    jobs = create_jobs_batch(req.book_ids)
    return {"ok": True, "jobs": jobs, "count": len(jobs)}
