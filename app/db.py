"""PostgreSQL 访问层（psycopg 3 连接池，同步驱动）。"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: Optional[ConnectionPool] = None

DDL_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    return (
        f"postgresql://{os.environ.get('POSTGRES_USER', 'coldchain')}:"
        f"{os.environ.get('POSTGRES_PASSWORD', 'coldchain')}"
        f"@{os.environ.get('POSTGRES_HOST', 'db')}:"
        f"{os.environ.get('POSTGRES_PORT', '5432')}"
        f"/{os.environ.get('POSTGRES_DB', 'coldchain')}"
    )


def init_pool() -> ConnectionPool:
    """启动时调用：连接池建连（等数据库就绪）并幂等执行 DDL。"""
    global _pool
    if _pool is not None:
        return _pool

    url = _database_url()
    # 不立即建连；wait 会在数据库启动期间持续重试约 60 秒。
    pool = ConnectionPool(
        url, min_size=1, max_size=10,
        kwargs={"autocommit": False, "row_factory": dict_row},
        open=False,
    )
    pool.wait(timeout=60.0)

    with open(DDL_PATH, "r", encoding="utf-8") as f:
        ddl = f.read()
    with pool.connection() as conn:
        conn.execute(ddl)
        conn.commit()

    _pool = pool
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("连接池未初始化")
    return _pool


@contextmanager
def transaction() -> Iterator[Connection]:
    """事务上下文：正常退出提交，异常回滚。"""
    pool = get_pool()
    with pool.connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
