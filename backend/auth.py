"""Web 密码认证 + Colab Worker Token 认证。"""

from __future__ import annotations

import hmac
import hashlib
import base64
import time
import json
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, JSONResponse

from .settings import settings

logger = logging.getLogger(__name__)

# Cookie 名称
COOKIE_NAME = "xm_auth"
COOKIE_MAX_AGE = 365 * 24 * 3600

# 不需要认证的路径前缀
PUBLIC_PATHS = (
    "/login",
    "/api/docs",
    "/api/redoc",
    "/openapi.json",
    "/api/system/health",
)

# Colab Worker API 路径前缀（使用 Worker Token 认证）
WORKER_API_PATHS = (
    "/api/jobs/claim",
    "/api/jobs/",  # 以 job_id 为路径参数的 Worker 端点（如 /api/jobs/{id}/chapter）
    "/api/config",
    "/api/worker/heartbeat",
)

# Worker API 中需要走 Web Cookie 认证的例外（管理端接口）
WORKER_API_EXEMPTS = (
    "/api/jobs/create",
    "/api/jobs/create-batch",
)


def _sign(payload: str) -> str:
    """HMAC-SHA256 签名。"""
    key = settings.secret_key.encode("utf-8")
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_auth_cookie_value() -> str:
    """生成签名 Cookie 值。"""
    payload = json.dumps({"t": int(time.time())}, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("utf-8")
    sig = _sign(payload_b64)
    return f"{payload_b64}.{sig}"


def verify_auth_cookie(cookie_value: str) -> bool:
    """验证签名 Cookie。"""
    if not cookie_value or "." not in cookie_value:
        return False
    parts = cookie_value.split(".", 1)
    if len(parts) != 2:
        return False
    payload_b64, sig = parts
    expected_sig = _sign(payload_b64)
    if not hmac.compare_digest(sig, expected_sig):
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")))
        issued_at = payload.get("t", 0)
        if time.time() - issued_at > COOKIE_MAX_AGE:
            return False
        return True
    except Exception:
        return False


def is_authenticated(request: Request) -> bool:
    """检查请求是否已认证。"""
    cookie = request.cookies.get(COOKIE_NAME)
    return verify_auth_cookie(cookie) if cookie else False


def _is_public(path: str) -> bool:
    for prefix in PUBLIC_PATHS:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            return True
    return False


def _is_worker_api(path: str) -> bool:
    """判断路径是否为 Colab Worker API。"""
    for exempt in WORKER_API_EXEMPTS:
        if path == exempt or path.startswith(exempt + "?"):
            return False
    for prefix in WORKER_API_PATHS:
        if path.startswith(prefix):
            return True
    return False


def _verify_worker_token(request: Request) -> bool:
    """验证 Colab Worker Token（Header 或 Query 参数）。"""
    token = request.headers.get("X-Worker-Token", "") or request.query_params.get("worker_token", "")
    if not token:
        return False
    return hmac.compare_digest(token, settings.worker_auth_token)


class AuthMiddleware(BaseHTTPMiddleware):
    """认证中间件 — Web Cookie 认证 + Colab Worker Token 认证。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 公开路径
        if _is_public(path):
            return await call_next(request)

        # Colab Worker API — Token 认证
        if _is_worker_api(path):
            if _verify_worker_token(request):
                return await call_next(request)
            return JSONResponse(status_code=401, content={"detail": "Worker Token 无效"})

        # Web Cookie 认证
        if is_authenticated(request):
            return await call_next(request)

        # API 请求返回 401
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"detail": "未登录或登录已过期"})

        # 页面重定向到登录
        return RedirectResponse(url="/login", status_code=302)
