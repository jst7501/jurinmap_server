"""
주식/시장/뉴스/매크로/헬스 관련 API 라우터
"""
import sys, os, threading, logging, asyncio, time, copy, random, re
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from typing import Optional
from datetime import datetime, timedelta
from collections import defaultdict, deque
import hashlib
from decimal import Decimal

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder

logger = logging.getLogger("server.routes.stocks")

import server.state as _state
from server.core.security import reject_websocket_if_unauthorized
from ..cache import redis_get_json, redis_set_json
from ..monitoring import (
    is_kis_degraded,
    observe_kis_ws_tick,
    observe_kis_ws_reconnect,
    observe_cache_read,
)
from ..state import (
    JSON_LATEST,
    _STOCKS_LIST_CACHE, _THEMES_CACHE, _MARKET_SIGNAL_CACHE,
    _MACRO_CACHE, _NEWS_CACHE,
    get_stocks_conn, get_news_conn,
    _get_db_mtime, _is_refresh_allowed,
    _bg_refresh_prices, _bg_refresh_signal_data,
    load_credit_trend_payload,
    sync_credit_trend_from_xls,
    get_short_ratio_map,
    jl, to_float, sanitize_floats,
    ROOT_DIR,
    get_market_status,
)

import json

router = APIRouter()
_LOGO_DIR = os.path.join(ROOT_DIR, "data", "company_logos")
_LOGO_EXTS = ("png", "svg", "jpg", "jpeg", "webp")


def _get_local_logo_url(code: str) -> Optional[str]:
    c = str(code or "").strip()
    if not c:
        return None
    for ext in _LOGO_EXTS:
        p = os.path.join(_LOGO_DIR, f"{c}.{ext}")
        if os.path.exists(p):
            return f"/assets/company_logos/{c}.{ext}"
    return None


def _apply_local_logo_urls(stocks_by_code: dict) -> None:
    for code, row in (stocks_by_code or {}).items():
        if not isinstance(row, dict):
            continue
        local_logo_url = _get_local_logo_url(code)
        if local_logo_url:
            row["logo_local_url"] = local_logo_url
        else:
            row.pop("logo_local_url", None)


def _apply_local_logo_urls_list(stock_rows: list) -> None:
    for row in stock_rows or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        local_logo_url = _get_local_logo_url(code)
        if local_logo_url:
            row["logo_local_url"] = local_logo_url
        else:
            row.pop("logo_local_url", None)


def _stocks_db_available() -> bool:
    # Postgres-only mode: always assume DB is available. Connection failures are
    # surfaced at query time by psycopg, not by a pre-check.
    return True


def _news_db_available() -> bool:
    return True


def _mtime_token(*_args) -> str:
    """Postgres-only mode: returns a rolling time-based cache token.
    Accepts (and ignores) any positional args for backwards compatibility
    with legacy call sites."""
    try:
        return str(int(_get_db_mtime()))
    except Exception:
        return "0"


def _search_key_token(q: str) -> str:
    raw = (q or "").strip().lower().encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()

# ─── 장 상태 판별 ────────────────────────────────────────────
def _market_status() -> str:
    """현재 장 상태 반환: open | pre_market | after_market | closed_weekend"""
    now = datetime.now()
    wd  = now.weekday()       # 0=월 ~ 6=일
    if wd >= 5:
        return "closed_weekend"
    t = now.hour * 100 + now.minute
    if t < 900:
        return "pre_market"
    if t >= 1530:
        return "after_market"
    return "open"


@router.get("/api/market-status")
def get_market_status_endpoint():
    return get_market_status()


# ─── 30분봉 헬퍼 ────────────────────────────────────────────
# 구현은 server/services/minute_bars.py 로 분리. 아래 import 가
# stocks_parts 공유 namespace 에 함수들을 노출하므로 part03/04 같은 다른
# part 파일들은 변경 없이 그대로 호출 가능.
from server.services.minute_bars import (  # noqa: F401
    _time_sub,
    _fetch_full_day_minutes,
    _fetch_multiday_minutes,
    _aggregate_to_30min,
    _aggregate_to_Nmin,
    _calc_scalping_index,
)


# ─── GET /api/indices ────────────────────────────────────────
_INDICES_CACHE = {"data": None, "fetched_at": None}

# ─── 캐시 크기 제한 헬퍼 (최대 200종목, 초과 시 오래된 항목 제거) ──
_CACHE_MAX = 200

def _cache_set(cache: dict, key, value):
    if key not in cache and len(cache) >= _CACHE_MAX:
        oldest = next(iter(cache))
        cache.pop(oldest, None)
    cache[key] = value

# ─── 실시간 가격 캐시 ─────────────────────────────────────────
_LIVE_PRICE_CACHE: dict = {}

# ─── 스케일핑 캐시 ───────────────────────────────────────────
_SCALPING_CACHE: dict = {}  # code → {data, fetched_at}

# ─── 호가 캐시 ───────────────────────────────────────────────
_ORDERBOOK_CACHE: dict = {}  # code → {data, fetched_at}

# ─── OHLCV 캐시 ──────────────────────────────────────────────
_OHLCV_CACHE: dict = {}  # "code:period" → {data, fetched_at}

# ─── 시장 브리프 캐시 ────────────────────────────────────────
_MARKET_BRIEF_CACHE = {"data": None, "fetched_at": 0}

_NEWS_WS_KIS_COLLECTOR = None
_NEWS_WS_KIS_LOCK = threading.Lock()
_NEWS_WS_KIS_CACHE: dict = {}  # code -> {"data": ..., "fetched_at": float}
_NEWS_WS_KIS_TTL_SEC = 3.0
_NEWS_WS_STRENGTH_SOFT_TTL_SEC = max(5.0, float(os.getenv("NEWS_WS_STRENGTH_SOFT_TTL_SEC", "20.0")))
_NEWS_WS_REST_STRENGTH_ENABLED = str(os.getenv("NEWS_WS_REST_STRENGTH_ENABLED", "1")).strip().lower() in ("1", "true", "yes", "on")
_NEWS_WS_REST_STRENGTH_MIN_INTERVAL_SEC = max(2.0, float(os.getenv("NEWS_WS_REST_STRENGTH_MIN_INTERVAL_SEC", "15.0")))
_NEWS_WS_REST_STRENGTH_MAX_CODES_PER_CYCLE = max(1, int(os.getenv("NEWS_WS_REST_STRENGTH_MAX_CODES_PER_CYCLE", "3")))
_NEWS_WS_REST_STRENGTH_NEXT_TS: dict[str, float] = {}
_NEWS_WS_RT_SET_CODES_MIN_INTERVAL_SEC = max(0.5, float(os.getenv("NEWS_WS_RT_SET_CODES_MIN_INTERVAL_SEC", "2.0")))
_NEWS_WS_RT_SET_CODES_HEARTBEAT_SEC = max(
    _NEWS_WS_RT_SET_CODES_MIN_INTERVAL_SEC,
    float(os.getenv("NEWS_WS_RT_SET_CODES_HEARTBEAT_SEC", "12.0")),
)

_KIS_RT_RECONNECT_BASE_SEC = max(2.0, float(os.getenv("KIS_RT_RECONNECT_BASE_SEC", "3.0")))
_KIS_RT_RECONNECT_MAX_SEC = max(_KIS_RT_RECONNECT_BASE_SEC, float(os.getenv("KIS_RT_RECONNECT_MAX_SEC", "60.0")))
_KIS_RT_RECONNECT_JITTER_SEC = max(0.0, float(os.getenv("KIS_RT_RECONNECT_JITTER_SEC", "1.0")))
_KIS_RT_STABLE_SESSION_SEC = max(8.0, float(os.getenv("KIS_RT_STABLE_SESSION_SEC", "20.0")))

_KIS_REST_ERROR_BASE_BACKOFF_SEC = max(1.0, float(os.getenv("KIS_REST_ERROR_BASE_BACKOFF_SEC", "1.5")))
_KIS_REST_ERROR_MAX_BACKOFF_SEC = max(
    _KIS_REST_ERROR_BASE_BACKOFF_SEC,
    float(os.getenv("KIS_REST_ERROR_MAX_BACKOFF_SEC", "8.0")),
)
_KIS_REST_ERROR_LOG_INTERVAL_SEC = max(5.0, float(os.getenv("KIS_REST_ERROR_LOG_INTERVAL_SEC", "20.0")))
# How many consecutive successes required to clear fail_count.
# Prevents a flapping KIS endpoint (success/fail/success/fail) from
# indefinitely resetting the breaker at fail_count=1 and re-logging on every miss.
_KIS_REST_SUCCESS_THRESHOLD = max(1, int(os.getenv("KIS_REST_SUCCESS_THRESHOLD", "3")))
_KIS_REST_BREAKER_LOCK = threading.Lock()
_KIS_REST_BREAKER_STATE: dict[str, dict] = {}
_KIS_SOFT_ERRORS = (
    "degraded_mode_auto",
    "degraded_mode_forced",
    "local_concurrency_limited",
    "kis rest cooldown",
)

# ─── KIS 국내주식 실시간체결 WS 싱글톤 ──────────────────────────
# H0STCNT0 스트림 → 현재가 + 등락률 + 체결강도(CTTR) 실시간 캐시
# _KIS_RT_CACHE[code] = {current_price, change_pct, strength, acml_vol, updated_at}
_KIS_RT_CACHE: dict = {}
_KIS_RT_LOCK  = threading.Lock()

class _KisDomesticRtHub:
    """KIS WS H0STCNT0 구독 → 실시간 체결 캐시 유지 (항시 연결)"""
    TR_ID  = "H0STCNT0"
    WS_URL = os.getenv("KIS_DOMESTIC_WSS", "ws://ops.koreainvestment.com:21000")
    MAX_CODES = 40  # KIS 단일 연결 구독 상한

    def __init__(self):
        self._codes: set[str] = set()
        self._lock  = threading.Lock()
        self._thread: threading.Thread | None = None
        self._approval_key: str | None = None
        self._ws = None           # websockets connection (in the WS thread)
        self._loop = None         # event loop in the WS thread
        self._last_closeframe_log_ts: float = 0.0

    # 동일 KIS appkey 로 WS 연결은 1개만 허용되므로 (OPSP8996 ALREADY IN USE appkey),
    # 한 연결에서 H0STCNT0 (KRX 체결) + H0NXCNT0 (NXT 체결) 모두 구독.
    # 각 종목당 두 tr_id 가 별도 메시지로 subscribe 됨.
    EXTRA_TR_IDS: tuple[str, ...] = ("H0NXCNT0",)

    # ── public: 외부에서 구독 코드 추가/제거 ─────────────────────
    def add_codes(self, codes: list[str]):
        with self._lock:
            before = set(self._codes)
            for c in (codes or []):
                code = str(c or "").strip()
                if not code:
                    continue
                if len(self._codes) >= self.MAX_CODES and code not in self._codes:
                    continue
                self._codes.add(code)
            added = self._codes - before
        if added:
            self._subscribe_live(added)
        if self._codes:
            self._ensure_running()

    def remove_codes(self, codes: list[str]):
        removed = set()
        with self._lock:
            req = {str(c or "").strip() for c in (codes or []) if str(c or "").strip()}
            removed = self._codes.intersection(req)
            self._codes.difference_update(req)
        if removed:
            self._unsubscribe_live(removed)

    def set_codes(self, codes: list[str]):
        normalized = []
        seen = set()
        for c in (codes or []):
            code = str(c or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            normalized.append(code)
            if len(normalized) >= self.MAX_CODES:
                break

        with self._lock:
            target = set(normalized)
            prev = set(self._codes)
            self._codes = set(target)
            added = target - prev
            removed = prev - target

        if removed:
            self._unsubscribe_live(removed)
        if added:
            self._subscribe_live(added)
        if target:
            self._ensure_running()

    def get(self, code: str) -> dict | None:
        return _KIS_RT_CACHE.get(code)

    # ── internal ────────────────────────────────────────────────
    def _ensure_running(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            t = threading.Thread(target=self._run, daemon=True, name="kis_rt_hub")
            self._thread = t
        t.start()

    def _get_approval_key(self) -> str | None:
        try:
            collector = _get_news_ws_collector()
            if collector is None:
                return None
            import requests as _req
            resp = _req.post(
                f"{collector.base_url}/oauth2/Approval",
                json={"grant_type": "client_credentials",
                      "appkey": collector.app_key,
                      "secretkey": collector.app_secret},
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("approval_key")
        except Exception as e:
            logger.warning("KIS RT approval_key 발급 실패: %s", e)
        return None

    def _all_tr_ids(self) -> tuple[str, ...]:
        return (self.TR_ID,) + tuple(self.EXTRA_TR_IDS or ())

    def _subscribe_live(self, codes):
        """이미 연결된 WS에 추가 구독 전송 (thread-safe). TR_ID + EXTRA_TR_IDS 모두 send."""
        ws = self._ws
        loop = self._loop
        if ws is None or loop is None:
            return
        ak = self._approval_key
        if not ak:
            return
        for code in codes:
            for tr_id in self._all_tr_ids():
                msg = json.dumps({
                    "header": {"approval_key": ak, "custtype": "P",
                                "tr_type": "1", "content-type": "utf-8"},
                    "body": {"input": {"tr_id": tr_id, "tr_key": code}},
                })
                try:
                    asyncio.run_coroutine_threadsafe(ws.send(msg), loop)
                except Exception:
                    pass

    def _unsubscribe_live(self, codes):
        """이미 연결된 WS에 구독 해제 전송 (thread-safe). 모든 tr_id 해제."""
        ws = self._ws
        loop = self._loop
        if ws is None or loop is None:
            return
        ak = self._approval_key
        if not ak:
            return
        for code in codes:
            for tr_id in self._all_tr_ids():
                msg = json.dumps({
                    "header": {"approval_key": ak, "custtype": "P",
                                "tr_type": "2", "content-type": "utf-8"},
                    "body": {"input": {"tr_id": tr_id, "tr_key": code}},
                })
                try:
                    asyncio.run_coroutine_threadsafe(ws.send(msg), loop)
                except Exception:
                    pass

    def _parse_tick(self, raw: str):
        """0|H0STCNT0|NNN|데이터1^...^데이터N 파싱

        ⚠️ 과거 버그: isdigit() 기반 가드가 '+1.23', ' 1.23', 지수 표기 등을
        전부 파싱 실패로 간주해 0.0/0으로 조용히 폴백 → TOP 종목 change_pct가
        0% ↔ 정상값 왕복하는 증상 원인(프론트 null guard는 0을 유효값으로 간주).
        해결: try/float() 로 안전 파싱 + 실패 필드는 기존 캐시값을 **유지**.
        """
        def _sf(s):
            if s is None:
                return None
            t = str(s).strip()
            if not t:
                return None
            try:
                return float(t)
            except (ValueError, TypeError):
                return None

        def _si(s):
            v = _sf(s)
            if v is None:
                return None
            try:
                return int(v)
            except (ValueError, TypeError, OverflowError):
                return None

        parts = raw.split("|", 3)
        if len(parts) < 4:
            return
        msg_tr = parts[1]
        if msg_tr not in self._all_tr_ids():
            return
        # tr_id 별 필드 수 (KRX H0STCNT0=45, NXT H0NXCNT0=46) + 캐시 분기
        if msg_tr == "H0NXCNT0":
            n_fields = 46
            target_cache = _KIS_NXT_RT_CACHE
            source_label = "kis_nxt_rt"
        else:
            n_fields = 45
            target_cache = _KIS_RT_CACHE
            source_label = "kis_rt"
        all_fields = parts[3].split("^")
        count = max(1, len(all_fields) // n_fields)
        ts = datetime.now().strftime("%H:%M:%S")
        for i in range(count):
            f = all_fields[i * n_fields: (i + 1) * n_fields]
            if len(f) < 19:
                continue
            code       = f[0].strip()
            cur        = _si(f[2])
            change_pct = _sf(f[5])
            cttr       = _sf(f[18])
            acml_vol   = _si(f[13])
            if not code or cur is None or cur <= 0:
                # 현재가는 최소 필수 — 없으면 이 tick 전체 무시 (이전 값 유지)
                continue

            # 기존 캐시를 베이스로 두고 유효한 필드만 덮어쓴다.
            # 파싱 실패한 change_pct/cttr/acml_vol 은 **기존값을 유지**해서
            # 간헐 실패가 정상값을 0 으로 덮어쓰는 참사 방지.
            prev = target_cache.get(code) or {}
            entry = dict(prev)
            entry.update({
                "current_price": cur,
                "updated_at":    ts,
                "updated_epoch": time.time(),
                "source":        source_label,
            })
            if change_pct is not None:
                entry["change_pct"] = round(change_pct, 2)
            if cttr is not None:
                entry["strength"] = round(cttr, 2)
            if acml_vol is not None:
                entry["acml_vol"] = acml_vol
                entry["trading_volume"] = acml_vol
            target_cache[code] = entry

            try:
                observe_kis_ws_tick(msg_tr)
            except Exception:
                pass

            # _NEWS_WS_KIS_CACHE (체결강도) 와 whale 알림은 KRX H0STCNT0 전용 경로.
            # NXT(H0NXCNT0) tick 은 _KIS_NXT_RT_CACHE 만 갱신하고 위 둘은 건드리지 않음.
            if msg_tr == "H0STCNT0":
                if cttr is not None:
                    _NEWS_WS_KIS_CACHE[code] = {
                        "data": {
                            "code": code,
                            "strength": round(cttr, 2),
                            "updated_at": ts,
                            "source": "kis_rt_strength",
                        },
                        "fetched_at": time.time(),
                    }
                if acml_vol is not None:
                    _update_rt_whale_state(code, cur, acml_vol, ts)

    def _run(self):
        """데몬 스레드: KIS WS 연결 유지 + 파싱"""
        import asyncio as _aio
        try:
            import websockets as _ws_lib
        except ImportError:
            logger.error("websockets 패키지 없음 — KIS RT Hub 비활성화")
            return

        loop = _aio.new_event_loop()
        self._loop = loop
        _aio.set_event_loop(loop)

        async def _send_subscribe(ws, approval_key: str, code: str, tr_type: str = "1"):
            for tr_id in self._all_tr_ids():
                msg = json.dumps(
                    {
                        "header": {
                            "approval_key": approval_key,
                            "custtype": "P",
                            "tr_type": tr_type,
                            "content-type": "utf-8",
                        },
                        "body": {"input": {"tr_id": tr_id, "tr_key": code}},
                    }
                )
                await ws.send(msg)

        async def _inner():
            retry_delay = _KIS_RT_RECONNECT_BASE_SEC
            while True:
                with self._lock:
                    codes_snap = list(self._codes)[: self.MAX_CODES]

                if not codes_snap:
                    # 구독 종목 없음 — idle wait (tight loop 방지)
                    self._approval_key = None
                    await _aio.sleep(10)
                    retry_delay = _KIS_RT_RECONNECT_BASE_SEC
                    continue

                session_started_ts = time.time()
                session_had_tick = False
                try:
                    ak = await _aio.to_thread(self._get_approval_key)
                    if not ak:
                        sleep_for = min(retry_delay, _KIS_RT_RECONNECT_MAX_SEC) + random.uniform(0.0, _KIS_RT_RECONNECT_JITTER_SEC)
                        await _aio.sleep(sleep_for)
                        retry_delay = min(max(_KIS_RT_RECONNECT_BASE_SEC, retry_delay * 1.5), _KIS_RT_RECONNECT_MAX_SEC)
                        continue
                    self._approval_key = ak

                    async with _ws_lib.connect(
                        self.WS_URL,
                        ping_interval=None,
                        close_timeout=5,
                        max_queue=2000,
                    ) as ws:
                        self._ws = ws
                        logger.info("KIS RT Hub connected (%s)", self.WS_URL)

                        subscribed: set[str] = set()
                        for code in codes_snap:
                            await _send_subscribe(ws, ak, code, "1")
                            subscribed.add(code)

                        while True:
                            with self._lock:
                                desired = set(list(self._codes)[: self.MAX_CODES])

                            add_codes = desired - subscribed
                            del_codes = subscribed - desired
                            for code in add_codes:
                                await _send_subscribe(ws, ak, code, "1")
                                subscribed.add(code)
                            for code in del_codes:
                                try:
                                    await _send_subscribe(ws, ak, code, "2")
                                except Exception:
                                    pass
                                subscribed.discard(code)

                            if not desired:
                                try:
                                    await ws.close(code=1000, reason="no_subscriptions")
                                except Exception:
                                    pass
                                break

                            try:
                                message = await _aio.wait_for(ws.recv(), timeout=25)
                            except _aio.TimeoutError:
                                pong = await ws.ping()
                                await _aio.wait_for(pong, timeout=8)
                                continue

                            if not isinstance(message, str):
                                continue
                            if message.startswith("0|"):
                                self._parse_tick(message)
                                session_had_tick = True
                                continue
                            # JSON ACK/error message; keep connection unless server closes.

                except Exception as e:
                    msg = str(e or "")
                    session_age = max(0.0, time.time() - session_started_ts)
                    session_stable = session_had_tick and session_age >= _KIS_RT_STABLE_SESSION_SEC
                    sleep_for = min(retry_delay, _KIS_RT_RECONNECT_MAX_SEC) + random.uniform(0.0, _KIS_RT_RECONNECT_JITTER_SEC)
                    try:
                        observe_kis_ws_reconnect("H0STCNT0", reason=msg[:120])
                    except Exception:
                        pass
                    if "no close frame received or sent" in msg.lower():
                        now_ts = time.time()
                        if (now_ts - self._last_closeframe_log_ts) >= 30.0:
                            logger.info(
                                "KIS RT Hub closed without close frame; reconnect in %.1fs (session_age=%.1fs, stable=%s)",
                                sleep_for,
                                session_age,
                                session_stable,
                            )
                            self._last_closeframe_log_ts = now_ts
                        else:
                            logger.debug(
                                "KIS RT Hub closed without close frame; reconnect in %.1fs (session_age=%.1fs, stable=%s)",
                                sleep_for,
                                session_age,
                                session_stable,
                            )
                    else:
                        logger.warning(
                            "KIS RT Hub error: %s (reconnect in %.1fs, session_age=%.1fs, stable=%s)",
                            e,
                            sleep_for,
                            session_age,
                            session_stable,
                        )
                    await _aio.sleep(sleep_for)
                    if session_stable:
                        retry_delay = _KIS_RT_RECONNECT_BASE_SEC
                    else:
                        retry_delay = min(max(_KIS_RT_RECONNECT_BASE_SEC, retry_delay * 2), _KIS_RT_RECONNECT_MAX_SEC)
                finally:
                    self._ws = None

        loop.run_until_complete(_inner())

_KIS_RT_HUB = _KisDomesticRtHub()


# ─── KIS NXT 실시간체결 WS 싱글톤 (H0NXCNT0) ───────────────────
# NextTrade ATS 거래소 체결가 실시간 스트림. 운영시간 08:00-20:00 (프리/정규/애프터).
# H0STCNT0 와 거의 동일한 구조이나 TR_ID 만 다르고 NXT 단독 가격을 받음.
_KIS_NXT_RT_CACHE: dict = {}


class _KisNxtRtHub:
    """NXT 캐시(_KIS_NXT_RT_CACHE) 조회용 proxy.

    KIS appkey 1개당 WS 연결 1개 제한 (OPSP8996) 때문에 자체 KIS WS 연결을
    띄우지 않고, _KIS_RT_HUB 가 H0STCNT0 + H0NXCNT0 를 동시에 구독하도록
    위임. 이 클래스는 외부 호출(set_codes/add_codes/get/kis-status)을
    _KIS_RT_HUB 로 forward 하면서 캐시 조회만 _KIS_NXT_RT_CACHE 로.
    """
    TR_ID = "H0NXCNT0"

    @property
    def MAX_CODES(self):
        return _KIS_RT_HUB.MAX_CODES

    @property
    def _approval_key(self):
        return _KIS_RT_HUB._approval_key

    @property
    def _ws(self):
        return _KIS_RT_HUB._ws

    @property
    def _codes(self):
        return _KIS_RT_HUB._codes

    @property
    def _lock(self):
        return _KIS_RT_HUB._lock

    def add_codes(self, codes: list[str]):
        _KIS_RT_HUB.add_codes(codes)

    def remove_codes(self, codes: list[str]):
        _KIS_RT_HUB.remove_codes(codes)

    def set_codes(self, codes: list[str]):
        _KIS_RT_HUB.set_codes(codes)

    def get(self, code: str) -> dict | None:
        return _KIS_NXT_RT_CACHE.get(code)

    def _ensure_running(self):
        # 자체 thread 시작 안 함 — KRX hub 가 양 TR 모두 처리
        pass

_KIS_NXT_RT_HUB = _KisNxtRtHub()


# Phase 2 — WS-first 가격 helpers ─────────────────────────────
# WS(_KIS_RT_HUB) 가 이미 실시간 tick 으로 채워주는 코드는 REST poll 에서 제외.
# Price Refresh Poller 와 /price handler 양쪽에서 공유 사용.
_RT_CACHE_FRESH_DEFAULT_SEC = max(5.0, float(os.getenv("RT_CACHE_FRESH_SEC", "60.0")))
_PRICE_HANDLER_WS_FRESH_SEC = max(1.0, float(os.getenv("PRICE_HANDLER_WS_FRESH_SEC", "5.0")))
_PRICE_HANDLER_WS_FIRST = str(os.getenv("PRICE_HANDLER_WS_FIRST", "1")).strip().lower() not in ("0", "false", "no", "off")
_PRICE_POLLER_EXCLUDE_SUBSCRIBED = str(os.getenv("PRICE_POLLER_EXCLUDE_SUBSCRIBED", "1")).strip().lower() not in ("0", "false", "no", "off")


def _rt_cache_fresh_codes(max_age_sec: float | None = None) -> set[str]:
    """WS tick이 max_age_sec 이내 수신된 code 집합."""
    max_age = float(_RT_CACHE_FRESH_DEFAULT_SEC if max_age_sec is None else max_age_sec)
    now_ts = time.time()
    fresh: set[str] = set()
    # _KIS_RT_CACHE 는 플레인 dict. 이터레이션 중 rebind 대비해 snapshot.
    for code, entry in list(_KIS_RT_CACHE.items()):
        if not isinstance(entry, dict):
            continue
        try:
            ts = float(entry.get("updated_epoch") or 0.0)
        except Exception:
            ts = 0.0
        if ts > 0 and (now_ts - ts) <= max_age:
            fresh.add(str(code))
    return fresh


def _rt_cache_get_fresh(code: str, max_age_sec: float | None = None) -> dict | None:
    """Return _KIS_RT_CACHE[code] entry if fresh within max_age_sec, else None."""
    if not code:
        return None
    max_age = float(_PRICE_HANDLER_WS_FRESH_SEC if max_age_sec is None else max_age_sec)
    entry = _KIS_RT_CACHE.get(code)
    if not isinstance(entry, dict):
        return None
    try:
        ts = float(entry.get("updated_epoch") or 0.0)
    except Exception:
        return None
    if ts <= 0 or (time.time() - ts) > max_age:
        return None
    return entry


def _ensure_rt_subscription(code: str) -> None:
    """HTTP 핸들러가 특정 code 에 접근했음을 RT hub 에 알려 구독 생성(멱등).

    Phase 2: hub.add_codes 만 호출.
    Phase 4: ledger 에도 touch 해 staleness 추적에 포함 (sweeper 는 RT hub 건드리지
    않지만 observability 목적으로 기록).
    """
    if not code:
        return
    try:
        _KIS_RT_HUB.add_codes([str(code)])
    except Exception:
        # Hub 기동 전/연결 전 호출도 조용히 패스 — 다음 주기에 반영됨
        pass
    try:
        # Phase 4 — ledger touch (NOTE: _SUB_LEDGER 는 이 함수 정의 이후에 선언됨.
        # 런타임에 호출되므로 forward reference 는 정상 작동).
        _SUB_LEDGER.touch(code, source="http_price")
    except Exception:
        pass


# ─── Phase 3: KIS 국내주식 실시간호가 (H0STASP0) WS Hub ──────────
# REST /orderbook 대체. 구독한 code 는 매 tick 마다 10호가 전체가 들어옴 →
# _KIS_ORDERBOOK_CACHE 에 REST snapshot 과 동일한 shape 으로 적재.
_KIS_ORDERBOOK_CACHE: dict = {}
_KIS_ORDERBOOK_LOCK = threading.Lock()

# Feature flags — 기본 ON (Phase 3 풀 활성화).
# 1) WS_ENABLED=1 → Hub 가동, parser 가 _KIS_ORDERBOOK_CACHE 에 적재 + shadow diff 로그.
# 2) WS_READ=1 → readers 가 WS 캐시를 우선 사용. 캐시 miss 시 자동으로 REST fallback.
# 문제 발생 시 env 로 두 플래그를 0 으로 내리고 서버 재시작하면 기존 REST 경로로 즉시 복귀.
_KIS_ORDERBOOK_WS_ENABLED = str(os.getenv("KIS_ORDERBOOK_WS_ENABLED", "1")).strip().lower() in ("1", "true", "yes", "on")
_KIS_ORDERBOOK_WS_READ = str(os.getenv("KIS_ORDERBOOK_WS_READ", "1")).strip().lower() in ("1", "true", "yes", "on")
_KIS_ORDERBOOK_WS_FRESH_SEC = max(0.5, float(os.getenv("KIS_ORDERBOOK_WS_FRESH_SEC", "3.0")))
_KIS_ORDERBOOK_SHADOW_LOG = str(os.getenv("KIS_ORDERBOOK_SHADOW_LOG", "1")).strip().lower() in ("1", "true", "yes", "on")
_KIS_ORDERBOOK_SHADOW_INTERVAL_SEC = max(5.0, float(os.getenv("KIS_ORDERBOOK_SHADOW_INTERVAL_SEC", "30.0")))
_KIS_ORDERBOOK_SHADOW_LAST_LOG: dict[str, float] = {}


class _KisDomesticOrderbookHub:
    """KIS WS H0STASP0 구독 → 실시간 호가 캐시.

    설계는 _KisDomesticRtHub 와 동일한 구조 (approval key 방식, reconnect,
    set_codes / add_codes / remove_codes API). TR_ID 와 tick parser 만 다름.
    """
    TR_ID = "H0STASP0"
    WS_URL = os.getenv("KIS_DOMESTIC_WSS", "ws://ops.koreainvestment.com:21000")
    MAX_CODES = 40  # 단일 연결 구독 상한 (RT hub 과 동일)

    # H0STASP0 응답 필드 (references/asking_price_krx/asking_price_krx.py 기준, 58필드)
    _FIELD_COUNT = 58

    def __init__(self):
        self._codes: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._approval_key: str | None = None
        self._ws = None
        self._loop = None
        self._last_closeframe_log_ts: float = 0.0

    # ── public ──────────────────────────────────────────────────
    def add_codes(self, codes: list[str]):
        if not _KIS_ORDERBOOK_WS_ENABLED:
            return
        with self._lock:
            before = set(self._codes)
            for c in (codes or []):
                code = str(c or "").strip()
                if not code:
                    continue
                if len(self._codes) >= self.MAX_CODES and code not in self._codes:
                    continue
                self._codes.add(code)
            added = self._codes - before
        if added:
            self._subscribe_live(added)
        if self._codes:
            self._ensure_running()

    def remove_codes(self, codes: list[str]):
        removed = set()
        with self._lock:
            req = {str(c or "").strip() for c in (codes or []) if str(c or "").strip()}
            removed = self._codes.intersection(req)
            self._codes.difference_update(req)
        if removed:
            self._unsubscribe_live(removed)

    def set_codes(self, codes: list[str]):
        if not _KIS_ORDERBOOK_WS_ENABLED:
            return
        normalized = []
        seen = set()
        for c in (codes or []):
            code = str(c or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            normalized.append(code)
            if len(normalized) >= self.MAX_CODES:
                break
        with self._lock:
            target = set(normalized)
            prev = set(self._codes)
            self._codes = set(target)
            added = target - prev
            removed = prev - target
        if removed:
            self._unsubscribe_live(removed)
        if added:
            self._subscribe_live(added)
        if target:
            self._ensure_running()

    def get(self, code: str) -> dict | None:
        with _KIS_ORDERBOOK_LOCK:
            return _KIS_ORDERBOOK_CACHE.get(code)

    # ── internal ────────────────────────────────────────────────
    def _ensure_running(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            t = threading.Thread(target=self._run, daemon=True, name="kis_orderbook_hub")
            self._thread = t
        t.start()

    def _get_approval_key(self) -> str | None:
        try:
            collector = _get_news_ws_collector()
            if collector is None:
                return None
            import requests as _req
            resp = _req.post(
                f"{collector.base_url}/oauth2/Approval",
                json={"grant_type": "client_credentials",
                      "appkey": collector.app_key,
                      "secretkey": collector.app_secret},
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("approval_key")
        except Exception as e:
            logger.warning("KIS Orderbook Hub approval_key 발급 실패: %s", e)
        return None

    def _subscribe_live(self, codes):
        ws = self._ws
        loop = self._loop
        if ws is None or loop is None:
            return
        ak = self._approval_key
        if not ak:
            return
        for code in codes:
            msg = json.dumps({
                "header": {"approval_key": ak, "custtype": "P",
                           "tr_type": "1", "content-type": "utf-8"},
                "body": {"input": {"tr_id": self.TR_ID, "tr_key": code}},
            })
            try:
                asyncio.run_coroutine_threadsafe(ws.send(msg), loop)
            except Exception:
                pass

    def _unsubscribe_live(self, codes):
        ws = self._ws
        loop = self._loop
        if ws is None or loop is None:
            return
        ak = self._approval_key
        if not ak:
            return
        for code in codes:
            msg = json.dumps({
                "header": {"approval_key": ak, "custtype": "P",
                           "tr_type": "2", "content-type": "utf-8"},
                "body": {"input": {"tr_id": self.TR_ID, "tr_key": code}},
            })
            try:
                asyncio.run_coroutine_threadsafe(ws.send(msg), loop)
            except Exception:
                pass

    def _parse_tick(self, raw: str):
        """0|H0STASP0|NNN|데이터1^...^데이터N 파싱 → 10호가 snapshot 재구성."""
        parts = raw.split("|", 3)
        if len(parts) < 4 or parts[1] != self.TR_ID:
            return
        all_fields = parts[3].split("^")
        # H0STASP0 는 보통 1건/메시지지만 연속 페이징 대비 n_fields 단위로 슬라이스.
        n = self._FIELD_COUNT
        count = max(1, len(all_fields) // n)
        now_epoch = time.time()
        ts_iso = datetime.now().strftime("%H:%M:%S")
        for i in range(count):
            f = all_fields[i * n: (i + 1) * n]
            if len(f) < 43:  # 최소 잔량 필드까지 필요
                continue
            code = (f[0] or "").strip()
            if not code:
                continue

            def _to_int(s):
                s = (s or "").strip()
                if not s or s == "-":
                    return 0
                try:
                    return int(s)
                except Exception:
                    try:
                        return int(float(s))
                    except Exception:
                        return 0

            asks, bids = [], []
            # ASKP1..10 at index 3..12, ASKP_RSQN1..10 at index 23..32
            for j in range(10):
                ap = _to_int(f[3 + j])
                aq = _to_int(f[23 + j])
                if ap > 0:
                    asks.append({"price": ap, "qty": aq})
            # BIDP1..10 at index 13..22, BIDP_RSQN1..10 at index 33..42
            for j in range(10):
                bp = _to_int(f[13 + j])
                bq = _to_int(f[33 + j])
                if bp > 0:
                    bids.append({"price": bp, "qty": bq})

            total_ask = _to_int(f[43]) if len(f) > 43 else 0
            total_bid = _to_int(f[44]) if len(f) > 44 else 0

            snapshot = {
                "asks": asks,
                "bids": bids,
                "total_ask_qty": total_ask,
                "total_bid_qty": total_bid,
                "market_status": _market_status(),
                "updated_at": ts_iso,
                "source_id": _STOCK_FLOW_WS_ORDERBOOK_SOURCE_ID,
                "source": "kis_ws",
            }
            with _KIS_ORDERBOOK_LOCK:
                _KIS_ORDERBOOK_CACHE[code] = {
                    "data": snapshot,
                    "updated_epoch": now_epoch,
                }
            try:
                observe_kis_ws_tick("H0STASP0")
            except Exception:
                pass

    def _run(self):
        import asyncio as _aio
        try:
            import websockets as _ws_lib
        except ImportError:
            logger.error("websockets 패키지 없음 — KIS Orderbook Hub 비활성화")
            return

        loop = _aio.new_event_loop()
        self._loop = loop
        _aio.set_event_loop(loop)

        async def _send_subscribe(ws, approval_key: str, code: str, tr_type: str = "1"):
            msg = json.dumps({
                "header": {"approval_key": approval_key, "custtype": "P",
                           "tr_type": tr_type, "content-type": "utf-8"},
                "body": {"input": {"tr_id": self.TR_ID, "tr_key": code}},
            })
            await ws.send(msg)

        async def _inner():
            retry_delay = _KIS_RT_RECONNECT_BASE_SEC
            while True:
                with self._lock:
                    codes_snap = list(self._codes)[: self.MAX_CODES]
                if not codes_snap:
                    self._approval_key = None
                    await _aio.sleep(10)
                    retry_delay = _KIS_RT_RECONNECT_BASE_SEC
                    continue

                session_started_ts = time.time()
                session_had_tick = False
                try:
                    ak = await _aio.to_thread(self._get_approval_key)
                    if not ak:
                        sleep_for = min(retry_delay, _KIS_RT_RECONNECT_MAX_SEC) + random.uniform(0.0, _KIS_RT_RECONNECT_JITTER_SEC)
                        await _aio.sleep(sleep_for)
                        retry_delay = min(max(_KIS_RT_RECONNECT_BASE_SEC, retry_delay * 1.5), _KIS_RT_RECONNECT_MAX_SEC)
                        continue
                    self._approval_key = ak

                    async with _ws_lib.connect(
                        self.WS_URL, ping_interval=None, close_timeout=5, max_queue=2000,
                    ) as ws:
                        self._ws = ws
                        logger.info("KIS Orderbook Hub connected (%s)", self.WS_URL)

                        subscribed: set[str] = set()
                        for code in codes_snap:
                            await _send_subscribe(ws, ak, code, "1")
                            subscribed.add(code)

                        while True:
                            with self._lock:
                                desired = set(list(self._codes)[: self.MAX_CODES])
                            add_codes = desired - subscribed
                            del_codes = subscribed - desired
                            for code in add_codes:
                                await _send_subscribe(ws, ak, code, "1")
                                subscribed.add(code)
                            for code in del_codes:
                                try:
                                    await _send_subscribe(ws, ak, code, "2")
                                except Exception:
                                    pass
                                subscribed.discard(code)

                            if not desired:
                                try:
                                    await ws.close(code=1000, reason="no_subscriptions")
                                except Exception:
                                    pass
                                break

                            try:
                                message = await _aio.wait_for(ws.recv(), timeout=25)
                            except _aio.TimeoutError:
                                pong = await ws.ping()
                                await _aio.wait_for(pong, timeout=8)
                                continue

                            if not isinstance(message, str):
                                continue
                            if message.startswith("0|"):
                                self._parse_tick(message)
                                session_had_tick = True
                                continue
                except Exception as e:
                    msg = str(e or "")
                    session_age = max(0.0, time.time() - session_started_ts)
                    session_stable = session_had_tick and session_age >= _KIS_RT_STABLE_SESSION_SEC
                    sleep_for = min(retry_delay, _KIS_RT_RECONNECT_MAX_SEC) + random.uniform(0.0, _KIS_RT_RECONNECT_JITTER_SEC)
                    try:
                        observe_kis_ws_reconnect("H0STASP0", reason=msg[:120])
                    except Exception:
                        pass
                    if "no close frame received or sent" in msg.lower():
                        now_ts = time.time()
                        if (now_ts - self._last_closeframe_log_ts) >= 30.0:
                            logger.info(
                                "KIS Orderbook Hub closed without close frame; reconnect in %.1fs (session_age=%.1fs, stable=%s)",
                                sleep_for, session_age, session_stable,
                            )
                            self._last_closeframe_log_ts = now_ts
                    else:
                        logger.warning(
                            "KIS Orderbook Hub error: %s (reconnect in %.1fs, session_age=%.1fs, stable=%s)",
                            e, sleep_for, session_age, session_stable,
                        )
                    await _aio.sleep(sleep_for)
                    if session_stable:
                        retry_delay = _KIS_RT_RECONNECT_BASE_SEC
                    else:
                        retry_delay = min(max(_KIS_RT_RECONNECT_BASE_SEC, retry_delay * 2), _KIS_RT_RECONNECT_MAX_SEC)
                finally:
                    self._ws = None

        loop.run_until_complete(_inner())


_KIS_ORDERBOOK_HUB = _KisDomesticOrderbookHub()


def _orderbook_ws_get_fresh(code: str, max_age_sec: float | None = None) -> dict | None:
    """WS orderbook cache 에서 신선한 snapshot 반환 (없으면 None).

    안전 장치: snapshot 의 asks/bids 가 둘 다 빈 배열이면 None 반환 (REST fallback 유도).
    장 마감 외 시간엔 정상적으로 빈 호가가 올 수 있으나, 장 중 빈값이면 파서 버그
    의심이므로 REST 로 갈 것. 이 함수는 호출측에서 장/외 구분 없이 fallback 체인
    이 이미 있으므로 일괄 처리.
    """
    if not code or not _KIS_ORDERBOOK_WS_ENABLED:
        return None
    max_age = float(_KIS_ORDERBOOK_WS_FRESH_SEC if max_age_sec is None else max_age_sec)
    with _KIS_ORDERBOOK_LOCK:
        entry = _KIS_ORDERBOOK_CACHE.get(code)
    if not isinstance(entry, dict):
        return None
    try:
        ts = float(entry.get("updated_epoch") or 0.0)
    except Exception:
        return None
    if ts <= 0 or (time.time() - ts) > max_age:
        return None
    data = entry.get("data")
    if not isinstance(data, dict):
        return None
    # 파서 안전장치 — 장 중에 asks/bids 둘 다 비어있으면 WS 를 신뢰하지 말고 REST.
    # 장 마감 시에도 빈값이 정상이지만 이 시간엔 REST 가 매우 드물게 호출되므로 영향 미미.
    asks = data.get("asks") or []
    bids = data.get("bids") or []
    if not asks and not bids:
        return None
    return data


def _ensure_orderbook_subscription(code: str) -> None:
    if not code or not _KIS_ORDERBOOK_WS_ENABLED:
        return
    try:
        _KIS_ORDERBOOK_HUB.add_codes([str(code)])
    except Exception:
        pass
    try:
        _SUB_LEDGER.touch(code, source="http_orderbook")
    except Exception:
        pass


# ─── Phase 4: Subscription Ledger ──────────────────────────
# 누가 이 code 를 보고 있나 추적. HTTP / WS 양쪽 접근 모두 touch.
# Sweeper 가 주기적으로 stale code 를 orderbook hub 에서 제거.
# RT hub (H0STCNT0) 는 기존 NEWS_PRICE_WS_HUB 가 자체 lifecycle 관리중이라
# 이 ledger 의 sweeper 에서 제외한다 (regression 방지).
# 클래스 본체는 server/services/kis_sub_ledger.py 로 분리됨.
from server.services.kis_sub_ledger import KisSubscriptionLedger as _KisSubscriptionLedger

_SUB_LEDGER = _KisSubscriptionLedger()
_SUB_LEDGER_SWEEP_ENABLED = str(os.getenv("KIS_SUBS_DEMAND_DRIVEN", "1")).strip().lower() not in ("0", "false", "no", "off")
_SUB_LEDGER_SWEEP_INTERVAL_SEC = max(10.0, float(os.getenv("KIS_SUB_SWEEP_SEC", "30")))
_SUB_LEDGER_SWEEP_STARTED = False
_SUB_LEDGER_SWEEP_LOCK = threading.Lock()


def _sub_ledger_sweep_loop():
    """Sweeper: stale code 를 orderbook hub 에서 제거. 30초 주기."""
    time.sleep(3)
    while True:
        try:
            active, stale = _SUB_LEDGER.snapshot()
            if stale and _KIS_ORDERBOOK_WS_ENABLED:
                try:
                    _KIS_ORDERBOOK_HUB.remove_codes(list(stale))
                except Exception:
                    pass
            _SUB_LEDGER.prune_stale()
        except Exception as e:
            logger.debug("[sub-ledger-sweeper] error: %s", e)
        time.sleep(_SUB_LEDGER_SWEEP_INTERVAL_SEC)


def _start_sub_ledger_sweeper():
    global _SUB_LEDGER_SWEEP_STARTED
    with _SUB_LEDGER_SWEEP_LOCK:
        if _SUB_LEDGER_SWEEP_STARTED:
            return
        if not _SUB_LEDGER_SWEEP_ENABLED:
            return
        _SUB_LEDGER_SWEEP_STARTED = True
    t = threading.Thread(target=_sub_ledger_sweep_loop, daemon=True, name="kis-sub-ledger-sweeper")
    t.start()
    logger.info(
        "[sub-ledger-sweeper] started (idle=%.0fs, dwell=%.0fs, max=%d, sweep=%.0fs)",
        _KisSubscriptionLedger.IDLE_SEC,
        _KisSubscriptionLedger.DWELL_SEC,
        _KisSubscriptionLedger.MAX_CODES,
        _SUB_LEDGER_SWEEP_INTERVAL_SEC,
    )


# 모듈 import 시 sweeper 자동 기동.
_start_sub_ledger_sweeper()


def _orderbook_shadow_log_diff(code: str, rest_snapshot: dict) -> None:
    """Shadow mode diff 로그: REST 결과와 WS 캐시의 top-of-book 차이 비교."""
    if not _KIS_ORDERBOOK_SHADOW_LOG or not _KIS_ORDERBOOK_WS_ENABLED:
        return
    now_ts = time.time()
    last = _KIS_ORDERBOOK_SHADOW_LAST_LOG.get(code, 0.0)
    if (now_ts - last) < _KIS_ORDERBOOK_SHADOW_INTERVAL_SEC:
        return
    try:
        with _KIS_ORDERBOOK_LOCK:
            entry = _KIS_ORDERBOOK_CACHE.get(code)
        if not isinstance(entry, dict):
            return
        try:
            ws_age = now_ts - float(entry.get("updated_epoch") or 0.0)
        except Exception:
            ws_age = 99.0
        if ws_age > 10.0:
            return
        ws_data = entry.get("data") or {}
        rest_asks = (rest_snapshot or {}).get("asks") or []
        rest_bids = (rest_snapshot or {}).get("bids") or []
        ws_asks = ws_data.get("asks") or []
        ws_bids = ws_data.get("bids") or []
        ra1 = (rest_asks[0] if rest_asks else {}) or {}
        rb1 = (rest_bids[0] if rest_bids else {}) or {}
        wa1 = (ws_asks[0] if ws_asks else {}) or {}
        wb1 = (ws_bids[0] if ws_bids else {}) or {}
        diff_price = (int(ra1.get("price") or 0) - int(wa1.get("price") or 0),
                      int(rb1.get("price") or 0) - int(wb1.get("price") or 0))
        diff_qty = (int(ra1.get("qty") or 0) - int(wa1.get("qty") or 0),
                    int(rb1.get("qty") or 0) - int(wb1.get("qty") or 0))
        logger.info(
            "ws_vs_rest_orderbook_diff code=%s ws_age=%.1fs price_diff=%s qty_diff=%s rest_top=ask(%s@%s)/bid(%s@%s) ws_top=ask(%s@%s)/bid(%s@%s)",
            code, ws_age, diff_price, diff_qty,
            ra1.get("price"), ra1.get("qty"), rb1.get("price"), rb1.get("qty"),
            wa1.get("price"), wa1.get("qty"), wb1.get("price"), wb1.get("qty"),
        )
        _KIS_ORDERBOOK_SHADOW_LAST_LOG[code] = now_ts
    except Exception:
        pass


_NEWS_WS_MAX_CODES = max(20, int(os.getenv("NEWS_WS_MAX_CODES", "140")))
_NEWS_WS_MAX_CLIENTS = 300
_LIVE_USERS_DISPLAY_OFFSET = int(str(os.getenv("LIVE_USERS_DISPLAY_OFFSET", "0") or "0").strip() or "0")
_NEWS_WS_IDLE_SEC = 45.0
_NEWS_WS_SEND_TIMEOUT_SEC = 1.0
_NEWS_WS_INTERVAL_OPEN_SEC = max(1.0, float(os.getenv("NEWS_WS_INTERVAL_OPEN_SEC", "5.0")))
_NEWS_WS_INTERVAL_CLOSED_SEC = max(5.0, float(os.getenv("NEWS_WS_INTERVAL_CLOSED_SEC", "5.0")))
_NEWS_WS_BROADCAST_TOPN = max(20, int(os.getenv("NEWS_WS_BROADCAST_TOPN", "100")))
_NEWS_WS_REQUEST_CODES_MAX = max(10, int(os.getenv("NEWS_WS_REQUEST_CODES_MAX", "40")))
_NEWS_WS_FETCH_CODES_CAP = _NEWS_WS_BROADCAST_TOPN + _NEWS_WS_REQUEST_CODES_MAX
_NEWS_WS_BROADCAST_CODES_TTL_SEC = max(2.0, float(os.getenv("NEWS_WS_BROADCAST_CODES_TTL_SEC", "8.0")))
_NEWS_WS_BETA_NOTICE = "베타 서비스: 실시간 이용자 수가 제한되어 있습니다."
_NEWS_WS_BUSY_MESSAGE = "베타 이용자 한도에 도달했습니다. 잠시 후 다시 시도해 주세요."
_NEWS_WS_BROADCAST_CODES_CACHE: dict = {"codes": [], "ts": 0.0}
_NEWS_WS_BROADCAST_CODES_LOCK = threading.Lock()

_STOCK_FLOW_WS_MAX_CLIENTS = 300
_STOCK_FLOW_WS_MAX_CODES = 40
_STOCK_FLOW_WS_IDLE_SEC = 45.0
_STOCK_FLOW_WS_SEND_TIMEOUT_SEC = 1.0
_STOCK_FLOW_WS_INTERVAL_OPEN_SEC = 1.0
_STOCK_FLOW_WS_INTERVAL_CLOSED_SEC = 3.0
_STOCK_FLOW_WS_ORDERBOOK_INTERVAL_SEC = 3.0
_STOCK_FLOW_WS_TRADES_INTERVAL_SEC = 1.0
_STOCK_FLOW_WS_BETA_NOTICE = "베타 서비스: 실시간 상세 체결/호가 이용자 수가 제한되어 있습니다."
_STOCK_FLOW_WS_BUSY_MESSAGE = "베타 이용자 한도에 도달했습니다. 잠시 후 다시 시도해 주세요."
_STOCK_FLOW_WS_TRADE_SOURCE_ID = "H0STCNT0"
_STOCK_FLOW_WS_ORDERBOOK_SOURCE_ID = "H0STASP0"
_STOCK_FLOW_WS_CACHE: dict = {}  # code -> fetched payload cache
_STOCK_FLOW_WS_TRADES_REST_FALLBACK_SEC = max(2.0, float(os.getenv("STOCK_FLOW_WS_TRADES_REST_FALLBACK_SEC", "6.0")))
_STOCK_FLOW_RT_RECENT_SEC = max(1.0, float(os.getenv("STOCK_FLOW_RT_RECENT_SEC", "5.0")))
_WHALE_MIN_ABS_VALUE = 10_000_000          # 절대 최소 1천만원
_WHALE_PRICE_FLOOR_MULTIPLIER = 3000       # 가격 x 3000주
_WHALE_PRICE_FLOOR_MIN = 10_000_000         # 가격기반 바닥 하한 1천만원
_WHALE_PRICE_FLOOR_MAX = 80_000_000         # 가격기반 바닥 상한 8천만원
_WHALE_TURNOVER_FLOOR_RATIO = 0.0003        # 당일 추정거래대금의 0.03%
_WHALE_TURNOVER_FLOOR_MAX = 300_000_000     # 거래대금비율 바닥 상한 3억원
_WHALE_FORMULA_TEXT = "max(P80, 중앙값×2.5, 가격기반바닥, 당일거래대금비율, 절대하한)"

_RT_WHALE_LOCK = threading.Lock()
_RT_WHALE_STATE: dict[str, dict] = {}  # code -> {last_acml_vol,last_price,events:deque,last_event_ts}


_FOCUS_CODES_LOCK = threading.Lock()
_FOCUS_CODE_EXPIRES_AT: dict[str, float] = {}
_FOCUS_CODE_TTL_SEC = max(10.0, float(os.getenv("FOCUS_CODE_TTL_SEC", "120.0")))
_FOCUS_CODE_MAX_TRACKED = max(100, int(os.getenv("FOCUS_CODE_MAX_TRACKED", "2000")))
_STOCK_CODE_RE = re.compile(r"^[0-9A-Za-z]{6}$")


def _display_live_users_count(real_count: int) -> int:
    try:
        base = int(real_count or 0)
    except Exception:
        base = 0
    return max(0, base + _LIVE_USERS_DISPLAY_OFFSET)


def _is_valid_stock_code(code: str) -> bool:
    return bool(_STOCK_CODE_RE.match(str(code or "").strip()))


def _safe_int(val, default: int = 0) -> int:
    try:
        if val is None:
            return default
        s = str(val).strip().replace(",", "")
        if s in ("", "-", "nan", "NaN"):
            return default
        return int(float(s))
    except Exception:
        return default


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


def _normalize_single_stock_code(code: str) -> str:
    c = str(code or "").strip()
    if not c:
        return ""
    if len(c) > 12:
        c = c[:12]
    return c


def _normalize_stock_flow_codes(codes) -> list[str]:
    out = []
    seen = set()
    for c in (codes or []):
        code = _normalize_single_stock_code(c)
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
        if len(out) >= _STOCK_FLOW_WS_MAX_CODES:
            break
    return out


def _infer_trade_side(price: int, ask: int, bid: int) -> str:
    if price > 0 and ask > 0 and price >= ask:
        return "buy"
    if price > 0 and bid > 0 and price <= bid:
        return "sell"
    return "neutral"


def _update_rt_whale_state(code: str, cur: int, acml_vol: int, ts: str) -> None:
    c = _normalize_single_stock_code(code)
    if not c or cur <= 0 or acml_vol <= 0:
        return
    now_ts = time.time()
    with _RT_WHALE_LOCK:
        entry = _RT_WHALE_STATE.get(c)
        if not isinstance(entry, dict):
            entry = {
                "last_acml_vol": int(acml_vol),
                "last_price": int(cur),
                "events": deque(maxlen=120),
                "last_event_ts": 0.0,
            }
            _RT_WHALE_STATE[c] = entry
            return

        prev_acml = int(entry.get("last_acml_vol") or 0)
        delta = int(acml_vol - prev_acml)
        if delta > 0 and delta < 50_000_000:  # 비정상 점프/리셋 방지
            value = int(cur * delta)
            events = entry.get("events")
            if not isinstance(events, deque):
                events = deque(maxlen=120)
                entry["events"] = events
            trade_key = f"rt:{ts}:{cur}:{delta}:{acml_vol}"
            events.append({
                "time": ts,
                "price": int(cur),
                "qty": int(delta),
                "value": int(value),
                "ask": 0,
                "bid": 0,
                "day_turnover_est": int(cur * acml_vol),
                "side": "neutral",
                "trade_key": trade_key,
            })
            entry["last_event_ts"] = now_ts

        entry["last_acml_vol"] = int(acml_vol)
        entry["last_price"] = int(cur)


def _build_whale_payload_from_rt(code: str) -> tuple[list[dict], dict] | None:
    c = _normalize_single_stock_code(code)
    if not c:
        return None
    with _RT_WHALE_LOCK:
        entry = _RT_WHALE_STATE.get(c)
        if not isinstance(entry, dict):
            return None
        events = list(entry.get("events") or [])
    if not events:
        return None

    ticks = list(reversed(events[-60:]))  # 최신 우선
    whales, rule = _build_whale_payload(ticks)
    if isinstance(rule, dict):
        rule["source_id"] = _STOCK_FLOW_WS_TRADE_SOURCE_ID
    return whales, rule


def _kis_rest_breaker_key(kind: str, code: str) -> str:
    return f"{str(kind or '').strip().lower()}:{_normalize_single_stock_code(code)}"


def _kis_rest_allow_call(kind: str, code: str, now: float | None = None) -> bool:
    now_ts = float(time.time() if now is None else now)
    key = _kis_rest_breaker_key(kind, code)
    with _KIS_REST_BREAKER_LOCK:
        st = _KIS_REST_BREAKER_STATE.get(key) or {}
        next_retry_ts = float(st.get("next_retry_ts") or 0.0)
    return now_ts >= next_retry_ts


def _kis_rest_mark_success(kind: str, code: str) -> None:
    key = _kis_rest_breaker_key(kind, code)
    with _KIS_REST_BREAKER_LOCK:
        prev = _KIS_REST_BREAKER_STATE.get(key) or {}
        fail_count = int(prev.get("fail_count") or 0)
        last_log_ts = float(prev.get("last_log_ts") or 0.0)

        # Already healthy: keep state idle.
        if fail_count <= 0:
            _KIS_REST_BREAKER_STATE[key] = {
                "fail_count": 0,
                "next_retry_ts": 0.0,
                "success_streak": 0,
                "last_log_ts": last_log_ts,
                "last_error": "",
            }
            return

        # Recovering from prior failures: require N consecutive successes
        # before clearing fail_count. Keeps backoff pressure while the KIS
        # endpoint flaps, so cooldown can actually grow toward the cap.
        streak = int(prev.get("success_streak") or 0) + 1
        if streak >= _KIS_REST_SUCCESS_THRESHOLD:
            _KIS_REST_BREAKER_STATE[key] = {
                "fail_count": 0,
                "next_retry_ts": 0.0,
                "success_streak": 0,
                "last_log_ts": last_log_ts,
                "last_error": "",
            }
        else:
            _KIS_REST_BREAKER_STATE[key] = {
                "fail_count": fail_count,
                "next_retry_ts": float(prev.get("next_retry_ts") or 0.0),
                "success_streak": streak,
                "last_log_ts": last_log_ts,
                "last_error": str(prev.get("last_error") or ""),
            }


def _kis_rest_mark_failure(kind: str, code: str, error_msg: str) -> tuple[int, float, bool]:
    now_ts = time.time()
    key = _kis_rest_breaker_key(kind, code)
    with _KIS_REST_BREAKER_LOCK:
        prev = _KIS_REST_BREAKER_STATE.get(key) or {}
        fail_count = int(prev.get("fail_count") or 0) + 1
        delay = min(_KIS_REST_ERROR_MAX_BACKOFF_SEC, _KIS_REST_ERROR_BASE_BACKOFF_SEC * (2 ** max(0, fail_count - 1)))
        jitter = random.uniform(0.0, min(1.0, delay * 0.2))
        next_retry_ts = now_ts + delay + jitter
        last_log_ts = float(prev.get("last_log_ts") or 0.0)
        should_log = (now_ts - last_log_ts) >= _KIS_REST_ERROR_LOG_INTERVAL_SEC
        _KIS_REST_BREAKER_STATE[key] = {
            "fail_count": fail_count,
            "next_retry_ts": next_retry_ts,
            # Any failure wipes the recovery streak so the breaker cannot be
            # lulled back to zero by a single sporadic success.
            "success_streak": 0,
            "last_log_ts": now_ts if should_log else last_log_ts,
            "last_error": str(error_msg or ""),
        }
    return fail_count, next_retry_ts, should_log


# Phase 1 safety net — global gate for KIS REST calls from background pollers.
# Returns True when pollers should pause (either auto-degrade flag is active or
# env override KIS_FORCE_DEGRADED=1). Handlers may still try REST as a last
# resort; pollers should honour this to avoid compounding KIS outages.
def _kis_rest_should_skip() -> bool:
    force = os.getenv("KIS_FORCE_DEGRADED", "0").strip().lower()
    if force in ("1", "true", "yes", "on"):
        return True
    gate = os.getenv("KIS_DEGRADE_GATES_POLLERS", "1").strip().lower()
    if gate in ("0", "false", "no", "off"):
        return False
    try:
        return bool(is_kis_degraded())
    except Exception:
        return False


def _is_soft_kis_error(error_msg: str) -> bool:
    msg = str(error_msg or "").strip().lower()
    if not msg:
        return False
    return any(token in msg for token in _KIS_SOFT_ERRORS)


def _fetch_orderbook_snapshot_from_kis(collector, code: str) -> dict:
    now_ts = time.time()
    if not _kis_rest_allow_call("orderbook", code, now=now_ts):
        return {
            "asks": [],
            "bids": [],
            "total_ask_qty": 0,
            "total_bid_qty": 0,
            "market_status": _market_status(),
            "updated_at": datetime.now().strftime("%H:%M:%S"),
            "source_id": _STOCK_FLOW_WS_ORDERBOOK_SOURCE_ID,
            "error": "KIS REST cooldown",
        }

    path = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
    }
    try:
        res = collector._get(path, params, "FHKST01010200")
    except Exception as e:
        res = {"rt_cd": "9", "error": str(e)}

    # Error propagation - Refactored to return error instead of raising
    if str(res.get("rt_cd") or "0") != "0":
        err_msg = res.get("error") or res.get("msg1") or "KIS API Error"
        is_soft = _is_soft_kis_error(str(err_msg))
        if not is_soft:
            fail_count, next_retry_ts, should_log = _kis_rest_mark_failure("orderbook", code, str(err_msg))
            if should_log:
                retry_after = max(0.0, next_retry_ts - time.time())
                logger.warning(
                    "[_fetch_orderbook_snapshot_from_kis] %s error: %s (fail=%s cooldown=%.1fs)",
                    code,
                    err_msg,
                    fail_count,
                    retry_after,
                )
        return {
            "asks": [],
            "bids": [],
            "total_ask_qty": 0,
            "total_bid_qty": 0,
            "market_status": _market_status(),
            "updated_at": datetime.now().strftime("%H:%M:%S"),
            "source_id": _STOCK_FLOW_WS_ORDERBOOK_SOURCE_ID,
            "error": f"KIS API Error: {err_msg}",
            "_soft_error": bool(is_soft),
        }

    # Fallback for output vs output1 (Expanded TR usually uses output1)
    output1 = res.get("output1") or res.get("output") or {}
    _kis_rest_mark_success("orderbook", code)

    asks, bids = [], []
    for i in range(1, 11):
        ap = _safe_int(output1.get(f"askp{i}"))
        aq = _safe_int(output1.get(f"askp_rsqn{i}"))
        bp = _safe_int(output1.get(f"bidp{i}"))
        bq = _safe_int(output1.get(f"bidp_rsqn{i}"))
        if ap > 0:
            asks.append({"price": ap, "qty": aq})
        if bp > 0:
            bids.append({"price": bp, "qty": bq})

    return {
        "asks": asks,
        "bids": bids,
        "total_ask_qty": _safe_int(output1.get("total_askp_rsqn")),
        "total_bid_qty": _safe_int(output1.get("total_bidp_rsqn")),
        "market_status": _market_status(),
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "source_id": _STOCK_FLOW_WS_ORDERBOOK_SOURCE_ID,
    }


def _fetch_trade_ticks_from_kis(collector, code: str) -> list[dict]:
    now_ts = time.time()
    if not _kis_rest_allow_call("trades", code, now=now_ts):
        return []

    path = "/uapi/domestic-stock/v1/quotations/inquire-time-itemconclusion"
    params = {
        "FID_ETC_CLS_CODE": "",
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_HOUR_1": datetime.now().strftime("%H%M%S"),
        "FID_PW_DATA_INCU_YN": "Y",
    }
    try:
        res = collector._get(path, params, "FHPST01060000")
    except Exception as e:
        res = {"rt_cd": "9", "error": str(e)}
    
    if str(res.get("rt_cd") or "0") != "0":
        err_msg = res.get("error") or res.get("msg1") or "KIS API Error"
        if not _is_soft_kis_error(str(err_msg)):
            fail_count, next_retry_ts, should_log = _kis_rest_mark_failure("trades", code, str(err_msg))
            if should_log:
                retry_after = max(0.0, next_retry_ts - time.time())
                logger.warning(
                    "[_fetch_trade_ticks_from_kis] %s error: %s (fail=%s cooldown=%.1fs)",
                    code,
                    err_msg,
                    fail_count,
                    retry_after,
                )
        # We return an empty list here to avoid breaking the consumer, but log the error
        return []

    raw = res.get("output2") or []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    _kis_rest_mark_success("trades", code)

    ticks = []
    for r in raw:
        t = str(r.get("stck_cntg_hour") or "").strip()
        price = _safe_int(r.get("stck_prpr"))
        qty = _safe_int(r.get("cnqn"))
        ask = _safe_int(r.get("askp"))
        bid = _safe_int(r.get("bidp"))
        acml_vol = _safe_int(r.get("acml_vol"))
        if not t or price <= 0 or qty <= 0:
            continue
        value = int(price * qty)
        ticks.append({
            "time": t,
            "price": price,
            "qty": qty,
            "value": value,
            "ask": ask,
            "bid": bid,
            "day_turnover_est": int(price * acml_vol) if acml_vol > 0 else 0,
            "side": _infer_trade_side(price, ask, bid),
            "trade_key": f"{t}:{price}:{qty}",
        })
    return ticks


def _build_whale_payload(ticks: list[dict]) -> tuple[list[dict], dict]:
    values = [float(t.get("value") or 0) for t in ticks if (t.get("value") or 0) > 0]
    prices = [float(t.get("price") or 0) for t in ticks if (t.get("price") or 0) > 0]
    day_turnover_est = int(max((t.get("day_turnover_est") or 0) for t in ticks)) if ticks else 0

    if not values:
        rule = {
            "threshold_value": 0,
            "threshold_qty_est": 0,
            "p80_value": 0,
            "median_value": 0,
            "floor_value": 0,
            "turnover_floor_value": 0,
            "abs_floor_value": _WHALE_MIN_ABS_VALUE,
            "day_turnover_est": day_turnover_est,
            "window_size": 0,
            "formula": _WHALE_FORMULA_TEXT,
            "explanation": "최근 체결 데이터가 부족해 고래 기준을 계산하지 못했습니다.",
            "updated_at": datetime.now().strftime("%H:%M:%S"),
            "source_id": _STOCK_FLOW_WS_TRADE_SOURCE_ID,
        }
        return [], rule

    p80_value = int(round(_percentile(values, 80)))
    median_value = int(round(_percentile(values, 50)))
    median_price = max(1, int(round(_percentile(prices, 50)))) if prices else 1
    floor_value = int(
        min(
            max(median_price * _WHALE_PRICE_FLOOR_MULTIPLIER, _WHALE_PRICE_FLOOR_MIN),
            _WHALE_PRICE_FLOOR_MAX,
        )
    )
    turnover_floor_value = int(
        min(
            max(day_turnover_est * _WHALE_TURNOVER_FLOOR_RATIO, 0),
            _WHALE_TURNOVER_FLOOR_MAX,
        )
    )
    threshold_value = int(
        max(
            p80_value,
            int(median_value * 2.5),
            floor_value,
            turnover_floor_value,
            _WHALE_MIN_ABS_VALUE,
        )
    )
    threshold_qty_est = max(1, threshold_value // max(median_price, 1))

    whales = []
    for t in ticks:
        if int(t.get("value") or 0) < threshold_value:
            continue
        whales.append({
            "time": t.get("time"),
            "price": int(t.get("price") or 0),
            "qty": int(t.get("qty") or 0),
            "value": int(t.get("value") or 0),
            "side": t.get("side") or "neutral",
            "trade_key": t.get("trade_key"),
        })
        if len(whales) >= 12:
            break

    rule = {
        "threshold_value": threshold_value,
        "threshold_qty_est": threshold_qty_est,
        "p80_value": p80_value,
        "median_value": median_value,
        "floor_value": floor_value,
        "turnover_floor_value": turnover_floor_value,
        "abs_floor_value": _WHALE_MIN_ABS_VALUE,
        "day_turnover_est": day_turnover_est,
        "window_size": len(values),
        "formula": _WHALE_FORMULA_TEXT,
        "explanation": (
            f"최근 {len(values)}건 체결대금의 P80·중앙값×2.5·가격기반바닥(가격×{_WHALE_PRICE_FLOOR_MULTIPLIER:,}주, "
            f"{_WHALE_PRICE_FLOOR_MIN:,}~{_WHALE_PRICE_FLOOR_MAX:,}원)·당일거래대금비율({_WHALE_TURNOVER_FLOOR_RATIO * 100:.2f}% "
            f"상한 {_WHALE_TURNOVER_FLOOR_MAX:,}원)·절대하한({_WHALE_MIN_ABS_VALUE:,}원) 중 최대값({threshold_value:,}원)을 "
            f"고래 기준으로 사용합니다."
        ),
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "source_id": _STOCK_FLOW_WS_TRADE_SOURCE_ID,
    }
    return whales, rule


def _default_orderbook_payload() -> dict:
    return {
        "asks": [],
        "bids": [],
        "total_ask_qty": 0,
        "total_bid_qty": 0,
        "market_status": _market_status(),
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "source_id": _STOCK_FLOW_WS_ORDERBOOK_SOURCE_ID,
    }


def _load_stock_flow_payload(code: str) -> dict:
    c = _normalize_single_stock_code(code)
    if not c:
        return {
            "code": "",
            "orderbook": _default_orderbook_payload(),
            "whales": [],
            "whale_rule": {
                "threshold_value": 0,
                "threshold_qty_est": 0,
                "p80_value": 0,
                "median_value": 0,
                "floor_value": 0,
                "turnover_floor_value": 0,
                "abs_floor_value": _WHALE_MIN_ABS_VALUE,
                "day_turnover_est": 0,
                "window_size": 0,
                "formula": _WHALE_FORMULA_TEXT,
                "explanation": "종목코드가 없어 고래 체결 기준을 계산하지 못했습니다.",
                "updated_at": datetime.now().strftime("%H:%M:%S"),
                "source_id": _STOCK_FLOW_WS_TRADE_SOURCE_ID,
            },
            "market_status": _market_status(),
            "source": {
                "trade": _STOCK_FLOW_WS_TRADE_SOURCE_ID,
                "orderbook": _STOCK_FLOW_WS_ORDERBOOK_SOURCE_ID,
            },
        }

    now = time.time()
    entry = _STOCK_FLOW_WS_CACHE.setdefault(c, {})
    collector = _get_news_ws_collector()

    # Phase 3 — 이 code 를 다음부터 WS orderbook 구독에도 태움 (멱등)
    try:
        _ensure_orderbook_subscription(c)
    except Exception:
        pass

    # Phase 3 — WS orderbook 캐시 우선. 신선하면 REST 호출 스킵.
    used_ws_orderbook = False
    if _KIS_ORDERBOOK_WS_READ:
        ws_snap = _orderbook_ws_get_fresh(c)
        if isinstance(ws_snap, dict):
            entry["orderbook"] = ws_snap
            entry["orderbook_fetched_at"] = now
            used_ws_orderbook = True

    # 10호가: WS 캐시 없으면 REST 로 fallback (3초 주기 갱신)
    if not used_ws_orderbook and (
        (now - float(entry.get("orderbook_fetched_at") or 0.0) >= _STOCK_FLOW_WS_ORDERBOOK_INTERVAL_SEC)
        or ("orderbook" not in entry)
    ):
        attempted_orderbook_fetch = False
        if collector is not None and _kis_rest_allow_call("orderbook", c, now=now):
            attempted_orderbook_fetch = True
            try:
                ob = _fetch_orderbook_snapshot_from_kis(collector, c)
                has_error = bool((ob or {}).get("error"))
                prev_ob = entry.get("orderbook") if isinstance(entry.get("orderbook"), dict) else None
                prev_has_depth = bool((prev_ob or {}).get("asks") or (prev_ob or {}).get("bids"))
                if has_error and prev_has_depth:
                    stale_ob = dict(prev_ob)
                    stale_ob["_stale"] = True
                    stale_ob["stale_reason"] = ob.get("error")
                    stale_ob["market_status"] = _market_status()
                    entry["orderbook"] = stale_ob
                else:
                    entry["orderbook"] = ob
                # Shadow diff 로그 (Phase 3)
                try:
                    _orderbook_shadow_log_diff(c, ob)
                except Exception:
                    pass
            except Exception as e:
                ob = _default_orderbook_payload()
                ob["error"] = str(e)
                entry["orderbook"] = ob
        elif "orderbook" not in entry:
            entry["orderbook"] = _default_orderbook_payload()
            entry["orderbook"]["error"] = "collector_unavailable" if collector is None else "kis_rest_cooldown"
        if attempted_orderbook_fetch:
            entry["orderbook_fetched_at"] = now

    # 체결: RT 기반 고래 계산 우선, REST는 제한적 폴백
    if (now - float(entry.get("trades_fetched_at") or 0.0) >= _STOCK_FLOW_WS_TRADES_INTERVAL_SEC) or ("whales" not in entry):
        rt = _KIS_RT_CACHE.get(c) or {}
        rt_recent = (now - float(rt.get("updated_epoch") or 0.0)) <= _STOCK_FLOW_RT_RECENT_SEC
        rt_payload = _build_whale_payload_from_rt(c) if rt_recent else None

        if rt_payload is not None:
            whales, whale_rule = rt_payload
            entry["whales"] = whales
            entry["whale_rule"] = whale_rule
        else:
            # RT가 충분하지 않을 때만 제한적으로 REST 폴백
            can_rest_fetch = (now - float(entry.get("trades_rest_fetched_at") or 0.0)) >= _STOCK_FLOW_WS_TRADES_REST_FALLBACK_SEC
            can_rest_fetch = can_rest_fetch and _kis_rest_allow_call("trades", c, now=now)
            if can_rest_fetch and collector is not None:
                try:
                    ticks = _fetch_trade_ticks_from_kis(collector, c)
                    if ticks:
                        whales, whale_rule = _build_whale_payload(ticks)
                        entry["whales"] = whales
                        entry["whale_rule"] = whale_rule
                    else:
                        entry["whales"] = list(entry.get("whales") or [])
                        entry["whale_rule"] = dict(entry.get("whale_rule") or {
                            "threshold_value": 0,
                            "threshold_qty_est": 0,
                            "p80_value": 0,
                            "median_value": 0,
                            "floor_value": 0,
                            "turnover_floor_value": 0,
                            "abs_floor_value": _WHALE_MIN_ABS_VALUE,
                            "day_turnover_est": 0,
                            "window_size": 0,
                            "formula": _WHALE_FORMULA_TEXT,
                            "explanation": "실시간 체결 fallback 결과가 비어 기존 값을 유지합니다.",
                            "updated_at": datetime.now().strftime("%H:%M:%S"),
                            "source_id": _STOCK_FLOW_WS_TRADE_SOURCE_ID,
                        })
                except Exception as e:
                    entry["whales"] = list(entry.get("whales") or [])
                    entry["whale_rule"] = dict(entry.get("whale_rule") or {
                        "threshold_value": 0,
                        "threshold_qty_est": 0,
                        "p80_value": 0,
                        "median_value": 0,
                        "floor_value": 0,
                        "turnover_floor_value": 0,
                        "abs_floor_value": _WHALE_MIN_ABS_VALUE,
                        "day_turnover_est": 0,
                        "window_size": 0,
                        "formula": _WHALE_FORMULA_TEXT,
                        "explanation": f"고래 체결 데이터를 가져오지 못했습니다: {e}",
                        "updated_at": datetime.now().strftime("%H:%M:%S"),
                        "source_id": _STOCK_FLOW_WS_TRADE_SOURCE_ID,
                    })
                finally:
                    entry["trades_rest_fetched_at"] = now
            else:
                # 기존값 유지 (없으면 기본값)
                entry["whales"] = list(entry.get("whales") or [])
                entry["whale_rule"] = dict(entry.get("whale_rule") or {
                    "threshold_value": 0,
                    "threshold_qty_est": 0,
                    "p80_value": 0,
                    "median_value": 0,
                    "floor_value": 0,
                    "turnover_floor_value": 0,
                    "abs_floor_value": _WHALE_MIN_ABS_VALUE,
                    "day_turnover_est": 0,
                    "window_size": 0,
                    "formula": _WHALE_FORMULA_TEXT,
                    "explanation": "실시간 체결 스트림 수신 대기 중입니다.",
                    "updated_at": datetime.now().strftime("%H:%M:%S"),
                    "source_id": _STOCK_FLOW_WS_TRADE_SOURCE_ID,
                })
        entry["trades_fetched_at"] = now

    return {
        "code": c,
        "orderbook": entry.get("orderbook") or _default_orderbook_payload(),
        "whales": list(entry.get("whales") or []),
        "whale_rule": dict(entry.get("whale_rule") or {}),
        "market_status": _market_status(),
        "source": {
            "trade": _STOCK_FLOW_WS_TRADE_SOURCE_ID,
            "orderbook": _STOCK_FLOW_WS_ORDERBOOK_SOURCE_ID,
        },
    }


def _load_stock_flow_payloads(codes: list[str]) -> dict:
    result = {}
    for code in _normalize_stock_flow_codes(codes):
        result[code] = _load_stock_flow_payload(code)
    return result


def _normalize_ws_codes(codes, max_codes: int | None = None) -> list[str]:
    cap = _NEWS_WS_MAX_CODES if max_codes is None else max(1, int(max_codes))
    out = []
    seen = set()
    for c in (codes or []):
        code = str(c or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
        if len(out) >= cap:
            break
    return out


def _fetch_top_trading_codes(limit: int) -> list[str]:
    if not _stocks_db_available():
        return []
    conn = get_stocks_conn()
    try:
        rows = conn.execute(
            """
            SELECT code
            FROM price_today
            WHERE code IS NOT NULL
              AND code <> ''
              AND COALESCE(current_price, 0) > 0
            ORDER BY COALESCE(trading_value, 0) DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        out = []
        seen = set()
        for r in rows:
            code = str((r["code"] if hasattr(r, "keys") else r[0]) or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            out.append(code)
        return out
    except Exception:
        return []
    finally:
        conn.close()


def _get_news_ws_broadcast_codes(force: bool = False) -> list[str]:
    now = time.time()
    if not force:
        with _NEWS_WS_BROADCAST_CODES_LOCK:
            cached_codes = list(_NEWS_WS_BROADCAST_CODES_CACHE.get("codes") or [])
            cached_ts = float(_NEWS_WS_BROADCAST_CODES_CACHE.get("ts") or 0.0)
        if cached_codes and (now - cached_ts) < _NEWS_WS_BROADCAST_CODES_TTL_SEC:
            return _normalize_ws_codes(cached_codes, max_codes=_NEWS_WS_BROADCAST_TOPN)

    codes = _normalize_ws_codes(
        _fetch_top_trading_codes(_NEWS_WS_BROADCAST_TOPN),
        max_codes=_NEWS_WS_BROADCAST_TOPN,
    )
    if not codes:
        codes = _normalize_ws_codes(
            _collect_focus_codes(_NEWS_WS_BROADCAST_TOPN),
            max_codes=_NEWS_WS_BROADCAST_TOPN,
        )

    with _NEWS_WS_BROADCAST_CODES_LOCK:
        _NEWS_WS_BROADCAST_CODES_CACHE["codes"] = list(codes)
        _NEWS_WS_BROADCAST_CODES_CACHE["ts"] = now
    return list(codes)


def _touch_focus_codes(codes, ttl_sec: float | None = None) -> int:
    now = time.monotonic()
    ttl = max(5.0, float(ttl_sec if ttl_sec is not None else _FOCUS_CODE_TTL_SEC))
    expires = now + ttl
    touched = 0
    with _FOCUS_CODES_LOCK:
        for c in (codes or []):
            code = _normalize_single_stock_code(c)
            if not code:
                continue
            prev = float(_FOCUS_CODE_EXPIRES_AT.get(code) or 0.0)
            if expires > prev:
                _FOCUS_CODE_EXPIRES_AT[code] = expires
            touched += 1
        if len(_FOCUS_CODE_EXPIRES_AT) > _FOCUS_CODE_MAX_TRACKED:
            keep = sorted(
                _FOCUS_CODE_EXPIRES_AT.items(),
                key=lambda kv: kv[1],
                reverse=True,
            )[:_FOCUS_CODE_MAX_TRACKED]
            _FOCUS_CODE_EXPIRES_AT.clear()
            _FOCUS_CODE_EXPIRES_AT.update(dict(keep))
    return touched


def _collect_focus_codes(max_codes: int) -> list[str]:
    now = time.monotonic()
    max_codes = max(1, int(max_codes or 1))
    with _FOCUS_CODES_LOCK:
        expired = [c for c, ts in _FOCUS_CODE_EXPIRES_AT.items() if ts <= now]
        for c in expired:
            _FOCUS_CODE_EXPIRES_AT.pop(c, None)
        ordered = sorted(
            _FOCUS_CODE_EXPIRES_AT.items(),
            key=lambda kv: kv[1],
            reverse=True,
        )
    return [c for c, _ in ordered[:max_codes]]


def _collect_codes_from_obj(obj, out: set[str], max_collect: int = 400) -> None:
    if len(out) >= max_collect:
        return
    if isinstance(obj, dict):
        code = obj.get("code")
        c = _normalize_single_stock_code(code)
        if c:
            out.add(c)
        for v in obj.values():
            _collect_codes_from_obj(v, out, max_collect=max_collect)
            if len(out) >= max_collect:
                return
        return
    if isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_codes_from_obj(v, out, max_collect=max_collect)
            if len(out) >= max_collect:
                return


def _touch_focus_from_payload(payload, ttl_sec: float = 120.0, max_collect: int = 400) -> int:
    codes: set[str] = set()
    _collect_codes_from_obj(payload, codes, max_collect=max_collect)
    if not codes:
        return 0
    return _touch_focus_codes(sorted(codes), ttl_sec=ttl_sec)


def _prioritize_rt_codes(codes, max_codes: int) -> list[str]:
    """
    Choose RT subscription candidates by recent viewport focus first,
    then fill with remaining codes.
    """
    max_codes = max(1, int(max_codes or 1))
    normalized = []
    seen = set()
    for c in (codes or []):
        code = _normalize_single_stock_code(c)
        if not code or code in seen:
            continue
        seen.add(code)
        normalized.append(code)
    if not normalized:
        return []

    code_set = set(normalized)
    _touch_focus_codes(normalized, ttl_sec=120.0)
    hot = _collect_focus_codes(max(max_codes * 3, max_codes))

    out = []
    used = set()
    for c in hot:
        if c in code_set and c not in used:
            used.add(c)
            out.append(c)
            if len(out) >= max_codes:
                return out

    for c in normalized:
        if c not in used:
            used.add(c)
            out.append(c)
            if len(out) >= max_codes:
                break
    return out


def _get_news_ws_collector():
    global _NEWS_WS_KIS_COLLECTOR
    if _NEWS_WS_KIS_COLLECTOR is not None:
        return _NEWS_WS_KIS_COLLECTOR
    with _NEWS_WS_KIS_LOCK:
        if _NEWS_WS_KIS_COLLECTOR is not None:
            return _NEWS_WS_KIS_COLLECTOR
        try:
            sys.path.insert(0, ROOT_DIR)
            from collectors.kis_api import KISCollector
            _NEWS_WS_KIS_COLLECTOR = KISCollector()
        except Exception:
            _NEWS_WS_KIS_COLLECTOR = None
        return _NEWS_WS_KIS_COLLECTOR


def _get_recent_strength_from_cache(code: str, now: float) -> float | None:
    rt = _KIS_RT_CACHE.get(code) or {}
    rt_strength = to_float(rt.get("strength"), None)
    rt_updated_epoch = float(rt.get("updated_epoch") or 0.0)
    if rt_strength is not None and rt_updated_epoch > 0 and (now - rt_updated_epoch) <= _NEWS_WS_STRENGTH_SOFT_TTL_SEC:
        return round(rt_strength, 2)

    cached = _NEWS_WS_KIS_CACHE.get(code)
    if isinstance(cached, dict):
        fetched_at = float(cached.get("fetched_at") or 0.0)
        if fetched_at > 0 and (now - fetched_at) <= _NEWS_WS_STRENGTH_SOFT_TTL_SEC:
            sv = to_float((cached.get("data") or {}).get("strength"), None)
            if sv is not None and sv >= 0:
                return round(sv, 2)
    return None


def _load_news_ws_prices(codes: list[str], max_codes: int | None = None) -> dict:
    codes = _normalize_ws_codes(codes, max_codes=max_codes)
    if not codes:
        return {}

    ts = datetime.now().strftime("%H:%M:%S")
    result: dict = {}

    # ① KIS RT WS 캐시 우선 — 가장 빠르고 실시간 (체결강도 포함)
    # KRX(H0STCNT0) 와 NXT(H0NXCNT0) 두 캐시를 모두 보고 더 신선한 tick 사용.
    # 정규장 시간(09:00-15:30)엔 KRX 가, NXT 단독 시간(08:00-09:00, 15:30-20:00)엔
    # NXT 가 자연스럽게 더 신선해짐 → 시간대 분기 없이 epoch 비교만으로 정답.
    rt_miss = []
    for code in codes:
        krx_rt = _KIS_RT_CACHE.get(code)
        nxt_rt = _KIS_NXT_RT_CACHE.get(code)
        krx_ok = bool(krx_rt and krx_rt.get("current_price", 0) > 0)
        nxt_ok = bool(nxt_rt and nxt_rt.get("current_price", 0) > 0)
        if not krx_ok and not nxt_ok:
            rt_miss.append(code)
            continue
        if krx_ok and nxt_ok:
            try:
                pick = nxt_rt if float(nxt_rt.get("updated_epoch") or 0) > float(krx_rt.get("updated_epoch") or 0) else krx_rt
            except Exception:
                pick = krx_rt
        else:
            pick = krx_rt if krx_ok else nxt_rt
        result[code] = {**pick, "updated_at": ts}

    # RT 캐시로 전부 해결된 경우 바로 반환
    if not rt_miss:
        return result

    # ② RT 미스 코드는 DB 스냅샷으로 보완 (장 마감 후 / WS 아직 수신 전)
    if _stocks_db_available():
        try:
            conn = get_stocks_conn()
            ph = ",".join("?" * len(rt_miss))
            rows = conn.execute(
                f"SELECT code, current_price, change_pct, trading_value, trading_volume, volume_turnover_rate "
                f"FROM price_today WHERE code IN ({ph})",
                rt_miss,
            ).fetchall()
            conn.close()
            for r in rows:
                code = str(r["code"] or "").strip()
                if not code:
                    continue
                cur = r["current_price"]
                if not cur:  # 0 또는 None → DB 스냅샷 없음, 생략
                    continue
                result[code] = {
                    "code": code,
                    "current_price": cur,
                    "change_pct": r["change_pct"],
                    "trading_value": r["trading_value"],    # 오늘 거래대금 (원)
                    "trading_volume": r["trading_volume"],  # 누적 거래량
                    "vol_tnrt": r["volume_turnover_rate"],  # 회전율 % (체결강도 대용)
                    "updated_at": ts,
                    "source": "db",
                }
        except Exception:
            pass

    now = time.time()

    # ③ RT/최근캐시 strength 보강 (REST 호출 없이)
    for code in codes:
        row = result.get(code)
        if not isinstance(row, dict):
            continue
        if row.get("strength") is not None:
            continue
        cached_strength = _get_recent_strength_from_cache(code, now)
        if cached_strength is not None:
            row["strength"] = cached_strength
            if row.get("source") == "db":
                row["source"] = "db+rt_strength"

    # ④ 장중 REST 폴백 (제한적): strength 없는 코드 일부만, 코드당 최소 간격 유지
    if _market_status() == "open" and _NEWS_WS_REST_STRENGTH_ENABLED:
        collector = _get_news_ws_collector()
        if collector is not None:
            hhmmss = datetime.now().strftime("%H%M%S")

            need_codes = []
            for code in codes:
                row = result.get(code)
                if not isinstance(row, dict):
                    continue
                if not row.get("current_price"):
                    continue
                if row.get("strength") is not None:
                    continue
                next_ok_ts = float(_NEWS_WS_REST_STRENGTH_NEXT_TS.get(code) or 0.0)
                if now < next_ok_ts:
                    continue
                need_codes.append(code)

            miss_codes = need_codes[:_NEWS_WS_REST_STRENGTH_MAX_CODES_PER_CYCLE]

            def _fetch_strength_only(code: str) -> tuple[str, float | None]:
                try:
                    res = collector._get(
                        "/uapi/domestic-stock/v1/quotations/inquire-time-itemconclusion",
                        {
                            "FID_ETC_CLS_CODE": "",
                            "FID_COND_MRKT_DIV_CODE": "J",
                            "FID_INPUT_ISCD": code,
                            "FID_INPUT_HOUR_1": hhmmss,
                            "FID_PW_DATA_INCU_YN": "Y",
                        },
                        "FHPST01060000",
                    )
                    if str(res.get("rt_cd") or "") != "0":
                        return code, None
                    out2 = res.get("output2") or []
                    if isinstance(out2, dict):
                        out2 = [out2]
                    if not out2:
                        return code, None
                    sv = to_float(out2[0].get("tday_rltv"), None)
                    if sv is None or sv < 0:
                        return code, None
                    return code, round(sv, 2)
                except Exception:
                    return code, None

            if miss_codes:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=min(len(miss_codes), 3)) as ex:
                    for code, strength_val in ex.map(_fetch_strength_only, miss_codes):
                        _NEWS_WS_REST_STRENGTH_NEXT_TS[code] = now + _NEWS_WS_REST_STRENGTH_MIN_INTERVAL_SEC
                        if strength_val is None:
                            continue
                        row = result.get(code) or {}
                        row["strength"] = strength_val
                        row["source"] = "kis_rest_strength"
                        result[code] = row
                        _NEWS_WS_KIS_CACHE[code] = {
                            "data": {"code": code, "strength": strength_val, "updated_at": ts, "source": "kis_rest_strength"},
                            "fetched_at": now,
                        }

    return result


class _NewsPriceWsHub:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._clients: dict[int, dict] = {}
        self._next_id = 1
        self._task = None
        self._last_rt_codes: tuple[str, ...] = tuple()
        self._last_rt_set_ts: float = 0.0

    @staticmethod
    def _fingerprint_from_ws(ws: WebSocket) -> str:
        try:
            xff = str((ws.headers.get("x-forwarded-for") or "")).split(",")[0].strip()
        except Exception:
            xff = ""
        try:
            host = str((ws.client.host if ws.client else "") or "").strip()
        except Exception:
            host = ""
        try:
            ua = str((ws.headers.get("user-agent") or "") or "").strip()
        except Exception:
            ua = ""
        ip = xff or host or "unknown"
        return f"{ip}|{ua[:96]}"

    @staticmethod
    def _count_unique_users(entries: list[dict]) -> int:
        users = set()
        for entry in entries:
            fp = str(entry.get("fp") or "").strip()
            if fp:
                users.add(fp)
            else:
                users.add(f"cid:{id(entry)}")
        return len(users)

    async def _stats_unlocked(self) -> dict:
        active_connections = self._count_unique_users(list(self._clients.values()))
        return {
            "active_connections": _display_live_users_count(active_connections),
            "max_connections": _NEWS_WS_MAX_CLIENTS,
            "beta_notice": _NEWS_WS_BETA_NOTICE,
            "broadcast": True,
            "top_n": _NEWS_WS_BROADCAST_TOPN,
        }

    async def get_stats(self) -> dict:
        async with self._lock:
            return await self._stats_unlocked()

    async def _sync_rt_codes(self, merged_codes: list[str], force: bool = False) -> list[str]:
        prioritized = tuple(_prioritize_rt_codes(merged_codes, _KIS_RT_HUB.MAX_CODES))
        now_ts = time.time()

        if not force:
            elapsed = now_ts - float(self._last_rt_set_ts or 0.0)
            same_codes = prioritized == self._last_rt_codes
            if same_codes and elapsed < _NEWS_WS_RT_SET_CODES_HEARTBEAT_SEC:
                return list(prioritized)
            if (not same_codes) and elapsed < _NEWS_WS_RT_SET_CODES_MIN_INTERVAL_SEC:
                return list(self._last_rt_codes)

        await asyncio.to_thread(_KIS_RT_HUB.set_codes, list(prioritized))
        # NXT hub 도 동일 종목 구독 — 정규장 외 시간에 NXT tick 으로 홈 가격 갱신
        try:
            await asyncio.to_thread(
                _KIS_NXT_RT_HUB.set_codes,
                list(prioritized)[:_KIS_NXT_RT_HUB.MAX_CODES],
            )
        except Exception:
            logger.exception("NXT hub set_codes failed (non-fatal)")
        self._last_rt_codes = prioritized
        self._last_rt_set_ts = now_ts
        return list(prioritized)

    async def connect(self, ws: WebSocket):
        async with self._lock:
            if len(self._clients) >= _NEWS_WS_MAX_CLIENTS:
                limit_stats = await self._stats_unlocked()
                accept = False
                cid = None
            else:
                cid = self._next_id
                self._next_id += 1
                self._clients[cid] = {
                    "ws": ws,
                    "codes": set(),
                    "last_active": time.monotonic(),
                    "fp": self._fingerprint_from_ws(ws),
                }
                if self._task is None or self._task.done():
                    self._task = asyncio.create_task(self._run())
                accept = True
                limit_stats = await self._stats_unlocked()

        await ws.accept()
        if not accept:
            await ws.send_json({
                "type": "busy",
                **limit_stats,
                "message": _NEWS_WS_BUSY_MESSAGE,
            })
            await ws.close(code=1013, reason="beta_user_limit")
            return None

        interval_sec = _NEWS_WS_INTERVAL_OPEN_SEC if _market_status() == "open" else _NEWS_WS_INTERVAL_CLOSED_SEC
        await ws.send_json({
            "type": "connected",
            "interval_ms": int(interval_sec * 1000),
            **limit_stats,
        })
        asyncio.create_task(self._send_initial_snapshot(cid))
        return cid

    async def _send_initial_snapshot(self, cid: int) -> None:
        try:
            async with self._lock:
                entry = self._clients.get(cid)
                if not entry:
                    return
                ws = entry.get("ws")
                limit_stats = await self._stats_unlocked()
            if ws is None:
                return

            codes = await asyncio.to_thread(_get_news_ws_broadcast_codes)
            if codes:
                # 초기 스냅샷은 broad set이므로 focus는 짧게만 터치
                _touch_focus_codes(codes[: min(len(codes), 30)], ttl_sec=60.0)
                await self._sync_rt_codes(codes, force=True)
            prices = await asyncio.to_thread(
                _load_news_ws_prices,
                codes,
                min(_NEWS_WS_MAX_CODES, _NEWS_WS_FETCH_CODES_CAP),
            )
            await ws.send_json({
                "type": "prices",
                "updated_at": datetime.now().strftime("%H:%M:%S"),
                "prices": prices,
                "codes": codes,
                "broadcast": True,
                "changed_only": False,
                **limit_stats,
            })
        except Exception:
            pass

    async def touch(self, cid: int) -> None:
        async with self._lock:
            entry = self._clients.get(cid)
            if entry:
                entry["last_active"] = time.monotonic()

    async def update_codes(self, cid: int, codes) -> list[str]:
        normalized = _normalize_ws_codes(codes, max_codes=_NEWS_WS_REQUEST_CODES_MAX)
        if normalized:
            _touch_focus_codes(normalized, ttl_sec=180.0)
        all_req_codes = set()
        async with self._lock:
            entry = self._clients.get(cid)
            if not entry:
                return []
            entry["codes"] = set(normalized)
            entry["last_active"] = time.monotonic()
            for client in self._clients.values():
                all_req_codes.update(client.get("codes") or set())

        base_codes = await asyncio.to_thread(_get_news_ws_broadcast_codes)
        merged_codes = _normalize_ws_codes(
            sorted(all_req_codes) + list(base_codes),
            max_codes=min(_NEWS_WS_MAX_CODES, _NEWS_WS_FETCH_CODES_CAP),
        )
        await self._sync_rt_codes(merged_codes, force=True)
        return normalized

    async def disconnect(self, cid: int) -> None:
        has_clients = False
        async with self._lock:
            self._clients.pop(cid, None)
            has_clients = bool(self._clients)
        if not has_clients:
            await asyncio.to_thread(_KIS_RT_HUB.set_codes, [])
            try:
                await asyncio.to_thread(_KIS_NXT_RT_HUB.set_codes, [])
            except Exception:
                pass
            self._last_rt_codes = tuple()
            self._last_rt_set_ts = 0.0

    async def _run(self):
        while True:
            async with self._lock:
                if not self._clients:
                    self._task = None
                    return
                snapshot = [
                    {
                        "cid": cid,
                        "ws": entry["ws"],
                        "codes": set(entry.get("codes") or set()),
                        "last_active": float(entry.get("last_active") or 0.0),
                    }
                    for cid, entry in self._clients.items()
                ]

            now_mono = time.monotonic()
            idle_cids = [
                s["cid"]
                for s in snapshot
                if now_mono - s["last_active"] > _NEWS_WS_IDLE_SEC
            ]
            idle_set = set(idle_cids)
            active_snapshot = [s for s in snapshot if s["cid"] not in idle_set]
            active_connections = self._count_unique_users(active_snapshot)
            if not active_snapshot:
                if idle_cids:
                    async with self._lock:
                        for cid in idle_cids:
                            self._clients.pop(cid, None)
                await asyncio.sleep(_NEWS_WS_INTERVAL_CLOSED_SEC)
                continue

            requested_codes = _normalize_ws_codes(
                sorted({code for s in active_snapshot for code in (s.get("codes") or set())}),
                max_codes=_NEWS_WS_REQUEST_CODES_MAX,
            )
            base_codes = await asyncio.to_thread(_get_news_ws_broadcast_codes)
            if requested_codes:
                _touch_focus_codes(requested_codes, ttl_sec=220.0)
            codes = _normalize_ws_codes(
                requested_codes + list(base_codes),
                max_codes=min(_NEWS_WS_MAX_CODES, _NEWS_WS_FETCH_CODES_CAP),
            )
            if codes and not requested_codes:
                _touch_focus_codes(codes[: min(len(codes), 30)], ttl_sec=90.0)
            prioritized_input = list(requested_codes) + [c for c in codes if c not in set(requested_codes)]
            await self._sync_rt_codes(prioritized_input, force=False)

            prices = await asyncio.to_thread(
                _load_news_ws_prices,
                codes,
                min(_NEWS_WS_MAX_CODES, _NEWS_WS_FETCH_CODES_CAP),
            )
            updated_at = datetime.now().strftime("%H:%M:%S")
            interval_sec = _NEWS_WS_INTERVAL_OPEN_SEC if _market_status() == "open" else _NEWS_WS_INTERVAL_CLOSED_SEC

            stale_cids = list(idle_cids)
            for s in active_snapshot:
                cid = s["cid"]
                ws = s["ws"]

                try:
                    await asyncio.wait_for(
                        ws.send_json(
                            {
                                "type": "prices",
                                "updated_at": updated_at,
                                "prices": prices,
                                "codes": codes,
                                "active_connections": _display_live_users_count(active_connections),
                                "max_connections": _NEWS_WS_MAX_CLIENTS,
                                "beta_notice": _NEWS_WS_BETA_NOTICE,
                                "broadcast": True,
                                "top_n": _NEWS_WS_BROADCAST_TOPN,
                                "changed_only": False,
                            }
                        ),
                        timeout=_NEWS_WS_SEND_TIMEOUT_SEC,
                    )
                except Exception:
                    stale_cids.append(cid)

            if stale_cids:
                stale_set = set(stale_cids)
                async with self._lock:
                    for cid in stale_set:
                        self._clients.pop(cid, None)

                # idle/failed clients are closed explicitly to release socket resources quickly
                for s in snapshot:
                    if s["cid"] in stale_set:
                        try:
                            await asyncio.wait_for(
                                s["ws"].close(code=1001, reason="stale_or_idle"),
                                timeout=0.5,
                            )
                        except Exception:
                            pass

            await asyncio.sleep(interval_sec)


_NEWS_PRICE_WS_HUB = _NewsPriceWsHub()


class _StockFlowWsHub:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._clients: dict[int, dict] = {}
        self._next_id = 1
        self._task = None
        self._tick = 0

    async def _stats_unlocked(self) -> dict:
        return {
            "active_connections": _display_live_users_count(len(self._clients)),
            "max_connections": _STOCK_FLOW_WS_MAX_CLIENTS,
            "beta_notice": _STOCK_FLOW_WS_BETA_NOTICE,
        }

    async def get_stats(self) -> dict:
        async with self._lock:
            return await self._stats_unlocked()

    async def connect(self, ws: WebSocket):
        async with self._lock:
            if len(self._clients) >= _STOCK_FLOW_WS_MAX_CLIENTS:
                stats = await self._stats_unlocked()
                accept = False
                cid = None
            else:
                cid = self._next_id
                self._next_id += 1
                self._clients[cid] = {
                    "ws": ws,
                    "code": "",
                    "last_active": time.monotonic(),
                    "last_orderbook_sig": None,
                    "last_whale_sig": None,
                }
                if self._task is None or self._task.done():
                    self._task = asyncio.create_task(self._run())
                stats = await self._stats_unlocked()
                accept = True

        await ws.accept()
        if not accept:
            await ws.send_json({
                "type": "busy",
                **stats,
                "message": _STOCK_FLOW_WS_BUSY_MESSAGE,
            })
            await ws.close(code=1013, reason="beta_user_limit")
            return None

        await ws.send_json({
            "type": "connected",
            "interval_ms": int(_STOCK_FLOW_WS_INTERVAL_OPEN_SEC * 1000),
            "orderbook_interval_sec": _STOCK_FLOW_WS_ORDERBOOK_INTERVAL_SEC,
            "trades_interval_sec": _STOCK_FLOW_WS_TRADES_INTERVAL_SEC,
            **stats,
        })
        # Note: we don't know the 'code' yet. snapshot will be sent inside 'update_code' or first loop run.
        return cid

    async def _send_initial_snapshot(self, cid: int, code: str) -> None:
        try:
            c = _normalize_single_stock_code(code)
            if not c:
                return
            
            payload = await asyncio.to_thread(_load_stock_flow_payload, c)
            async with self._lock:
                entry = self._clients.get(cid)
                if not entry or entry.get("code") != c:
                    return
                ws = entry.get("ws")
                stats = await self._stats_unlocked()
            
            if ws:
                orderbook = payload.get("orderbook") or {}
                whales = payload.get("whales") or []
                await ws.send_json({
                    "type": "stock_flow",
                    "code": c,
                    "orderbook": orderbook,
                    "whales": whales,
                    "whale_rule": payload.get("whale_rule"),
                    "market_status": payload.get("market_status"),
                    "source": payload.get("source"),
                    "changed": {"orderbook": True, "whales": True},
                    "orderbook_interval_sec": _STOCK_FLOW_WS_ORDERBOOK_INTERVAL_SEC,
                    "trades_interval_sec": _STOCK_FLOW_WS_TRADES_INTERVAL_SEC,
                    **stats,
                })
        except Exception:
            pass

    async def touch(self, cid: int) -> None:
        async with self._lock:
            entry = self._clients.get(cid)
            if entry:
                entry["last_active"] = time.monotonic()

    async def update_code(self, cid: int, code: str) -> str:
        c = _normalize_single_stock_code(code)
        if c:
            _touch_focus_codes([c], ttl_sec=180.0)
        async with self._lock:
            entry = self._clients.get(cid)
            if not entry:
                return ""
            entry["code"] = c
            entry["last_active"] = time.monotonic()
            entry["last_orderbook_sig"] = None
            entry["last_whale_sig"] = None
        
        # 즉시 스냅샷 발송 (비동기)
        if c:
            asyncio.create_task(self._send_initial_snapshot(cid, c))
        return c

    async def disconnect(self, cid: int) -> None:
        async with self._lock:
            self._clients.pop(cid, None)

    async def _run(self):
        while True:
            async with self._lock:
                if not self._clients:
                    self._task = None
                    return
                snapshot = [
                    {
                        "cid": cid,
                        "ws": entry["ws"],
                        "code": str(entry.get("code") or ""),
                        "last_active": float(entry.get("last_active") or 0.0),
                        "last_orderbook_sig": entry.get("last_orderbook_sig"),
                        "last_whale_sig": entry.get("last_whale_sig"),
                    }
                    for cid, entry in self._clients.items()
                ]

            now_mono = time.monotonic()
            idle_cids = [
                s["cid"]
                for s in snapshot
                if now_mono - s["last_active"] > _STOCK_FLOW_WS_IDLE_SEC
            ]
            idle_set = set(idle_cids)
            active_snapshot = [s for s in snapshot if s["cid"] not in idle_set]
            active_connections = len(active_snapshot)

            codes = sorted({s["code"] for s in active_snapshot if s["code"]})
            if codes:
                _touch_focus_codes(codes, ttl_sec=180.0)
            payloads = await asyncio.to_thread(_load_stock_flow_payloads, codes)

            interval_sec = (
                _STOCK_FLOW_WS_INTERVAL_OPEN_SEC
                if _market_status() == "open"
                else _STOCK_FLOW_WS_INTERVAL_CLOSED_SEC
            )
            self._tick += 1
            heartbeat_every = max(1, int(round(10.0 / max(interval_sec, 0.1))))

            stale_cids = list(idle_cids)
            sig_updates: dict[int, tuple] = {}

            for s in active_snapshot:
                cid = s["cid"]
                ws = s["ws"]
                code = s["code"]
                if not code:
                    continue

                payload = payloads.get(code) or {}
                orderbook = payload.get("orderbook") or {}
                whales = payload.get("whales") or []
                whale_rule = payload.get("whale_rule") or {}

                orderbook_sig = (
                    orderbook.get("updated_at"),
                    orderbook.get("total_ask_qty"),
                    orderbook.get("total_bid_qty"),
                    len(orderbook.get("asks") or []),
                    len(orderbook.get("bids") or []),
                )
                whale_sig = tuple(w.get("trade_key") for w in whales)

                changed_orderbook = (s.get("last_orderbook_sig") != orderbook_sig)
                changed_whales = (s.get("last_whale_sig") != whale_sig)
                should_send = changed_orderbook or changed_whales or (self._tick % heartbeat_every == 0)
                if not should_send:
                    sig_updates[cid] = (orderbook_sig, whale_sig)
                    continue

                try:
                    await asyncio.wait_for(
                        ws.send_json({
                            "type": "stock_flow",
                            "code": code,
                            "orderbook": orderbook if changed_orderbook else None,
                            "whales": whales if changed_whales else None,
                            "whale_rule": whale_rule,
                            "market_status": payload.get("market_status"),
                            "source": payload.get("source"),
                            "changed": {
                                "orderbook": bool(changed_orderbook),
                                "whales": bool(changed_whales),
                            },
                            "orderbook_interval_sec": _STOCK_FLOW_WS_ORDERBOOK_INTERVAL_SEC,
                            "trades_interval_sec": _STOCK_FLOW_WS_TRADES_INTERVAL_SEC,
                            "active_connections": _display_live_users_count(active_connections),
                            "max_connections": _STOCK_FLOW_WS_MAX_CLIENTS,
                            "beta_notice": _STOCK_FLOW_WS_BETA_NOTICE,
                        }),
                        timeout=_STOCK_FLOW_WS_SEND_TIMEOUT_SEC,
                    )
                    sig_updates[cid] = (orderbook_sig, whale_sig)
                except Exception:
                    stale_cids.append(cid)

            if stale_cids or sig_updates:
                stale_set = set(stale_cids)
                async with self._lock:
                    for cid in stale_set:
                        self._clients.pop(cid, None)
                    for cid, sig_pair in sig_updates.items():
                        entry = self._clients.get(cid)
                        if entry is None:
                            continue
                        entry["last_orderbook_sig"] = sig_pair[0]
                        entry["last_whale_sig"] = sig_pair[1]

                for s in snapshot:
                    if s["cid"] in stale_set:
                        try:
                            await asyncio.wait_for(
                                s["ws"].close(code=1001, reason="stale_or_idle"),
                                timeout=0.5,
                            )
                        except Exception:
                            pass

            await asyncio.sleep(interval_sec)
