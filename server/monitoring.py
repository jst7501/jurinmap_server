import threading
import time
import os
from collections import deque
from typing import Any

_LOCK = threading.Lock()
_STARTED_AT = time.time()
_WINDOW_SEC_DEFAULT = 300

_HTTP_TOTAL = 0
_HTTP_ERRORS = 0
_HTTP_LATENCY_SUM = 0.0
_HTTP_WINDOW = deque(maxlen=20000)  # (ts, is_error, latency_sec)

_KIS_TOTAL = 0
_KIS_ERRORS = 0
_KIS_LATENCY_SUM = 0.0
_KIS_WINDOW = deque(maxlen=20000)  # (ts, is_error, latency_sec)

_REDIS_GET_TOTAL = 0
_REDIS_GET_HIT = 0
_REDIS_GET_MISS = 0
_REDIS_GET_ERRORS = 0
_REDIS_GET_UNAVAILABLE = 0

_REDIS_SET_TOTAL = 0
_REDIS_SET_OK = 0
_REDIS_SET_ERRORS = 0
_REDIS_SET_UNAVAILABLE = 0

_REDIS_CLAIM_TOTAL = 0
_REDIS_CLAIM_OK = 0
_REDIS_CLAIM_EXISTS = 0
_REDIS_CLAIM_ERRORS = 0
_REDIS_CLAIM_UNAVAILABLE = 0

_REDIS_WINDOW = deque(maxlen=20000)  # (ts, kind)
_KIS_DEGRADE_STATE = {
    "last_eval_ts": 0.0,
    "streak": 0,
    "active": False,
    "last_ratio": 0.0,
    "last_total": 0,
}

# Phase 5 — KIS WebSocket + cache-read counters ─────────────
_KIS_WS_TICKS: dict[str, int] = {}          # tr_id → total count
_KIS_WS_RECONNECTS: dict[str, int] = {}     # tr_id → total count
_KIS_WS_LAST_TICK_TS: dict[str, float] = {} # tr_id → unix
_KIS_WS_WINDOW = deque(maxlen=20000)        # (ts, tr_id)

_CACHE_READ_TOTAL = 0
_CACHE_READ_WINDOW = deque(maxlen=30000)    # (ts, surface, outcome)


def _prune_window(dq: deque, now_ts: float, window_sec: int):
    threshold = now_ts - max(1, int(window_sec))
    while dq and dq[0][0] < threshold:
        dq.popleft()


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    arr = sorted(float(v) for v in values)
    if len(arr) == 1:
        return arr[0]
    rank = max(0.0, min(100.0, float(p))) / 100.0 * (len(arr) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(arr) - 1)
    frac = rank - lo
    return arr[lo] * (1.0 - frac) + arr[hi] * frac


def observe_http_request(status_code: int, latency_sec: float):
    is_error = int((status_code or 0) >= 400)
    now_ts = time.time()
    with _LOCK:
        global _HTTP_TOTAL, _HTTP_ERRORS, _HTTP_LATENCY_SUM
        _HTTP_TOTAL += 1
        _HTTP_ERRORS += is_error
        _HTTP_LATENCY_SUM += max(0.0, float(latency_sec or 0.0))
        _HTTP_WINDOW.append((now_ts, is_error, max(0.0, float(latency_sec or 0.0))))


def observe_kis_call(success: bool, latency_sec: float):
    is_error = 0 if success else 1
    now_ts = time.time()
    with _LOCK:
        global _KIS_TOTAL, _KIS_ERRORS, _KIS_LATENCY_SUM
        _KIS_TOTAL += 1
        _KIS_ERRORS += is_error
        _KIS_LATENCY_SUM += max(0.0, float(latency_sec or 0.0))
        _KIS_WINDOW.append((now_ts, is_error, max(0.0, float(latency_sec or 0.0))))


def observe_kis_ws_tick(tr_id: str) -> None:
    """KIS WS tick 수신 카운터."""
    tr = str(tr_id or "").strip() or "unknown"
    now_ts = time.time()
    with _LOCK:
        _KIS_WS_TICKS[tr] = int(_KIS_WS_TICKS.get(tr, 0)) + 1
        _KIS_WS_LAST_TICK_TS[tr] = now_ts
        _KIS_WS_WINDOW.append((now_ts, tr))


def observe_kis_ws_reconnect(tr_id: str, reason: str = "") -> None:
    tr = str(tr_id or "").strip() or "unknown"
    with _LOCK:
        _KIS_WS_RECONNECTS[tr] = int(_KIS_WS_RECONNECTS.get(tr, 0)) + 1


def observe_cache_read(surface: str, outcome: str) -> None:
    """Surface 별 캐시 읽기 결과. outcome = ws_hit / rest_hit / miss / stale."""
    s = str(surface or "").strip().lower() or "unknown"
    o = str(outcome or "").strip().lower() or "unknown"
    now_ts = time.time()
    with _LOCK:
        global _CACHE_READ_TOTAL
        _CACHE_READ_TOTAL += 1
        _CACHE_READ_WINDOW.append((now_ts, s, o))


def _window_ws_stats(now_ts: float, window_sec: int) -> dict[str, Any]:
    _prune_window(_KIS_WS_WINDOW, now_ts, window_sec)
    counts: dict[str, int] = {}
    for _, tr in _KIS_WS_WINDOW:
        counts[tr] = counts.get(tr, 0) + 1
    return {
        "tick_counts_total": dict(_KIS_WS_TICKS),
        "reconnects_total": dict(_KIS_WS_RECONNECTS),
        "last_tick_sec_ago": {
            tr: round(now_ts - ts, 2) for tr, ts in _KIS_WS_LAST_TICK_TS.items()
        },
        "window_ticks_per_tr": counts,
    }


def _window_cache_stats(now_ts: float, window_sec: int) -> dict[str, Any]:
    _prune_window(_CACHE_READ_WINDOW, now_ts, window_sec)
    # surface → {ws_hit, rest_hit, miss, stale, ratio}
    agg: dict[str, dict[str, int]] = {}
    for _, s, o in _CACHE_READ_WINDOW:
        row = agg.setdefault(s, {"ws_hit": 0, "rest_hit": 0, "miss": 0, "stale": 0, "other": 0})
        if o in row:
            row[o] += 1
        else:
            row["other"] += 1
    out: dict[str, Any] = {}
    for s, row in agg.items():
        total = sum(row.values())
        ws = row.get("ws_hit", 0)
        rest = row.get("rest_hit", 0)
        hit_ratio = (ws + rest) / total if total > 0 else 0.0
        ws_ratio = ws / total if total > 0 else 0.0
        out[s] = {
            **row,
            "total": total,
            "hit_ratio": round(hit_ratio, 4),
            "ws_ratio": round(ws_ratio, 4),
        }
    return {
        "window_sec": window_sec,
        "surfaces": out,
        "total_reads_lifetime": _CACHE_READ_TOTAL,
    }


def observe_redis_get(kind: str):
    now_ts = time.time()
    k = (kind or "error").strip().lower()
    with _LOCK:
        global _REDIS_GET_TOTAL, _REDIS_GET_HIT, _REDIS_GET_MISS, _REDIS_GET_ERRORS, _REDIS_GET_UNAVAILABLE
        _REDIS_GET_TOTAL += 1
        if k == "hit":
            _REDIS_GET_HIT += 1
        elif k == "miss":
            _REDIS_GET_MISS += 1
        elif k == "unavailable":
            _REDIS_GET_UNAVAILABLE += 1
        else:
            _REDIS_GET_ERRORS += 1
        _REDIS_WINDOW.append((now_ts, f"get:{k}"))


def observe_redis_set(kind: str):
    now_ts = time.time()
    k = (kind or "error").strip().lower()
    with _LOCK:
        global _REDIS_SET_TOTAL, _REDIS_SET_OK, _REDIS_SET_ERRORS, _REDIS_SET_UNAVAILABLE
        _REDIS_SET_TOTAL += 1
        if k == "ok":
            _REDIS_SET_OK += 1
        elif k == "unavailable":
            _REDIS_SET_UNAVAILABLE += 1
        else:
            _REDIS_SET_ERRORS += 1
        _REDIS_WINDOW.append((now_ts, f"set:{k}"))


def observe_redis_claim(kind: str):
    now_ts = time.time()
    k = (kind or "error").strip().lower()
    with _LOCK:
        global _REDIS_CLAIM_TOTAL, _REDIS_CLAIM_OK, _REDIS_CLAIM_EXISTS, _REDIS_CLAIM_ERRORS, _REDIS_CLAIM_UNAVAILABLE
        _REDIS_CLAIM_TOTAL += 1
        if k == "ok":
            _REDIS_CLAIM_OK += 1
        elif k == "exists":
            _REDIS_CLAIM_EXISTS += 1
        elif k == "unavailable":
            _REDIS_CLAIM_UNAVAILABLE += 1
        else:
            _REDIS_CLAIM_ERRORS += 1
        _REDIS_WINDOW.append((now_ts, f"claim:{k}"))


def _window_http_stats(now_ts: float, window_sec: int) -> dict[str, Any]:
    _prune_window(_HTTP_WINDOW, now_ts, window_sec)
    total = len(_HTTP_WINDOW)
    if total <= 0:
        return {
            "total": 0,
            "errors": 0,
            "failure_rate": 0.0,
            "avg_latency_ms": 0.0,
            "p50_latency_ms": 0.0,
            "p95_latency_ms": 0.0,
            "p99_latency_ms": 0.0,
        }
    errors = sum(row[1] for row in _HTTP_WINDOW)
    lat_sum = sum(row[2] for row in _HTTP_WINDOW)
    lats = [row[2] for row in _HTTP_WINDOW]
    return {
        "total": int(total),
        "errors": int(errors),
        "failure_rate": round(float(errors) / float(total), 6),
        "avg_latency_ms": round((lat_sum / float(total)) * 1000.0, 2),
        "p50_latency_ms": round(_percentile(lats, 50) * 1000.0, 2),
        "p95_latency_ms": round(_percentile(lats, 95) * 1000.0, 2),
        "p99_latency_ms": round(_percentile(lats, 99) * 1000.0, 2),
    }


def _window_kis_stats(now_ts: float, window_sec: int) -> dict[str, Any]:
    _prune_window(_KIS_WINDOW, now_ts, window_sec)
    total = len(_KIS_WINDOW)
    if total <= 0:
        return {
            "total": 0,
            "errors": 0,
            "failure_rate": 0.0,
            "avg_latency_ms": 0.0,
            "p50_latency_ms": 0.0,
            "p95_latency_ms": 0.0,
            "p99_latency_ms": 0.0,
        }
    errors = sum(row[1] for row in _KIS_WINDOW)
    lat_sum = sum(row[2] for row in _KIS_WINDOW)
    lats = [row[2] for row in _KIS_WINDOW]
    return {
        "total": int(total),
        "errors": int(errors),
        "failure_rate": round(float(errors) / float(total), 6),
        "avg_latency_ms": round((lat_sum / float(total)) * 1000.0, 2),
        "p50_latency_ms": round(_percentile(lats, 50) * 1000.0, 2),
        "p95_latency_ms": round(_percentile(lats, 95) * 1000.0, 2),
        "p99_latency_ms": round(_percentile(lats, 99) * 1000.0, 2),
    }


def _window_redis_stats(now_ts: float, window_sec: int) -> dict[str, Any]:
    _prune_window(_REDIS_WINDOW, now_ts, window_sec)
    stats = {
        "get_hit": 0,
        "get_miss": 0,
        "get_error": 0,
        "get_unavailable": 0,
        "set_ok": 0,
        "set_error": 0,
        "set_unavailable": 0,
        "claim_ok": 0,
        "claim_exists": 0,
        "claim_error": 0,
        "claim_unavailable": 0,
    }
    for _, kind in _REDIS_WINDOW:
        if kind == "get:hit":
            stats["get_hit"] += 1
        elif kind == "get:miss":
            stats["get_miss"] += 1
        elif kind == "get:unavailable":
            stats["get_unavailable"] += 1
        elif kind == "set:ok":
            stats["set_ok"] += 1
        elif kind == "set:unavailable":
            stats["set_unavailable"] += 1
        elif kind == "claim:ok":
            stats["claim_ok"] += 1
        elif kind == "claim:exists":
            stats["claim_exists"] += 1
        elif kind == "claim:unavailable":
            stats["claim_unavailable"] += 1
        elif kind.startswith("get:"):
            stats["get_error"] += 1
        elif kind.startswith("set:"):
            stats["set_error"] += 1
        elif kind.startswith("claim:"):
            stats["claim_error"] += 1

    get_total = stats["get_hit"] + stats["get_miss"] + stats["get_error"] + stats["get_unavailable"]
    stats["get_total"] = get_total
    stats["hit_ratio"] = round(float(stats["get_hit"]) / float(get_total), 6) if get_total > 0 else 0.0
    return stats


def snapshot(window_sec: int = _WINDOW_SEC_DEFAULT) -> dict[str, Any]:
    now_ts = time.time()
    ws = max(30, int(window_sec or _WINDOW_SEC_DEFAULT))
    with _LOCK:
        http_total = _HTTP_TOTAL
        http_errors = _HTTP_ERRORS
        http_avg_ms = ((_HTTP_LATENCY_SUM / http_total) * 1000.0) if http_total > 0 else 0.0

        kis_total = _KIS_TOTAL
        kis_errors = _KIS_ERRORS
        kis_avg_ms = ((_KIS_LATENCY_SUM / kis_total) * 1000.0) if kis_total > 0 else 0.0

        redis_get_total = _REDIS_GET_TOTAL
        redis_get_hit = _REDIS_GET_HIT
        redis_hit_ratio = round(float(redis_get_hit) / float(redis_get_total), 6) if redis_get_total > 0 else 0.0

        result = {
            "uptime_sec": int(max(0.0, now_ts - _STARTED_AT)),
            "window_sec": ws,
            "http": {
                "total": int(http_total),
                "errors": int(http_errors),
                "failure_rate": round(float(http_errors) / float(http_total), 6) if http_total > 0 else 0.0,
                "avg_latency_ms": round(http_avg_ms, 2),
                "window": _window_http_stats(now_ts, ws),
            },
            "kis": {
                "total": int(kis_total),
                "errors": int(kis_errors),
                "failure_rate": round(float(kis_errors) / float(kis_total), 6) if kis_total > 0 else 0.0,
                "avg_latency_ms": round(kis_avg_ms, 2),
                "window": _window_kis_stats(now_ts, ws),
            },
            "redis": {
                "get_total": int(_REDIS_GET_TOTAL),
                "get_hit": int(_REDIS_GET_HIT),
                "get_miss": int(_REDIS_GET_MISS),
                "get_error": int(_REDIS_GET_ERRORS),
                "get_unavailable": int(_REDIS_GET_UNAVAILABLE),
                "set_total": int(_REDIS_SET_TOTAL),
                "set_ok": int(_REDIS_SET_OK),
                "set_error": int(_REDIS_SET_ERRORS),
                "set_unavailable": int(_REDIS_SET_UNAVAILABLE),
                "claim_total": int(_REDIS_CLAIM_TOTAL),
                "claim_ok": int(_REDIS_CLAIM_OK),
                "claim_exists": int(_REDIS_CLAIM_EXISTS),
                "claim_error": int(_REDIS_CLAIM_ERRORS),
                "claim_unavailable": int(_REDIS_CLAIM_UNAVAILABLE),
                "hit_ratio": redis_hit_ratio,
                "window": _window_redis_stats(now_ts, ws),
            },
        }
    return result


def is_kis_degraded(
    window_sec: int | None = None,
    min_calls: int | None = None,
    failure_rate_threshold: float | None = None,
) -> bool:
    ws = max(30, int(window_sec or int(os.getenv("KIS_DEGRADE_WINDOW_SEC", "180"))))
    min_n = max(1, int(min_calls or int(os.getenv("KIS_DEGRADE_MIN_CALLS", "40"))))
    threshold = float(
        failure_rate_threshold
        if failure_rate_threshold is not None
        else float(os.getenv("KIS_DEGRADE_FAILURE_RATE", "0.45"))
    )
    eval_interval_sec = max(10, int(os.getenv("KIS_DEGRADE_EVAL_INTERVAL_SEC", "60")))
    required_streak = max(1, int(os.getenv("KIS_DEGRADE_CONSEC_WINDOWS", "3")))
    now_ts = time.time()
    with _LOCK:
        _prune_window(_KIS_WINDOW, now_ts, ws)
        total = len(_KIS_WINDOW)
        errors = sum(row[1] for row in _KIS_WINDOW) if total > 0 else 0
        ratio = (float(errors) / float(total)) if total > 0 else 0.0

        should_evaluate = (now_ts - float(_KIS_DEGRADE_STATE.get("last_eval_ts") or 0.0)) >= eval_interval_sec
        if should_evaluate:
            if total >= min_n and ratio >= threshold:
                _KIS_DEGRADE_STATE["streak"] = int(_KIS_DEGRADE_STATE.get("streak") or 0) + 1
            else:
                _KIS_DEGRADE_STATE["streak"] = 0
            _KIS_DEGRADE_STATE["active"] = int(_KIS_DEGRADE_STATE.get("streak") or 0) >= required_streak
            _KIS_DEGRADE_STATE["last_eval_ts"] = now_ts
            _KIS_DEGRADE_STATE["last_ratio"] = ratio
            _KIS_DEGRADE_STATE["last_total"] = total

        return bool(_KIS_DEGRADE_STATE.get("active"))
