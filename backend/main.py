"""FastAPI 应用入口 — 喜马拉雅有声书管理系统。"""

from __future__ import annotations

import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .settings import settings as app_settings
from .auth import AuthMiddleware, COOKIE_NAME, COOKIE_MAX_AGE, create_auth_cookie_value
from .api import albums, jobs, workers, settings_api, dashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。"""
    logger.info("应用启动中...")
    logger.info(f"基础 URL: {app_settings.base_url}")

    # 数据库迁移（幂等，补充可能缺失的列）
    try:
        from .database import execute
        from psycopg import sql as pg_sql
        # 兼容已有表：补充列
        execute(pg_sql.SQL("ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS chapter_order integer"))
        execute(pg_sql.SQL("ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS duration integer"))
        # xm_jobs 补充 params 列（Colab 传递参数）
        execute(pg_sql.SQL("ALTER TABLE public.xm_jobs ADD COLUMN IF NOT EXISTS params jsonb"))
        logger.info("数据库迁移完成")
    except Exception as e:
        logger.warning(f"数据库迁移失败（非致命）: {e}")

    yield

    # 关闭连接池
    try:
        from .database import close_pool
        close_pool()
    except Exception:
        pass
    logger.info("应用关闭")


app = FastAPI(
    title="喜马拉雅有声书管理系统",
    description="采集、处理、TG缓存管理",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan,
)

app.add_middleware(AuthMiddleware)

# 注册 API 路由
app.include_router(dashboard.router)
app.include_router(albums.router)
app.include_router(jobs.router)
app.include_router(workers.router)
app.include_router(settings_api.router)

# Jinja2 模板
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


# ═══════════════════════════════════════
# 登录 / 登出
# ═══════════════════════════════════════

@app.get("/login", response_class=HTMLResponse)
async def page_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def do_login(request: Request, password: str = Form(...)):
    if password == app_settings.app_password:
        resp = RedirectResponse(url="/", status_code=302)
        resp.set_cookie(
            key=COOKIE_NAME,
            value=create_auth_cookie_value(),
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
        logger.info("用户登录成功")
        return resp
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": "密码错误，请重试",
    })


@app.get("/logout")
async def do_logout():
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ═══════════════════════════════════════
# 页面路由（服务端渲染）
# ═══════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/albums", response_class=HTMLResponse)
async def page_albums(request: Request):
    return templates.TemplateResponse("albums.html", {"request": request})


@app.get("/albums/{book_id}", response_class=HTMLResponse)
async def page_album_detail(request: Request, book_id: str):
    return templates.TemplateResponse("album_detail.html", {
        "request": request, "book_id": book_id,
    })


@app.get("/jobs", response_class=HTMLResponse)
async def page_jobs(request: Request):
    return templates.TemplateResponse("jobs.html", {"request": request})


@app.get("/workers", response_class=HTMLResponse)
async def page_workers(request: Request):
    return templates.TemplateResponse("workers.html", {"request": request})


@app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request})


@app.get("/api/system/health")
async def health():
    return {"ok": True, "service": "ximalaya_manager"}
