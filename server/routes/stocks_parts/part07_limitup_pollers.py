def _refresh_program_default(force: bool = False) -> dict:
    if force:
        with _PROGRAM_LOCK:
            _PROGRAM_CACHE["payload"] = None
            _PROGRAM_CACHE["ts"] = 0.0
    return get_ranking_program(limit=10)


_LIMIT_UP_BREAK_EVENTS: deque = deque(maxlen=300)
_LIMIT_UP_STATE: dict = {}
_LIMIT_UP_LOCK = threading.Lock()
_LIMIT_UP_LAST_RUN: float = 0.0
_LIMIT_UP_POLLER_STARTED = False
_LIMIT_UP_LAST_EVENT_TS: dict = {}
_LIMIT_UP_LAST_FETCH_ERR: str | None = None
_LIMIT_UP_POLL_INTERVAL_SEC = max(15, int(os.getenv("LIMIT_UP_POLL_INTERVAL_SEC", "30")))
_LIMIT_UP_THRESHOLD_PCT = float(os.getenv("LIMIT_UP_THRESHOLD_PCT", "29.5"))


_LIMIT_UP_SCHEMA_READY = False


def _ensure_limit_up_table(conn):
    global _LIMIT_UP_SCHEMA_READY
    if _LIMIT_UP_SCHEMA_READY:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS limit_up_break_events (
            id SERIAL PRIMARY KEY,
            event_date DATE NOT NULL DEFAULT CURRENT_DATE,
            time VARCHAR(8),
            code VARCHAR(10),
            name VARCHAR(50),
            prev_change_pct REAL,
            change_pct REAL,
            current_price INTEGER,
            source VARCHAR(20),
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    _LIMIT_UP_SCHEMA_READY = True


def _save_limit_up_events_to_db(events: list):
    if not events:
        return
    try:
        conn = get_stocks_conn()
        _ensure_limit_up_table(conn)
        today = datetime.now().strftime("%Y-%m-%d")
        for ev in events:
            conn.execute(
                "INSERT INTO limit_up_break_events (event_date,time,code,name,prev_change_pct,change_pct,current_price,source) VALUES (?,?,?,?,?,?,?,?)",
                (today, ev["time"], ev["code"], ev["name"], ev["prev_change_pct"], ev["change_pct"], ev["current_price"], ev["source"]),
            )
        conn.commit()
    except Exception as e:
        logger.debug("[limit-up] db save error: %s", e)


def _remove_recaptured_from_db(codes: set):
    """상한가 재진입 종목을 오늘 풀림 이벤트 DB에서 제거."""
    if not codes:
        return
    try:
        conn = get_stocks_conn()
        today = datetime.now().strftime("%Y-%m-%d")
        for code in codes:
            conn.execute(
                "DELETE FROM limit_up_break_events WHERE event_date=? AND code=?",
                (today, code),
            )
        conn.commit()
    except Exception as e:
        logger.debug("[limit-up] db remove error: %s", e)


def _load_today_limit_up_events_from_db():
    try:
        conn = get_stocks_conn()
        _ensure_limit_up_table(conn)
        today = datetime.now().strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT time,code,name,prev_change_pct,change_pct,current_price,source FROM limit_up_break_events WHERE event_date=? ORDER BY id DESC",
            (today,),
        ).fetchall()
        events = []
        for r in rows:
            events.append({
                "time": r[0], "code": r[1], "name": r[2],
                "event_type": "LIMIT_UP_RELEASED",
                "prev_change_pct": r[3], "change_pct": r[4],
                "current_price": r[5], "source": r[6],
            })
        with _LIMIT_UP_LOCK:
            _LIMIT_UP_BREAK_EVENTS.clear()
            for ev in events:
                _LIMIT_UP_BREAK_EVENTS.append(ev)
        logger.info("[limit-up] loaded %d today events from DB", len(events))
    except Exception as e:
        logger.debug("[limit-up] db load error: %s", e)


def _is_limit_up(change_pct: float) -> bool:
    try:
        return float(change_pct) >= _LIMIT_UP_THRESHOLD_PCT
    except Exception:
        return False


def _scan_limit_up_break(force: bool = False) -> dict:
    global _LIMIT_UP_LAST_RUN, _LIMIT_UP_LAST_FETCH_ERR
    now_ts = time.time()
    if (not force) and (now_ts - _LIMIT_UP_LAST_RUN < _LIMIT_UP_POLL_INTERVAL_SEC - 1):
        return {"scanned": False, "reason": "throttled"}
    _LIMIT_UP_LAST_RUN = now_ts

    try:
        from collectors.kis_api import KISCollector
        col = KISCollector()
        kospi = col.get_transaction_value_ranking("0001") or []
        kosdaq = col.get_transaction_value_ranking("1001") or []
        rows = kospi + kosdaq
    except Exception as e:
        _LIMIT_UP_LAST_FETCH_ERR = str(e)
        return {"scanned": False, "reason": "fetch_failed", "error": str(e)}

    curr_map = {}
    for item in rows:
        code = str(item.get("code", "")).zfill(6)
        if not code:
            continue
        curr_map[code] = {
            "code": code,
            "name": item.get("name", "") or code,
            "change_pct": float(item.get("change_pct") or 0.0),
            "current_price": int(item.get("close") or item.get("price") or 0),
            "is_limit_up": _is_limit_up(float(item.get("change_pct") or 0.0)),
            "detected_at": datetime.now().strftime("%H:%M:%S"),
            "source": "ranking",
        }

    with _LIMIT_UP_LOCK:
        prev_state = dict(_LIMIT_UP_STATE)

    # If a previously locked stock disappears from ranking, probe it directly once.
    missing_watch = [c for c, s in prev_state.items() if s.get("is_limit_up") and c not in curr_map][:25]
    for code in missing_watch:
        try:
            p = col.get_price(code)
            cp = float(p.get("change_pct") or 0.0)
            curr_map[code] = {
                "code": code,
                "name": prev_state.get(code, {}).get("name") or code,
                "change_pct": cp,
                "current_price": int(p.get("current_price") or 0),
                "is_limit_up": _is_limit_up(cp),
                "detected_at": datetime.now().strftime("%H:%M:%S"),
                "source": "price_probe",
            }
        except Exception:
            pass

    now_label = datetime.now().strftime("%H:%M:%S")
    release_events = []
    next_state = {}
    for code, cur in curr_map.items():
        prev = prev_state.get(code) or {}
        prev_is_limit = bool(prev.get("is_limit_up"))
        cur_is_limit = bool(cur.get("is_limit_up"))
        if prev_is_limit and (not cur_is_limit):
            last_ev_ts = float(_LIMIT_UP_LAST_EVENT_TS.get(code) or 0.0)
            if now_ts - last_ev_ts >= 60:
                release_events.append({
                    "time": now_label,
                    "code": code,
                    "name": cur.get("name") or prev.get("name") or code,
                    "event_type": "LIMIT_UP_RELEASED",
                    "prev_change_pct": round(float(prev.get("change_pct") or 0.0), 2),
                    "change_pct": round(float(cur.get("change_pct") or 0.0), 2),
                    "current_price": int(cur.get("current_price") or 0),
                    "source": cur.get("source", "ranking"),
                })
                _LIMIT_UP_LAST_EVENT_TS[code] = now_ts

        next_state[code] = {
            "code": code,
            "name": cur.get("name") or code,
            "change_pct": float(cur.get("change_pct") or 0.0),
            "current_price": int(cur.get("current_price") or 0),
            "is_limit_up": cur_is_limit,
            "detected_at": cur.get("detected_at") or now_label,
            "source": cur.get("source", "ranking"),
        }

    # Keep previously tracked limit-up stocks briefly if probe failed.
    for code, prev in prev_state.items():
        if code in next_state:
            continue
        if prev.get("is_limit_up"):
            next_state[code] = prev

    # 풀림 이벤트가 있던 종목 중 다시 상한가로 복귀한 것만 제거
    with _LIMIT_UP_LOCK:
        broken_codes = {e["code"] for e in _LIMIT_UP_BREAK_EVENTS}
    recaptured = {
        code for code, cur in curr_map.items()
        if cur.get("is_limit_up") and code in broken_codes
    }

    _save_limit_up_events_to_db(release_events)
    with _LIMIT_UP_LOCK:
        _LIMIT_UP_STATE.clear()
        _LIMIT_UP_STATE.update(next_state)
        for ev in release_events:
            _LIMIT_UP_BREAK_EVENTS.appendleft(ev)
        # 상한가 복귀 종목 풀림 목록에서 제거
        if recaptured:
            filtered = deque(
                (e for e in _LIMIT_UP_BREAK_EVENTS if e["code"] not in recaptured),
                maxlen=300,
            )
            _LIMIT_UP_BREAK_EVENTS.clear()
            _LIMIT_UP_BREAK_EVENTS.extend(filtered)
    # DB에서도 복귀 종목 제거
    if recaptured:
        _remove_recaptured_from_db(recaptured)
    _LIMIT_UP_LAST_FETCH_ERR = None
    return {
        "scanned": True,
        "events_added": len(release_events),
        "tracked": len(next_state),
        "polled_at": now_label,
    }


def start_limit_up_break_background_poller():
    global _LIMIT_UP_POLLER_STARTED
    with _LIMIT_UP_LOCK:
        if _LIMIT_UP_POLLER_STARTED:
            return
        _LIMIT_UP_POLLER_STARTED = True

    _load_today_limit_up_events_from_db()

    def _loop():
        time.sleep(7)
        while True:
            try:
                _scan_limit_up_break(force=False)
            except Exception as e:
                logger.debug("[limit-up-break-poller] error: %s", e)
            time.sleep(_LIMIT_UP_POLL_INTERVAL_SEC)

    t = threading.Thread(target=_loop, daemon=True, name="limit-up-break-poller")
    t.start()
    logger.info("[limit-up-break] background poller started (%ss)", _LIMIT_UP_POLL_INTERVAL_SEC)


@router.get("/api/alerts/limit-up-break")
def get_limit_up_break_alerts(limit: int = 20, force_refresh: bool = False):
    try:
        limit = int(limit)
    except Exception:
        limit = 20
    limit = max(1, min(limit, 100))

    _scan_limit_up_break(force=bool(force_refresh))
    with _LIMIT_UP_LOCK:
        raw_events = list(_LIMIT_UP_BREAK_EVENTS)
        active = [s for s in _LIMIT_UP_STATE.values() if s.get("is_limit_up")]
        active.sort(key=lambda x: x.get("change_pct", 0.0), reverse=True)

    # 종목별 최신 1건 dedup → 최대 3개
    seen: set = set()
    deduped = []
    for e in raw_events:
        if e["code"] not in seen:
            deduped.append(e)
            seen.add(e["code"])
        if len(deduped) >= 3:
            break

    return {
        "events": deduped,
        "active_limit_up": active[:50],
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "poll_interval_sec": _LIMIT_UP_POLL_INTERVAL_SEC,
        "threshold_pct": _LIMIT_UP_THRESHOLD_PCT,
        "last_error": _LIMIT_UP_LAST_FETCH_ERR,
    }


_HOT_RANKINGS_POLLER_STARTED = False
_HOT_RANKINGS_POLLER_LOCK = threading.Lock()
_HOT_RANKINGS_INTERVAL_OPEN_SEC = max(10, int(os.getenv("HOT_RANKINGS_INTERVAL_OPEN_SEC", "20")))
_HOT_RANKINGS_INTERVAL_CLOSED_SEC = max(60, int(os.getenv("HOT_RANKINGS_INTERVAL_CLOSED_SEC", "180")))
_HOT_RANKINGS_INTERVAL_JITTER_SEC = max(0.0, float(os.getenv("HOT_RANKINGS_INTERVAL_JITTER_SEC", "1.0")))

_PRICE_REFRESH_POLLER_STARTED = False
_PRICE_REFRESH_POLLER_LOCK = threading.Lock()
# Phase 2 — 기본 주기를 3s → 15s 로 상향. WS tick 이 실시간 가격을 이미 공급하므로
# REST poll 은 cold 코드(최근 60초 내 WS tick 없는 종목)만 담당하면 충분.
# 기존 트래픽/환경변수 오버라이드는 그대로 유효.
_PRICE_REFRESH_INTERVAL_OPEN_SEC = max(1.0, float(os.getenv("PRICE_REFRESH_INTERVAL_OPEN_SEC", "15.0")))
_PRICE_REFRESH_INTERVAL_CLOSED_SEC = max(10.0, float(os.getenv("PRICE_REFRESH_INTERVAL_CLOSED_SEC", "30.0")))
_PRICE_REFRESH_INTERVAL_JITTER_SEC = max(0.0, float(os.getenv("PRICE_REFRESH_INTERVAL_JITTER_SEC", "0.5")))
_PRICE_REFRESH_WHEN_CLOSED = str(os.getenv("PRICE_REFRESH_WHEN_CLOSED", "0")).strip().lower() in ("1", "true", "yes", "on")
_PRICE_REFRESH_MODE = str(os.getenv("PRICE_REFRESH_MODE", "focus")).strip().lower() or "focus"
if _PRICE_REFRESH_MODE not in ("focus", "hybrid", "full"):
    _PRICE_REFRESH_MODE = "focus"
_PRICE_REFRESH_FOCUS_MAX_CODES = max(20, int(os.getenv("PRICE_REFRESH_FOCUS_MAX_CODES", "200")))
_PRICE_REFRESH_FALLBACK_FULL_SEC = max(0.0, float(os.getenv("PRICE_REFRESH_FALLBACK_FULL_SEC", "900.0")))


def refresh_hot_rankings(force: bool = False) -> dict:
    started = time.time()
    status = _market_status()
    out = {
        "market_status": status,
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strength_ok": False,
        "program_ok": False,
    }
    try:
        strength = _refresh_strength_default(force=force)
        out["strength_ok"] = True
        out["strength_count"] = len((strength or {}).get("items") or [])
    except Exception as e:
        out["strength_error"] = str(e)
        out["strength_count"] = 0

    try:
        program = _refresh_program_default(force=force)
        out["program_ok"] = True
        out["program_buy_count"] = len((program or {}).get("items") or [])
        out["program_sell_count"] = len((program or {}).get("items_sell") or [])
    except Exception as e:
        out["program_error"] = str(e)
        out["program_buy_count"] = 0
        out["program_sell_count"] = 0

    out["ok"] = bool(out.get("strength_ok")) and bool(out.get("program_ok"))
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    return out


def start_hot_rankings_background_poller():
    global _HOT_RANKINGS_POLLER_STARTED
    with _HOT_RANKINGS_POLLER_LOCK:
        if _HOT_RANKINGS_POLLER_STARTED:
            return
        _HOT_RANKINGS_POLLER_STARTED = True

    def _loop():
        time.sleep(9)
        while True:
            try:
                stats = refresh_hot_rankings(force=False)
                logger.debug(
                    "[rankings-poller] m=%s strength=%s(%s) program=%s(%s/%s) %sms",
                    stats.get("market_status"),
                    stats.get("strength_ok"),
                    stats.get("strength_count"),
                    stats.get("program_ok"),
                    stats.get("program_buy_count"),
                    stats.get("program_sell_count"),
                    stats.get("elapsed_ms"),
                )
            except Exception as e:
                logger.debug("[rankings-poller] error: %s", e)

            interval = _HOT_RANKINGS_INTERVAL_OPEN_SEC
            if _market_status() != "open":
                interval = _HOT_RANKINGS_INTERVAL_CLOSED_SEC
            if _HOT_RANKINGS_INTERVAL_JITTER_SEC > 0:
                interval += random.uniform(0, _HOT_RANKINGS_INTERVAL_JITTER_SEC)
            time.sleep(max(5, interval))

    t = threading.Thread(target=_loop, daemon=True, name="hot-rankings-poller")
    t.start()
    logger.info(
        "[rankings-poller] started (open=%ss, closed=%ss)",
        _HOT_RANKINGS_INTERVAL_OPEN_SEC,
        _HOT_RANKINGS_INTERVAL_CLOSED_SEC,
    )


def start_price_refresh_background_poller():
    """
    Keep rotating full-universe price refresh running in background,
    independent from request traffic.
    """
    global _PRICE_REFRESH_POLLER_STARTED
    with _PRICE_REFRESH_POLLER_LOCK:
        if _PRICE_REFRESH_POLLER_STARTED:
            return
        _PRICE_REFRESH_POLLER_STARTED = True

    def _loop():
        time.sleep(2)
        last_full_ts = 0.0
        degrade_log_ts = 0.0
        while True:
            t0 = time.time()
            market_open = (_market_status() == "open")
            interval = _PRICE_REFRESH_INTERVAL_OPEN_SEC if market_open else _PRICE_REFRESH_INTERVAL_CLOSED_SEC
            if not _PRICE_REFRESH_WHEN_CLOSED and not market_open:
                interval = max(interval, 20.0)

            # Phase 1 degrade gate: KIS 가 불안정하면 이 루프는 쉰다.
            # _kis_rest_should_skip 은 part01 에서 정의됨.
            if _kis_rest_should_skip():
                if time.time() - degrade_log_ts > 60:
                    logger.info("[price-refresh-poller] skipped (kis degraded)")
                    degrade_log_ts = time.time()
                time.sleep(max(5.0, interval))
                continue

            try:
                allowed = _is_refresh_allowed()
                if allowed or _PRICE_REFRESH_WHEN_CLOSED:
                    focus_codes = _collect_focus_codes(_PRICE_REFRESH_FOCUS_MAX_CODES)
                    did_focus = False
                    # Phase 2 — WS tick 이 신선한 code 는 REST poll 에서 제외.
                    # _rt_cache_fresh_codes / _PRICE_POLLER_EXCLUDE_SUBSCRIBED 는 part01 에서 정의.
                    if focus_codes and _PRICE_POLLER_EXCLUDE_SUBSCRIBED:
                        try:
                            fresh = _rt_cache_fresh_codes(60.0)
                        except Exception:
                            fresh = set()
                        if fresh:
                            before = len(focus_codes)
                            focus_codes = [c for c in focus_codes if c not in fresh]
                            after = len(focus_codes)
                            if before and (before - after) > 0 and (before - after) >= max(5, before // 10):
                                logger.debug(
                                    "[price-refresh-poller] excluded %d ws-fresh codes (%d → %d)",
                                    before - after, before, after,
                                )
                    if _PRICE_REFRESH_MODE in ("focus", "hybrid") and focus_codes:
                        _state.bg_refresh_prices_for_codes(focus_codes, reason="hot_track")
                        did_focus = True

                    run_full = False
                    now_ts = time.time()
                    if _PRICE_REFRESH_MODE == "full":
                        run_full = True
                    elif _PRICE_REFRESH_MODE == "hybrid" and _PRICE_REFRESH_FALLBACK_FULL_SEC > 0:
                        run_full = (now_ts - last_full_ts) >= _PRICE_REFRESH_FALLBACK_FULL_SEC
                    elif _PRICE_REFRESH_MODE == "focus" and _PRICE_REFRESH_FALLBACK_FULL_SEC > 0:
                        run_full = (now_ts - last_full_ts) >= _PRICE_REFRESH_FALLBACK_FULL_SEC

                    if run_full:
                        _bg_refresh_prices()
                        last_full_ts = time.time()
            except Exception as e:
                logger.debug("[price-refresh-poller] error: %s", e)

            if _PRICE_REFRESH_INTERVAL_JITTER_SEC > 0:
                interval += random.uniform(0.0, _PRICE_REFRESH_INTERVAL_JITTER_SEC)

            elapsed = time.time() - t0
            sleep_for = max(0.2, interval - elapsed)
            time.sleep(sleep_for)

    t = threading.Thread(target=_loop, daemon=True, name="price-refresh-poller")
    t.start()
    logger.info(
        "[price-refresh-poller] started mode=%s (open=%.1fs, closed=%.1fs, focus_max=%d, fallback_full=%ss, closed_run=%s)",
        _PRICE_REFRESH_MODE,
        _PRICE_REFRESH_INTERVAL_OPEN_SEC,
        _PRICE_REFRESH_INTERVAL_CLOSED_SEC,
        _PRICE_REFRESH_FOCUS_MAX_CODES,
        int(_PRICE_REFRESH_FALLBACK_FULL_SEC),
        _PRICE_REFRESH_WHEN_CLOSED,
    )


_HOME_SNAPSHOT_CACHE: dict = {"data": None, "ts": 0.0}
_HOME_SNAPSHOT_LOCK = threading.Lock()
_HOME_SNAPSHOT_INFLIGHT: threading.Event | None = None





