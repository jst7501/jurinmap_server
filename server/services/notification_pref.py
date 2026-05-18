"""
사용자별 알림 종류 × 시간대 환경설정.

테이블: user_notification_pref(token, pref_json, updated_at)

pref_json 스키마:
{
  "kinds": {
    "news":     {"enabled": true,  "hours": [0,1,2,...,23]},
    "briefing": {"enabled": true,  "hours": [6,11,15,20]},
    "stock":    {"enabled": true,  "hours": [9,10,...,15]}
  }
}

- enabled=false 면 시간 무관 차단
- hours 비어있으면 해당 kind 모두 차단 (UI 에서 모든 칸 OFF 한 경우)
- pref 자체가 없으면 default = 모든 kind/시간 허용 (기존 사용자 backward compat)
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Iterable

logger = logging.getLogger("server.services.notification_pref")

ALL_HOURS = list(range(24))
DEFAULT_PREF: dict = {
    "kinds": {
        "news": {"enabled": True, "hours": ALL_HOURS},
        "briefing": {"enabled": True, "hours": [6, 11, 15, 20]},
        "stock": {"enabled": True, "hours": list(range(9, 16))},
    }
}

KNOWN_KINDS = {"news", "briefing", "stock"}

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False

_PREF_CACHE: dict[str, tuple[float, dict]] = {}
_PREF_CACHE_TTL = 30.0


def _ensure_table_locked(conn) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_notification_pref (
                token TEXT PRIMARY KEY,
                pref_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        try:
            conn.commit()
        except Exception:
            pass
        _SCHEMA_READY = True


def _get_conn():
    # 지연 import로 cyclic 방지
    from server.db.connections import get_stocks_conn

    return get_stocks_conn()


def _normalize_pref(raw: dict) -> dict:
    """입력 pref 를 안전 형식으로 정규화."""
    pref: dict = {"kinds": {}}
    kinds = (raw or {}).get("kinds") or {}
    for kind in KNOWN_KINDS:
        cfg = kinds.get(kind) or {}
        enabled = bool(cfg.get("enabled", True))
        raw_hours = cfg.get("hours")
        if raw_hours is None:
            hours = list(ALL_HOURS)
        else:
            hours = sorted({int(h) for h in raw_hours if isinstance(h, (int, float)) and 0 <= int(h) <= 23})
        pref["kinds"][kind] = {"enabled": enabled, "hours": hours}
    return pref


def get_pref(token: str) -> dict:
    """토큰별 pref 조회. 캐시 30초. 없으면 DEFAULT_PREF."""
    if not token:
        return dict(DEFAULT_PREF)
    now = time.time()
    cached = _PREF_CACHE.get(token)
    if cached and (now - cached[0]) < _PREF_CACHE_TTL:
        return cached[1]
    conn = _get_conn()
    try:
        _ensure_table_locked(conn)
        row = conn.execute(
            "SELECT pref_json FROM user_notification_pref WHERE token=%s",
            (token,),
        ).fetchone()
    except Exception:
        logger.exception("get_pref failed")
        return dict(DEFAULT_PREF)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not row or not row[0]:
        pref = dict(DEFAULT_PREF)
    else:
        try:
            pref = _normalize_pref(json.loads(row[0]))
        except Exception:
            pref = dict(DEFAULT_PREF)

    _PREF_CACHE[token] = (now, pref)
    return pref


def upsert_pref(token: str, raw_pref: dict) -> dict:
    if not token:
        raise ValueError("token required")
    pref = _normalize_pref(raw_pref or {})
    payload = json.dumps(pref, ensure_ascii=False)
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_conn()
    try:
        _ensure_table_locked(conn)
        conn.execute(
            """
            INSERT INTO user_notification_pref (token, pref_json, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (token) DO UPDATE SET
                pref_json = excluded.pref_json,
                updated_at = excluded.updated_at
            """,
            (token, payload, updated_at),
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    _PREF_CACHE.pop(token, None)
    return pref


def is_token_allowed(token: str, kind: str, hour: int | None = None) -> bool:
    """토큰이 (kind, hour) 시점에 알림 받기를 허용하는지."""
    if not token:
        return True  # 토큰 없으면 사용자 식별 불가 → 보수적으로 허용
    if kind not in KNOWN_KINDS:
        return True  # 알 수 없는 종류는 통과
    if hour is None:
        hour = datetime.now().hour
    pref = get_pref(token)
    cfg = pref.get("kinds", {}).get(kind) or {}
    if not cfg.get("enabled", True):
        return False
    return int(hour) in set(cfg.get("hours", ALL_HOURS))


def filter_allowed_tokens(tokens: Iterable[str], kind: str, hour: int | None = None) -> list[str]:
    if hour is None:
        hour = datetime.now().hour
    return [t for t in (tokens or []) if is_token_allowed(t, kind, hour)]
