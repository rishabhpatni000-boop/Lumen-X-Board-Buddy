#!/usr/bin/env python3
"""Small Postgres access layer for Lumen's Neon backend."""

from __future__ import annotations

import os
import threading

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row


_POOL = None
_POOL_URL = None
_POOL_LOCK = threading.Lock()


def data_provider() -> str:
    return "neon" if os.getenv("DATA_PROVIDER", "supabase").strip().lower() == "neon" else "supabase"


def neon_database_enabled() -> bool:
    return data_provider() == "neon" and bool(os.getenv("DATABASE_URL", "").strip())


def connection_pool() -> ConnectionPool:
    global _POOL, _POOL_URL
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    if _POOL is not None and _POOL_URL == database_url:
        return _POOL
    with _POOL_LOCK:
        if _POOL is not None and _POOL_URL == database_url:
            return _POOL
        if _POOL is not None:
            _POOL.close()
        _POOL = ConnectionPool(
            conninfo=database_url,
            min_size=0,
            max_size=max(2, int(os.getenv("DATABASE_POOL_SIZE", "8"))),
            timeout=float(os.getenv("DATABASE_POOL_TIMEOUT", "15")),
            kwargs={"row_factory": dict_row},
            check=ConnectionPool.check_connection,
            open=True,
        )
        _POOL_URL = database_url
        return _POOL


def fetch_all(statement: str, params=()) -> list[dict]:
    with connection_pool().connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(statement, params)
            return list(cursor.fetchall())


def fetch_one(statement: str, params=()) -> dict | None:
    with connection_pool().connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(statement, params)
            return cursor.fetchone()


def execute(statement: str, params=()) -> int:
    with connection_pool().connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(statement, params)
            return cursor.rowcount
