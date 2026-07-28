"""全局设置 API。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from psycopg import sql
from ..database import fetch_all, execute

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/settings")
def get_settings():
    """获取全局设置。"""
    rows = fetch_all(
        "SELECT setting_key, setting_value, description, is_secret "
        "FROM public.global_settings ORDER BY setting_key"
    )
    # 对 secret 设置隐藏值
    result = []
    for row in (rows or []):
        item = dict(row)
        if item.get("is_secret") and item.get("setting_value"):
            item["setting_value"] = "******"
        result.append(item)
    return {"settings": result}


class UpdateSetting(BaseModel):
    setting_key: str
    setting_value: str
    description: str | None = None
    is_secret: bool | None = None


@router.post("/settings")
def update_setting(req: UpdateSetting):
    """更新或新建单个设置项。

    如果值是 ****** 则跳过（不修改，用于 secret 字段未修改时）。
    新建设置项时可附带 description 和 is_secret。
    """
    if req.setting_value == "******":
        return {"ok": True, "skipped": True}

    # COALESCE 保留已有的 description / is_secret
    execute(
        sql.SQL("""
            INSERT INTO public.global_settings (setting_key, setting_value, description, is_secret, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (setting_key) DO UPDATE SET
                setting_value = EXCLUDED.setting_value,
                description = COALESCE(NULLIF(EXCLUDED.description, ''), public.global_settings.description),
                is_secret = COALESCE(EXCLUDED.is_secret, public.global_settings.is_secret),
                updated_at = now()
        """),
        (
            req.setting_key,
            req.setting_value,
            req.description or "",
            req.is_secret,
        ),
    )
    return {"ok": True, "key": req.setting_key}


@router.delete("/settings/{key}")
def delete_setting(key: str):
    """删除设置项。"""
    rowcount = execute(
        sql.SQL("DELETE FROM public.global_settings WHERE setting_key = %s"),
        (key,),
    )
    return {"ok": True, "deleted": rowcount}
