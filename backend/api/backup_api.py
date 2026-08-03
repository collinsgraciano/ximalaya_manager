"""B2 云备份 API 路由。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from ..services import backup_service

router = APIRouter(prefix="/api/backup", tags=["backup"])


# ═══════════════════════════════════════════
# 配置管理
# ═══════════════════════════════════════════

@router.get("/config")
def get_backup_config():
    """获取备份配置（secret 字段脱敏）。"""
    return {"config": backup_service.get_backup_config()}


class SaveConfigReq(BaseModel):
    config: dict


@router.post("/config")
def save_backup_config(req: SaveConfigReq):
    """保存备份配置。"""
    return backup_service.save_backup_config(req.config)


# ═══════════════════════════════════════════
# 备份操作
# ═══════════════════════════════════════════

@router.post("/run")
def run_backup():
    """立即执行备份（后台异步）。"""
    return backup_service.run_backup_async()


@router.get("/status")
def get_backup_status():
    """获取备份进度状态。"""
    return backup_service.get_backup_status()


# ═══════════════════════════════════════════
# 恢复操作
# ═══════════════════════════════════════════

class RestoreReq(BaseModel):
    folder: str = ""       # 空字符串表示最新
    mode: str = "full"     # "full" | "db" | "config"


@router.post("/restore")
def run_restore(req: RestoreReq):
    """执行数据恢复（后台异步）。"""
    return backup_service.run_restore_async(req.folder, req.mode)


@router.get("/restore/status")
def get_restore_status():
    """获取恢复进度状态。"""
    return backup_service.get_restore_status()


# ═══════════════════════════════════════════
# 备份列表
# ═══════════════════════════════════════════

@router.get("/list")
def list_backups():
    """列出 B2 中的所有备份。"""
    return {"backups": backup_service.list_backups()}


# ═══════════════════════════════════════════
# 定时备份
# ═══════════════════════════════════════════

class ScheduleReq(BaseModel):
    interval_hours: float = 24.0


@router.post("/schedule/start")
def start_schedule(req: ScheduleReq):
    """开启定时备份。"""
    return backup_service.start_scheduled_backup(req.interval_hours)


@router.post("/schedule/stop")
def stop_schedule():
    """停止定时备份。"""
    return backup_service.stop_scheduled_backup()


@router.get("/schedule/status")
def get_schedule_status():
    """获取定时备份状态。"""
    return backup_service.get_schedule_status()
