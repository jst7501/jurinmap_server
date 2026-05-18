@router.get("/api/stocks/ranking/surge")
def get_ranking_surge():
    """직전 스냅샷 대비 거래대금 급증 TOP 5"""
    global _SURGE_LAST_RUN
    now_ts = time.time()
    now_str = datetime.now().strftime("%H:%M:%S")

    # 5분 throttle (너무 자주 KIS 호출 방지)
    # 장중 2분, 그 외 5분 throttle
    run_ok = (now_ts - _SURGE_LAST_RUN) >= (120 if _market_status() == "open" else 270)
    new_items = []

    if run_ok:
        _SURGE_LAST_RUN = now_ts
        try:
            from collectors.kis_api import KISCollector
            col = KISCollector()
            kospi  = col.get_transaction_value_ranking("0001")
            kosdaq = col.get_transaction_value_ranking("1001")
            all_items = kospi + kosdaq
            snap = {
                str(i.get("code", "")).zfill(6): {
                    "tv": int(i.get("trading_value") or 0),
                    "name": i.get("name") or "",
                    "change_pct": float(i.get("change_pct") or 0),
                    "price": int(i.get("close") or i.get("price") or 0),
                }
                for i in all_items if i.get("code")
            }
            with _SURGE_LOCK:
                _SURGE_SNAPSHOTS.appendleft({"ts": now_ts, "data": snap})
                new_items = all_items
        except Exception as e:
            logger.warning("[surge] KIS error: %s", e)

    with _SURGE_LOCK:
        snapshots = list(_SURGE_SNAPSHOTS)

    if len(snapshots) < 2:
        # 스냅샷 부족 → 현재 거래대금 순 TOP 5 반환 (surge_pct=None)
        cur = snapshots[0]["data"] if snapshots else {}
        items = sorted(cur.values(), key=lambda x: x["tv"], reverse=True)[:5]
        return {
            "items": [
                {"rank": i+1, "code": list(cur.keys())[list(cur.values()).index(v)],
                 "name": v["name"], "trading_value": v["tv"],
                 "surge_pct": None, "change_pct": v["change_pct"], "price": v["price"]}
                for i, v in enumerate(items)
            ],
            "snapshot_gap_min": None,
            "updated_at": now_str,
            "_note": "스냅샷 수집 중 — 5분 후 급증 데이터 표시",
        }

    cur  = snapshots[0]["data"]
    prev = snapshots[-1]["data"]
    gap_min = round((snapshots[0]["ts"] - snapshots[-1]["ts"]) / 60, 1)

    surges = []
    for code, cv in cur.items():
        pv = prev.get(code)
        if not pv or pv["tv"] <= 0:
            continue
        surge_pct = (cv["tv"] - pv["tv"]) / pv["tv"] * 100
        if surge_pct < 10:   # 10% 미만은 제외
            continue
        surges.append({
            "code": code, "name": cv["name"],
            "trading_value": cv["tv"], "prev_trading_value": pv["tv"],
            "surge_pct": round(surge_pct, 1),
            "change_pct": cv["change_pct"], "price": cv["price"],
        })

    surges.sort(key=lambda x: x["surge_pct"], reverse=True)
    top5 = [{"rank": i+1, **s} for i, s in enumerate(surges[:5])]

    return {
        "items": top5,
        "snapshot_gap_min": gap_min,
        "updated_at": now_str,
    }


# ─── 외인/기관 순매수 순위 ────────────────────────────────────
_INVESTOR_CACHE: dict = {"data": {}, "ts": 0.0}
_INVESTOR_LOCK = threading.Lock()

@router.get("/api/stocks/ranking/investor")
def get_ranking_investor(type: str = "foreign", limit: int = 10):
    """
    외인(foreign) / 기관(institution) 순매수 TOP N
    volume ranking 상위 40종목의 오늘 수급 데이터를 병렬 조회 후 집계.
    캐시 TTL = 3분.
    """
    type = type.lower()
    if type not in ("foreign", "institution"):
        raise HTTPException(400, "type must be 'foreign' or 'institution'")

    now_ts = time.time()
    m_status = _market_status()
    ttl = 60 if m_status == "open" else 180
    
    with _INVESTOR_LOCK:
        cached = _INVESTOR_CACHE["data"].get(type)
        if cached and (now_ts - _INVESTOR_CACHE["ts"]) < ttl:
            return cached

    try:
        from collectors.kis_api import KISCollector
        import concurrent.futures

        col   = KISCollector()
        vol   = col.get_transaction_value_ranking("0001") + col.get_transaction_value_ranking("1001")
        codes = [str(i.get("code","")).zfill(6) for i in vol[:40] if i.get("code")]
        names = {str(i.get("code","")).zfill(6): i.get("name","") for i in vol[:40]}

        def _fetch(code):
            try:
                return code, col.get_investor_today(code)
            except Exception:
                return code, None

        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            for code, inv in ex.map(_fetch, codes):
                if not inv:
                    continue
                net = inv.get("foreign" if type == "foreign" else "institution") or 0
                net_amt = inv.get("foreign_net_amt" if type == "foreign" else "institution_net_amt") or 0
                change_pct = inv.get("change_pct") or 0
                results.append({
                    "code": code,
                    "name": names.get(code, code),
                    "net_buy_qty": int(net),
                    "net_buy_amt": int(net_amt),
                    "change_pct": float(change_pct),
                })

        results.sort(key=lambda x: x["net_buy_amt"], reverse=True)
        payload_buy  = [{"rank": i+1, **r} for i, r in enumerate(results[:limit])]
        results_sell = sorted(results, key=lambda x: x["net_buy_amt"])
        payload_sell = [{"rank": i+1, **r} for i, r in enumerate(results_sell[:limit])]

        out = {
            "type": type,
            "items": payload_buy,
            "items_sell": payload_sell,
            "updated_at": datetime.now().strftime("%H:%M:%S"),
        }
        with _INVESTOR_LOCK:
            _INVESTOR_CACHE["data"][type] = out
            _INVESTOR_CACHE["ts"] = now_ts
        return out

    except Exception as e:
        logger.error("[investor ranking] %s", e)
        with _INVESTOR_LOCK:
            stale = _INVESTOR_CACHE["data"].get(type)
        if stale:
            stale["_stale"] = True
            return stale
        raise HTTPException(503, f"수급 데이터 조회 실패: {e}")


# ─── stock summary 캐시 ──────────────────────────────────────
_SUMMARY_MEM_CACHE: dict[str, dict] = {}
_SUMMARY_MEM_LOCK = threading.Lock()
_SUMMARY_MEM_MAX = max(200, int(os.getenv("STOCK_SUMMARY_MEM_MAX", "4000")))
_SUMMARY_MEM_TTL_OPEN_SEC = max(10, int(os.getenv("STOCK_SUMMARY_MEM_TTL_OPEN_SEC", "120")))
_SUMMARY_MEM_TTL_CLOSED_SEC = max(20, int(os.getenv("STOCK_SUMMARY_MEM_TTL_CLOSED_SEC", "600")))
_SUMMARY_STALE_TTL_SEC = max(600, int(os.getenv("STOCK_SUMMARY_STALE_TTL_SEC", "86400")))
_SUMMARY_REFRESHING: set[str] = set()
_SUMMARY_REFRESH_LOCK = threading.Lock()


def _summary_mem_ttl_sec() -> int:
    return _SUMMARY_MEM_TTL_OPEN_SEC if _market_status() == "open" else _SUMMARY_MEM_TTL_CLOSED_SEC


def _summary_keys(code: str, token: str | int) -> tuple[str, str, str]:
    active_key = f"stocks:summary:{code}:v{token}"
    latest_key = f"stocks:summary:latest:{code}:v1"
    stale_key = f"stocks:summary:stale:{code}:v1"
    return active_key, latest_key, stale_key


def _summary_mem_get(code: str) -> tuple[dict | None, float]:
    with _SUMMARY_MEM_LOCK:
        entry = _SUMMARY_MEM_CACHE.get(code)
        if not isinstance(entry, dict):
            return None, 0.0
        return entry.get("data"), float(entry.get("ts") or 0.0)


def _summary_mem_put(code: str, payload: dict) -> None:
    now_ts = time.time()
    with _SUMMARY_MEM_LOCK:
        _SUMMARY_MEM_CACHE[code] = {"data": payload, "ts": now_ts}
        if len(_SUMMARY_MEM_CACHE) > _SUMMARY_MEM_MAX:
            oldest_key = min(
                _SUMMARY_MEM_CACHE.items(),
                key=lambda kv: float((kv[1] or {}).get("ts") or 0.0),
            )[0]
            if oldest_key != code:
                _SUMMARY_MEM_CACHE.pop(oldest_key, None)


def _summary_query_db(code: str) -> dict:
    conn = get_stocks_conn()
    try:
        try:
            row = conn.execute(
                "SELECT summary FROM company_summary WHERE code=?", (code,)
            ).fetchone()
        except Exception:
            row = None
        return {"code": code, "summary": row["summary"] if row else None}
    finally:
        conn.close()


def _summary_cache_persist(code: str, payload: dict, token: str | int) -> None:
    active_key, latest_key, stale_key = _summary_keys(code, token)
    _summary_mem_put(code, payload)
    redis_set_json(active_key, payload, ttl_seconds=600)
    redis_set_json(latest_key, payload, ttl_seconds=1800)
    redis_set_json(stale_key, payload, ttl_seconds=_SUMMARY_STALE_TTL_SEC)


def _summary_refresh_async(code: str) -> bool:
    with _SUMMARY_REFRESH_LOCK:
        if code in _SUMMARY_REFRESHING:
            return False
        _SUMMARY_REFRESHING.add(code)

    def _worker() -> None:
        try:
            token = int(time.time() // 600)
            payload = _summary_query_db(code)
            _summary_cache_persist(code, payload, token)
        except Exception as e:
            logger.debug("[stock-summary-refresh] code=%s err=%s", code, e)
        finally:
            with _SUMMARY_REFRESH_LOCK:
                _SUMMARY_REFRESHING.discard(code)

    threading.Thread(target=_worker, daemon=True, name=f"stock-summary-refresh-{code}").start()
    return True


# ─── GET /api/stocks/{code}/summary ─────────────────────────
@router.get("/api/stocks/{code}/summary")
def get_stock_summary(code: str):
    if not _stocks_db_available():
        return {"code": code, "summary": None}
    if not _is_valid_stock_code(code):
        raise HTTPException(400, f"invalid stock code: {code}")

    token = int(time.time() // 600)
    active_key, latest_key, stale_key = _summary_keys(code, token)

    mem_data, mem_ts = _summary_mem_get(code)
    if isinstance(mem_data, dict):
        age = time.time() - mem_ts
        if age < _summary_mem_ttl_sec():
            return mem_data
        mem_stale = copy.deepcopy(mem_data)
        mem_stale["_stale"] = True
        mem_stale["snapshot_age_sec"] = int(max(0.0, age))
        _summary_refresh_async(code)
        return mem_stale

    redis_hit = redis_get_json(active_key)
    if isinstance(redis_hit, dict):
        _summary_mem_put(code, redis_hit)
        return redis_hit

    redis_latest = redis_get_json(latest_key)
    if isinstance(redis_latest, dict):
        _summary_mem_put(code, redis_latest)
        payload = copy.deepcopy(redis_latest)
        payload["_stale"] = True
        _summary_refresh_async(code)
        return payload

    redis_stale = redis_get_json(stale_key)
    if isinstance(redis_stale, dict):
        _summary_mem_put(code, redis_stale)
        payload = copy.deepcopy(redis_stale)
        payload["_stale"] = True
        _summary_refresh_async(code)
        return payload

    result = _summary_query_db(code)
    _summary_cache_persist(code, result, token)
    return result


# ─── GET /api/futures/night ──────────────────────────────────
# CME 연계 코스피200 야간선물 현재가 + 일봉 차트
# fo_cme_code.mst 파싱으로 코스피200 전월물 단축코드 자동 탐색
# ─────────────────────────────────────────────────────────────
import io, ssl, zipfile, urllib.request as _urlreq

_CME_CODE_CACHE: dict = {}   # {"codes": [...], "fetched_at": float}
_CME_CODE_TTL   = 3600 * 6   # 6시간마다 재다운로드

def _fetch_cme_codes() -> list[dict]:
    """fo_cme_code.mst 다운로드 → 코스피200 관련 CME 종목 추출"""
    import time as _time
    now = _time.time()
    if _CME_CODE_CACHE.get("codes") and now - _CME_CODE_CACHE.get("fetched_at", 0) < _CME_CODE_TTL:
        return _CME_CODE_CACHE["codes"]

    try:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        url = "https://new.real.download.dws.co.kr/common/master/fo_cme_code.mst.zip"
        with _urlreq.urlopen(url, context=ssl_ctx, timeout=15) as resp:
            raw = resp.read()
        zf = zipfile.ZipFile(io.BytesIO(raw))
        mst = zf.read("fo_cme_code.mst").decode("cp949", errors="replace")
    except Exception as e:
        logger.warning(f"[night_futures] fo_cme_code.mst 다운로드 실패: {e}")
        return []

    codes = []
    for line in mst.splitlines():
        if len(line) < 63:
            continue
        prod_type  = line[0:1]
        short_code = line[1:10].strip()
        name_kor   = line[22:63].strip()
        base_code  = line[72:81].strip()
        base_name  = line[81:].strip()
        # 코스피200 선물만 (상품종류 F=선물)
        if prod_type == "F" and ("코스피" in name_kor or "KOSPI" in name_kor.upper()):
            codes.append({
                "srs_cd": short_code,
                "name":   name_kor,
                "base":   base_name or base_code,
            })

    _CME_CODE_CACHE["codes"]      = codes
    _CME_CODE_CACHE["fetched_at"] = now
    logger.info(f"[night_futures] CME 코드 파싱 완료: {len(codes)}개")
    return codes


@router.get("/api/futures/night")
def get_night_futures():
    """
    CME 연계 코스피200 야간선물 — 현재가 + 60일 일봉 차트
    캐시 TTL: 5분 (300초)
    """
    CACHE_KEY = "investpulse:futures:night:v1"
    cached = redis_get_json(CACHE_KEY)
    if isinstance(cached, dict) and cached.get("candles"):
        return cached

    from collectors.kis_api import KISCollector
    kis = KISCollector()

    # 1. fo_cme_code.mst에서 코스피200 CME 코드 목록 가져오기
    cme_codes = _fetch_cme_codes()
    if not cme_codes:
        # 파일 다운로드 실패 시 알려진 단축코드 fallback
        cme_codes = [{"srs_cd": "101S9", "name": "코스피200선물", "base": "코스피200"}]

    # 2. 현재가 조회 (첫 번째 유효한 코드 사용)
    price_info = None
    used_code  = None
    for entry in cme_codes[:5]:  # 최대 5개 시도
        result = kis.get_night_futures_price(entry["srs_cd"])
        if result.get("error") is None and result.get("close", 0) > 0:
            price_info = result
            used_code  = entry["srs_cd"]
            break

    if not price_info:
        price_info = {"close": None, "change_pct": None, "error": "가격 조회 실패"}

    # 3. 일봉 차트 조회
    candles = []
    if used_code:
        candles = kis.get_night_futures_chart(used_code, days=60)

    result = {
        "price":   price_info,
        "candles": candles,
        "codes":   cme_codes[:10],  # UI 참고용
    }

    if candles:
        redis_set_json(CACHE_KEY, result, ttl_seconds=300)

    return result

# Program ranking cache (net buy / net sell)
_PROGRAM_CACHE: dict = {"payload": None, "ts": 0.0}
_PROGRAM_LOCK = threading.Lock()
_PROGRAM_INFLIGHT: threading.Event | None = None
_PROGRAM_REDIS_KEY = "stocks:ranking:program:v2"
_PROGRAM_UNIVERSE_CAP_OPEN = max(10, int(os.getenv("PROGRAM_UNIVERSE_CAP_OPEN", "24")))
_PROGRAM_UNIVERSE_CAP_CLOSED = max(10, int(os.getenv("PROGRAM_UNIVERSE_CAP_CLOSED", "40")))
_PROGRAM_WORKERS = max(1, int(os.getenv("PROGRAM_RANKING_WORKERS", "5")))
_PROGRAM_WAIT_OPEN_SEC = max(1.0, float(os.getenv("PROGRAM_WAIT_OPEN_SEC", "12.0")))
_PROGRAM_WAIT_CLOSED_SEC = max(1.0, float(os.getenv("PROGRAM_WAIT_CLOSED_SEC", "15.0")))
_SHORT_RANK_CACHE: dict = {"payload": None, "ts": 0.0}
_SHORT_RANK_LOCK = threading.Lock()


def _program_payload_to_response(payload: dict, limit: int) -> dict:
    buy_all = payload.get("buy_all") or []
    sell_all = payload.get("sell_all") or []
    buy = [{"rank": i + 1, **r} for i, r in enumerate(buy_all[:limit])]
    sell = [{"rank": i + 1, **r} for i, r in enumerate(sell_all[:limit])]
    return {
        "items": buy,
        "items_sell": sell,
        "updated_at": payload.get("updated_at"),
        "universe_size": int(payload.get("universe_size") or 0),
    }


def _program_ttl(market_status: str) -> int:
    return 20 if market_status == "open" else 120


def _program_universe_cap(market_status: str) -> int:
    return _PROGRAM_UNIVERSE_CAP_OPEN if market_status == "open" else _PROGRAM_UNIVERSE_CAP_CLOSED


def _program_wait_timeout(market_status: str) -> float:
    return _PROGRAM_WAIT_OPEN_SEC if market_status == "open" else _PROGRAM_WAIT_CLOSED_SEC


def _build_program_universe(collector, market_status: str) -> tuple[list[str], dict]:
    cap = _program_universe_cap(market_status)
    seed_items = []
    try:
        vol = get_ranking_volume()
        if isinstance(vol, dict):
            seed_items = (vol.get("kospi") or []) + (vol.get("kosdaq") or [])
    except Exception:
        seed_items = []

    if not seed_items:
        kospi = collector.get_transaction_value_ranking("0001") or []
        kosdaq = collector.get_transaction_value_ranking("1001") or []
        seed_items = kospi + kosdaq

    names: dict = {}
    universe: list[str] = []
    seen = set()
    for item in seed_items:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        if code.isdigit():
            code = code.zfill(6)
        if code in seen:
            continue
        seen.add(code)
        names[code] = str(item.get("name") or code)
        universe.append(code)
        if len(universe) >= cap:
            break
    return universe, names


def _compute_program_payload(market_status: str) -> dict:
    from collectors.kis_api import KISCollector
    import concurrent.futures

    seed_collector = KISCollector()
    universe, names = _build_program_universe(seed_collector, market_status)
    if not universe:
        return {
            "buy_all": [],
            "sell_all": [],
            "updated_at": datetime.now().strftime("%H:%M:%S"),
            "universe_size": 0,
            "market_status": market_status,
        }

    thread_local = threading.local()

    def _fetch(code: str):
        try:
            collector = getattr(thread_local, "collector", None)
            if collector is None:
                collector = KISCollector()
                thread_local.collector = collector
            rows = collector.get_program_trade_5d(code) or []
            if not rows:
                return None
            row = rows[0] if isinstance(rows[0], dict) else None
            if not row:
                return None
            program_buy = int(row.get("program_buy") or 0)
            program_sell = int(row.get("program_sell") or 0)
            net_buy_qty = int(row.get("program_net") or (program_buy - program_sell))
            net_buy_amt = int(row.get("program_net_amt") or 0)
            # 가격 enrichment — KIS RT 캐시 우선, 빈칸은 아래 post-처리에서 price_today JOIN 으로 채움
            from server.routes.stocks_parts.part01_realtime_base import _KIS_RT_CACHE
            rt = _KIS_RT_CACHE.get(code) or {}
            return {
                "code": code,
                "name": names.get(code, code),
                "net_buy_qty": net_buy_qty,
                "net_buy_amt": net_buy_amt,
                "program_buy": program_buy,
                "program_sell": program_sell,
                "base_date": row.get("date", "-"),
                "current_price": rt.get("current_price"),
                "change_pct": rt.get("change_pct"),
            }
        except Exception:
            return None

    rows = []
    workers = min(_PROGRAM_WORKERS, max(1, len(universe)))
    if workers <= 1:
        for code in universe:
            row = _fetch(code)
            if isinstance(row, dict):
                rows.append(row)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for row in ex.map(_fetch, universe):
                if isinstance(row, dict):
                    rows.append(row)

    # RT 캐시에 없는 종목은 price_today 로 보강 (장마감 후 수집된 종가)
    missing_codes = [r["code"] for r in rows if r.get("current_price") is None]
    if missing_codes:
        try:
            from server.db.connections import get_stocks_conn
            conn = get_stocks_conn()
            cur = conn.cursor()
            placeholders = ",".join(["?"] * len(missing_codes))
            cur.execute(
                f"SELECT code, close AS current_price, change_pct FROM price_today WHERE code IN ({placeholders})",
                missing_codes,
            )
            pt = {}
            for r in cur.fetchall():
                pt[r[0]] = {"current_price": r[1], "change_pct": r[2]}
            for r in rows:
                if r.get("current_price") is None and r["code"] in pt:
                    r["current_price"] = pt[r["code"]]["current_price"]
                    r["change_pct"] = pt[r["code"]]["change_pct"]
        except Exception as _e:
            logger.debug("program price_today fallback failed: %s", _e)

    buy_all = sorted(rows, key=lambda x: (x["net_buy_amt"], x["net_buy_qty"]), reverse=True)
    sell_all = sorted(rows, key=lambda x: (x["net_buy_amt"], x["net_buy_qty"]))
    return {
        "buy_all": buy_all,
        "sell_all": sell_all,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "universe_size": len(universe),
        "market_status": market_status,
    }


def _short_payload_to_response(payload: dict, limit: int) -> dict:
    all_items = payload.get("all_items") or []
    items = [{"rank": i + 1, **r} for i, r in enumerate(all_items[:limit])]
    return {
        "items": items,
        "updated_at": payload.get("updated_at"),
        "source": payload.get("source", "db_short_data"),
        "total_count": int(payload.get("total_count") or 0),
    }


@router.get("/api/stocks/ranking/short")
def get_ranking_short(limit: int = 10, min_ratio: float = 0.1):
    """
    Short-selling ratio ranking from DB (all symbols).
    - No KIS request at query time
    - Cache: 60s while market open, 300s otherwise
    """
    try:
        limit = int(limit)
    except Exception:
        limit = 10
    limit = max(1, min(limit, 100))

    try:
        min_ratio = float(min_ratio)
    except Exception:
        min_ratio = 0.1
    min_ratio = max(0.0, min(min_ratio, 100.0))

    now_ts = time.time()
    m_status = _market_status()
    ttl = 60 if m_status == "open" else 300
    redis_key = f"stocks:ranking:short:v1:min{min_ratio:.2f}"

    with _SHORT_RANK_LOCK:
        mem_payload = _SHORT_RANK_CACHE.get("payload")
        mem_ts = float(_SHORT_RANK_CACHE.get("ts") or 0.0)
        if (
            isinstance(mem_payload, dict)
            and float(mem_payload.get("min_ratio", -1.0)) == float(min_ratio)
            and (now_ts - mem_ts) < ttl
        ):
            return _short_payload_to_response(mem_payload, limit)

    redis_payload = redis_get_json(redis_key)
    if isinstance(redis_payload, dict):
        if isinstance(redis_payload.get("all_items"), list):
            with _SHORT_RANK_LOCK:
                _SHORT_RANK_CACHE["payload"] = redis_payload
                _SHORT_RANK_CACHE["ts"] = now_ts
            return _short_payload_to_response(redis_payload, limit)

    conn = get_stocks_conn()
    try:
        rows = conn.execute(
            """
            SELECT
                s.code,
                s.name,
                COALESCE(sd.short_selling_volume_ratio, 0) AS short_ratio,
                sd.updated_at,
                pt.current_price AS current_price,
                pt.change_pct AS change_pct
            FROM short_data sd
            JOIN stocks s ON s.code = sd.code
            LEFT JOIN price_today pt ON pt.code = s.code
            WHERE COALESCE(sd.short_selling_volume_ratio, 0) >= ?
            ORDER BY COALESCE(sd.short_selling_volume_ratio, 0) DESC, s.code ASC
            LIMIT 500
            """,
            (min_ratio,),
        ).fetchall()

        # RT 캐시로 price_today 값 덮어쓰기 (더 신선)
        from server.routes.stocks_parts.part01_realtime_base import _KIS_RT_CACHE

        all_items = []
        latest_updated_at = None
        for r in rows:
            row = dict(r)
            updated_at = row.get("updated_at")
            if updated_at and (latest_updated_at is None or str(updated_at) > str(latest_updated_at)):
                latest_updated_at = updated_at
            code = str(row.get("code") or "").zfill(6)
            # RT 캐시에 최신값 있으면 우선
            rt = _KIS_RT_CACHE.get(code) or {}
            cur_price = rt.get("current_price") if rt.get("current_price") is not None else row.get("current_price")
            chg_pct = rt.get("change_pct") if rt.get("change_pct") is not None else row.get("change_pct")
            all_items.append(
                {
                    "code": code,
                    "name": row.get("name") or "",
                    "short_ratio": round(float(row.get("short_ratio") or 0.0), 2),
                    "data_updated_at": updated_at,
                    "current_price": cur_price,
                    "change_pct": chg_pct,
                }
            )

        payload = {
            "all_items": all_items,
            "updated_at": datetime.now().strftime("%H:%M:%S"),
            "latest_data_updated_at": latest_updated_at,
            "source": "db_short_data",
            "total_count": len(all_items),
            "min_ratio": min_ratio,
        }

        with _SHORT_RANK_LOCK:
            _SHORT_RANK_CACHE["payload"] = payload
            _SHORT_RANK_CACHE["ts"] = now_ts
        redis_set_json(redis_key, payload, ttl_seconds=ttl)
        out = _short_payload_to_response(payload, limit)
        out["latest_data_updated_at"] = latest_updated_at
        out["min_ratio"] = min_ratio
        return out
    except Exception as e:
        logger.error("[short ranking] %s", e)
        with _SHORT_RANK_LOCK:
            stale = _SHORT_RANK_CACHE.get("payload")
        if isinstance(stale, dict):
            out = _short_payload_to_response(stale, limit)
            out["_stale"] = True
            return out
        raise HTTPException(503, f"short ranking fetch failed: {e}")
    finally:
        conn.close()


@router.get("/api/stocks/ranking/program")
def get_ranking_program(limit: int = 10):
    """
    Program net buy / net sell top ranking.
    Universe: transaction value top (KOSPI + KOSDAQ, up to 40 symbols)
    Cache: 30s while market open, 120s otherwise
    """
    try:
        limit = int(limit)
    except Exception:
        limit = 10
    limit = max(1, min(limit, 30))

    global _PROGRAM_INFLIGHT
    now_ts = time.time()
    m_status = _market_status()
    ttl = _program_ttl(m_status)
    redis_key = _PROGRAM_REDIS_KEY

    with _PROGRAM_LOCK:
        mem_payload = _PROGRAM_CACHE.get("payload")
        mem_ts = float(_PROGRAM_CACHE.get("ts") or 0.0)
        if isinstance(mem_payload, dict) and (now_ts - mem_ts) < ttl:
            return _program_payload_to_response(mem_payload, limit)

    redis_payload = redis_get_json(redis_key)
    if isinstance(redis_payload, dict):
        buy_all = redis_payload.get("buy_all")
        sell_all = redis_payload.get("sell_all")
        if isinstance(buy_all, list) and isinstance(sell_all, list):
            with _PROGRAM_LOCK:
                _PROGRAM_CACHE["payload"] = redis_payload
                _PROGRAM_CACHE["ts"] = now_ts
            return _program_payload_to_response(redis_payload, limit)

    stale_payload = mem_payload if isinstance(mem_payload, dict) else None
    with _PROGRAM_LOCK:
        inflight_event = _PROGRAM_INFLIGHT
        if inflight_event is None:
            inflight_event = threading.Event()
            _PROGRAM_INFLIGHT = inflight_event
            is_leader = True
        else:
            is_leader = False

    if not is_leader:
        if inflight_event.wait(timeout=_program_wait_timeout(m_status)):
            with _PROGRAM_LOCK:
                after_payload = _PROGRAM_CACHE.get("payload")
                after_ts = float(_PROGRAM_CACHE.get("ts") or 0.0)
            if isinstance(after_payload, dict) and (time.time() - after_ts) < max(ttl, 5):
                return _program_payload_to_response(after_payload, limit)
            redis_after = redis_get_json(redis_key)
            if isinstance(redis_after, dict):
                buy_all = redis_after.get("buy_all")
                sell_all = redis_after.get("sell_all")
                if isinstance(buy_all, list) and isinstance(sell_all, list):
                    with _PROGRAM_LOCK:
                        _PROGRAM_CACHE["payload"] = redis_after
                        _PROGRAM_CACHE["ts"] = time.time()
                    return _program_payload_to_response(redis_after, limit)
        if isinstance(stale_payload, dict):
            out = _program_payload_to_response(stale_payload, limit)
            out["_stale"] = True
            out["error"] = "refresh_timeout"
            return out
        raise HTTPException(503, "program ranking refresh in progress")

    try:
        payload = _compute_program_payload(m_status)
        with _PROGRAM_LOCK:
            _PROGRAM_CACHE["payload"] = payload
            _PROGRAM_CACHE["ts"] = time.time()
        redis_set_json(redis_key, payload, ttl_seconds=ttl)
        return _program_payload_to_response(payload, limit)
    except Exception as e:
        logger.error("[program ranking] %s", e)
        stale = redis_get_json(redis_key)
        if isinstance(stale, dict):
            buy_all = stale.get("buy_all")
            sell_all = stale.get("sell_all")
            if isinstance(buy_all, list) and isinstance(sell_all, list):
                with _PROGRAM_LOCK:
                    _PROGRAM_CACHE["payload"] = stale
                    _PROGRAM_CACHE["ts"] = time.time()
                out = _program_payload_to_response(stale, limit)
                out["_stale"] = True
                out["error"] = str(e)
                return out
        if isinstance(stale_payload, dict):
            out = _program_payload_to_response(stale_payload, limit)
            out["_stale"] = True
            out["error"] = str(e)
            return out
        raise HTTPException(503, f"program ranking fetch failed: {e}")
    finally:
        with _PROGRAM_LOCK:
            done_event = _PROGRAM_INFLIGHT
            _PROGRAM_INFLIGHT = None
        if done_event is not None:
            done_event.set()


# ─── 거래원 (Broker) 매수/매도 Top — broker_trade_top 테이블 ─────────
@router.get("/api/stocks/{code}/brokers")
def get_stock_brokers(code: str, days: int = 5):
    """종목별 거래원 매수/매도 Top5 — 최근 N영업일.

    응답:
    {
      "code": "005930",
      "days": 5,
      "by_date": {
        "2026-05-08": {"buy": [{rank, broker_name, qty, qty_change, is_foreign}, ...],
                       "sell": [...]},
        ...
      },
      "foreign_share_today": {"buy_pct": 32.4, "sell_pct": 12.7}  // 외국계 거래원 비중
    }
    """
    if not _is_valid_stock_code(code):
        raise HTTPException(400, f"invalid stock code: {code}")
    days = max(1, min(int(days or 5), 30))

    conn = get_stocks_conn()
    try:
        rows = conn.execute(
            """
            SELECT date, side, rank, broker_name, broker_no,
                   qty, qty_change, is_foreign, fetched_at
            FROM broker_trade_top
            WHERE code = ?
            ORDER BY date DESC, side ASC, rank ASC
            LIMIT ?
            """,
            (code, days * 2 * 5),  # 하루 buy 5 + sell 5 = 10 row
        ).fetchall()
    finally:
        conn.close()

    by_date: dict[str, dict] = {}
    for r in rows:
        d = dict(r) if hasattr(r, "keys") else {}
        date_v = str(d.get("date") or "")
        if not date_v:
            continue
        bucket = by_date.setdefault(date_v, {"buy": [], "sell": []})
        side = str(d.get("side") or "").lower()
        if side not in ("buy", "sell"):
            continue
        bucket[side].append({
            "rank": d.get("rank"),
            "broker_name": d.get("broker_name"),
            "broker_no": d.get("broker_no"),
            "qty": d.get("qty"),
            "qty_change": d.get("qty_change"),
            "is_foreign": bool(d.get("is_foreign")),
        })

    # 외국계 비중 (가장 최근 일자 기준)
    foreign_share_today = None
    if by_date:
        latest = max(by_date.keys())
        latest_b = by_date[latest]
        def _foreign_pct(rows: list) -> float | None:
            total = sum(int(r.get("qty") or 0) for r in rows)
            if total <= 0:
                return None
            foreign_qty = sum(int(r.get("qty") or 0) for r in rows if r.get("is_foreign"))
            return round(foreign_qty / total * 100, 2)
        foreign_share_today = {
            "date": latest,
            "buy_pct": _foreign_pct(latest_b.get("buy") or []),
            "sell_pct": _foreign_pct(latest_b.get("sell") or []),
        }

    return {
        "code": code,
        "days": days,
        "by_date": by_date,
        "foreign_share_today": foreign_share_today,
    }


@router.get("/api/stocks/{code}/investor-breakdown")
def get_stock_investor_breakdown(code: str, days: int = 20):
    """투자자 세분화 시계열 — investor_flow 테이블의 6개 기관 세부 + 외국인/개인.

    응답:
    {
      "code": "005930",
      "days": 20,
      "rows": [
        {date, foreign, institution, individual, etc_org, program,
         bank, insurance, trust, pension, private_fund, etc_finance}, ...
      ],
      "totals_5d": {foreign: ..., pension: ..., trust: ..., ...},
      "totals_20d": {...}
    }
    """
    if not _is_valid_stock_code(code):
        raise HTTPException(400, f"invalid stock code: {code}")
    days = max(1, min(int(days or 20), 60))

    conn = get_stocks_conn()
    try:
        rows = conn.execute(
            """
            SELECT date,
                   foreign_net AS foreign_qty,
                   institution_net AS institution_qty,
                   individual_net AS individual_qty,
                   etc_org_net,
                   program_net,
                   bank_net, insurance_net, trust_net,
                   pension_net, private_fund_net, etc_finance_net
            FROM investor_flow
            WHERE code = ?
            ORDER BY date DESC
            LIMIT ?
            """,
            (code, days),
        ).fetchall()
    finally:
        conn.close()

    parsed = []
    for r in rows:
        d = dict(r) if hasattr(r, "keys") else {}
        parsed.append({
            "date": str(d.get("date") or ""),
            "foreign": int(d.get("foreign_qty") or 0),
            "institution": int(d.get("institution_qty") or 0),
            "individual": int(d.get("individual_qty") or 0),
            "etc_org": int(d.get("etc_org_net") or 0),
            "program": int(d.get("program_net") or 0),
            "bank": int(d.get("bank_net") or 0),
            "insurance": int(d.get("insurance_net") or 0),
            "trust": int(d.get("trust_net") or 0),
            "pension": int(d.get("pension_net") or 0),
            "private_fund": int(d.get("private_fund_net") or 0),
            "etc_finance": int(d.get("etc_finance_net") or 0),
        })

    def _sum(rows: list, n: int, key: str) -> int:
        return sum(int(r.get(key) or 0) for r in rows[:n])

    keys = ("foreign", "institution", "individual", "etc_org", "program",
            "bank", "insurance", "trust", "pension", "private_fund", "etc_finance")
    return {
        "code": code,
        "days": days,
        "rows": parsed,
        "totals_5d":  {k: _sum(parsed, 5, k) for k in keys},
        "totals_20d": {k: _sum(parsed, 20, k) for k in keys},
    }


# ─── 종목별 시간대 투자자 수급 (lazy fetch + 30초 in-process 캐시) ─────
_INVESTOR_TIME_CACHE: dict[str, tuple[float, dict]] = {}
_INVESTOR_TIME_CACHE_TTL_SEC = 30.0


@router.get("/api/stocks/{code}/investor-flow-time")
def get_stock_investor_flow_time(code: str):
    """종목별 장중 시간대 투자자 누적 — KIS inquire-investor (당일 부분 누적).

    KIS 의 단일 종목 시간대 TR 이 별도로 제공되지 않아, FHKST01010900 의 첫 행
    (오늘분 부분 누적) 을 30초 캐시로 노출. 사용자가 종목 페이지 머무는 동안
    매 30초 폴링하면 누적 변화 곡선을 그릴 수 있음.

    응답:
    {
      "code": "005930",
      "fetched_at": "...",
      "today": {foreign, institution, individual, etc_org, program,
                bank, insurance, trust, pension, private_fund, etc_finance,
                foreign_net_amt, institution_net_amt, individual_net_amt}
    }
    """
    if not _is_valid_stock_code(code):
        raise HTTPException(400, f"invalid stock code: {code}")

    now = time.time()
    cached = _INVESTOR_TIME_CACHE.get(code)
    if cached and (now - cached[0]) < _INVESTOR_TIME_CACHE_TTL_SEC:
        return cached[1]

    try:
        from collectors.kis_api import KISCollector
        col = KISCollector()
        rows = col.get_investor_history(code, max_days=1)
    except Exception as e:
        raise HTTPException(503, f"kis_failed: {e}")

    if not rows:
        return {"code": code, "fetched_at": datetime.now().isoformat(), "today": None}

    r = rows[0]
    today = {
        "date": r.get("date"),
        "foreign": int(r.get("foreign") or 0),
        "institution": int(r.get("institution") or 0),
        "individual": int(r.get("individual") or 0),
        "etc_org": int(r.get("etc_org") or 0),
        "program": int(r.get("program") or 0),
        "bank": int(r.get("bank") or 0),
        "insurance": int(r.get("insurance") or 0),
        "trust": int(r.get("trust") or 0),
        "pension": int(r.get("pension") or 0),
        "private_fund": int(r.get("private_fund") or 0),
        "etc_finance": int(r.get("etc_finance") or 0),
        "foreign_net_amt": int(r.get("foreign_net_amt") or 0),
        "institution_net_amt": int(r.get("institution_net_amt") or 0),
        "individual_net_amt": int(r.get("individual_net_amt") or 0),
    }
    payload = {"code": code, "fetched_at": datetime.now().isoformat(), "today": today}
    _INVESTOR_TIME_CACHE[code] = (now, payload)

    # 캐시 prune (1000 종목 넘으면 가장 오래된 200개 제거)
    if len(_INVESTOR_TIME_CACHE) > 1000:
        oldest = sorted(_INVESTOR_TIME_CACHE.items(), key=lambda kv: kv[1][0])[:200]
        for k, _ in oldest:
            _INVESTOR_TIME_CACHE.pop(k, None)

    return payload


@router.get("/api/stocks/{code}/brokers/refresh")
def refresh_stock_brokers(code: str):
    """단일 종목 거래원 즉시 새로고침 (장중 호출 가능, 단 KIS 응답이 마감 직후
    가장 신뢰).
    """
    if not _is_valid_stock_code(code):
        raise HTTPException(400, f"invalid stock code: {code}")
    try:
        from collectors.kis_api import KISCollector
        col = KISCollector()
        payload = col.get_member_trade(code)
    except Exception as e:
        raise HTTPException(503, f"kis_failed: {e}")
    if "error" in payload:
        return {"ok": False, "error": payload["error"], "code": code}

    today = datetime.now().strftime("%Y-%m-%d")
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_stocks_conn()
    try:
        # broker_trade_top 스키마는 sync_broker_top.py 가 생성. 없으면 무시.
        try:
            params: list[tuple] = []
            for side in ("buy", "sell"):
                for entry in payload.get(side) or []:
                    params.append((
                        code, today, side, int(entry.get("rank") or 0),
                        str(entry.get("broker_name") or ""),
                        str(entry.get("broker_no") or ""),
                        int(entry.get("qty") or 0),
                        int(entry.get("qty_change") or 0),
                        bool(entry.get("is_foreign")),
                        fetched_at,
                    ))
            if params:
                conn.executemany(
                    """
                    INSERT INTO broker_trade_top (
                        code, date, side, rank, broker_name, broker_no,
                        qty, qty_change, is_foreign, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (code, date, side, rank) DO UPDATE SET
                        broker_name = excluded.broker_name,
                        broker_no   = excluded.broker_no,
                        qty         = excluded.qty,
                        qty_change  = excluded.qty_change,
                        is_foreign  = excluded.is_foreign,
                        fetched_at  = excluded.fetched_at
                    """,
                    params,
                )
                conn.commit()
        except Exception:
            pass  # 테이블 미생성 시 응답만 반환
    finally:
        conn.close()

    return {"ok": True, **payload}

