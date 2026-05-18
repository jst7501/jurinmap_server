import re
from functools import lru_cache
from typing import Any, Iterable, Optional

import psycopg


def _replace_group_concat(sql: str) -> str:
    upper = sql.upper()
    needle = "GROUP_CONCAT("
    i = 0
    out: list[str] = []

    while True:
        idx = upper.find(needle, i)
        if idx < 0:
            out.append(sql[i:])
            break

        out.append(sql[i:idx])
        j = idx + len(needle)
        depth = 1
        in_single = False
        in_double = False

        while j < len(sql):
            ch = sql[j]
            if ch == "'" and not in_double:
                if in_single and j + 1 < len(sql) and sql[j + 1] == "'":
                    j += 2
                    continue
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif not in_single and not in_double:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1

        if j >= len(sql):
            out.append(sql[idx:])
            break

        args_text = sql[idx + len(needle) : j].strip()
        arg1, arg2 = _split_top_level_comma(args_text)
        if arg2 is None:
            rep = f"STRING_AGG(({arg1})::text, ',')"
        else:
            rep = f"STRING_AGG(({arg1})::text, {arg2})"
        out.append(rep)
        i = j + 1

    return "".join(out)


def _split_top_level_comma(s: str) -> tuple[str, Optional[str]]:
    depth = 0
    in_single = False
    in_double = False
    for i, ch in enumerate(s):
        if ch == "'" and not in_double:
            if in_single and i + 1 < len(s) and s[i + 1] == "'":
                continue
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                return s[:i].strip(), s[i + 1 :].strip()
    return s.strip(), None


def _qmark_to_pyformat(sql: str) -> str:
    out: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_double:
            if in_single and i + 1 < len(sql) and sql[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            in_single = not in_single
            out.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            out.append(ch)
            i += 1
            continue
        if ch == "?" and not in_single and not in_double:
            out.append("%s")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


@lru_cache(maxsize=4096)
def _rewrite_sql(sql: str) -> Optional[str]:
    """Rewrite SQLite-flavored SQL into Postgres-compatible SQL.

    Returns the rewritten string, or None for queries that have no Postgres
    equivalent (PRAGMA, sqlite_master) — callers should treat that as a no-op.

    Cached: the same SQL string is rewritten only once per process. Hot paths
    re-execute identical statements thousands of times so this saves substantial
    regex/parsing CPU.
    """
    stripped = sql.strip()
    if not stripped:
        return sql
    upper_prefix = stripped.upper()
    if upper_prefix.startswith("PRAGMA "):
        return None
    # SQLite-only metadata queries: no-op on Postgres (callers usually fall back
    # to pg_tables / information_schema or simply ignore the empty result).
    if "SQLITE_MASTER" in upper_prefix:
        return None

    rewritten = sql

    # SQLite INSERT OR IGNORE / OR REPLACE → Postgres ON CONFLICT DO NOTHING / UPDATE
    had_or_ignore = bool(re.search(r"\bOR\s+IGNORE\b", rewritten, flags=re.IGNORECASE))
    had_or_replace = bool(re.search(r"\bOR\s+REPLACE\b", rewritten, flags=re.IGNORECASE))
    rewritten = re.sub(
        r"\bINSERT\s+OR\s+(IGNORE|REPLACE)\b",
        "INSERT",
        rewritten,
        flags=re.IGNORECASE,
    )

    # SQLite-specific functions → Postgres equivalents
    rewritten = re.sub(r"\bIFNULL\s*\(", "COALESCE(", rewritten, flags=re.IGNORECASE)
    rewritten = re.sub(r"\bdatetime\s*\(\s*'now'\s*\)", "CURRENT_TIMESTAMP", rewritten, flags=re.IGNORECASE)
    rewritten = re.sub(r"\bdate\s*\(\s*'now'\s*\)", "CURRENT_DATE", rewritten, flags=re.IGNORECASE)

    # SQLite DDL shims: AUTOINCREMENT → (nothing, Postgres uses SERIAL/BIGSERIAL),
    # INTEGER PRIMARY KEY AUTOINCREMENT → BIGSERIAL PRIMARY KEY.
    rewritten = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "BIGSERIAL PRIMARY KEY",
        rewritten,
        flags=re.IGNORECASE,
    )
    rewritten = re.sub(r"\bAUTOINCREMENT\b", "", rewritten, flags=re.IGNORECASE)

    rewritten = _replace_group_concat(rewritten)
    rewritten = _qmark_to_pyformat(rewritten)

    if had_or_ignore and re.search(r"\bINSERT\b", rewritten, flags=re.IGNORECASE):
        rewritten = f"{rewritten} ON CONFLICT DO NOTHING"
    # NOTE: OR REPLACE → Postgres needs an explicit target column for ON CONFLICT,
    # so we don't auto-synthesize it. Callers should use explicit ON CONFLICT DO UPDATE.

    return rewritten


class PgCompatRow:
    def __init__(self, columns: list[str], values: tuple[Any, ...]):
        self._columns = columns
        self._values = list(values)
        self._dict = {k: v for k, v in zip(columns, values)}

    def keys(self):
        return list(self._columns)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._dict[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


class _NoopCursor:
    def execute(self, sql: str, params: Optional[Iterable[Any]] = None):
        return self

    def executemany(self, sql: str, param_sets: Iterable[Iterable[Any]]):
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, size: int = 0):
        return []

    def close(self):
        return None


class PgCompatCursor:
    def __init__(self, raw_cursor):
        self._raw = raw_cursor
        self._noop = False
        self._columns: list[str] = []

    def execute(self, sql: str, params: Optional[Iterable[Any]] = None):
        rewritten = _rewrite_sql(sql)
        if rewritten is None:
            self._noop = True
            self._columns = []
            return self
        self._noop = False
        self._raw.execute(rewritten, tuple(params or ()))
        self._columns = [d.name for d in (self._raw.description or [])]
        return self

    def executemany(self, sql: str, param_sets: Iterable[Iterable[Any]]):
        rewritten = _rewrite_sql(sql)
        if rewritten is None:
            self._noop = True
            self._columns = []
            return self
        self._noop = False
        self._raw.executemany(rewritten, param_sets)
        self._columns = [d.name for d in (self._raw.description or [])]
        return self

    def _wrap(self, row: Optional[tuple[Any, ...]]):
        if row is None:
            return None
        return PgCompatRow(self._columns, row)

    def fetchone(self):
        if self._noop:
            return None
        return self._wrap(self._raw.fetchone())

    def fetchall(self):
        if self._noop:
            return []
        return [self._wrap(r) for r in self._raw.fetchall()]

    def fetchmany(self, size: int = 0):
        if self._noop:
            return []
        rows = self._raw.fetchmany(size) if size else self._raw.fetchmany()
        return [self._wrap(r) for r in rows]

    def close(self):
        return self._raw.close()


class PgCompatConnection:
    def __init__(self, raw_conn):
        self._raw = raw_conn

    def cursor(self):
        return PgCompatCursor(self._raw.cursor())

    def execute(self, sql: str, params: Optional[Iterable[Any]] = None):
        cur = self.cursor()
        return cur.execute(sql, params)

    def executemany(self, sql: str, param_sets: Iterable[Iterable[Any]]):
        cur = self.cursor()
        return cur.executemany(sql, param_sets)

    def executescript(self, sql: str):
        """SQLite executescript() 호환: 세미콜론으로 분리해 개별 실행."""
        stmts = [s.strip() for s in sql.split(";")]
        for stmt in stmts:
            if stmt:
                try:
                    self.execute(stmt)
                except Exception:
                    pass  # CREATE IF NOT EXISTS 등 무해한 오류 무시
        return self

    def commit(self):
        return self._raw.commit()

    def rollback(self):
        return self._raw.rollback()

    def close(self):
        return self._raw.close()


def open_pg_compat_conn(
    *,
    host: str,
    port: int,
    dbname: str,
    user: str,
    password: str,
    sslmode: str = "disable",
    connect_timeout: int = 5,
) -> PgCompatConnection:
    raw = psycopg.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
        sslmode=sslmode,
        connect_timeout=connect_timeout,
    )
    return PgCompatConnection(raw)
