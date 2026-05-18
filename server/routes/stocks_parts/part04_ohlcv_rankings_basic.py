# ─── GET /api/stocks/{code}/ohlcv ────────────────────────────
_OHLCV_TTL = {
    "1M": 600,   # 10분
    "3M": 1800,  # 30분
    "6M": 3600,  # 1시간
    "1Y": 7200,  # 2시간
}

def _fetch_ohlcv_pykrx(code: str, days: int) -> list:
    """pykrx로 장기 OHLCV 조회 → 표준 candle dict 리스트 반환"""
    from datetime import timedelta
    from pykrx import stock as pykrx_stock
    end = datetime.now()
    start = end - timedelta(days=days)
    df = pykrx_stock.get_market_ohlcv_by_date(
        start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), code
    )
    if df is None or df.empty:
        return []
    df = df.reset_index()
    # pykrx 컬럼: 날짜, 시가, 고가, 저가, 종가, 거래량
    col_map = {
        "날짜": "date", "시가": "open", "고가": "high",
        "저가": "low",  "종가": "close", "거래량": "volume",
    }
    df = df.rename(columns=col_map)
    candles = []
    for _, row in df.iterrows():
        date_val = row.get("date")
        if hasattr(date_val, "strftime"):
            date_str = date_val.strftime("%Y%m%d")
        else:
            date_str = str(date_val).replace("-", "")
        close = int(row.get("close") or 0)
        if not close:
            continue
        candles.append({
            "date": date_str,
            "open":   int(row.get("open")   or 0),
            "high":   int(row.get("high")   or 0),
            "low":    int(row.get("low")    or 0),
            "close":  close,
            "volume": int(row.get("volume") or 0),
        })
    return candles


def _save_ohlcv_to_db(code: str, candles: list):
    """OHLCV 데이터를 price_daily 테이블에 저장 (upsert)"""
    if not candles:
        return
    try:
        conn = get_stocks_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_daily (
                code TEXT, date TEXT, open INTEGER, high INTEGER,
                low INTEGER, close INTEGER, volume INTEGER,
                trading_value INTEGER, credit_rate REAL,
                PRIMARY KEY (code, date)
            )
        """)
        conn.executemany(
            """INSERT INTO price_daily (code, date, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(code, date) DO UPDATE SET
                   open=excluded.open,
                   high=excluded.high,
                   low=excluded.low,
                   close=excluded.close,
                   volume=excluded.volume""",
            [(code, c["date"], c["open"], c["high"], c["low"], c["close"], c["volume"])
             for c in candles]
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception("failed to persist ohlcv into price_daily (code=%s, candles=%d): %s", code, len(candles), e)


@router.get("/api/stocks/{code}/ohlcv")
def get_stock_ohlcv(code: str, period: str = "1M"):
    import time
    cache_key = f"{code}:{period}"
    now = time.time()
    ttl = _OHLCV_TTL.get(period, 600)
    cached = _OHLCV_CACHE.get(cache_key)
    if cached and now - cached["fetched_at"] < ttl:
        return cached["data"]

    limit_map = {"1M": 30, "3M": 65, "6M": 130, "1Y": 260}
    n = limit_map.get(period, 30)
    candles: list = []

    # ── 1단계: price_daily DB 확인 ──────────────────────────────
    try:
        conn = get_stocks_conn()
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM price_daily "
            "WHERE code=? AND close>0 ORDER BY date DESC LIMIT ?", (code, n)
        ).fetchall()
        conn.close()
        if rows:
            candles = [
                {"date": r["date"], "open": r["open"] or 0, "high": r["high"] or 0,
                 "low": r["low"] or 0, "close": r["close"] or 0, "volume": r["volume"] or 0}
                for r in reversed(rows) if r["close"]
            ]
    except Exception:
        pass

    # ── 2단계: 1M/3M은 KIS API (빠름) ──────────────────────────
    if len(candles) < n and period in ("1M", "3M"):
        try:
            sys.path.insert(0, ROOT_DIR)
            from collectors.kis_api import KISCollector
            raw = KISCollector().get_daily_price(code, "D")
            raw = raw[:n]
            candles_kis = list(reversed(raw))
            if len(candles_kis) > len(candles):
                candles = candles_kis
                threading.Thread(target=_save_ohlcv_to_db, args=(code, candles), daemon=True).start()
        except Exception:
            pass

    # ── 3단계: 3M/6M/1Y는 pykrx (장기 데이터) ─────────────────────
    if len(candles) < n and period in ("3M", "6M", "1Y"):
        try:
            pykrx_days = {"3M": 100, "6M": 200, "1Y": 400}[period]
            candles_pyk = _fetch_ohlcv_pykrx(code, pykrx_days)
            if candles_pyk:
                candles = candles_pyk[-n:]
                threading.Thread(target=_save_ohlcv_to_db, args=(code, candles_pyk), daemon=True).start()
        except Exception:
            pass

    # ── 4단계: KIS API 폴백 (6M/1Y DB·pykrx 모두 실패 시) ──────
    if not candles:
        try:
            sys.path.insert(0, ROOT_DIR)
            from collectors.kis_api import KISCollector
            raw = KISCollector().get_daily_price(code, "D")
            candles = list(reversed(raw[:n]))
        except Exception:
            pass

    result = {"candles": candles, "period": period, "count": len(candles)}
    _cache_set(_OHLCV_CACHE, cache_key, {"data": result, "fetched_at": now})
    return result


# ─── 시황 한 줄 요약 (home snapshot 전용 — part08가 직접 호출) ───
# NOTE: 과거에는 @router.get("/api/market-brief")로 노출되어 있었으나
# server/routes/market_brief.py 의 같은 경로(브리핑 히스토리 응답)와 충돌해
# BriefView가 항상 빈 상태로 렌더되는 버그가 있었음. 라우트는 market_brief.py
# 한 곳으로 일원화하고, 이 함수는 home snapshot용 내부 헬퍼로만 남김.
def get_market_brief():
    import time
    now = time.time()
    if _MARKET_BRIEF_CACHE["data"] and now - _MARKET_BRIEF_CACHE["fetched_at"] < 120:
        return _MARKET_BRIEF_CACHE["data"]
    
    redis_key = f"stocks:market_brief:v{_mtime_token()}"
    redis_hit = redis_get_json(redis_key)
    if isinstance(redis_hit, dict):
        _MARKET_BRIEF_CACHE["data"] = redis_hit
        _MARKET_BRIEF_CACHE["fetched_at"] = now
        return redis_hit

    if not _stocks_db_available():
        return {"text": None}

    conn = get_stocks_conn()
    try:
        # A) 지수 기반 avg_pct 우선 계산
        indices = get_indices()
        kp_pct = indices.get("kospi", {}).get("change_pct")
        kd_pct = indices.get("kosdaq", {}).get("change_pct")
        
        avg_pct = 0.0
        source = "price_today_fallback"
        
        valid_indices = [p for p in [kp_pct, kd_pct] if p is not None]
        if valid_indices:
            avg_pct = sum(valid_indices) / len(valid_indices)
            source = "indices"
        else:
            avg_pct = conn.execute("SELECT AVG(COALESCE(change_pct,0)) FROM price_today").fetchone()[0] or 0.0

        # B) up/down/total 안정화 및 flat 계산
        total_raw = conn.execute("SELECT COUNT(*) FROM price_today").fetchone()[0]
        up_raw    = conn.execute("SELECT COUNT(*) FROM price_today WHERE COALESCE(change_pct,0) > 0").fetchone()[0]
        down_raw  = conn.execute("SELECT COUNT(*) FROM price_today WHERE COALESCE(change_pct,0) < 0").fetchone()[0]
        
        total = max(0, int(total_raw))
        up    = max(0, min(total, int(up_raw)))
        down  = max(0, min(total, int(down_raw)))
        
        # up + down > total 인 경우 보정 (down 우선 삭감)
        if up + down > total:
            excess = (up + down) - total
            # down에서 먼저 삭감
            reduction_down = min(down, excess)
            down -= reduction_down
            excess -= reduction_down
            # 그래도 남으면 up에서 삭감
            if excess > 0:
                up = max(0, up - excess)
        
        flat = max(0, total - up - down)

        # C) 테마 데이터 및 무드/텍스트
        theme_rows = conn.execute("""
            SELECT st.theme,
                   AVG(COALESCE(pt.change_pct, 0)) AS avg_change,
                   COUNT(*) AS stock_count
            FROM stock_themes st
            JOIN stocks s ON s.code = st.code
            LEFT JOIN price_today pt ON pt.code = st.code
            GROUP BY st.theme
            HAVING COUNT(*) >= 1
            ORDER BY avg_change DESC
            LIMIT 3
        """).fetchall()
        top_themes = [{"name": r["theme"], "avg_change": round(r["avg_change"] or 0, 2)} for r in theme_rows]

        if avg_pct > 0.5:
            mood = "전반적으로 강세"
        elif avg_pct > 0:
            mood = "소폭 상승"
        elif avg_pct > -0.5:
            mood = "소폭 하락"
        else:
            mood = "전반적으로 약세"

        sign = "+" if avg_pct >= 0 else ""
        parts = [f"오늘 시장은 {mood}입니다. {total}개 종목 중 {up}개 상승, {down}개 하락, {flat}개 보합이며 평균 {sign}{avg_pct:.2f}%입니다."]

        rising = [t for t in top_themes if t["avg_change"] > 0]
        if rising:
            theme_str = ", ".join(
                f"'{t['name']}({'+' if t['avg_change'] >= 0 else ''}{t['avg_change']}%)'"
                for t in rising
            )
            parts.append(f"가장 강세인 테마는 {theme_str}입니다.")

        result = {
            "text": " ".join(parts),
            "up": up,
            "down": down,
            "flat": flat,
            "total": total,
            "avg_pct": round(avg_pct, 2),
            "source": source
        }
        
        _MARKET_BRIEF_CACHE["data"] = result
        _MARKET_BRIEF_CACHE["fetched_at"] = now
        redis_set_json(redis_key, result, ttl_seconds=120)
        return result
    finally:
        conn.close()


# ─── GET /api/stocks/{code}/minutes ─────────────────────────
_MINUTES_CACHE: dict = {}   # key: f"{code}:{interval}" → {"data":..., "ts":float}

@router.get("/api/stocks/{code}/minutes")
def get_minute_chart(code: str, interval: int = 5):
    """당일 분봉 차트. interval=1|5|30|60. 장중 5분 캐시, 장외 30분 캐시."""
    valid_intervals = {1, 5, 30, 60}
    if interval not in valid_intervals:
        interval = 5

    mstatus = _market_status()
    # 장중 5분 / 장외 12시간 (한번 가져오면 다음날까지 유지)
    cache_ttl = 300 if mstatus == "open" else 43200 
    cache_key = f"{code}:{interval}"
    now = time.time()
    entry = _MINUTES_CACHE.get(cache_key)
    if entry and (now - entry["ts"]) < cache_ttl:
        return entry["data"]

    try:
        from collectors.kis_api import KISCollector
        collector = KISCollector()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"KIS collector unavailable: {e}")

    try:
        minute_candles = _fetch_full_day_minutes(collector, code)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"KIS API error: {e}")

    candles = _aggregate_to_Nmin(minute_candles, interval) if interval > 1 else minute_candles
    result = {
        "code": code,
        "interval": interval,
        "market_status": mstatus,
        "candles": candles,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
    }
    _MINUTES_CACHE[cache_key] = {"data": result, "ts": now}
    return result


# ─── GET /api/vi-status ──────────────────────────────────────
_VI_STATUS_CACHE: dict = {"data": None, "ts": 0.0}
_VI_STATUS_REFRESH_LOCK = threading.Lock()
_VI_STATUS_REFRESHING = False
_VI_STATUS_SOFT_TTL_OPEN_SEC = max(5.0, float(os.getenv("VI_STATUS_SOFT_TTL_OPEN_SEC", "15.0")))
_VI_STATUS_SOFT_TTL_CLOSED_SEC = max(30.0, float(os.getenv("VI_STATUS_SOFT_TTL_CLOSED_SEC", "60.0")))
_VI_STATUS_HARD_TTL_OPEN_SEC = max(_VI_STATUS_SOFT_TTL_OPEN_SEC, float(os.getenv("VI_STATUS_HARD_TTL_OPEN_SEC", "300.0")))
_VI_STATUS_HARD_TTL_CLOSED_SEC = max(_VI_STATUS_SOFT_TTL_CLOSED_SEC, float(os.getenv("VI_STATUS_HARD_TTL_CLOSED_SEC", "900.0")))


def _compute_vi_status_payload() -> dict:
    from collectors.kis_api import KISCollector
    collector = KISCollector()
    vi_result = collector.get_vi_status()
    items = vi_result.get("items", [])
    api_error = vi_result.get("error")
    return {
        "items": items,
        "count": len(items),
        "api_error": api_error,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
    }


def _queue_vi_status_refresh() -> None:
    global _VI_STATUS_REFRESHING
    with _VI_STATUS_REFRESH_LOCK:
        if _VI_STATUS_REFRESHING:
            return
        _VI_STATUS_REFRESHING = True

    def _worker():
        global _VI_STATUS_REFRESHING
        try:
            payload = _compute_vi_status_payload()
            if not payload.get("api_error"):
                _VI_STATUS_CACHE["data"] = payload
                _VI_STATUS_CACHE["ts"] = time.time()
        except Exception:
            pass
        finally:
            with _VI_STATUS_REFRESH_LOCK:
                _VI_STATUS_REFRESHING = False

    threading.Thread(target=_worker, daemon=True, name="vi_status_refresh").start()


@router.get("/api/vi-status")
def get_vi_status():
    now = time.time()
    m_status = _market_status()
    soft_ttl = _VI_STATUS_SOFT_TTL_OPEN_SEC if m_status == "open" else _VI_STATUS_SOFT_TTL_CLOSED_SEC
    hard_ttl = _VI_STATUS_HARD_TTL_OPEN_SEC if m_status == "open" else _VI_STATUS_HARD_TTL_CLOSED_SEC
    cached = _VI_STATUS_CACHE.get("data")
    cached_ts = float(_VI_STATUS_CACHE.get("ts") or 0.0)

    if cached is not None:
        age = now - cached_ts
        if age <= soft_ttl:
            return cached
        _queue_vi_status_refresh()
        if age <= hard_ttl:
            return cached

    try:
        result = _compute_vi_status_payload()
    except Exception as e:
        if cached is not None:
            return cached
        raise HTTPException(status_code=503, detail=str(e))

    if not result.get("api_error"):
        _VI_STATUS_CACHE["data"] = result
        _VI_STATUS_CACHE["ts"] = time.time()
    elif cached is not None:
        return cached
    return result


# ─── 공통 유틸 ───────────────────────────────────────────────
def _pad_codes(lst: list) -> list:
    """KIS 코드 6자리 zero-padding — 숫자가 아닌 코드는 제거"""
    result = []
    for item in lst:
        code = item.get("code")
        if code and str(code).isdigit():
            item["code"] = str(code).zfill(6)
            result.append(item)
    return result


def _upsert_ranking_to_db(items: list) -> None:
    """KIS 랭킹 데이터를 price_today에 upsert (백그라운드)"""
    if not items:
        return
    conn = get_stocks_conn()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for item in items:
            code = item.get("code")
            if not code:
                continue
            conn.execute(
                """
                INSERT INTO price_today (code, current_price, change_pct, change_amt,
                                        trading_value, trading_volume, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (code) DO UPDATE SET
                  current_price  = EXCLUDED.current_price,
                  change_pct     = EXCLUDED.change_pct,
                  change_amt     = EXCLUDED.change_amt,
                  trading_value  = EXCLUDED.trading_value,
                  trading_volume = EXCLUDED.trading_volume,
                  updated_at     = EXCLUDED.updated_at
                """,
                (code, item.get("close"), item.get("change_pct"), item.get("change_amt"),
                 item.get("trading_value"), item.get("volume"), now)
            )
        conn.commit()
        logger.info("ranking upsert: %d rows", len(items))
    except Exception as e:
        logger.warning("ranking upsert failed: %s", e)
    finally:
        conn.close()


def _ranking_volume_from_db() -> dict:
    """DB price_today 기반 거래대금 순위 fallback"""
    conn = get_stocks_conn()
    try:
        rows = conn.execute(
            """
            SELECT s.code, s.name, COALESCE(s.market,'') AS market,
                   pt.current_price, pt.change_pct, pt.change_amt,
                   pt.trading_value, pt.trading_volume
            FROM stocks s
            JOIN price_today pt ON pt.code = s.code
            WHERE COALESCE(pt.trading_value, 0) > 0
            ORDER BY pt.trading_value DESC NULLS LAST
            LIMIT 100
            """
        ).fetchall()
    finally:
        conn.close()
    items = _pad_codes([dict(r) for r in rows])
    return {
        "kospi": items,
        "kosdaq": [],
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "_source": "db_fallback",
    }


# ─── GET /api/stocks/ranking/volume ─────────────────────────
_RANK_VOLUME_CACHE: dict = {"data": None, "ts": 0.0}
_REDIS_RANK_VOL_KEY  = "ranking:volume:v1"
_RANK_VOLUME_LOCK = threading.Lock()
_RANK_VOLUME_INFLIGHT: threading.Event | None = None
_RANK_VOLUME_MEM_TTL_OPEN = max(3, int(os.getenv("RANK_VOLUME_MEM_TTL_OPEN", "12")))
_RANK_VOLUME_REDIS_TTL_OPEN = max(5, int(os.getenv("RANK_VOLUME_REDIS_TTL_OPEN", "18")))
_RANK_VOLUME_MEM_TTL_CLOSED = max(10, int(os.getenv("RANK_VOLUME_MEM_TTL_CLOSED", "60")))
_RANK_VOLUME_REDIS_TTL_CLOSED = max(30, int(os.getenv("RANK_VOLUME_REDIS_TTL_CLOSED", "300")))
_RANK_VOLUME_WAIT_ON_MISS_OPEN_SEC = max(0.0, float(os.getenv("RANK_VOLUME_WAIT_ON_MISS_OPEN_SEC", "0.25")))
_RANK_VOLUME_WAIT_ON_MISS_CLOSED_SEC = max(0.0, float(os.getenv("RANK_VOLUME_WAIT_ON_MISS_CLOSED_SEC", "0.5")))


def _refresh_ranking_volume_sync(redis_ttl: int) -> dict | None:
    now = time.time()
    try:
        from collectors.kis_api import KISCollector
        collector = KISCollector()
        kospi = _pad_codes(collector.get_transaction_value_ranking("0001"))
        kosdaq = _pad_codes(collector.get_transaction_value_ranking("1001"))
        result = {"kospi": kospi, "kosdaq": kosdaq, "updated_at": datetime.now().strftime("%H:%M:%S")}
        _RANK_VOLUME_CACHE["data"] = result
        _RANK_VOLUME_CACHE["ts"] = now
        redis_set_json(_REDIS_RANK_VOL_KEY, result, ttl_seconds=max(5, int(redis_ttl or 5)))
        threading.Thread(target=_upsert_ranking_to_db, args=(kospi + kosdaq,), daemon=True).start()
        return result
    except Exception as kis_err:
        logger.warning("KIS ranking/volume failed: %s ; falling back to DB", kis_err)

    try:
        result = _ranking_volume_from_db()
        _RANK_VOLUME_CACHE["data"] = result
        _RANK_VOLUME_CACHE["ts"] = now
        redis_set_json(_REDIS_RANK_VOL_KEY, result, ttl_seconds=max(5, int(redis_ttl or 5)))
        return result
    except Exception as db_err:
        logger.warning("ranking/volume background refresh failed: %s", db_err)
        return None


def _trigger_ranking_volume_refresh(redis_ttl: int) -> None:
    global _RANK_VOLUME_INFLIGHT
    with _RANK_VOLUME_LOCK:
        if _RANK_VOLUME_INFLIGHT is not None:
            return
        _RANK_VOLUME_INFLIGHT = threading.Event()
        done_event = _RANK_VOLUME_INFLIGHT

    def _worker():
        global _RANK_VOLUME_INFLIGHT
        try:
            _refresh_ranking_volume_sync(redis_ttl=redis_ttl)
        finally:
            with _RANK_VOLUME_LOCK:
                _RANK_VOLUME_INFLIGHT = None
            done_event.set()

    threading.Thread(target=_worker, daemon=True, name="rank-volume-refresh").start()


@router.get("/api/stocks/ranking/volume")
def get_ranking_volume():
    """Ranking volume: cache-first endpoint, no blocking live KIS calls on request path."""
    m_status = _market_status()
    mem_ttl = _RANK_VOLUME_MEM_TTL_OPEN if m_status == "open" else _RANK_VOLUME_MEM_TTL_CLOSED
    redis_ttl = _RANK_VOLUME_REDIS_TTL_OPEN if m_status == "open" else _RANK_VOLUME_REDIS_TTL_CLOSED
    wait_on_miss = _RANK_VOLUME_WAIT_ON_MISS_OPEN_SEC if m_status == "open" else _RANK_VOLUME_WAIT_ON_MISS_CLOSED_SEC
    now = time.time()

    if _RANK_VOLUME_CACHE["data"] is not None and (now - _RANK_VOLUME_CACHE["ts"]) < mem_ttl:
        return _RANK_VOLUME_CACHE["data"]

    redis_hit = redis_get_json(_REDIS_RANK_VOL_KEY)
    if isinstance(redis_hit, dict) and redis_hit.get("kospi") is not None:
        _RANK_VOLUME_CACHE["data"] = redis_hit
        _RANK_VOLUME_CACHE["ts"] = now
        return redis_hit

    _trigger_ranking_volume_refresh(redis_ttl=redis_ttl)

    with _RANK_VOLUME_LOCK:
        inflight_event = _RANK_VOLUME_INFLIGHT

    if inflight_event is not None and wait_on_miss > 0:
        inflight_event.wait(timeout=wait_on_miss)
        now2 = time.time()
        if _RANK_VOLUME_CACHE["data"] is not None and (now2 - _RANK_VOLUME_CACHE["ts"]) < max(mem_ttl, 3):
            return _RANK_VOLUME_CACHE["data"]
        redis_after = redis_get_json(_REDIS_RANK_VOL_KEY)
        if isinstance(redis_after, dict) and redis_after.get("kospi") is not None:
            _RANK_VOLUME_CACHE["data"] = redis_after
            _RANK_VOLUME_CACHE["ts"] = now2
            return redis_after

    stale = _RANK_VOLUME_CACHE.get("data")
    if isinstance(stale, dict):
        out = copy.deepcopy(stale)
        out["_stale"] = True
        return out
    try:
        fallback = _ranking_volume_from_db()
        fallback["_stale"] = True
        return fallback
    except Exception:
        return {"kospi": [], "kosdaq": [], "updated_at": datetime.now().strftime("%H:%M:%S"), "_stale": True}


# ─── GET /api/stocks/ranking/fluctuation ────────────────────
_RANK_STRENGTH_CACHE: dict = {}
_RANK_STRENGTH_LOCK = threading.Lock()
_RANK_STRENGTH_INFLIGHT: dict = {}
_RANK_STRENGTH_DEFAULT_TOP = max(1, int(os.getenv("RANK_STRENGTH_DEFAULT_TOP", "10")))
_RANK_STRENGTH_DEFAULT_SCAN = max(
    _RANK_STRENGTH_DEFAULT_TOP,
    int(os.getenv("RANK_STRENGTH_DEFAULT_SCAN", "25")),
)
_RANK_STRENGTH_SCAN_CAP_OPEN = max(
    _RANK_STRENGTH_DEFAULT_TOP,
    int(os.getenv("RANK_STRENGTH_SCAN_CAP_OPEN", "16")),
)
_RANK_STRENGTH_SCAN_CAP_CLOSED = max(
    _RANK_STRENGTH_DEFAULT_TOP,
    int(os.getenv("RANK_STRENGTH_SCAN_CAP_CLOSED", "80")),
)
_RANK_STRENGTH_WORKERS = max(1, int(os.getenv("RANK_STRENGTH_WORKERS", "4")))
_RANK_STRENGTH_WAIT_OPEN_SEC = max(1.0, float(os.getenv("RANK_STRENGTH_WAIT_OPEN_SEC", "12.0")))
_RANK_STRENGTH_WAIT_CLOSED_SEC = max(1.0, float(os.getenv("RANK_STRENGTH_WAIT_CLOSED_SEC", "15.0")))






