"""数据库连接与查询工具 — psycopg3 连接池。"""

from __future__ import annotations

import threading
from typing import Any, Sequence
from psycopg import connect, sql
from psycopg.rows import dict_row, tuple_row

from .settings import get_dsn


# ═══════════════════════════════════════════════════════════
# 连接池（惰性初始化，线程安全）
# ═══════════════════════════════════════════════════════════

_pool = None
_pool_lock = threading.Lock()
_POOL_MIN = 1
_POOL_MAX = 5
_POOL_TIMEOUT = 10


def _get_pool():
    """获取或创建全局连接池（线程安全，double-checked locking）。"""
    global _pool
    if _pool is not None:
        return _pool

    with _pool_lock:
        if _pool is not None:
            return _pool

        try:
            from psycopg_pool import ConnectionPool
        except ImportError:
            return None

        _pool = ConnectionPool(
            conninfo=get_dsn(),
            min_size=_POOL_MIN,
            max_size=_POOL_MAX,
            timeout=_POOL_TIMEOUT,
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
            },
            open=True,
        )
        return _pool


def close_pool():
    """关闭连接池（应用退出时调用）。"""
    global _pool
    if _pool is not None:
        try:
            _pool.close()
        except Exception:
            pass
        _pool = None


# ═══════════════════════════════════════════════════════════
# 同步查询
# ═══════════════════════════════════════════════════════════

def fetch_one(statement, params=None) -> dict[str, Any] | None:
    """执行查询，返回单行。"""
    pool = _get_pool()
    if pool is not None:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, params or ())
                row = cur.fetchone()
                return dict(row) if row else None
    with connect(get_dsn(), autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(statement, params or ())
            row = cur.fetchone()
            return dict(row) if row else None


def fetch_all(statement, params=None) -> list[dict[str, Any]]:
    """执行查询，返回多行。"""
    pool = _get_pool()
    if pool is not None:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, params or ())
                return [dict(row) for row in cur.fetchall()]
    with connect(get_dsn(), autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(statement, params or ())
            return [dict(row) for row in cur.fetchall()]


def fetch_val(statement, params=None):
    """执行查询，返回单个标量值（第一列）。"""
    pool = _get_pool()
    if pool is not None:
        with pool.connection() as conn:
            with conn.cursor(row_factory=tuple_row) as cur:
                cur.execute(statement, params or ())
                row = cur.fetchone()
                return row[0] if row else None
    with connect(get_dsn(), autocommit=True) as conn:
        with conn.cursor(row_factory=tuple_row) as cur:
            cur.execute(statement, params or ())
            row = cur.fetchone()
            return row[0] if row else None


def execute(statement, params=None) -> int:
    """执行 INSERT/UPDATE/DELETE，返回受影响行数。"""
    pool = _get_pool()
    if pool is not None:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, params or ())
                return cur.rowcount
    with connect(get_dsn(), autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(statement, params or ())
            return cur.rowcount


def execute_batch(statement, params_seq: Sequence[tuple]) -> None:
    """批量执行 INSERT/UPDATE（如 executemany），减少 DB 往返次数。"""
    pool = _get_pool()
    if pool is not None:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(statement, params_seq)
        return
    with connect(get_dsn(), autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.executemany(statement, params_seq)


def execute_returning(statement, params=None) -> dict[str, Any] | None:
    """执行 INSERT/UPDATE 带 RETURNING，返回首行。"""
    pool = _get_pool()
    if pool is not None:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, params or ())
                row = cur.fetchone()
                return dict(row) if row else None
    with connect(get_dsn(), autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(statement, params or ())
            row = cur.fetchone()
            return dict(row) if row else None


def table_identifier(table_name: str):
    """返回 public.table_name 的 sql.Identifier。"""
    return sql.Identifier("public", table_name)


# ═══════════════════════════════════════════════════════════
# 重导出
# ═══════════════════════════════════════════════════════════

__all__ = [
    "fetch_one", "fetch_all", "fetch_val",
    "execute", "execute_batch", "execute_returning",
    "close_pool", "table_identifier",
]
