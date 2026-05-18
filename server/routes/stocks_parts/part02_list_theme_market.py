_STOCK_FLOW_WS_HUB = _StockFlowWsHub()


# ─── [1] GET /api/stocks ─────────────────────────────────────
@router.get("/api/stocks")
def get_stocks_list(
    sort_by: str = "trading_value",
    order: str = "desc",
    limit: int = 100,
    offset: int = 0
):
    import time as _t

    # 갱신 시각 체크하여 조회 시점에만 백그라운드 KIS → DB 갱신 (오후 8시 이전만)
    if (not _PRICE_REFRESH_POLLER_STARTED) and _is_refresh_allowed() and (_t.time() - _state._LAST_PRICE_REFRESH_AT > 60):
        _state._LAST_PRICE_REFRESH_AT = _t.time()  # 즉시 갱신해서 중복 방지
        threading.Thread(target=_bg_refresh_prices, daemon=True).start()

    cache_key = (sort_by, order, limit, offset)
    mtime = _get_db_mtime()
    if cache_key in _STOCKS_LIST_CACHE:
        entry = _STOCKS_LIST_CACHE[cache_key]
        if entry["mtime"] == mtime:
            _apply_local_logo_urls(entry["data"])
            return entry["data"]
    redis_key = (
        f"stocks:list:{sort_by}:{order}:{limit}:{offset}:v{_mtime_token()}"
    )
    redis_hit = redis_get_json(redis_key)
    if isinstance(redis_hit, dict):
        _apply_local_logo_urls(redis_hit)
        _STOCKS_LIST_CACHE[cache_key] = {"data": redis_hit, "mtime": mtime}
        return redis_hit

    if not _stocks_db_available():
        raise HTTPException(503, "DB 없음. import_to_db.py를 먼저 실행하세요.")

    # 정렬 컬럼 맵핑 (SQL Injection 방지)
    sort_map = {
        "market_cap": "pt.market_cap",
        "trading_value": "pt.trading_value",
        "trading_volume": "pt.trading_volume",
        "change_pct": "pt.change_pct",
        "rs_score": "ta.rs_score",
        "name": "s.name"
    }
    sort_col = sort_map.get(sort_by, "pt.trading_value")
    direction = "DESC" if order.lower() == "desc" else "ASC"

    conn = get_stocks_conn()
    try:
        query = f"""
        SELECT
            s.code, s.name, s.market,
            ne.item_logo_url AS logo_url,
            ne.item_logo_png_url AS logo_png_url,
            pt.current_price, pt.change_pct, pt.change_amt,
            pt.trading_value, pt.trading_volume, pt.volume_turnover_rate,
            pt.market_cap, pt.per, pt.pbr, pt.eps,
            pt.foreign_hold_pct, pt.listed_shares,
            sd.short_enabled, sd.short_selling_volume_ratio,
            cd.rate_today AS credit_rate_today,
            ta.ma5, ta.ma20, ta.rs_score, ta.div_5, ta.div_20, ta.returns_json,
            it.foreign_net, it.institution_net, it.individual_net,
            bs.score AS sentiment_score, bs.mood, bs.grade,
            bs.top_euphoria_json, bs.top_despair_json,
            ai.human_indicator_score, ai.sentiment_phase, ai.sentiment_phase_kor,
            ai.contrarian_signal, ai.contrarian_signal_kor, ai.core_issue, ai.summary,
            ai.sentiment_keywords_json, ai.issue_keywords_json,
            (SELECT GROUP_CONCAT(theme) FROM stock_themes WHERE code = s.code) AS themes_csv,
            s.updated_at
        FROM stocks s
        LEFT JOIN price_today     pt ON s.code = pt.code
        LEFT JOIN short_data      sd ON s.code = sd.code
        LEFT JOIN credit_data     cd ON s.code = cd.code
        LEFT JOIN tech_analysis   ta ON s.code = ta.code
        LEFT JOIN investor_today  it ON s.code = it.code
        LEFT JOIN board_sentiment bs ON s.code = bs.code
        LEFT JOIN ai_analysis     ai ON s.code = ai.code
        LEFT JOIN naver_extended  ne ON s.code = ne.code
        ORDER BY {sort_col} {direction} NULLS LAST
        LIMIT ? OFFSET ?
        """
        rows = conn.execute(query, (limit, offset)).fetchall()

        result = {}
        for r in rows:
            r = dict(r)
            code = r.pop("code")
            r["returns"]              = jl(r.pop("returns_json", None))
            r["top_euphoria"]         = jl(r.pop("top_euphoria_json", None))
            r["top_despair"]          = jl(r.pop("top_despair_json", None))
            r["sentiment_keywords"]   = jl(r.pop("sentiment_keywords_json", None))
            r["issue_keywords"]       = jl(r.pop("issue_keywords_json", None))
            themes_csv                = r.pop("themes_csv", None)
            r["themes"]               = [t.strip() for t in themes_csv.split(",")] if themes_csv else []
            result[code] = r

        # ── 위험도 배지: 20일 변동성 계산 ──────────────────────
        if result:
            codes = list(result.keys())
            ph = ",".join("?" * len(codes))
            vol_rows = conn.execute(
                f"SELECT code, close FROM price_daily WHERE code IN ({ph}) AND close > 0 ORDER BY code, date DESC",
                codes,
            ).fetchall()
            from collections import defaultdict
            closes_map = defaultdict(list)
            for vcode, vclose in vol_rows:
                if len(closes_map[vcode]) < 22:
                    closes_map[vcode].append(vclose)

            for code in codes:
                cls = list(reversed(closes_map[code]))
                if len(cls) >= 5:
                    rets = [(cls[i] - cls[i-1]) / cls[i-1] * 100 for i in range(1, len(cls))]
                    mean = sum(rets) / len(rets)
                    std  = (sum((x - mean) ** 2 for x in rets) / len(rets)) ** 0.5
                    if std < 1.5:
                        result[code]["risk_level"] = "안전"
                    elif std < 3.0:
                        result[code]["risk_level"] = "보통"
                    else:
                        result[code]["risk_level"] = "위험"
                else:
                    result[code]["risk_level"] = None

        _apply_local_logo_urls(result)
        _STOCKS_LIST_CACHE[cache_key] = {"data": result, "mtime": mtime}
        redis_set_json(redis_key, result, ttl_seconds=300)
        return result
    finally:
        conn.close()


# ─── /ws/nxt-prices — NXT (NextTrade) 실시간 체결가 ──────────────────────────
# 클라이언트가 subscribe 메시지로 종목 코드 list 보내면 서버가 _KIS_NXT_RT_HUB
# 에 add_codes 후 1초마다 _KIS_NXT_RT_CACHE 의 해당 코드 가격을 broadcast.
# 디스커넥트 시 다른 클라이언트가 같은 코드 쓰지 않으면 hub 에서 코드 제거.
class _NxtPriceWsHub:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._clients: dict[int, dict] = {}
        self._next_id = 1
        self._task = None
        self._broadcast_interval_sec = 1.0
        self._max_clients = int(os.getenv("NXT_WS_MAX_CLIENTS", "200"))

    async def connect(self, ws: WebSocket):
        async with self._lock:
            if len(self._clients) >= self._max_clients:
                await ws.accept()
                await ws.send_json({"type": "busy", "max_connections": self._max_clients})
                await ws.close(code=1013, reason="nxt_ws_full")
                return None
            cid = self._next_id
            self._next_id += 1
            self._clients[cid] = {"ws": ws, "codes": set(), "last_active": time.monotonic()}
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run())
        await ws.accept()
        await ws.send_json({"type": "connected", "interval_ms": int(self._broadcast_interval_sec * 1000)})
        return cid

    async def disconnect(self, cid: int):
        async with self._lock:
            self._clients.pop(cid, None)
            if not self._clients:
                # 모든 클라이언트 떠나면 NXT hub 구독 해제
                await asyncio.to_thread(_KIS_NXT_RT_HUB.set_codes, [])
                return
            # 남은 클라이언트들의 union 으로 hub 갱신
            union = set()
            for entry in self._clients.values():
                union.update(entry.get("codes") or set())
        await asyncio.to_thread(_KIS_NXT_RT_HUB.set_codes, sorted(union)[:_KIS_NXT_RT_HUB.MAX_CODES])

    async def touch(self, cid: int):
        async with self._lock:
            entry = self._clients.get(cid)
            if entry:
                entry["last_active"] = time.monotonic()

    async def update_codes(self, cid: int, codes) -> list[str]:
        normalized = []
        seen = set()
        for c in (codes or []):
            code = str(c or "").strip()
            if not code or code in seen:
                continue
            if not (len(code) == 6 and code.isdigit()):
                continue
            seen.add(code)
            normalized.append(code)
            if len(normalized) >= 10:  # 한 클라이언트당 최대 10종목
                break
        async with self._lock:
            entry = self._clients.get(cid)
            if not entry:
                return []
            entry["codes"] = set(normalized)
            entry["last_active"] = time.monotonic()
            union = set()
            for c_entry in self._clients.values():
                union.update(c_entry.get("codes") or set())
        await asyncio.to_thread(_KIS_NXT_RT_HUB.set_codes, sorted(union)[:_KIS_NXT_RT_HUB.MAX_CODES])
        return normalized

    async def _run(self):
        while True:
            async with self._lock:
                if not self._clients:
                    self._task = None
                    return
                snapshot = [
                    {"cid": cid, "ws": entry["ws"], "codes": set(entry.get("codes") or set())}
                    for cid, entry in self._clients.items()
                ]
            stale_cids: list[int] = []
            for s in snapshot:
                codes = list(s["codes"])
                if not codes:
                    continue
                ticks = {}
                for code in codes:
                    ent = _KIS_NXT_RT_CACHE.get(code)
                    if not isinstance(ent, dict):
                        continue
                    ticks[code] = {
                        "current_price": ent.get("current_price"),
                        "change_pct": ent.get("change_pct"),
                        "acml_vol": ent.get("acml_vol"),
                        "updated_at": ent.get("updated_at"),
                    }
                if not ticks:
                    continue
                try:
                    await asyncio.wait_for(
                        s["ws"].send_json({"type": "nxt_prices", "ticks": ticks}),
                        timeout=2.0,
                    )
                except Exception:
                    stale_cids.append(s["cid"])
            if stale_cids:
                async with self._lock:
                    for cid in stale_cids:
                        self._clients.pop(cid, None)
            await asyncio.sleep(self._broadcast_interval_sec)


_NXT_PRICE_WS_HUB = _NxtPriceWsHub()


@router.websocket("/ws/nxt-prices")
async def ws_nxt_prices(websocket: WebSocket):
    if await reject_websocket_if_unauthorized(websocket):
        return
    client_id = await _NXT_PRICE_WS_HUB.connect(websocket)
    if client_id is None:
        return
    try:
        while True:
            raw = await websocket.receive_text()
            await _NXT_PRICE_WS_HUB.touch(client_id)
            try:
                payload = json.loads(raw or "{}")
            except Exception:
                payload = {}
            msg_type = str(payload.get("type") or "").strip().lower()
            if msg_type == "subscribe":
                codes = await _NXT_PRICE_WS_HUB.update_codes(client_id, payload.get("codes") or [])
                await websocket.send_json({"type": "subscribed", "codes": codes})
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            else:
                await websocket.send_json({"type": "error", "message": "unknown_message_type"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await _NXT_PRICE_WS_HUB.disconnect(client_id)


@router.websocket("/ws/news-prices")
async def ws_news_prices(websocket: WebSocket):
    if await reject_websocket_if_unauthorized(websocket):
        return
    client_id = await _NEWS_PRICE_WS_HUB.connect(websocket)
    if client_id is None:
        return
    try:
        while True:
            raw = await websocket.receive_text()
            await _NEWS_PRICE_WS_HUB.touch(client_id)
            try:
                payload = json.loads(raw or "{}")
            except Exception:
                payload = {}

            msg_type = str(payload.get("type") or "").strip().lower()
            if msg_type == "subscribe":
                codes = await _NEWS_PRICE_WS_HUB.update_codes(client_id, payload.get("codes") or [])
                stats = await _NEWS_PRICE_WS_HUB.get_stats()
                await websocket.send_json(
                    {
                        "type": "subscribed",
                        "codes": codes,
                        "broadcast": True,
                        "changed_only": False,
                        **stats,
                    }
                )
            elif msg_type == "ping":
                stats = await _NEWS_PRICE_WS_HUB.get_stats()
                await websocket.send_json({"type": "pong", **stats})
            else:
                await websocket.send_json({"type": "error", "message": "unknown_message_type"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await _NEWS_PRICE_WS_HUB.disconnect(client_id)


@router.websocket("/ws/stock-flow")
async def ws_stock_flow(websocket: WebSocket):
    if await reject_websocket_if_unauthorized(websocket):
        return
    client_id = await _STOCK_FLOW_WS_HUB.connect(websocket)
    if client_id is None:
        return
    try:
        while True:
            raw = await websocket.receive_text()
            await _STOCK_FLOW_WS_HUB.touch(client_id)
            try:
                payload = json.loads(raw or "{}")
            except Exception:
                payload = {}

            msg_type = str(payload.get("type") or "").strip().lower()
            if msg_type == "subscribe":
                code = await _STOCK_FLOW_WS_HUB.update_code(client_id, payload.get("code"))
                stats = await _STOCK_FLOW_WS_HUB.get_stats()
                await websocket.send_json({"type": "subscribed", "code": code, **stats})
            elif msg_type == "ping":
                stats = await _STOCK_FLOW_WS_HUB.get_stats()
                await websocket.send_json({"type": "pong", **stats})
            else:
                await websocket.send_json({"type": "error", "message": "unknown_message_type"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await _STOCK_FLOW_WS_HUB.disconnect(client_id)


# ─── GET /api/etf/kr ─────────────────────────────────────────
# kr_etf_master 테이블 → 한국 ETF 목록 + 괴리율 계산
@router.get("/api/etf/kr")
def get_kr_etf_list(limit: int = 50, sort_by: str = "amount"):
    if not _stocks_db_available():
        raise HTTPException(503, "DB 없음.")
    sort_map = {
        "amount":      "e.amount",
        "volume":      "e.volume",
        "market_cap":  "e.market_cap",
        "change_rate": "ABS(e.change_rate)",
        "name":        "e.name",
    }
    sort_col = sort_map.get(sort_by, "e.amount")
    conn = get_stocks_conn()
    try:
        rows = conn.execute(f"""
            SELECT
                e.code,
                e.name,
                e.category,
                e.price,
                e.change_amt,
                e.change_rate,
                e.nav,
                e.volume,
                e.amount,
                e.market_cap,
                e.asof_date,
                CASE
                    WHEN e.nav IS NOT NULL AND e.nav > 0
                    THEN ROUND(CAST(CAST((e.price - e.nav) AS FLOAT) / e.nav * 100 AS NUMERIC), 2)
                    ELSE NULL
                END AS premium_discount
            FROM kr_etf_master e
            WHERE e.price IS NOT NULL AND e.price > 0
            ORDER BY {sort_col} DESC NULLS LAST
            LIMIT ?
        """, (limit,)).fetchall()
        items = []
        for r in rows:
            d = dict(r) if hasattr(r, "keys") else {
                "code": r[0], "name": r[1], "category": r[2],
                "price": r[3], "change_amt": r[4], "change_rate": r[5],
                "nav": r[6], "volume": r[7], "amount": r[8],
                "market_cap": r[9], "asof_date": r[10], "premium_discount": r[11],
            }
            items.append(sanitize_floats(d))
        return {"items": items, "count": len(items), "sort_by": sort_by}
    finally:
        conn.close()


# ─── ETF 백그라운드 폴러 ─────────────────────────────────────
_ETF_BG_STARTED = False

def start_etf_background_poller():
    """서버 시작 시 1회 호출 — ETF 시세/NAV를 주기적으로 갱신."""
    global _ETF_BG_STARTED
    if _ETF_BG_STARTED:
        return
    _ETF_BG_STARTED = True

    _last_crawl_date = [None]

    def _refresh():
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
            from collectors.etf_collector import ETFCollector
            collector = ETFCollector()
            etfs, asof_date = collector.fetch_kr_etf_list()
            if etfs:
                collector.save_to_db(etfs, asof_date)
                logger.info("[etf-bg] ETF 시세 갱신: %s건", len(etfs))
            else:
                logger.debug("[etf-bg] ETF 데이터 없음")
        except Exception as e:
            logger.warning("[etf-bg] 시세 갱신 실패: %s", e)

    def _crawl_details():
        """일 1회 전체 ETF 메타+구성종목 크롤링"""
        today = time.strftime("%Y%m%d")
        if _last_crawl_date[0] == today:
            return
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
            from collectors.etf_collector import ETFCollector
            collector = ETFCollector()
            result = collector.crawl_all_etf_details(sleep_sec=0.3)
            _last_crawl_date[0] = today
            logger.info("[etf-bg] 전체 크롤링 완료: meta=%s, holdings=%s", result["meta"], result["holdings"])
        except Exception as e:
            logger.warning("[etf-bg] 크롤링 실패: %s", e)

    def _loop():
        # 서버 시작 직후 크롤링 폭주 방지 — 5분 지연 후 첫 사이클 실행.
        # ETF_POLLER_INITIAL_DELAY_SEC env 로 조정 가능(기본 300초).
        initial_delay = int(os.getenv("ETF_POLLER_INITIAL_DELAY_SEC", "300"))
        logger.info("[etf-bg] initial delay %ss before first refresh", initial_delay)
        time.sleep(max(3, initial_delay))
        _refresh()
        _crawl_details()  # 시작 시 1회
        while True:
            m = get_market_status() if callable(get_market_status) else "closed"
            interval = 600 if m == "open" else 3600
            time.sleep(interval)
            try:
                _refresh()
                _crawl_details()
            except Exception as e:
                logger.warning("[etf-bg] loop error: %s", e)

    threading.Thread(target=_loop, daemon=True, name="etf-bg-poller").start()
    logger.info("[etf-bg] ETF 폴러 시작 (장중 10분, 장외 60분)")


# ─── GET /api/etf/{code} — ETF 상세 (시세 + 일봉) ────────────
@router.get("/api/etf/{code}")
def get_etf_detail(code: str):
    if not _stocks_db_available():
        raise HTTPException(503, "DB 없음.")
    conn = get_stocks_conn()
    try:
        # 메타 칼럼 존재 여부 확인
        meta_cols = ""
        try:
            cols = {r[0] if not hasattr(r, "keys") else r["column_name"]
                    for r in conn.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_name='kr_etf_master'"
                    ).fetchall()}
            if "base_index" in cols:
                meta_cols = ", base_index, etf_type, listed_date, asset_manager, total_fee, return_1m, return_3m, return_6m, return_1y"
        except Exception:
            pass

        row = conn.execute(f"""
            SELECT code, name, category, price, change_amt, change_rate,
                   nav, volume, amount, market_cap, asof_date,
                   CASE WHEN nav IS NOT NULL AND nav > 0
                        THEN ROUND(CAST(CAST((price - nav) AS FLOAT) / nav * 100 AS NUMERIC), 2)
                        ELSE NULL END AS premium_discount
                   {meta_cols}
            FROM kr_etf_master WHERE code = ?
        """, (code,)).fetchone()
        if not row:
            raise HTTPException(404, f"ETF 없음: {code}")
        etf = dict(row) if hasattr(row, "keys") else dict(zip(
            ["code", "name", "category", "price", "change_amt", "change_rate",
             "nav", "volume", "amount", "market_cap", "asof_date", "premium_discount"],
            row
        ))
    finally:
        conn.close()

    # 일봉 30일 (KIS API)
    daily_ohlcv = []
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        from collectors.kis_api import KISCollector
        kis = KISCollector()
        raw = kis.get_daily_price(code)
        if isinstance(raw, list):
            daily_ohlcv = raw
    except Exception:
        pass

    etf["daily_ohlcv"] = daily_ohlcv
    return sanitize_floats(etf)


# ─── GET /api/etf/{code}/holdings — 구성종목 (DB 조회) ───────
@router.get("/api/etf/{code}/holdings")
def get_etf_holdings(code: str):
    """ETF 구성종목 — etf_holdings 테이블에서 조회 (배치 크롤링 결과)"""
    if not _stocks_db_available():
        raise HTTPException(503, "DB 없음.")
    conn = get_stocks_conn()
    try:
        rows = conn.execute("""
            SELECT stock_name, weight, shares, trade_date
            FROM etf_holdings
            WHERE etf_code = ?
            ORDER BY weight DESC
        """, (code,)).fetchall()
        holdings = [
            sanitize_floats({
                "name": r[0] if not hasattr(r, "keys") else r["stock_name"],
                "weight": r[1] if not hasattr(r, "keys") else r["weight"],
                "shares": r[2] if not hasattr(r, "keys") else r["shares"],
            })
            for r in rows
        ]
        return {"code": code, "holdings": holdings, "total": len(holdings)}
    except Exception as e:
        # 테이블 없을 때
        return {"code": code, "holdings": [], "total": 0, "error": str(e)}
    finally:
        conn.close()


# ─── GET /api/etf/{code}/similar — 유사 ETF ─────────────────
@router.get("/api/etf/{code}/similar")
def get_etf_similar(code: str, limit: int = 5):
    if not _stocks_db_available():
        raise HTTPException(503, "DB 없음.")
    conn = get_stocks_conn()
    try:
        cat_row = conn.execute("SELECT category FROM kr_etf_master WHERE code = ?", (code,)).fetchone()
        if not cat_row:
            raise HTTPException(404, f"ETF 없음: {code}")
        category = cat_row[0] if not hasattr(cat_row, "keys") else cat_row["category"]
        # total_fee 칼럼 존재 여부 확인
        fee_col = ""
        try:
            cols_q = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name='kr_etf_master'").fetchall()
            if any((r[0] if not hasattr(r, "keys") else r["column_name"]) == "total_fee" for r in cols_q):
                fee_col = ", total_fee"
        except Exception:
            try:
                if any(r[1] == "total_fee" for r in conn.execute("PRAGMA table_info(kr_etf_master)").fetchall()):
                    fee_col = ", total_fee"
            except Exception:
                pass

        rows = conn.execute(f"""
            SELECT code, name, category, price, change_amt, change_rate,
                   nav, volume, amount, market_cap,
                   CASE WHEN nav IS NOT NULL AND nav > 0
                        THEN ROUND(CAST(CAST((price - nav) AS FLOAT) / nav * 100 AS NUMERIC), 2)
                        ELSE NULL END AS premium_discount
                   {fee_col}
            FROM kr_etf_master
            WHERE category = ? AND code != ? AND price > 0
            ORDER BY amount DESC NULLS LAST
            LIMIT ?
        """, (category, code, limit)).fetchall()
        items = [sanitize_floats(dict(r) if hasattr(r, "keys") else dict(zip(
            ["code", "name", "category", "price", "change_amt", "change_rate",
             "nav", "volume", "amount", "market_cap", "premium_discount"] + (["total_fee"] if fee_col else []),
            r
        ))) for r in rows]
        return {"category": category, "items": items}
    finally:
        conn.close()


# ─── [3] GET /api/themes ─────────────────────────────────────
@router.get("/api/themes")
def get_themes(top: int = 15):
    mtime = _get_db_mtime()
    if _THEMES_CACHE["data"] and _THEMES_CACHE["mtime"] == mtime and _THEMES_CACHE.get("top") == top:
        return _THEMES_CACHE["data"]
    redis_key = f"stocks:themes:top:{top}:v{_mtime_token()}"
    redis_hit = redis_get_json(redis_key)
    if isinstance(redis_hit, dict):
        _THEMES_CACHE["data"] = redis_hit
        _THEMES_CACHE["mtime"] = mtime
        return redis_hit

    if not _stocks_db_available():
        raise HTTPException(503, "DB 없음.")
    conn = get_stocks_conn()
    try:
        # ── [1] 당일 거래대금 상위 N개 = "메가캡" 리스트 ──
        # 이들은 어느 테마에 들어가든 거래대금 총합을 지배함 (삼성전자, SK하이닉스 등)
        # hot ranking 점수 계산 시 이들 기여분을 제외하여 "진짜 모멘텀" 테마를 드러냄
        MEGA_TOP_N = 5
        mega_rows = conn.execute(
            "SELECT code FROM price_today WHERE trading_value IS NOT NULL "
            "ORDER BY trading_value DESC LIMIT ?",
            (MEGA_TOP_N,),
        ).fetchall()
        mega_codes = {r[0] for r in mega_rows}

        rows = conn.execute("""
            SELECT
                st.theme, st.code, s.name,
                COALESCE(pt.change_pct, 0)    AS pct,
                COALESCE(pt.trading_value, 0) AS tv
            FROM stock_themes st
            JOIN stocks s     ON s.code  = st.code
            LEFT JOIN price_today pt ON pt.code = st.code
        """).fetchall()

        # Aggregate in Python so we can apply the mega-cap filter to total_value.
        by_theme: dict[str, dict] = {}
        for r in rows:
            theme = r["theme"] if not isinstance(r, dict) else r["theme"]
            code  = r["code"]
            name  = r["name"]
            pct   = float(r["pct"] or 0)
            tv    = float(r["tv"] or 0)

            bucket = by_theme.setdefault(theme, {
                "name": theme,
                "stock_count": 0,
                "_sum_pct": 0.0,
                "total_value": 0.0,
                "total_value_ex_mega": 0.0,  # 메가캡 제외 거래대금 (hot 랭킹 기준)
                "members": [],
            })
            bucket["stock_count"] += 1
            bucket["_sum_pct"] += pct
            bucket["total_value"] += tv
            if code not in mega_codes:
                bucket["total_value_ex_mega"] += tv
            bucket["members"].append({"name": name, "pct": pct})

        themes = []
        for b in by_theme.values():
            n = max(1, b["stock_count"])
            themes.append({
                "name": b["name"],
                "stock_count": b["stock_count"],
                "avg_change": round(b["_sum_pct"] / n, 2),
                "total_value": int(b["total_value"]),
                "total_value_ex_mega": int(b["total_value_ex_mega"]),
                "members": b["members"],
            })

        rising = sorted(
            [t for t in themes if t["avg_change"] > 0],
            key=lambda t: t["avg_change"], reverse=True
        )[:top]

        # hot = 메가캡 제외 거래대금 기준. 모든 대장주가 모인 테마(HBM 등)는
        # 우연이 아니라 "메가캡 외 중소형주도 같이 붙어야" 상위.
        hot = sorted(
            themes,
            key=lambda t: t["total_value_ex_mega"], reverse=True,
        )[:top]

        res = {"rising": rising, "hot": hot, "mega_excluded_codes": sorted(mega_codes)}
        _THEMES_CACHE["data"] = res
        _THEMES_CACHE["mtime"] = mtime
        _THEMES_CACHE["top"] = top
        redis_set_json(redis_key, res, ttl_seconds=1800)
        return res
    finally:
        conn.close()


# ─── GET /api/indices ────────────────────────────────────────
@router.get("/api/indices")
def get_indices():
    import time
    now = time.time()
    cached = _INDICES_CACHE
    if cached["data"] and cached["fetched_at"] and now - cached["fetched_at"] < 60:
        return cached["data"]
    redis_key = "stocks:indices"
    redis_hit = redis_get_json(redis_key)
    if isinstance(redis_hit, dict):
        _INDICES_CACHE["data"] = redis_hit
        _INDICES_CACHE["fetched_at"] = now
        return redis_hit

    result = {}
    headers = {"User-Agent": "Mozilla/5.0"}
    for key, ticker in [("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")]:
        try:
            import urllib.request, json as _json
            req = urllib.request.Request(
                f"https://m.stock.naver.com/api/index/{ticker}/basic",
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                d = _json.loads(resp.read())
            result[key] = {
                "price": d.get("closePrice", "-"),
                "change_pct": float(d.get("fluctuationsRatio", 0)),
                "change_amt": d.get("compareToPreviousClosePrice", "0"),
            }
        except Exception:
            pass

    _INDICES_CACHE["data"] = result
    _INDICES_CACHE["fetched_at"] = now
    redis_set_json(redis_key, result, ttl_seconds=60)
    return result


# ─── GET /api/market-signal ──────────────────────────────────
@router.get("/api/market-signal")
def get_market_signal():
    mtime = _get_db_mtime()
    if _MARKET_SIGNAL_CACHE["data"] and _MARKET_SIGNAL_CACHE["mtime"] == mtime:
        return _MARKET_SIGNAL_CACHE["data"]
    redis_key = f"stocks:market_signal:v{_mtime_token()}"
    redis_hit = redis_get_json(redis_key)
    if isinstance(redis_hit, dict):
        _MARKET_SIGNAL_CACHE["data"] = redis_hit
        _MARKET_SIGNAL_CACHE["mtime"] = mtime
        return redis_hit

    if not _stocks_db_available():
        raise HTTPException(503, "DB 없음.")

    conn = get_stocks_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM price_today").fetchone()[0]
        up = conn.execute("SELECT COUNT(*) FROM price_today WHERE COALESCE(change_pct,0) > 0").fetchone()[0]
        down = conn.execute("SELECT COUNT(*) FROM price_today WHERE COALESCE(change_pct,0) < 0").fetchone()[0]
        flat = max(0, total - up - down)
        avg_change = conn.execute("SELECT AVG(COALESCE(change_pct,0)) FROM price_today").fetchone()[0] or 0.0

        short_avg = conn.execute("""
            SELECT AVG(ratio) FROM (
              SELECT COALESCE(short_selling_volume_ratio,0) AS ratio
              FROM short_data
              WHERE COALESCE(short_selling_volume_ratio,0) > 0
              ORDER BY ratio DESC
              LIMIT 100
            )
        """).fetchone()[0]
        short_avg = to_float(short_avg, 0.0)

        # ── 신호등/빚투 데이터 백그라운드 갱신 트리거 (5분 간격, 조회 시점에만) ──
        try:
            import time as _t
            # 조회 시점에만 갱신 + 오후 8시까지만 작동
            if _is_refresh_allowed() and (_t.time() - _state._LAST_SIGNAL_REFRESH_AT > 300):
                _state._LAST_SIGNAL_REFRESH_AT = _t.time()
                threading.Thread(target=_bg_refresh_signal_data, daemon=True).start()
        except Exception:
            pass

        credit_payload = load_credit_trend_payload(conn)
        latest_credit = (credit_payload or {}).get("latest") or {}
        rules = (credit_payload or {}).get("risk_rules") or {}
        total_credit_trillion = to_float(latest_credit.get("total_credit_trillion"), 0.0)
        kospi_credit_mil = to_float(latest_credit.get("kospi_credit_mil"), 0.0)
        kosdaq_credit_mil = to_float(latest_credit.get("kosdaq_credit_mil"), 0.0)
        kospi_ratio = to_float(latest_credit.get("kospi_ratio"), 0.0)
        kosdaq_ratio = to_float(latest_credit.get("kosdaq_ratio"), 0.0)

        breadth = (up / total) if total > 0 else 0.5
        momentum_score = max(-20.0, min(20.0, avg_change * 3.0))
        breadth_score = max(-25.0, min(25.0, (breadth - 0.5) * 50.0))

        warning_credit = to_float(rules.get("warning_total_credit_trillion"), 22.0)
        caution_credit = to_float(rules.get("psychological_total_credit_trillion"), 20.0)
        if total_credit_trillion >= warning_credit:
            credit_penalty = 22.0
        elif total_credit_trillion >= caution_credit:
            credit_penalty = 12.0
        else:
            credit_penalty = 0.0

        if short_avg >= 8:
            short_penalty = 14.0
        elif short_avg >= 5:
            short_penalty = 8.0
        elif short_avg >= 3:
            short_penalty = 3.0
        else:
            short_penalty = 0.0

        score = 50.0 + momentum_score + breadth_score - credit_penalty - short_penalty
        score = max(0.0, min(100.0, score))

        if score >= 65:
            signal = "BUY"
            signal_kor = "매수"
        elif score <= 40:
            signal = "RISK"
            signal_kor = "위험"
        else:
            signal = "WATCH"
            signal_kor = "관망"

        res = {
            "score": round(score),
            "signal": signal,
            "signal_kor": signal_kor,
            "up_count": up,
            "down_count": down,
            "flat_count": flat,
            "avg_change_pct": round(float(avg_change), 2),
            "short_top_avg_ratio": round(short_avg, 2),
            "total_credit_trillion": round(total_credit_trillion, 2) if total_credit_trillion else None,
            "kospi_credit_trillion": round(kospi_credit_mil / 1_000_000.0, 2) if kospi_credit_mil else None,
            "kosdaq_credit_trillion": round(kosdaq_credit_mil / 1_000_000.0, 2) if kosdaq_credit_mil else None,
            "kospi_ratio_pct": round(kospi_ratio, 2) if kospi_ratio else None,
            "kosdaq_ratio_pct": round(kosdaq_ratio, 2) if kosdaq_ratio else None,
            "kospi_anchor_ratio_pct": to_float(rules.get("kospi_anchor_ratio_pct"), 1.0),
            "kospi_warning_ratio_pct": to_float(rules.get("kospi_warning_ratio_pct"), 1.2),
            "kosdaq_anchor_ratio_pct": to_float(rules.get("kosdaq_anchor_ratio_pct"), 2.0),
            "kosdaq_warning_ratio_pct": to_float(rules.get("kosdaq_warning_ratio_pct"), 2.6),
            "credit_warning_trillion": warning_credit,
            "credit_caution_trillion": caution_credit,
            "risk_breakdown": {
                "momentum_score": round(momentum_score, 2),
                "breadth_score": round(breadth_score, 2),
                "credit_penalty": round(credit_penalty, 2),
                "short_penalty": round(short_penalty, 2),
            },
            "source": "server_db",
        }
        _MARKET_SIGNAL_CACHE["data"] = res
        _MARKET_SIGNAL_CACHE["mtime"] = mtime
        redis_set_json(redis_key, res, ttl_seconds=180)
        return res
    finally:
        conn.close()


# ─── GET /api/themes/{theme_name}/context ───────────────────
# 2026-05-11: `theme_name:path` 가 슬래시 포함 path 를 통째로 잡아먹어
# `/api/themes/전선/context` 같은 후행 path 가 404 되던 문제 fix.
# stocks_router 안에서 `theme_name:path` 보다 **위에** 정의해 매칭 우선권 확보.
@router.get("/api/themes/{theme_name:path}/context")
def get_theme_daily_context_route(theme_name: str):
    import json as _json
    conn = get_stocks_conn()
    try:
        row = conn.execute(
            "SELECT theme, context_date, context, drivers_json, tone, "
            "avg_change_pct, stock_count, model, status, updated_at "
            "FROM theme_daily_context "
            "WHERE theme=%s AND status='ok' "
            "ORDER BY context_date DESC, updated_at DESC LIMIT 1",
            (theme_name,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"theme": theme_name, "available": False}
    drivers = []
    try:
        drivers = _json.loads(row[3] or "[]")
    except Exception:
        pass
    def _jsonable(v):
        if v is None: return None
        try: return str(v) if not isinstance(v, (str, int, float, bool)) else v
        except Exception: return str(v)
    return {
        "theme": row[0],
        "available": True,
        "context_date": _jsonable(row[1]),
        "context": row[2] or "",
        "drivers": drivers,
        "tone": row[4] or "",
        "avg_change_pct": float(row[5]) if row[5] is not None else None,
        "stock_count": int(row[6]) if row[6] is not None else None,
        "model": row[7] or "",
        "updated_at": _jsonable(row[9]),
    }


# ─── GET /api/themes/{theme_name} ───────────────────────────
# 2026-05-08: theme_name 에 슬래시 포함 가능 (예: "방위산업/전쟁 및 테러", "2차전지(소재/부품)").
# FastAPI path 컨버터로 슬래시도 path param 으로 받음.
# ⚠️ `/context` 라우트가 위에 먼저 등록되어 있어야 함 (FastAPI 는 등록 순서대로 매칭).
@router.get("/api/themes/{theme_name:path}")
def get_theme_detail(theme_name: str):
    redis_key = f"stocks:theme_detail:{_search_key_token(theme_name)}:v{_mtime_token()}"
    redis_hit = redis_get_json(redis_key)
    if isinstance(redis_hit, dict):
        _apply_local_logo_urls_list(redis_hit.get("stocks"))
        return redis_hit
    if not _stocks_db_available():
        raise HTTPException(503, "DB 없음.")
    conn = get_stocks_conn()
    try:
        # ── 1. 종목 기본 데이터 ──────────────────────────────
        rows = conn.execute("""
            SELECT
                s.code, s.name, s.market,
                pt.current_price, pt.change_pct, pt.change_amt,
                pt.trading_value, pt.trading_volume, pt.market_cap,
                ta.rs_score, ta.ma5, ta.ma20, ta.returns_json,
                ta.avg_20d_trading_value,
                it.foreign_net, it.institution_net, it.individual_net,
                ai.sentiment_phase_kor, ai.human_indicator_score,
                ai.contrarian_signal_kor, ai.summary
            FROM stock_themes st
            JOIN stocks s          ON s.code  = st.code
            LEFT JOIN price_today  pt ON pt.code = st.code
            LEFT JOIN tech_analysis ta ON ta.code = st.code
            LEFT JOIN investor_today it ON it.code = st.code
            LEFT JOIN ai_analysis   ai ON ai.code  = st.code
            WHERE st.theme = ?
            ORDER BY COALESCE(pt.trading_value, 0) DESC
        """, (theme_name,)).fetchall()

        if not rows:
            raise HTTPException(404, f"테마 없음: {theme_name}")

        codes = [r["code"] for r in rows]

        # ── 2. 종목별 20일 OHLCV ─────────────────────────────
        cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        ohlcv_rows = conn.execute(f"""
            SELECT code, date, open, high, low, close, volume
            FROM price_daily
            WHERE code IN ({','.join('?' * len(codes))})
              AND date >= ?
            ORDER BY code, date
        """, (*codes, cutoff)).fetchall()

        # code → sorted daily list
        ohlcv_map: dict = {}
        for r in ohlcv_rows:
            c = r["code"]
            if c not in ohlcv_map:
                ohlcv_map[c] = []
            ohlcv_map[c].append({
                "date":  r["date"][:4] + "-" + r["date"][4:6] + "-" + r["date"][6:],
                "open":  r["open"],  "high": r["high"],
                "low":   r["low"],   "close": r["close"],
                "volume": r["volume"],
            })

        # ── 3. 테마 지수 차트 계산 ───────────────────────────
        date_pct_sum:   dict = {}
        date_pct_count: dict = {}

        for code, days in ohlcv_map.items():
            if not days:
                continue
            base_close = days[0]["close"]
            if not base_close:
                continue
            for d in days:
                dt = d["date"]
                pct = (d["close"] - base_close) / base_close * 100
                date_pct_sum[dt]   = date_pct_sum.get(dt, 0.0) + pct
                date_pct_count[dt] = date_pct_count.get(dt, 0) + 1

        theme_chart = []
        for dt in sorted(date_pct_sum):
            cnt = date_pct_count[dt]
            theme_chart.append({
                "date":     dt,
                "avg_pct":  round(date_pct_sum[dt] / cnt, 2) if cnt else 0,
                "stock_count": cnt,
            })

        # ── 4. 테마 집계 통계 ────────────────────────────────
        returns_list = [jl(r["returns_json"]) or {} for r in rows]
        def _avg(key):
            vals = [float(rv.get(key, 0)) for rv in returns_list if rv.get(key) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        total_value = sum(r["trading_value"] or 0 for r in rows)
        stats = {
            "stock_count":     len(rows),
            "avg_change_1d":   _avg("1d"),
            "avg_change_5d":   _avg("5d"),
            "avg_change_20d":  _avg("20d"),
            "total_value_today": total_value,
        }

        # ── 5. 종목 리스트 조립 ──────────────────────────────
        stocks = []
        for r in rows:
            code = r["code"]
            stocks.append({
                "code":                 code,
                "name":                 r["name"],
                "market":               r["market"],
                "current_price":        r["current_price"],
                "change_pct":           r["change_pct"],
                "change_amt":           r["change_amt"],
                "trading_value":        r["trading_value"],
                "trading_volume":       r["trading_volume"],
                "market_cap":           r["market_cap"],
                "rs_score":             r["rs_score"],
                "ma5":                  r["ma5"],
                "ma20":                 r["ma20"],
                "avg_20d_trading_value": r["avg_20d_trading_value"],
                "returns":              jl(r["returns_json"]) or {},
                "foreign_net":          r["foreign_net"],
                "institution_net":      r["institution_net"],
                "individual_net":       r["individual_net"],
                "sentiment_phase_kor":  r["sentiment_phase_kor"],
                "human_indicator_score": r["human_indicator_score"],
                "contrarian_signal_kor": r["contrarian_signal_kor"],
                "summary":              r["summary"],
                "daily_ohlcv":          ohlcv_map.get(code, []),
            })

        result = {
            "theme":       theme_name,
            "stats":       stats,
            "theme_chart": theme_chart,
            "stocks":      stocks,
        }
        _apply_local_logo_urls_list(result.get("stocks"))
        redis_set_json(redis_key, result, ttl_seconds=300)
        return result
    finally:
        conn.close()


