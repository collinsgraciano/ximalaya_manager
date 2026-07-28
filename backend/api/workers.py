"""Worker 统计 API。"""

from __future__ import annotations

from fastapi import APIRouter
from ..services.job_service import get_worker_stats

router = APIRouter(prefix="/api", tags=["workers"])


@router.get("/workers")
def api_workers():
    """获取所有 Worker 统计。"""
    workers = get_worker_stats()
    return {"workers": workers}
