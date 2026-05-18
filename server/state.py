"""
공유 전역 상태, DB 헬퍼, 푸시 헬퍼, 신용공여/빚투 헬퍼, 시가총액 헬퍼 등
"""
import sys, os, json, csv, glob, threading, random, logging, time
from typing import Optional
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import io

logger = logging.getLogger("server.state")

from pydantic import BaseModel

from .core.settings import (
    DATA_DIR,
    FIREBASE_VAPID_KEY,
    FIREBASE_WEB_CONFIG,
    JSON_LATEST,
    ROOT_DIR,
    SHORT_RATIO_FILES,
    TELEMSG_PUSH_TOKEN,
    VAPID_PUBLIC_KEY,
)
from .db.connections import get_news_conn, get_stocks_conn

_XLRD_MODULE = None
_XLRD_ATTEMPTED = False
_FDR_MODULE = None
_FDR_ATTEMPTED = False
_REQUESTS_MODULE = None
_REQUESTS_ATTEMPTED = False
_PANDAS_MODULE = None
_PANDAS_ATTEMPTED = False

# Push service: import directly. Earlier code used a lazy-loader thin wrapper —
# unnecessary since push_service is always importable in this deployment.
from .services.push_service import (  # noqa: E402,F401
    ensure_push_schema,
    _upsert_push_subscription,
    _remove_push_subscription,
    _collect_push_subscriptions,
    _webpush_config_ready,
    _send_webpush_to_all,
    _save_fcm_token,
    _remove_fcm_token,
    _collect_fcm_tokens,
    _collect_fcm_token_rows,
    _verify_push_dev_token,
    _get_firebase_app,
    _send_fcm_to_token,
    _send_fcm_to_all,
    _record_pwa_install,
    _count_pwa_installs,
    firebase_messaging,
)


def _get_xlrd():
    global _XLRD_MODULE, _XLRD_ATTEMPTED
    if _XLRD_ATTEMPTED:
        return _XLRD_MODULE
    _XLRD_ATTEMPTED = True
    try:
        import xlrd as _mod
    except Exception:
        _mod = None
    _XLRD_MODULE = _mod
    return _XLRD_MODULE


def _get_fdr():
    global _FDR_MODULE, _FDR_ATTEMPTED
    if _FDR_ATTEMPTED:
        return _FDR_MODULE
    _FDR_ATTEMPTED = True
    try:
        import FinanceDataReader as _mod
    except Exception:
        _mod = None
    _FDR_MODULE = _mod
    return _FDR_MODULE


def _get_requests():
    global _REQUESTS_MODULE, _REQUESTS_ATTEMPTED
    if _REQUESTS_ATTEMPTED:
        return _REQUESTS_MODULE
    _REQUESTS_ATTEMPTED = True
    try:
        import requests as _mod
    except Exception:
        _mod = None
    _REQUESTS_MODULE = _mod
    return _REQUESTS_MODULE


def _get_pandas():
    global _PANDAS_MODULE, _PANDAS_ATTEMPTED
    if _PANDAS_ATTEMPTED:
        return _PANDAS_MODULE
    _PANDAS_ATTEMPTED = True
    try:
        import pandas as _mod
    except Exception:
        _mod = None
    _PANDAS_MODULE = _mod
    return _PANDAS_MODULE


# Keep existing code paths stable for now.
xlrd = _get_xlrd()
fdr = _get_fdr()
requests = _get_requests()
pd = _get_pandas()


def ensure_hot_query_indexes() -> None:
    """
    Best-effort index bootstrap for API hot paths.
    Safe to call repeatedly on startup.
    """
    stock_stmts = [
        "CREATE INDEX IF NOT EXISTS idx_price_today_trading_value ON price_today (trading_value DESC)",
        "CREATE INDEX IF NOT EXISTS idx_price_today_change_pct ON price_today (change_pct DESC)",
        "CREATE INDEX IF NOT EXISTS idx_price_today_updated_at ON price_today (updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_price_daily_code_date ON price_daily (code, date DESC)",
    ]
    news_stmts = [
        "CREATE INDEX IF NOT EXISTS idx_news_events_id_desc ON news_events (id DESC)",
    ]

    try:
        conn = get_stocks_conn()
        try:
            for sql in stock_stmts:
                try:
                    conn.execute(sql)
                except Exception:
                    pass
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug("ensure_hot_query_indexes stocks skipped: %s", e)

    try:
        conn = get_news_conn()
        try:
            for sql in news_stmts:
                try:
                    conn.execute(sql)
                except Exception:
                    pass
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug("ensure_hot_query_indexes news skipped: %s", e)


_SHORT_RATIO_CACHE = None
_CREDIT_TREND_SYNC_META = {"path": None, "mtime": None}
_MARKET_CAP_REF_WON = {
    "kospi": 2_250_000_000_000_000.0,   # 2,250조원 (2026-04 기준 추정치)
    "kosdaq": 430_000_000_000_000.0,    # 430조원 (2026-04 기준 추정치)
}
_MARKET_CAP_CACHE = {"fetched_at": None, "payload": None}
_STOCKS_LIST_CACHE = {}  # key: (sort_by, order, limit, offset), value: {"mtime": 0, "data": ...}
_THEMES_CACHE = {"mtime": 0, "data": None}
_US_THEMES_CACHE = {"mtime": 0, "data": None, "top": 0}
_MARKET_SIGNAL_CACHE = {"mtime": 0, "data": None}
_LAST_SIGNAL_REFRESH_AT: float = 0.0  # 마지막 신호등/빚투 갱신 시각
_MACRO_CACHE = {"mtime": 0, "data": None}
_NEWS_CACHE = {}  # key: (page, limit), value: {"mtime": 0, "data": ...}
_BG_REFRESH_LOCK = threading.Lock()
_BG_SIGNAL_LOCK = threading.Lock()
_BG_ALERT_LOCK = threading.Lock()


# 유저 닉네임 생성용 소스
_NICK_ADJECTIVES = ["단단한", "빛나는", "배고픈", "화난", "즐거운", "차가운", "뜨거운", "느긋한", "빠른", "조용한", "용감한", "소심한", "영리한", "둔한", "푸른", "붉은", "검은", "하얀"]
_NICK_NOUNS = ["사자", "별빛", "호랑이", "펭귄", "독수리", "바다거북", "고양이", "강아지", "구름", "바람", "바위", "파도", "숲", "달빛", "고래", "사슴", "여우", "곰"]


# ─── 백그라운드 price_today 갱신 ─────────────────────────────
def _upsert_price_today(conn, code: str, pt: dict, ts: str):
    """price_today 테이블 UPSERT"""
    conn.execute(
        """
        INSERT INTO price_today(
            code,current_price,change_pct,change_amt,trading_value,trading_volume,
            volume_turnover_rate,market_cap,per,pbr,eps,foreign_hold_pct,
            listed_shares,raw_json,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(code) DO UPDATE SET
            current_price=excluded.current_price,
            change_pct=excluded.change_pct,
            change_amt=excluded.change_amt,
            trading_value=excluded.trading_value,
            trading_volume=excluded.trading_volume,
            volume_turnover_rate=excluded.volume_turnover_rate,
            market_cap=excluded.market_cap,
            per=excluded.per, pbr=excluded.pbr, eps=excluded.eps,
            foreign_hold_pct=excluded.foreign_hold_pct,
            listed_shares=excluded.listed_shares,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
        """,
        (
            code,
            pt.get("current_price"),
            pt.get("change_pct"),
            pt.get("change_amt"),
            pt.get("trading_value"),
            pt.get("trading_volume"),
            pt.get("volume_turnover_rate"),
            pt.get("market_cap"),
            str(pt.get("per", "") or ""),
            str(pt.get("pbr", "") or ""),
            str(pt.get("eps", "") or ""),
            pt.get("foreign_hold_pct"),
            pt.get("listed_shares"),
            json.dumps(pt.get("_raw") or {}, ensure_ascii=False),
            ts,
        ),
    )


def _is_pg_deadlock_error(exc: Exception) -> bool:
    sqlstate = str(getattr(exc, "sqlstate", "") or "").strip().upper()
    if sqlstate == "40P01":
        return True
    msg = str(exc).lower()
    return "deadlock" in msg


def _write_price_today_batch(results: dict, ts: str, reason: str, max_retry: int = 3) -> int:
    ordered_items = sorted(results.items(), key=lambda item: item[0])
    attempt = 1
    while True:
        conn = get_stocks_conn()
        try:
            for code, pt in ordered_items:
                _upsert_price_today(conn, code, pt, ts)
            conn.commit()
            return len(ordered_items)
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            if _is_pg_deadlock_error(e) and attempt < max(1, max_retry):
                wait_sec = min(1.0, 0.12 * attempt + random.random() * 0.15)
                logger.warning(
                    "[%s] deadlock detected, retrying (%d/%d) after %.2fs",
                    reason,
                    attempt,
                    max_retry,
                    wait_sec,
                )
                time.sleep(wait_sec)
                attempt += 1
                continue
            raise
        finally:
            conn.close()


_LAST_PRICE_REFRESH_AT: float = 0.0

_BG_REFRESH_HOT_COUNT = max(0, int(os.getenv("BG_REFRESH_HOT_COUNT", "80")))
_BG_REFRESH_COLD_BATCH = max(0, int(os.getenv("BG_REFRESH_COLD_BATCH", "160")))
_BG_REFRESH_WORKERS = max(1, int(os.getenv("BG_REFRESH_WORKERS", "6")))
_BG_REFRESH_FOCUS_MAX_CODES = max(1, int(os.getenv("BG_REFRESH_FOCUS_MAX_CODES", "200")))
_BG_REFRESH_ALERTS_ON_FOCUS = str(os.getenv("BG_REFRESH_ALERTS_ON_FOCUS", "0")).strip().lower() in ("1", "true", "yes", "on")
_BG_REFRESH_ROTATE_OFFSET = 0
_BG_REFRESH_ROTATE_LOCK = threading.Lock()


def _row_code(row) -> str:
    try:
        code = row["code"]
    except Exception:
        try:
            code = row[0]
        except Exception:
            code = ""
    return str(code or "").strip()


def _pick_refresh_codes(all_codes: list[str], hot_codes: list[str]) -> tuple[list[str], int, int, int]:
    """Return selected codes + cold-batch stats for rotating refresh."""
    global _BG_REFRESH_ROTATE_OFFSET
    if not all_codes:
        return hot_codes, 0, 0, 0

    hot_set = set(hot_codes)
    cold_codes = [c for c in all_codes if c not in hot_set]
    if not cold_codes or _BG_REFRESH_COLD_BATCH <= 0:
        merged = []
        seen = set()
        for c in hot_codes:
            if c and c not in seen:
                seen.add(c)
                merged.append(c)
        return merged, len(cold_codes), 0, 0

    with _BG_REFRESH_ROTATE_LOCK:
        start = _BG_REFRESH_ROTATE_OFFSET % len(cold_codes)
        take = min(_BG_REFRESH_COLD_BATCH, len(cold_codes))
        end = start + take
        if end <= len(cold_codes):
            cold_pick = cold_codes[start:end]
        else:
            cold_pick = cold_codes[start:] + cold_codes[: end - len(cold_codes)]
        _BG_REFRESH_ROTATE_OFFSET = (start + take) % len(cold_codes)

    merged = []
    seen = set()
    for c in hot_codes + cold_pick:
        if c and c not in seen:
            seen.add(c)
            merged.append(c)
    return merged, len(cold_codes), len(cold_pick), start


def _bg_refresh_prices():
    """Refresh prices for hot symbols + rotating batch of all other symbols."""
    if not _BG_REFRESH_LOCK.acquire(blocking=False):
        return
    try:
        sys.path.insert(0, ROOT_DIR)
        from collectors.kis_api import KISCollector

        conn = get_stocks_conn()
        try:
            hot_rows = []
            if _BG_REFRESH_HOT_COUNT > 0:
                hot_rows = conn.execute(
                    """
                    SELECT code FROM price_today
                    WHERE code IS NOT NULL AND code <> ''
                    ORDER BY trading_value DESC NULLS LAST
                    LIMIT ?
                    """,
                    (_BG_REFRESH_HOT_COUNT,),
                ).fetchall()
            hot_codes = []
            hot_seen = set()
            for r in hot_rows:
                c = _row_code(r)
                if c and c not in hot_seen:
                    hot_seen.add(c)
                    hot_codes.append(c)

            all_rows = conn.execute(
                """
                SELECT code FROM stocks
                WHERE code IS NOT NULL AND code <> ''
                ORDER BY code
                """
            ).fetchall()
            all_codes = []
            all_seen = set()
            for r in all_rows:
                c = _row_code(r)
                if c and c not in all_seen:
                    all_seen.add(c)
                    all_codes.append(c)
        finally:
            conn.close()

        codes, cold_total, cold_picked, cold_start = _pick_refresh_codes(all_codes, hot_codes)
        if not codes:
            return

        collector = KISCollector()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def _fetch(code):
            try:
                return code, collector.get_price(code)
            except Exception:
                return code, {}

        results = {}
        with ThreadPoolExecutor(max_workers=_BG_REFRESH_WORKERS) as ex:
            for code, pt in ex.map(lambda c: _fetch(c), codes):
                if pt.get("current_price"):
                    results[code] = pt

        if not results:
            return

        _write_price_today_batch(results, ts, reason="bg_refresh", max_retry=3)

        _STOCKS_LIST_CACHE.clear()
        global _LAST_PRICE_REFRESH_AT
        import time as _t
        _LAST_PRICE_REFRESH_AT = _t.time()
        logger.info(
            "[bg_refresh] prices refreshed ok=%d/%d hot=%d cold=%d/%d offset=%d (%s)",
            len(results),
            len(codes),
            len(hot_codes),
            cold_picked,
            cold_total,
            cold_start,
            ts,
        )
        threading.Thread(target=_bg_check_price_alerts, daemon=True).start()

    except Exception as e:
        logger.exception("[bg_refresh] error: %s", e)
    finally:
        _BG_REFRESH_LOCK.release()


def bg_refresh_prices_for_codes(codes: list[str], reason: str = "focus") -> dict:
    """
    Refresh price_today for specific codes only (focus-path).
    Returns a small stats dict; skips when another refresh is in-flight.
    """
    uniq = []
    seen = set()
    for c in (codes or []):
        code = str(c or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        uniq.append(code)
        if len(uniq) >= _BG_REFRESH_FOCUS_MAX_CODES:
            break

    if not uniq:
        return {"ok": True, "picked": 0, "updated": 0, "reason": reason}

    if not _BG_REFRESH_LOCK.acquire(blocking=False):
        return {"ok": False, "picked": len(uniq), "updated": 0, "reason": reason, "skipped": "busy"}

    try:
        sys.path.insert(0, ROOT_DIR)
        from collectors.kis_api import KISCollector

        collector = KISCollector()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def _fetch(code):
            try:
                return code, collector.get_price(code)
            except Exception:
                return code, {}

        workers = max(1, min(_BG_REFRESH_WORKERS, len(uniq)))
        results = {}
        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for code, pt in ex.map(lambda c: _fetch(c), uniq):
                    if pt.get("current_price"):
                        results[code] = pt
        except RuntimeError:
            # 인터프리터 종료 중 — 조용히 무시
            return {"ok": False, "picked": len(uniq), "updated": 0, "reason": "shutdown"}

        if not results:
            return {"ok": False, "picked": len(uniq), "updated": 0, "reason": reason}

        _write_price_today_batch(results, ts, reason="bg_refresh_focus", max_retry=3)

        _STOCKS_LIST_CACHE.clear()
        global _LAST_PRICE_REFRESH_AT
        import time as _t
        _LAST_PRICE_REFRESH_AT = _t.time()

        logger.debug(
            "[bg_refresh_focus] reason=%s updated=%d/%d (%s)",
            reason,
            len(results),
            len(uniq),
            ts,
        )
        if _BG_REFRESH_ALERTS_ON_FOCUS:
            try:
                threading.Thread(target=_bg_check_price_alerts, daemon=True).start()
            except Exception:
                pass
        return {"ok": True, "picked": len(uniq), "updated": len(results), "reason": reason}
    except Exception as e:
        logger.exception("[bg_refresh_focus] error: %s", e)
        return {"ok": False, "picked": len(uniq), "updated": 0, "reason": reason, "error": str(e)}
    finally:
        _BG_REFRESH_LOCK.release()


def _get_db_mtime(*_args):
    # Postgres-only mode: there is no local file mtime. Use a short rolling token
    # so cache keys rotate every 30s (same contract callers always expected).
    # Accepts (and ignores) any positional args for backwards compatibility.
    return int(time.time() // 30)


# ─── 가격 알림 DB 헬퍼 ───────────────────────────────────────
# 구현은 server/services/price_alerts.py 로 이동. facade 만 유지.
from .services.price_alerts import (  # noqa: E402,F401
    _ensure_price_alerts_table,
    set_price_alert,
    cancel_price_alert,
    _bg_check_price_alerts,
)


# ─── DB 헬퍼 ─────────────────────────────────────────────────
def sanitize_floats(obj):
    """
    JSON standard does not support NaN, Inf, -Inf. 
    Replace them with None (null) to avoid serialization errors in FastAPI.
    """
    import math
    if isinstance(obj, dict):
        return {k: sanitize_floats(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_floats(v) for v in obj]
    # Check for float and also for numpy-style floats if they exist
    try:
        if math.isnan(obj) or math.isinf(obj):
            return None
    except (TypeError, ValueError):
        pass
    return obj


def jl(v):
    """JSON 문자열 → Python 객체 (NaN/Inf 자동 보정)"""
    if v is None:
        return None
    try:
        data = json.loads(v)
        return sanitize_floats(data)
    except Exception:
        return v


def to_float(v, default=0.0):
    if v is None:
        return default
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return default


def _with_proxy_disabled(fn):
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    previous = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        return fn()
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _sum_marcap(series):
    total = 0.0
    for v in series:
        n = to_float(v, default=None)
        if n is None:
            continue
        total += n
    return total if total > 0 else None


def get_market_cap_snapshot():
    now = datetime.now()
    cached = _MARKET_CAP_CACHE.get("payload")
    cached_at = _MARKET_CAP_CACHE.get("fetched_at")
    if cached and cached_at and (now - cached_at).total_seconds() < 1800:
        return cached

    fallback = {
        "source": "manual_baseline",
        "status": "fallback",
        "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "kospi_won": _MARKET_CAP_REF_WON["kospi"],
        "kosdaq_won": _MARKET_CAP_REF_WON["kosdaq"],
        "error": None,
    }

    _fdr = _get_fdr()
    if _fdr is None:
        fallback["error"] = "FinanceDataReader_not_installed"
        _MARKET_CAP_CACHE["payload"] = fallback
        _MARKET_CAP_CACHE["fetched_at"] = now
        return fallback

    try:
        def _fetch():
            df_kospi = _fdr.StockListing("KOSPI")
            df_kosdaq = _fdr.StockListing("KOSDAQ")
            return df_kospi, df_kosdaq

        df_kospi, df_kosdaq = _with_proxy_disabled(_fetch)
        if "MarCap" not in df_kospi.columns or "MarCap" not in df_kosdaq.columns:
            raise RuntimeError("MarCap_column_missing")

        kospi_sum = _sum_marcap(df_kospi["MarCap"])
        kosdaq_sum = _sum_marcap(df_kosdaq["MarCap"])
        if not kospi_sum or not kosdaq_sum:
            raise RuntimeError("MarCap_sum_unavailable")

        payload = {
            "source": "fdr_krx_stocklisting",
            "status": "ok",
            "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "kospi_won": float(kospi_sum),
            "kosdaq_won": float(kosdaq_sum),
            "error": None,
        }
        _MARKET_CAP_CACHE["payload"] = payload
        _MARKET_CAP_CACHE["fetched_at"] = now
        return payload
    except Exception as e:
        fallback["error"] = f"{type(e).__name__}: {e}"
        _MARKET_CAP_CACHE["payload"] = fallback
        _MARKET_CAP_CACHE["fetched_at"] = now
        return fallback


def get_fdr_individual_credit_capability():
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _fdr = _get_fdr()
    if _fdr is None:
        return {
            "provider": "FinanceDataReader",
            "supported": False,
            "checked_at": checked_at,
            "reason": "FinanceDataReader_not_installed",
            "note": "개별 종목 신용잔고는 별도 소스(pykrx/KRX API) 연동이 필요합니다.",
        }

    candidates = [n for n in dir(_fdr) if any(k in n.lower() for k in ("credit", "loan", "margin", "balance"))]
    if candidates:
        return {
            "provider": "FinanceDataReader",
            "supported": False,
            "checked_at": checked_at,
            "reason": "No_direct_per_stock_credit_balance_api_in_FDR",
            "detected_related_names": candidates,
            "note": "노출된 API 목록에 개별 신용잔고 조회 함수가 없습니다.",
        }

    return {
        "provider": "FinanceDataReader",
        "supported": False,
        "checked_at": checked_at,
        "reason": "No_credit_balance_endpoint",
        "note": "개별 종목 신용잔고는 pykrx 또는 KRX 직접 연동이 필요합니다.",
    }


def _iter_short_ratio_files():
    seen = set()
    for p in SHORT_RATIO_FILES:
        if os.path.exists(p) and p not in seen:
            seen.add(p)
            yield p
    for p in glob.glob(os.path.join(DATA_DIR, "*공매도.csv")):
        if os.path.exists(p) and p not in seen:
            seen.add(p)
            yield p


def get_short_ratio_map():
    global _SHORT_RATIO_CACHE
    if _SHORT_RATIO_CACHE is not None:
        return _SHORT_RATIO_CACHE

    ratio_map = {}
    candidates = ("수량_비중", "비중", "금액_비중")

    for file_path in _iter_short_ratio_files():
        raw = None
        for enc in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
            try:
                with open(file_path, "r", encoding=enc, newline="") as f:
                    raw = f.read()
                break
            except Exception:
                raw = None

        if not raw:
            continue

        delim = "\t" if "\t" in (raw.splitlines()[0] if raw.splitlines() else "") else ","
        reader = csv.DictReader(raw.splitlines(), delimiter=delim)
        if not reader.fieldnames:
            continue

        for row in reader:
            code = str((row.get("종목코드") or "")).strip().zfill(6)
            if not code or code == "000000":
                continue
            ratio = None
            for c in candidates:
                if c in row and row.get(c) not in (None, ""):
                    ratio = to_float(row.get(c), default=None)
                    if ratio is not None:
                        break
            if ratio is None:
                continue
            prev = ratio_map.get(code)
            if prev is None or ratio > prev:
                ratio_map[code] = ratio

    _SHORT_RATIO_CACHE = ratio_map
    return ratio_map


def _iter_credit_trend_files():
    seen = set()
    candidates = [
        os.path.join(DATA_DIR, "신용공여 잔고 추이.xls"),
        os.path.join(DATA_DIR, "신용공여_잔고_추이.xls"),
    ]
    for p in candidates:
        if os.path.exists(p) and p not in seen:
            seen.add(p)
            yield p
    for p in glob.glob(os.path.join(DATA_DIR, "*잔고*추이*.xls")):
        if os.path.exists(p) and p not in seen:
            seen.add(p)
            yield p


def _parse_credit_number(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except Exception:
        return None


def _parse_credit_date(v, datemode):
    if v is None:
        return None
    if isinstance(v, (int, float)) and xlrd is not None:
        try:
            return xlrd.xldate_as_datetime(v, datemode).strftime("%Y-%m-%d")
        except Exception:
            pass

    s = str(v).strip()
    if not s or s == "-":
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    try:
        return datetime.fromisoformat(s).strftime("%Y-%m-%d")
    except Exception:
        return None


def ensure_credit_trend_schema(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS credit_trend (
        date               TEXT PRIMARY KEY,
        total_credit_mil   REAL,
        kospi_credit_mil   REAL,
        kosdaq_credit_mil  REAL,
        kospi_bittu_ratio  REAL,
        kosdaq_bittu_ratio REAL,
        updated_at         TEXT
    )
    """)


def sync_credit_trend_from_xls(conn):
    if xlrd is None:
        return

    src = next(_iter_credit_trend_files(), None)
    if not src:
        return

    mtime = os.path.getmtime(src)
    if _CREDIT_TREND_SYNC_META.get("path") == src and _CREDIT_TREND_SYNC_META.get("mtime") == mtime:
        return

    ensure_credit_trend_schema(conn)

    wb = xlrd.open_workbook(src)
    sh = wb.sheet_by_index(0)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for i in range(4, sh.nrows):
        date_v = _parse_credit_date(sh.cell_value(i, 0), wb.datemode)
        total = _parse_credit_number(sh.cell_value(i, 1))
        kospi = _parse_credit_number(sh.cell_value(i, 2))
        kosdaq = _parse_credit_number(sh.cell_value(i, 3))

        if not date_v or total is None or total <= 0:
            continue

        kospi_ratio = (kospi / total) * 100 if kospi is not None else None
        kosdaq_ratio = (kosdaq / total) * 100 if kosdaq is not None else None

        rows.append((
            date_v,
            total,
            kospi,
            kosdaq,
            kospi_ratio,
            kosdaq_ratio,
            ts,
        ))

    if not rows:
        return

    conn.executemany("""
    INSERT INTO credit_trend (
        date, total_credit_mil, kospi_credit_mil, kosdaq_credit_mil,
        kospi_bittu_ratio, kosdaq_bittu_ratio, updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(date) DO UPDATE SET
      total_credit_mil=excluded.total_credit_mil,
      kospi_credit_mil=excluded.kospi_credit_mil,
      kosdaq_credit_mil=excluded.kosdaq_credit_mil,
      kospi_bittu_ratio=excluded.kospi_bittu_ratio,
      kosdaq_bittu_ratio=excluded.kosdaq_bittu_ratio,
      updated_at=excluded.updated_at
    """, rows)
    conn.commit()

    _CREDIT_TREND_SYNC_META["path"] = src
    _CREDIT_TREND_SYNC_META["mtime"] = mtime


def _fetch_credit_trend_from_naver():
    """네이버 금융에서 최신 신용융자 잔고 추이를 스크래핑"""
    if not (requests and pd): return []
    try:
        url = "https://finance.naver.com/sise/sise_deposit.naver"
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = 'cp949'

        tables = pd.read_html(io.StringIO(res.text))
        target_df = None
        for t in tables:
            if '신용융자' in str(t.columns):
                target_df = t
                break

        if target_df is None: return []

        if isinstance(target_df.columns, pd.MultiIndex):
            target_df.columns = [f"{col[0]}_{col[1]}" if col[0] != col[1] else col[0] for col in target_df.columns]

        col_map = {}
        for c in target_df.columns:
            if '날짜' in c: col_map[c] = 'date'
            elif '신용융자' in c and '전체' in c: col_map[c] = 'total'
            elif '신용융자' in c and '거래소' in c: col_map[c] = 'kospi'
            elif '신용융자' in c and '코스닥' in c: col_map[c] = 'kosdaq'

        if 'date' not in col_map.values() or 'total' not in col_map.values(): return []

        target_df = target_df[list(col_map.keys())].rename(columns=col_map)
        target_df = target_df.dropna(subset=['date', 'total'])

        cur_year = datetime.now().year
        results = []
        for _, row in target_df.iterrows():
            d_str = _parse_credit_date(row['date'], 0)  # 기본 파서 활용
            if not d_str: continue

            try:
                total = float(str(row['total']).replace(',', ''))
                kospi = float(str(row['kospi']).replace(',', '')) if 'kospi' in row and str(row['kospi']) != '-' else 0
                kosdaq = float(str(row['kosdaq']).replace(',', '')) if 'kosdaq' in row and str(row['kosdaq']) != '-' else 0
                if total <= 0: continue
                results.append((d_str, total, kospi, kosdaq))
            except: continue
        return results
    except Exception as e:
        logger.warning("[_fetch_credit_trend_from_naver] 오류: %s", e)
        return []


def _sync_credit_trend_data(conn):
    """지표 통합 갱신: 네이버 스크래핑 + 기존 XLS 동기화"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. 웹 스크래핑 데이터 반영
    web_rows = _fetch_credit_trend_from_naver()
    if web_rows:
        insert_data = []
        for d_str, total, kospi, kosdaq in web_rows:
            insert_data.append((d_str, total, kospi, kosdaq, ts))

        ensure_credit_trend_schema(conn)
        conn.executemany("""
            INSERT INTO credit_trend (date, total_credit_mil, kospi_credit_mil, kosdaq_credit_mil, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                total_credit_mil=excluded.total_credit_mil,
                kospi_credit_mil=excluded.kospi_credit_mil,
                kosdaq_credit_mil=excluded.kosdaq_credit_mil,
                updated_at=excluded.updated_at
        """, insert_data)
        conn.commit()

    # 2. 기존 XLS 파일 기반 보충 (웹에 없는 옛날 데이터 등)
    try:
        sync_credit_trend_from_xls(conn)
    except:
        pass


def _is_refresh_allowed() -> bool:
    """KRX 평일 08:30 ~ 16:30 사이만 갱신 허용 (장 전/후 30분 여유)"""
    now = datetime.now()
    if now.weekday() >= 5:  # 토/일
        return False
    total = now.hour * 60 + now.minute
    return 8 * 60 + 30 <= total <= 16 * 60 + 30


def is_market_open() -> bool:
    """KRX 정규장 시간 여부 (09:00 ~ 15:30 평일)"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    total = now.hour * 60 + now.minute
    return 9 * 60 <= total <= 15 * 60 + 30


def get_market_status() -> dict:
    """KRX 시장 상태 반환"""
    now = datetime.now()
    weekday = now.weekday()
    total = now.hour * 60 + now.minute

    if weekday >= 5:
        return {"status": "closed", "label": "주말 휴장", "open": False, "color": "gray"}

    if total < 8 * 60 + 30:
        return {"status": "pre", "label": "장 시작 전", "open": False, "color": "gray"}
    elif total < 9 * 60:
        return {"status": "pre_open", "label": "동시호가", "open": False, "color": "amber"}
    elif total <= 15 * 60 + 30:
        return {"status": "open", "label": "장 중", "open": True, "color": "green"}
    elif total <= 16 * 60:
        return {"status": "after", "label": "시간외", "open": False, "color": "amber"}
    else:
        return {"status": "closed", "label": "장 마감", "open": False, "color": "gray"}


def _bg_refresh_signal_data():
    """상단 신호등 및 빚투 데이터 백그라운드 갱신"""
    if not _BG_SIGNAL_LOCK.acquire(blocking=False):
        return
    try:
        conn = get_stocks_conn()
        _sync_credit_trend_data(conn)
        conn.close()

        # 신호등 캐시 무효화 -> 다음 요청 시 새 데이터 계산
        _MARKET_SIGNAL_CACHE["data"] = None
        logger.info("[bg_refresh_signal] 빚투 및 매수신호 갱신 완료 (%s)", datetime.now().strftime('%H:%M:%S'))
    except Exception as e:
        logger.exception("[bg_refresh_signal] 오류: %s", e)
    finally:
        _BG_SIGNAL_LOCK.release()


def load_credit_trend_payload(conn):
    ensure_credit_trend_schema(conn)
    rows = conn.execute("""
    SELECT
      date, total_credit_mil, kospi_credit_mil, kosdaq_credit_mil,
      kospi_bittu_ratio, kosdaq_bittu_ratio
    FROM credit_trend
    ORDER BY date ASC
    """).fetchall()

    if not rows:
        return None

    market_cap_info = get_market_cap_snapshot()
    kospi_cap_won = to_float(market_cap_info.get("kospi_won"), default=_MARKET_CAP_REF_WON["kospi"])
    kosdaq_cap_won = to_float(market_cap_info.get("kosdaq_won"), default=_MARKET_CAP_REF_WON["kosdaq"])
    kospi_cap_mil = kospi_cap_won / 1_000_000.0
    kosdaq_cap_mil = kosdaq_cap_won / 1_000_000.0

    kospi = []
    kosdaq = []
    for r in rows:
        d = dict(r)
        date_v = d["date"]
        kospi_credit_mil = to_float(d.get("kospi_credit_mil"), default=None)
        kosdaq_credit_mil = to_float(d.get("kosdaq_credit_mil"), default=None)
        total_credit_mil = to_float(d.get("total_credit_mil"), default=None)

        kospi_ratio = None
        kosdaq_ratio = None
        if kospi_credit_mil is not None and kospi_cap_mil > 0:
            kospi_ratio = (kospi_credit_mil / kospi_cap_mil) * 100.0
        if kosdaq_credit_mil is not None and kosdaq_cap_mil > 0:
            kosdaq_ratio = (kosdaq_credit_mil / kosdaq_cap_mil) * 100.0

        kospi.append({
            "date": date_v,
            "ratio": kospi_ratio,
            "credit_mil": kospi_credit_mil,
            "market_cap_won": kospi_cap_won,
            "total_credit_mil": total_credit_mil,
        })
        kosdaq.append({
            "date": date_v,
            "ratio": kosdaq_ratio,
            "credit_mil": kosdaq_credit_mil,
            "market_cap_won": kosdaq_cap_won,
            "total_credit_mil": total_credit_mil,
        })

    latest = dict(rows[-1])
    latest_total_credit_mil = to_float(latest.get("total_credit_mil"), default=None)
    latest_kospi_credit_mil = to_float(latest.get("kospi_credit_mil"), default=None)
    latest_kosdaq_credit_mil = to_float(latest.get("kosdaq_credit_mil"), default=None)
    latest_kospi_ratio = (latest_kospi_credit_mil / kospi_cap_mil) * 100.0 if latest_kospi_credit_mil is not None and kospi_cap_mil > 0 else None
    latest_kosdaq_ratio = (latest_kosdaq_credit_mil / kosdaq_cap_mil) * 100.0 if latest_kosdaq_credit_mil is not None and kosdaq_cap_mil > 0 else None
    individual_credit_api = get_fdr_individual_credit_capability()

    return {
        "kospi": kospi[-120:],
        "kosdaq": kosdaq[-120:],
        "market_caps": {
            "source": market_cap_info.get("source"),
            "status": market_cap_info.get("status"),
            "fetched_at": market_cap_info.get("fetched_at"),
            "error": market_cap_info.get("error"),
            "formula": "(신용융자 잔액 / 시장 시가총액) * 100",
            "kospi_won": kospi_cap_won,
            "kosdaq_won": kosdaq_cap_won,
        },
        "risk_rules": {
            "warning_total_credit_trillion": 22.0,
            "psychological_total_credit_trillion": 20.0,
            "high_risk_individual_credit_ratio_pct": 5.0,
            "kospi_anchor_ratio_pct": 1.0,
            "kospi_warning_ratio_pct": 1.2,
            "kosdaq_anchor_ratio_pct": 2.0,
            "kosdaq_warning_ratio_pct": 2.6,
        },
        "individual_credit_api": individual_credit_api,
        "latest": {
            "date": latest.get("date"),
            "total_credit_mil": latest_total_credit_mil,
            "total_credit_trillion": (latest_total_credit_mil / 1_000_000.0) if latest_total_credit_mil is not None else None,
            "kospi_ratio": latest_kospi_ratio,
            "kosdaq_ratio": latest_kosdaq_ratio,
            "kospi_credit_mil": latest_kospi_credit_mil,
            "kosdaq_credit_mil": latest_kosdaq_credit_mil,
            "kospi_market_cap_won": kospi_cap_won,
        },
    }


# ─── 건의사항(유저 피드백) DB 헬퍼 ─────────────────────────────
# 구현은 server/services/suggestions.py 로 이동. 아래는 backward-compat facade.
from .services.suggestions import (  # noqa: E402,F401
    ensure_suggestions_table,
    save_suggestion,
)
