"""Database connection helpers — Postgres-only (SQLite path removed)."""

import threading

from ..db_compat import open_pg_compat_conn

from ..core.settings import (
    PG_DBNAME,
    PG_HOST,
    PG_PASSWORD,
    PG_PORT,
    PG_SSLMODE,
    PG_USER,
)

_PG_SCHEMA_LOCK = threading.Lock()
_PG_SCHEMA_READY = False


def _qident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _ensure_postgres_id_defaults(conn) -> None:
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.columns
        WHERE table_schema='public'
          AND column_name='id'
          AND is_nullable='NO'
          AND column_default IS NULL
        ORDER BY table_name
        """
    ).fetchall()
    skip_tables = {"macro"}
    for row in rows:
        table = str(row["table_name"])
        if table in skip_tables:
            continue
        seq_name = f"{table}_id_seq"
        table_q = _qident(table)
        seq_q = _qident(seq_name)
        conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq_q}")
        conn.execute(
            f"ALTER TABLE public.{table_q} "
            f"ALTER COLUMN id SET DEFAULT nextval('{seq_name}'::regclass)"
        )
        conn.execute(
            f"SELECT setval('{seq_name}', COALESCE((SELECT MAX(id) FROM public.{table_q}), 0) + 1, false)"
        )


def _ensure_postgres_runtime_schema(conn) -> None:
    global _PG_SCHEMA_READY
    if _PG_SCHEMA_READY:
        return
    with _PG_SCHEMA_LOCK:
        if _PG_SCHEMA_READY:
            return
        _ensure_postgres_id_defaults(conn)
        _PG_SCHEMA_READY = True


def _open_postgres_conn():
    conn = open_pg_compat_conn(
        host=PG_HOST,
        port=PG_PORT,
        dbname=PG_DBNAME,
        user=PG_USER,
        password=PG_PASSWORD,
        sslmode=PG_SSLMODE,
        connect_timeout=5,
    )
    _ensure_postgres_runtime_schema(conn)
    return conn


def get_stocks_conn():
    return _open_postgres_conn()


def get_news_conn():
    # 뉴스/게시판 데이터도 Postgres로 통합됨
    return _open_postgres_conn()
