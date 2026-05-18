def _strength_mem_ttl(market_status: str) -> int:
    return 7 if market_status == "open" else 30


def _strength_redis_ttl(market_status: str) -> int:
    return 20 if market_status == "open" else 120


def _strength_wait_timeout(market_status: str) -> float:
    return _RANK_STRENGTH_WAIT_OPEN_SEC if market_status == "open" else _RANK_STRENGTH_WAIT_CLOSED_SEC


def _strength_scan_cap(market_status: str) -> int:
    return _RANK_STRENGTH_SCAN_CAP_OPEN if market_status == "open" else _RANK_STRENGTH_SCAN_CAP_CLOSED


def _normalize_strength_params(top: int, scan: int, market_status: str) -> tuple[int, int]:
    try:
        top_n = int(top)
    except Exception:
        top_n = 10
    top_n = max(1, min(top_n, 50))
    try:
        scan_n = int(scan)
    except Exception:
        scan_n = 25
    scan_n = max(top_n, scan_n)
    scan_n = min(scan_n, _strength_scan_cap(market_status))
    return top_n, scan_n


def _strength_cache_key(top: int, scan: int, market_status: str):
    return (top, scan, market_status)


def _strength_redis_key(top: int, scan: int, market_status: str) -> str:
    return f"ranking:strength:top{top}:scan{scan}:{market_status}:v2"


def _strength_read_cache(cache_key):
    with _RANK_STRENGTH_LOCK:
        return _RANK_STRENGTH_CACHE.get(cache_key)


def _strength_store_cache(cache_key, payload: dict):
    with _RANK_STRENGTH_LOCK:
        _cache_set(_RANK_STRENGTH_CACHE, cache_key, {"data": payload, "ts": time.time()})


def _strength_mark_stale(payload: dict, error: Exception | str | None = None) -> dict:
    out = copy.deepcopy(payload or {})
    out["_stale"] = True
    if error is not None:
        out["error"] = str(error)
    return out


def _normalize_rank_item(item: dict, market_fallback: str = "") -> dict | None:
    code = str(item.get("code") or "").strip()
    if not code:
        return None
    if code.isdigit():
        code = code.zfill(6)
    return {
        "code": code,
        "name": str(item.get("name") or code).strip(),
        "market": str(item.get("market") or market_fallback or "").strip(),
        "trading_value": _safe_int(item.get("trading_value")),
        "current_price": _safe_int(item.get("current_price") or item.get("close")),
    }


def _load_strength_candidates(scan: int) -> list[dict]:
    candidates: list[dict] = []
    seen = set()

    try:
        vol = get_ranking_volume()
    except Exception:
        vol = {}

    for market_key, market_name in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")):
        for raw in (vol.get(market_key) or []):
            item = _normalize_rank_item(raw, market_fallback=market_name)
            if not item:
                continue
            if item["code"] in seen:
                continue
            seen.add(item["code"])
            candidates.append(item)

    if not candidates and _stocks_db_available():
        conn = get_stocks_conn()
        try:
            rows = conn.execute(
                """
                SELECT s.code, s.name, COALESCE(s.market,'') AS market,
                       pt.current_price, pt.trading_value
                FROM stocks s
                JOIN price_today pt ON pt.code = s.code
                WHERE COALESCE(pt.trading_value, 0) > 0
                ORDER BY pt.trading_value DESC NULLS LAST
                LIMIT ?
                """,
                (max(20, min(scan, 120)),),
            ).fetchall()
            for r in rows:
                item = _normalize_rank_item(dict(r))
                if not item:
                    continue
                if item["code"] in seen:
                    continue
                seen.add(item["code"])
                candidates.append(item)
        finally:
            conn.close()

    candidates.sort(key=lambda x: x.get("trading_value", 0), reverse=True)
    return candidates[: max(1, scan)]


def _fetch_strength_snapshot(collector, code: str, hhmmss: str) -> dict | None:
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
        return None

    out2 = res.get("output2") or []
    if isinstance(out2, dict):
        out2 = [out2]
    if not isinstance(out2, list) or not out2:
        return None

    row = out2[0] or {}
    strength = to_float(row.get("tday_rltv"), 0.0)
    if strength <= 0:
        return None

    return {
        "strength": round(float(strength), 2),
        "trade_time": str(row.get("stck_cntg_hour") or "").strip(),
        "current_price": _safe_int(row.get("stck_prpr")),
        "change_pct": to_float(row.get("prdy_ctrt"), 0.0),
        "trade_qty": _safe_int(row.get("cnqn")),
        "acc_volume": _safe_int(row.get("acml_vol")),
    }


@router.get("/api/stocks/ranking/strength")
def get_ranking_strength(top: int = 10, scan: int = 25):
    market_status = _market_status()
    top, scan = _normalize_strength_params(top, scan, market_status)
    mem_ttl = _strength_mem_ttl(market_status)
    redis_ttl = _strength_redis_ttl(market_status)
    cache_key = _strength_cache_key(top, scan, market_status)
    redis_key = _strength_redis_key(top, scan, market_status)

    now = time.time()
    cached_entry = _strength_read_cache(cache_key)
    if cached_entry and (now - float(cached_entry.get("ts") or 0.0) < mem_ttl):
        return cached_entry["data"]

    redis_hit = redis_get_json(redis_key)
    if isinstance(redis_hit, dict) and isinstance(redis_hit.get("items"), list):
        _strength_store_cache(cache_key, redis_hit)
        return redis_hit

    stale_payload = cached_entry.get("data") if isinstance(cached_entry, dict) else None

    with _RANK_STRENGTH_LOCK:
        inflight_event = _RANK_STRENGTH_INFLIGHT.get(cache_key)
        if inflight_event is None:
            inflight_event = threading.Event()
            _RANK_STRENGTH_INFLIGHT[cache_key] = inflight_event
            is_leader = True
        else:
            is_leader = False

    if not is_leader:
        if inflight_event.wait(timeout=_strength_wait_timeout(market_status)):
            after = _strength_read_cache(cache_key)
            if after and isinstance(after.get("data"), dict):
                return after["data"]
            redis_after = redis_get_json(redis_key)
            if isinstance(redis_after, dict) and isinstance(redis_after.get("items"), list):
                _strength_store_cache(cache_key, redis_after)
                return redis_after
        if isinstance(stale_payload, dict):
            return _strength_mark_stale(stale_payload, "refresh_timeout")
        raise HTTPException(status_code=503, detail="strength ranking refresh in progress")

    try:
        candidates = _load_strength_candidates(scan)
        if not candidates:
            result = {
                "items": [],
                "count": 0,
                "top": top,
                "scan": scan,
                "market_status": market_status,
                "updated_at": datetime.now().strftime("%H:%M:%S"),
                "source": "none",
                "note": "no_candidates",
            }
            _strength_store_cache(cache_key, result)
            redis_set_json(redis_key, result, ttl_seconds=30)
            return result

        hhmmss = datetime.now().strftime("%H%M%S")
        thread_local = threading.local()

        def _fetch(cand: dict) -> dict | None:
            try:
                collector = getattr(thread_local, "collector", None)
                if collector is None:
                    from collectors.kis_api import KISCollector
                    collector = KISCollector()
                    thread_local.collector = collector
                snap = _fetch_strength_snapshot(collector, cand["code"], hhmmss)
                if not snap:
                    return None
                return {
                    "code": cand["code"],
                    "name": cand.get("name"),
                    "market": cand.get("market"),
                    "strength": snap["strength"],
                    "current_price": snap["current_price"] or cand.get("current_price"),
                    "change_pct": snap["change_pct"],
                    "trade_time": snap["trade_time"],
                    "trade_qty": snap["trade_qty"],
                    "acc_volume": snap["acc_volume"],
                    "trading_value_ref": cand.get("trading_value", 0),
                }
            except Exception:
                return None

        items = []
        worker_count = min(_RANK_STRENGTH_WORKERS, max(1, len(candidates)))
        if worker_count <= 1:
            for cand in candidates:
                row = _fetch(cand)
                if isinstance(row, dict):
                    items.append(row)
        else:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as ex:
                for row in ex.map(_fetch, candidates):
                    if isinstance(row, dict):
                        items.append(row)

        items.sort(
            key=lambda x: (
                float(x.get("strength") or 0.0),
                int(x.get("trading_value_ref") or 0),
            ),
            reverse=True,
        )
        items = items[:top]
        result = {
            "items": items,
            "count": len(items),
            "top": top,
            "scan": scan,
            "candidate_count": len(candidates),
            "market_status": market_status,
            "updated_at": datetime.now().strftime("%H:%M:%S"),
            "source": "kis",
        }
        _strength_store_cache(cache_key, result)
        redis_set_json(redis_key, result, ttl_seconds=redis_ttl)
        return result
    except Exception as e:
        stale = redis_get_json(redis_key)
        if isinstance(stale, dict) and isinstance(stale.get("items"), list):
            _strength_store_cache(cache_key, stale)
            return _strength_mark_stale(stale, e)
        if isinstance(stale_payload, dict):
            return _strength_mark_stale(stale_payload, e)
        raise HTTPException(status_code=503, detail=f"strength ranking unavailable: {e}")
    finally:
        with _RANK_STRENGTH_LOCK:
            done_event = _RANK_STRENGTH_INFLIGHT.pop(cache_key, None)
        if done_event is not None:
            done_event.set()


def _refresh_strength_default(force: bool = False) -> dict:
    market_status = _market_status()
    top, scan = _normalize_strength_params(
        _RANK_STRENGTH_DEFAULT_TOP,
        _RANK_STRENGTH_DEFAULT_SCAN,
        market_status,
    )
    cache_key = _strength_cache_key(top, scan, market_status)
    if force:
        with _RANK_STRENGTH_LOCK:
            _RANK_STRENGTH_CACHE.pop(cache_key, None)
    return get_ranking_strength(top=top, scan=scan)


_RANK_FLUCT_CACHE: dict = {"data": None, "ts": 0.0}
_REDIS_RANK_FLUCT_KEY = "ranking:fluctuation:v1"

@router.get("/api/stocks/ranking/fluctuation")
def get_ranking_fluctuation():
    """등락률 순위 — 장중 1분, 그 외 5분/30분 캐시"""
    m_status = _market_status()
    mem_ttl   = 60 if m_status == "open" else 300
    redis_ttl = 60 if m_status == "open" else 1800
    now = time.time()

    if _RANK_FLUCT_CACHE["data"] is not None and (now - _RANK_FLUCT_CACHE["ts"]) < mem_ttl:
        return _RANK_FLUCT_CACHE["data"]

    redis_hit = redis_get_json(_REDIS_RANK_FLUCT_KEY)
    if isinstance(redis_hit, dict) and redis_hit.get("up") is not None:
        _RANK_FLUCT_CACHE["data"] = redis_hit
        _RANK_FLUCT_CACHE["ts"]   = now
        return redis_hit

    try:
        from collectors.kis_api import KISCollector
        collector = KISCollector()
        up   = _pad_codes(collector.get_fluctuation_rank("0001", is_up=True))
        down = _pad_codes(collector.get_fluctuation_rank("0001", is_up=False))
        result = {"up": up, "down": down, "updated_at": datetime.now().strftime("%H:%M:%S")}
        _RANK_FLUCT_CACHE["data"] = result
        _RANK_FLUCT_CACHE["ts"]   = now
        redis_set_json(_REDIS_RANK_FLUCT_KEY, result, ttl_seconds=redis_ttl)
        return result
    except Exception as e:
        stale = redis_get_json(_REDIS_RANK_FLUCT_KEY)
        if isinstance(stale, dict) and stale.get("up"):
            stale["_stale"] = True
            return stale
        raise HTTPException(status_code=503, detail=str(e))


# ─── 타임라인 이벤트 큐 ──────────────────────────────────────
# 상한가/52주신고가/급등 등 주요 이벤트를 인메모리 큐에 쌓고 /api/timeline으로 서빙
_TIMELINE_EVENTS: deque = deque(maxlen=50)
_TIMELINE_SEEN: dict = {}     # code → last_seen_ts (중복 방지 10분)
_TIMELINE_LOCK = threading.Lock()
_TIMELINE_LAST_RUN: float = 0.0
_TIMELINE_POLLER_STARTED = False
_TIMELINE_SOURCE: str = ""    # "kis" | "db" — 마지막 데이터 출처


def _get_52w_highs_from_db(codes: list) -> dict:
    """price_daily에서 코드별 52주(365일) 최고가 조회. {code: max_high}"""
    if not codes or not _stocks_db_available():
        return {}
    try:
        cutoff_ymd = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
        ph = ",".join("?" * len(codes))
        conn = get_stocks_conn()
        try:
            rows = conn.execute(
                f"SELECT code, MAX(high) as w52_high FROM price_daily "
                f"WHERE code IN ({ph}) "
                f"  AND REPLACE(CAST(date AS TEXT), '-', '') >= ? "
                f"  AND high > 0 "
                f"GROUP BY code",
                [*codes, cutoff_ymd],
            ).fetchall()
        finally:
            conn.close()
        return {r["code"]: r["w52_high"] for r in rows}
    except Exception as e:
        logger.debug("[timeline] 52w high DB error: %s", e)
        return {}


def _classify_event(pct: float, price: int = 0, w52_high: int = 0):
    """등락률 + 52주신고가 기준으로 이벤트 분류.
    Returns (emoji, label, extra_badge)
    """
    is_new_high  = (price > 0 and w52_high > 0 and price >= w52_high)
    is_near_high = (price > 0 and w52_high > 0 and price >= w52_high * 0.98)

    if pct >= 29.5:
        badge = "52주신고가" if is_new_high else None
        return "🔒", "상한가", badge
    if is_new_high and pct >= 1.0:
        return "🏔", "52주 신고가", None
    if is_near_high and pct >= 1.0:
        return "📈", "신고가 근접", None
    if pct >= 15.0:  return "🚀", "급등",  None
    if pct >= 10.0:  return "⬆️", "강세",  None
    if pct <= -29.5: return "💀", "하한가", None
    if pct <= -10.0: return "⬇️", "급락",  None
    if pct <= -5.0:  return "📉", "약세",   None
    return None, None, None


def _push_events(rows_iter, time_str: str, now_ts: float, w52_map: dict):
    """공통 이벤트 추가 루틴 (KIS / DB 공용)"""
    added = 0
    for item in rows_iter:
        pct   = float(item.get("change_pct") or item.get("day_chg") or 0)
        code  = str(item.get("code") or "").strip().zfill(6)
        name  = item.get("name") or code
        price = int(item.get("close") or item.get("price") or item.get("current_price") or 0)
        if not code:
            continue
        w52h  = w52_map.get(code, 0)
        emoji, label, badge = _classify_event(pct, price, w52h)
        if not label:
            continue
        last_seen = _TIMELINE_SEEN.get(code, 0)
        if now_ts - last_seen < 600:
            continue
        _TIMELINE_SEEN[code] = now_ts
        ev = {
            "time":       time_str,
            "code":       code,
            "name":       name,
            "emoji":      emoji,
            "event_type": label,
            "change_pct": round(pct, 2),
            "price":      price,
        }
        if badge:
            ev["badge"] = badge
        if w52h and pct >= 1.0:
            ev["w52_high"] = w52h
        _TIMELINE_EVENTS.appendleft(ev)
        added += 1
    return added


def _bootstrap_from_db():
    """장외 / KIS 빈 응답일 때 price_daily 최신일로 타임라인 초기화.
    이미 이벤트가 있으면 스킵.
    """
    global _TIMELINE_SOURCE
    if not _stocks_db_available():
        return
    try:
        conn = get_stocks_conn()
        try:
            latest_date = conn.execute(
                "SELECT MAX(REPLACE(CAST(date AS TEXT), '-', '')) FROM price_daily WHERE close > 0 AND open > 0"
            ).fetchone()[0]
            if not latest_date:
                return

            # 최신일 급등/급락 종목
            rows = conn.execute("""
                SELECT p.code, s.name, p.close, p.open, p.high,
                       ROUND((p.close * 1.0 - p.open) / p.open * 100, 2) as day_chg
                FROM price_daily p
                JOIN stocks s ON p.code = s.code
                WHERE REPLACE(CAST(p.date AS TEXT), '-', '') = ?
                  AND p.close > 0 AND p.open > 0
                  AND (
                      (p.close * 1.0 - p.open) / p.open * 100 >= 10
                   OR (p.close * 1.0 - p.open) / p.open * 100 <= -10
                  )
                ORDER BY ABS((p.close * 1.0 - p.open) / p.open) DESC
                LIMIT 40
            """, [latest_date]).fetchall()
        finally:
            conn.close()

        if not rows:
            return

        # time 레이블 — "04/06" 형태
        d = str(latest_date).replace("-", "")  # "20260406"
        if len(d) < 8:
            return
        time_str = f"{d[4:6]}/{d[6:8]}"

        now_ts = time.time()
        row_dicts = [dict(r) for r in rows]
        row_codes = [str(r.get("code") or "").strip().zfill(6) for r in row_dicts]
        w52_map = _get_52w_highs_from_db([c for c in row_codes if c])

        with _TIMELINE_LOCK:
            added = _push_events(row_dicts, time_str, now_ts, w52_map)

        _TIMELINE_SOURCE = "db"
        logger.info("[timeline] DB 폴백: %s 기준 %d건 추가 (총 %d건)", latest_date, added, len(_TIMELINE_EVENTS))
    except Exception as e:
        logger.warning("[timeline] DB 폴백 오류: %s", e)


def _refresh_timeline():
    """거래대금 랭킹 기반 장중 이벤트 감지 (60초 throttle).
    fluctuation-rank 대신 transaction_value_ranking 사용 (KOSPI+KOSDAQ).
    KIS 실패 또는 이벤트 없을 때 → DB 폴백.
    """
    global _TIMELINE_LAST_RUN, _TIMELINE_SOURCE
    now_ts = time.time()
    if now_ts - _TIMELINE_LAST_RUN < 58:
        return
    _TIMELINE_LAST_RUN = now_ts

    kis_added = 0
    try:
        from collectors.kis_api import KISCollector
        col = KISCollector()
        # 거래대금 상위 종목 (KOSPI + KOSDAQ) — change_pct 포함
        kospi  = col.get_transaction_value_ranking("0001") or []
        kosdaq = col.get_transaction_value_ranking("1001") or []
        items  = kospi + kosdaq

        # change_pct 기준 주목 종목 필터 (±5% 이상만)
        notable = [it for it in items if abs(float(it.get("change_pct") or 0)) >= 5.0]

        if notable:
            up_codes = [
                str(it.get("code") or "").strip().zfill(6)
                for it in notable if float(it.get("change_pct") or 0) >= 1.0
            ]
            w52_map = _get_52w_highs_from_db(up_codes) if up_codes else {}
            now_str = datetime.now().strftime("%H:%M")
            with _TIMELINE_LOCK:
                kis_added = _push_events(notable, now_str, now_ts, w52_map)
            if kis_added:
                _TIMELINE_SOURCE = "kis"
                logger.info("[timeline] KIS 거래대금 랭킹: %d건 추가 (총 %d건)", kis_added, len(_TIMELINE_EVENTS))
    except Exception as e:
        logger.info("[timeline] KIS 오류 (DB 폴백 시도): %s", e)

    # 이벤트 큐가 비어있으면 DB로 초기화
    if len(_TIMELINE_EVENTS) == 0:
        _bootstrap_from_db()


def start_timeline_background_poller():
    """Start a single timeline poller thread per process."""
    global _TIMELINE_POLLER_STARTED
    with _TIMELINE_LOCK:
        if _TIMELINE_POLLER_STARTED:
            return
        _TIMELINE_POLLER_STARTED = True

    def _loop():
        time.sleep(5)
        while True:
            try:
                _refresh_timeline()
            except Exception as e:
                logger.debug("[timeline-poller] error: %s", e)
            time.sleep(60)

    t = threading.Thread(target=_loop, daemon=True, name="timeline-poller")
    t.start()
    logger.info("[timeline] background poller started (60s)")

@router.get("/api/timeline")
def get_timeline():
    """주요 이벤트 타임라인 (상한가/52주신고가/급등/급락) — 최근 20건"""
    _refresh_timeline()
    with _TIMELINE_LOCK:
        events = list(_TIMELINE_EVENTS)[:20]
    return {
        "events":     events,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "total":      len(events),
        "source":     _TIMELINE_SOURCE,   # "kis" | "db" | ""
    }


# ─── 거래대금 급증 순위 ───────────────────────────────────────
_SURGE_SNAPSHOTS: deque = deque(maxlen=6)   # 최근 6회 스냅샷 (약 30분치)
_SURGE_LOCK = threading.Lock()
_SURGE_LAST_RUN: float = 0.0




