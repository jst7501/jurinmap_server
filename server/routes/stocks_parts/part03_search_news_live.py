# ─── GET /api/stocks/search ──────────────────────────────────
@router.get("/api/stocks/search")
def search_stocks(q: str = ""):
    if not q:
        return {}
    redis_key = f"stocks:search:{_search_key_token(q)}:v{_mtime_token()}"
    redis_hit = redis_get_json(redis_key)
    if isinstance(redis_hit, dict):
        _apply_local_logo_urls(redis_hit)
        return redis_hit
    conn = get_stocks_conn()
    try:
        query = """
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
        WHERE s.name ILIKE ? OR s.code ILIKE ?
        ORDER BY pt.trading_value DESC NULLS LAST
        LIMIT 50
        """
        rows = conn.execute(query, (f"%{q}%", f"%{q}%")).fetchall()

        result = {}
        for r in rows:
            r = dict(r)
            code = r.pop("code")
            # JSON 컬럼 파싱
            r["returns"]              = jl(r.pop("returns_json", None))
            r["top_euphoria"]         = jl(r.pop("top_euphoria_json", None))
            r["top_despair"]          = jl(r.pop("top_despair_json", None))
            r["sentiment_keywords"]   = jl(r.pop("sentiment_keywords_json", None))
            r["issue_keywords"]       = jl(r.pop("issue_keywords_json", None))
            local_logo_url = _get_local_logo_url(code)
            if local_logo_url:
                r["logo_local_url"] = local_logo_url
            result[code] = r

        _apply_local_logo_urls(result)
        redis_set_json(redis_key, result, ttl_seconds=180)
        return result
    finally:
        conn.close()


# 상세 캐시: 메모리 + redis(active/latest/stale) 다층
_STOCK_DETAIL_MEM_CACHE: dict[str, dict] = {}
_STOCK_DETAIL_MEM_LOCK = threading.Lock()
_STOCK_DETAIL_MEM_MAX = max(200, int(os.getenv("STOCK_DETAIL_MEM_MAX", "1200")))
_STOCK_DETAIL_MEM_TTL_OPEN_SEC = max(5, int(os.getenv("STOCK_DETAIL_MEM_TTL_OPEN_SEC", "25")))
_STOCK_DETAIL_MEM_TTL_CLOSED_SEC = max(10, int(os.getenv("STOCK_DETAIL_MEM_TTL_CLOSED_SEC", "90")))
_STOCK_DETAIL_STALE_TTL_SEC = max(300, int(os.getenv("STOCK_DETAIL_STALE_TTL_SEC", "43200")))
_STOCK_DETAIL_MAX_STALE_SEC = max(30, int(os.getenv("STOCK_DETAIL_MAX_STALE_SEC", "300")))
_STOCK_DETAIL_REFRESHING: set[str] = set()
_STOCK_DETAIL_REFRESH_LOCK = threading.Lock()
_STOCK_DETAIL_BYPASS_TLS = threading.local()


def _stock_detail_mem_ttl_sec() -> int:
    return _STOCK_DETAIL_MEM_TTL_OPEN_SEC if _market_status() == "open" else _STOCK_DETAIL_MEM_TTL_CLOSED_SEC


def _stock_detail_mem_get(code: str) -> tuple[dict | None, float]:
    with _STOCK_DETAIL_MEM_LOCK:
        entry = _STOCK_DETAIL_MEM_CACHE.get(code)
        if not isinstance(entry, dict):
            return None, 0.0
        data = entry.get("data")
        ts = float(entry.get("ts") or 0.0)
    return (data if isinstance(data, dict) else None), ts


def _stock_detail_mem_put(code: str, payload: dict) -> None:
    now_ts = time.time()
    with _STOCK_DETAIL_MEM_LOCK:
        _STOCK_DETAIL_MEM_CACHE[code] = {"data": payload, "ts": now_ts}
        if len(_STOCK_DETAIL_MEM_CACHE) > _STOCK_DETAIL_MEM_MAX:
            oldest_key = min(
                _STOCK_DETAIL_MEM_CACHE.items(),
                key=lambda kv: float((kv[1] or {}).get("ts") or 0.0),
            )[0]
            if oldest_key != code:
                _STOCK_DETAIL_MEM_CACHE.pop(oldest_key, None)


def _stock_detail_redis_keys(code: str, token: str | int) -> tuple[str, str, str]:
    active_key = f"stocks:detail:{code}:v{token}"
    latest_key = f"stocks:detail:latest:{code}:v1"
    stale_key = f"stocks:detail:stale:{code}:v1"
    return active_key, latest_key, stale_key


def _stock_detail_redis_get_best(active_key: str, latest_key: str, stale_key: str) -> tuple[str, dict | None]:
    hit = redis_get_json(active_key)
    if isinstance(hit, dict):
        return "active", hit
    hit = redis_get_json(latest_key)
    if isinstance(hit, dict):
        return "latest", hit
    hit = redis_get_json(stale_key)
    if isinstance(hit, dict):
        return "stale", hit
    return "miss", None


def _stock_detail_apply_local_logo(payload: dict, code: str) -> dict:
    out = copy.deepcopy(payload)
    local_logo_url = _get_local_logo_url(code)
    if local_logo_url:
        out["logo_local_url"] = local_logo_url
    else:
        out.pop("logo_local_url", None)
    return out


def _stock_detail_cache_bypass_enabled(code: str) -> bool:
    codes = getattr(_STOCK_DETAIL_BYPASS_TLS, "codes", None)
    return isinstance(codes, set) and code in codes


def _stock_detail_call_with_cache_bypass(code: str, fn):
    prev = getattr(_STOCK_DETAIL_BYPASS_TLS, "codes", None)
    codes = set(prev) if isinstance(prev, set) else set()
    codes.add(code)
    _STOCK_DETAIL_BYPASS_TLS.codes = codes
    try:
        return fn()
    finally:
        codes.discard(code)
        if prev is None:
            try:
                delattr(_STOCK_DETAIL_BYPASS_TLS, "codes")
            except Exception:
                pass
        else:
            _STOCK_DETAIL_BYPASS_TLS.codes = set(prev)


def _stock_detail_refresh_async(code: str) -> bool:
    with _STOCK_DETAIL_REFRESH_LOCK:
        if code in _STOCK_DETAIL_REFRESHING:
            return False
        _STOCK_DETAIL_REFRESHING.add(code)

    def _worker() -> None:
        try:
            _stock_detail_call_with_cache_bypass(code, lambda: get_stock_detail(code))
        except Exception as e:
            logger.debug("[stock-detail-refresh] code=%s err=%s", code, e)
        finally:
            with _STOCK_DETAIL_REFRESH_LOCK:
                _STOCK_DETAIL_REFRESHING.discard(code)

    threading.Thread(
        target=_worker,
        daemon=True,
        name=f"stock-detail-refresh-{code}",
    ).start()
    return True


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _to_float_safe(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _fmt_signed_pct(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def _fmt_krw_rough(value: float) -> str:
    n = abs(value)
    sign = "+" if value >= 0 else "-"
    if n >= 100_000_000:
        return f"{sign}{n / 100_000_000:.1f}억원"
    if n >= 10_000:
        return f"{sign}{n / 10_000:.1f}만원"
    return f"{sign}{int(n):,}원"


def _normalize_probabilities(raw: list[float]) -> list[int]:
    vals = [max(1.0, float(v)) for v in raw]
    total = sum(vals) or 1.0
    scaled = [int(round(v * 100.0 / total)) for v in vals]
    gap = 100 - sum(scaled)
    if gap != 0:
        idx = max(range(len(scaled)), key=lambda i: vals[i])
        scaled[idx] += gap
    return scaled


def _build_tomorrow_scenarios(payload: dict) -> dict:
    pt = payload.get("price_today") or {}
    ta = payload.get("tech_analysis") or {}
    inv = payload.get("investor_today") or {}
    sd = payload.get("short_data") or {}

    change_pct = _to_float_safe(pt.get("change_pct"), 0.0)
    rs_score = _to_float_safe(ta.get("rs_score"), 50.0)
    div5 = _to_float_safe(ta.get("div_5"), 0.0)
    div20 = _to_float_safe(ta.get("div_20"), 0.0)
    short_ratio = _to_float_safe(sd.get("short_selling_volume_ratio"), 0.0)
    foreign_net = _to_float_safe(inv.get("foreign_net"), 0.0)
    institution_net = _to_float_safe(inv.get("institution_net"), 0.0)
    flow_sum = foreign_net + institution_net

    trend_factor = _clamp((rs_score - 50.0) / 10.0, -3.0, 3.0)
    momentum_factor = _clamp(change_pct / 2.0, -3.0, 3.0)
    flow_factor = _clamp(flow_sum / 5_000_000_000.0, -2.0, 2.0)
    short_factor = _clamp(short_ratio / 5.0, 0.0, 3.0)
    ma_bias = _clamp((div5 + div20) / 4.0, -2.0, 2.0)

    bull_raw = 34.0 + trend_factor * 7.5 + momentum_factor * 6.0 + flow_factor * 8.0 + ma_bias * 4.0 - short_factor * 3.0
    bear_raw = 34.0 - trend_factor * 7.5 - momentum_factor * 6.0 - flow_factor * 6.0 - ma_bias * 3.0 + short_factor * 5.0
    side_raw = 32.0 - abs(momentum_factor) * 4.0 - abs(flow_factor) * 3.0 + (1.2 if abs(change_pct) < 1.2 else 0.0)

    p_bull, p_side, p_bear = _normalize_probabilities([bull_raw, side_raw, bear_raw])

    evidence_common = [
        f"시장 대비 힘(RS) {rs_score:.1f}점 (평균 50점), 이평선 대비 이격 {div5:.2f}% / {div20:.2f}%",
        f"심리상태: {'과열' if div5 > 10 else ('침체' if div5 < -10 else '적정')} 구간 ({div5:.1f}%)",
    ]

    scenarios = [
        {
            "key": "bull",
            "title": "상승 조건 시나리오",
            "summary": "시초가 지지 후 고점 돌파 흐름이 나오면 상승 추세가 확장될 데이터적 근거가 있습니다.",
            "action_hint": "모멘텀 강화",
            "triggers": [
                "시가 형성 후 30분 고점 상향 돌파",
                "돌파 구간에서 대량 체결량 동반",
            ],
            "evidence": [
                evidence_common[0],
                f"당일 등락률 { _fmt_signed_pct(change_pct) }로 모멘텀 포착",
            ],
        },
        {
            "key": "base",
            "title": "박스권 흐름 시나리오",
            "summary": "추가 수급 유입 전까지는 에너지 응축 기간으로, 박스권 내 등락 가능성이 높습니다.",
            "action_hint": "방향성 탐색",
            "triggers": [
                "전일 종가 대비 ±1.5% 내 좁은 변동폭",
                "외인·기관 수급이 매수/매도 균형 상태",
            ],
            "evidence": [
                evidence_common[0],
                "수급 신호가 엇갈려 있어 명확한 방향성 확증 부족",
            ],
        },
        {
            "key": "bear",
            "title": "하락 리스크 시나리오",
            "summary": "수급 이탈과 주요 지지선 붕괴가 겹치면 단기 변동성 확대 리스크가 존재합니다.",
            "action_hint": "변동성 주의",
            "triggers": [
                "시초가 또는 직전 저점 이탈 시",
                "외국인·기관의 동반 순매도 우위",
            ],
            "evidence": [
                f"외인·기관 합계 수급 { _fmt_krw_rough(flow_sum) }, 공매도 비중 {short_ratio:.2f}%",
                f"당일 변동폭 { _fmt_signed_pct(change_pct) } 구간에서 하방 압력 누적",
            ],
        },
    ]

    # 내부적으로만 순위 결정용으로 사용
    probs = [p_bull, p_side, p_bear]
    max_idx = probs.index(max(probs))
    quick = scenarios[max_idx]

    return {
        "quick_conclusion": {
            "action": quick.get("action_hint"),
            "title": quick.get("title"),
            "message": f"현재 데이터는 '{quick.get('action_hint')}' 상태를 우선 나타내고 있습니다.",
        },
        "items": scenarios,
        "basis": "price_today + tech_analysis + investor_today + short_data",
        "disclaimer": "본 정보는 통계 데이터 기반 참고용이며, 특정 종목의 매수·매도를 권유하지 않습니다. 최종 투자 판단은 본인에게 있습니다.",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ─── GET /api/stocks/{code} ──────────────────────────────────
@router.get("/api/stocks/{code}")
def get_stock_detail(code: str):
    if not _stocks_db_available():
        raise HTTPException(503, "DB not found.")
    if not _is_valid_stock_code(code):
        raise HTTPException(400, f"invalid stock code: {code}")
    try:
        _touch_focus_codes([code], ttl_sec=240.0)
    except Exception:
        pass
    # Kick auxiliary refresh early so detail widgets hit warm cache.
    try:
        _queue_scalping_refresh(code)
        _queue_orderbook_refresh(code)
    except Exception:
        pass

    # Postgres mode: _mtime_token rolls every 30s → 비인기 종목 매번 캐시 미스
    # 종목 상세는 10분 단위 토큰 사용 (데이터 갱신 주기 대비 충분)
    detail_token = int(time.time() // 600)
    _detail_ttl = 700
    active_key, latest_key, stale_key = _stock_detail_redis_keys(code, detail_token)

    bypass_cache = _stock_detail_cache_bypass_enabled(code)

    if not bypass_cache:
        mem_data, mem_ts = _stock_detail_mem_get(code)
        if isinstance(mem_data, dict):
            age = time.time() - mem_ts
            if age < _stock_detail_mem_ttl_sec():
                return sanitize_floats(_stock_detail_apply_local_logo(mem_data, code))
            if age < _STOCK_DETAIL_MAX_STALE_SEC:
                _stock_detail_refresh_async(code)
                payload = _stock_detail_apply_local_logo(mem_data, code)
                payload["_stale"] = True
                payload["snapshot_age_sec"] = int(max(0.0, age))
                return sanitize_floats(payload)

        cache_source, redis_hit = _stock_detail_redis_get_best(active_key, latest_key, stale_key)
        if isinstance(redis_hit, dict):
            _stock_detail_mem_put(code, redis_hit)
            payload = _stock_detail_apply_local_logo(redis_hit, code)
            if cache_source in ("latest", "stale"):
                payload["_stale"] = True
                _stock_detail_refresh_async(code)
            return sanitize_floats(payload)

    conn = get_stocks_conn()
    try:
        # ── 단일 행 테이블 전부 한 번에 JOIN (15개 쿼리 → 1개) ──
        row1 = conn.execute("""
            SELECT
                s.code, s.name, s.market,
                pt.current_price, pt.change_pct, pt.change_amt,
                pt.trading_value, pt.trading_volume, pt.volume_turnover_rate,
                pt.market_cap, pt.foreign_hold_pct,
                pt.listed_shares AS pt_listed_shares,
                pt.per AS pt_per, pt.pbr AS pt_pbr, pt.eps AS pt_eps,
                pt.raw_json AS pt_raw_json,
                ne.item_logo_url, ne.item_logo_png_url,
                ne.high_52w, ne.low_52w, ne.target_price, ne.consensus_analyst_count,
                ne.dividend_yield, ne.investment_opinion_label, ne.per_ttm, ne.est_per,
                ne.investor_trend_7d_json, ne.broker_top_json, ne.peer_compare_json,
                it.foreign_net, it.institution_net, it.individual_net,
                it.full_json AS it_full_json,
                sd.short_enabled, sd.short_selling_volume_ratio, sd.updated_at AS sd_updated_at,
                cd.rate_today AS cd_rate_today, cd.daily_json AS cd_daily_json,
                fr.roe, fr.roa, fr.debt_ratio, fr.retention_ratio,
                fr.eps AS fr_eps, fr.bps,
                ds.data_json AS ds_data_json,
                bs.score AS bs_score, bs.mood, bs.grade,
                bs.top_euphoria_json, bs.top_despair_json, bs.hot_posts_json,
                ai.sentiment_phase_kor, ai.human_indicator_score,
                ai.contrarian_signal_kor, ai.summary AS ai_summary,
                ai.sentiment_keywords_json, ai.issue_keywords_json,
                ta.ma5, ta.ma20, ta.ma60, ta.rs_score,
                ta.div_5, ta.div_20, ta.avg_20d_trading_value,
                ta.volume_profile_json, ta.returns_json,
                op.full_json AS op_full_json,
                cas.one_liner        AS cas_one_liner,
                cas.business_summary AS cas_business_summary,
                cas.products         AS cas_products,
                cas.revenue_mix      AS cas_revenue_mix,
                cas.sector           AS cas_sector,
                cas.themes           AS cas_themes,
                cas.investor_point   AS cas_investor_point,
                cas.full_summary     AS cas_full_summary,
                cas.sources_json     AS cas_sources_json,
                cas.status           AS cas_status,
                cas.updated_at       AS cas_updated_at
            FROM stocks s
            LEFT JOIN price_today         pt  ON pt.code = s.code
            LEFT JOIN naver_extended      ne  ON ne.code = s.code
            LEFT JOIN investor_today      it  ON it.code = s.code
            LEFT JOIN short_data          sd  ON sd.code = s.code
            LEFT JOIN credit_data         cd  ON cd.code = s.code
            LEFT JOIN finance_ratio       fr  ON fr.code = s.code
            LEFT JOIN dart_shareholders   ds  ON ds.code = s.code
            LEFT JOIN board_sentiment     bs  ON bs.code = s.code
            LEFT JOIN ai_analysis         ai  ON ai.code = s.code
            LEFT JOIN tech_analysis       ta  ON ta.code = s.code
            LEFT JOIN ownership_summary   op  ON op.code = s.code
            LEFT JOIN company_ai_summary  cas ON cas.code = s.code
            WHERE s.code = ?
        """, (code,)).fetchone()

        if not row1:
            raise HTTPException(404, f"종목 없음: {code}")

        r = dict(row1)

        # ── stocks 기본 ──
        # stocks 테이블 컬럼만 추출 (code, name, market만 존재)
        stock_fields = ["code", "name", "market"]
        d = {k: r[k] for k in stock_fields if k in r}

        local_logo_url = _get_local_logo_url(code)
        if local_logo_url:
            d["logo_local_url"] = local_logo_url

        # ── naver_extended ──
        if r.get("item_logo_url"):
            d["logo_url"]     = r["item_logo_url"]
            d["logo_png_url"] = r["item_logo_png_url"]
            d["naver_financials"] = {
                "high_52w":                 r["high_52w"],
                "low_52w":                  r["low_52w"],
                "target_price":             r["target_price"],
                "consensus_analyst_count":  r["consensus_analyst_count"],
                "dividend_yield":           r["dividend_yield"],
                "investment_opinion_label": r["investment_opinion_label"],
                "per_ttm":                  r["per_ttm"],
                "est_per":                  r["est_per"],
                "investor_trend_7d":        jl(r["investor_trend_7d_json"]) or [],
                "broker_top":               jl(r["broker_top_json"]) or [],
                "peer_compare":             jl(r["peer_compare_json"]) or [],
            }

        # ── company_ai_summary (회사 정체성 / 주린이 소개) ──
        if r.get("cas_status") == "ok" and r.get("cas_one_liner"):
            d["ai_summary"] = {
                "one_liner":        r.get("cas_one_liner"),
                "business_summary": r.get("cas_business_summary"),
                "products":         r.get("cas_products"),
                "revenue_mix":      r.get("cas_revenue_mix"),
                "sector":           r.get("cas_sector"),
                "themes":           r.get("cas_themes"),
                "investor_point":   r.get("cas_investor_point"),
                "full_summary":     r.get("cas_full_summary"),
                "sources":          jl(r.get("cas_sources_json")) or [],
                "updated_at":       r.get("cas_updated_at"),
            }

        # ── price_today ──
        if r.get("current_price"):
            pt_raw = jl(r["pt_raw_json"])
            d["price_today"] = {
                "code": code,
                "current_price":       r["current_price"],
                "change_pct":          r["change_pct"],
                "change_amt":          r["change_amt"],
                "trading_value":       r["trading_value"],
                "trading_volume":      r["trading_volume"],
                "volume_turnover_rate": r["volume_turnover_rate"],
                "market_cap":          r["market_cap"],
                "foreign_hold_pct":    r.get("foreign_hold_pct"),
                "listed_shares":       r.get("pt_listed_shares"),
                "per":                 r.get("pt_per"),
                "pbr":                 r.get("pt_pbr"),
                "eps":                 r.get("pt_eps"),
                "_raw":                pt_raw,
            }

        # ── investor_today ──
        if r.get("it_full_json") or r.get("foreign_net") is not None:
            d["investor_today"] = jl(r["it_full_json"]) or {
                "foreign_net": r["foreign_net"],
                "institution_net": r["institution_net"],
                "individual_net": r["individual_net"],
            }

        # ── short_data ──
        if r.get("sd_updated_at") is not None or r.get("short_selling_volume_ratio") is not None:
            ratio = r["short_selling_volume_ratio"]
            enabled = bool(r["short_enabled"]) if r["short_enabled"] is not None else True
            if not ratio or to_float(ratio, 0.0) <= 0:
                fallback = get_short_ratio_map().get(code)
                d["short_data"] = {
                    "code": code, "short_enabled": enabled,
                    "short_selling_volume_ratio": fallback,
                    "updated_at": r["sd_updated_at"],
                    "ratio_source": "csv_fallback" if fallback is not None else "db",
                }
            else:
                d["short_data"] = {
                    "code": code, "short_enabled": enabled,
                    "short_selling_volume_ratio": ratio,
                    "updated_at": r["sd_updated_at"],
                    "ratio_source": "db",
                }
        else:
            fallback = get_short_ratio_map().get(code)
            if fallback is not None:
                d["short_data"] = {
                    "code": code, "short_enabled": True,
                    "short_selling_volume_ratio": fallback,
                    "updated_at": None, "ratio_source": "csv_fallback",
                }

        # ── credit_data ──
        raw_credit_rate = to_float(((d.get("price_today") or {}).get("_raw") or {}).get("whol_loan_rmnd_rate"), 0.0)
        if r.get("cd_rate_today") is not None:
            cr = to_float(r["cd_rate_today"], 0.0)
            d["credit_data"] = {
                "rate_today": cr if cr > 0 else raw_credit_rate,
                "daily": jl(r["cd_daily_json"]) or [],
                "rate_source": "db" if cr > 0 else ("price_today_raw" if raw_credit_rate > 0 else "db"),
            }
        else:
            d["credit_data"] = {
                "rate_today": raw_credit_rate,
                "daily": [],
                "rate_source": "price_today_raw" if raw_credit_rate > 0 else "db",
            }
        d["credit_data"]["individual_balance_supported"] = False
        d["credit_data"]["individual_balance_note"] = "현재 API는 종목별 신용비율(rate)만 제공하며, 신용잔고 금액은 미제공입니다."

        # ── finance_ratio ──
        if r.get("debt_ratio") is not None or r.get("roe") is not None:
            d["finance_ratio"] = {
                "roe":            r.get("roe"),
                "roa":            r.get("roa"),
                "debt_ratio":     r.get("debt_ratio"),
                "retention_ratio": r.get("retention_ratio"),
                "eps":            r.get("fr_eps"),
                "bps":            r.get("bps"),
                # per/pbr/eps from price_today (more up-to-date)
                "per":            r.get("pt_per"),
                "pbr":            r.get("pt_pbr"),
            }

        # ── dart_shareholders ──
        d["dart_shareholders"] = jl(r["ds_data_json"]) if r.get("ds_data_json") else {}

        # ── board_sentiment ──
        if r.get("bs_score") is not None:
            d["board_sentiment"] = {
                "score": r["bs_score"], "mood": r["mood"], "grade": r["grade"],
                "top_euphoria":  jl(r["top_euphoria_json"]),
                "top_despair":   jl(r["top_despair_json"]),
                "top_hot_posts": jl(r["hot_posts_json"]),
            }

        # ── ai_analysis ──
        if r.get("sentiment_phase_kor") or r.get("ai_summary"):
            d["ai_analysis"] = {
                "sentiment_phase_kor":   r["sentiment_phase_kor"],
                "human_indicator_score": r["human_indicator_score"],
                "contrarian_signal_kor": r["contrarian_signal_kor"],
                "summary":               r["ai_summary"],
                "sentiment_keywords":    jl(r["sentiment_keywords_json"]),
                "issue_keywords":        jl(r["issue_keywords_json"]),
            }
            if not d.get("themes"):
                themes = []
                for kw in (jl(r["issue_keywords_json"]) or []):
                    w = str(kw.get("word", "") if isinstance(kw, dict) else kw).strip()
                    if w and w not in themes:
                        themes.append(w)
                d["themes"] = themes[:8]

        # ── tech_analysis ──
        if r.get("ma5") is not None or r.get("rs_score") is not None:
            d["tech_analysis"] = {
                "ma5": r["ma5"], "ma20": r["ma20"], "ma60": r["ma60"],
                "rs_score": r["rs_score"],
                "div_5": r["div_5"], "div_20": r["div_20"],
                "avg_20d_trading_value": r["avg_20d_trading_value"],
                "volume_profile": jl(r["volume_profile_json"]),
                "returns":        jl(r["returns_json"]),
            }

        # ── ownership_summary ──
        d["ownership_summary_pct"] = jl(r["op_full_json"]) if r.get("op_full_json") else {}

        # ── 멀티 로우: 4개 쿼리만 남음 ──
        # LIMIT을 120일로 상향 (차트는 평균 60-120일 보여주므로)
        rows = conn.execute("""
            SELECT date,open,high,low,close,volume,trading_value,credit_rate
            FROM price_daily WHERE code=? ORDER BY date DESC LIMIT 120
        """, (code,)).fetchall()
        d["daily_ohlcv"] = [dict(r2) for r2 in rows]

        rows = conn.execute("""
            SELECT * FROM investor_flow WHERE code=? ORDER BY date DESC LIMIT 20
        """, (code,)).fetchall()
        investor_rows = [dict(r2) for r2 in rows]
        d["investor_5d"]  = investor_rows[:5]
        d["investor_20d"] = investor_rows

        rows = conn.execute("""
            SELECT date,program_buy,program_sell,program_net,program_net_amt
            FROM program_trade WHERE code=? ORDER BY date DESC LIMIT 5
        """, (code,)).fetchall()
        d["program_5d"] = [dict(r2) for r2 in rows]

        rows = conn.execute("""
            SELECT date,title,type FROM dart_disclosures WHERE code=? ORDER BY date DESC LIMIT 5
        """, (code,)).fetchall()
        d["dart_disclosures"] = [dict(r2) for r2 in rows]

        # 최근 3개년 핵심 재무계정 (DART fnlttSinglAcntAll 기반) → 프론트 FinancialTruthWidget이
        # Object.entries(row).filter(([k,v]) => k.includes('최근') ...) 로 읽으므로
        # 키를 "최근 YYYY" 포맷으로 직렬화해서 naver_financials.financial_table 에 주입.
        fin_rows = conn.execute("""
            SELECT year, account_nm, amount
            FROM finance_statements
            WHERE code=?
            ORDER BY account_nm, year DESC
        """, (code,)).fetchall()
        if fin_rows:
            by_acc: dict = {}
            for r2 in fin_rows:
                year = int(r2[0]) if r2[0] is not None else None
                acc = r2[1]
                amt = r2[2]
                if year is None or acc is None or amt is None:
                    continue
                by_acc.setdefault(acc, {})[year] = int(amt)
            financial_table = []
            for acc_nm in ("영업이익", "매출액", "당기순이익"):
                ymap = by_acc.get(acc_nm) or {}
                if not ymap:
                    continue
                years_desc = sorted(ymap.keys(), reverse=True)[:3]
                entry = {"계정명": acc_nm}
                for y in years_desc:
                    entry[f"최근 {y}"] = ymap[y]
                financial_table.append(entry)
            if financial_table:
                nf = d.get("naver_financials") or {}
                nf["financial_table"] = financial_table
                d["naver_financials"] = nf

        # 투자주의/경고/위험/거래정지 지정 상태 (KRX 시장경보)
        # 한 종목이 여러 단계에 동시 지정될 수 있어 리스트로 반환.
        rows = conn.execute("""
            SELECT warning_type, designated_date, reason, updated_at
            FROM investment_warnings WHERE code=?
        """, (code,)).fetchall()
        d["investment_warnings"] = [dict(r2) for r2 in rows]

        # market_change_pct
        d["market_change_pct"] = (d.get("price_today") or {}).get("change_pct", 0)
        d["tomorrow_scenarios"] = _build_tomorrow_scenarios(d)

        # Sanitize floats (NaN, Inf) before serializing to JSON
        d = sanitize_floats(d)

        _stock_detail_mem_put(code, d)
        redis_set_json(active_key, d, ttl_seconds=_detail_ttl)
        redis_set_json(latest_key, d, ttl_seconds=max(_detail_ttl, 120))
        redis_set_json(stale_key, d, ttl_seconds=_STOCK_DETAIL_STALE_TTL_SEC)
        return d

    finally:
        conn.close()


# ─── 매크로(환율 등) 백그라운드 갱신 ─────────────────────────────
_MACRO_BG_STARTED = False

def start_macro_background_poller():
    """서버 시작 시 1회 호출 — 환율·야간선물 등 매크로 지표를 주기적으로 갱신."""
    global _MACRO_BG_STARTED
    if _MACRO_BG_STARTED:
        return
    _MACRO_BG_STARTED = True

    def _refresh():
        try:
            import sys, json as _json
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
            from collectors.macro_collector import MacroCollector
            macro = MacroCollector()
            data = macro.get_all_macros()
            if not data or not data.get("exchange_rate"):
                return
            conn = get_stocks_conn()
            try:
                # 칼럼 마이그레이션 (idempotent ALTER TABLE)
                for col, col_type in [
                    ("exchange_rate_change_pct", "REAL"),
                    ("exchange_rate_change_amt", "REAL"),
                ]:
                    try:
                        conn.execute(f"ALTER TABLE macro ADD COLUMN {col} {col_type}")
                        conn.commit()
                    except Exception:
                        try:
                            conn.rollback()
                        except Exception:
                            pass

                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                # usa_indices 갱신은 fetch_us_indices.py(KST 07시 morning cron)가 전담.
                # 이 루프는 환율·야간선물만 갱신 — usa_indices_json은 기존 값 유지.
                usa_new = data.get("usa_indices") or {}
                if usa_new:
                    # 새 usa_indices 있으면 전체 upsert
                    conn.execute("""
                        INSERT INTO macro(id, exchange_rate, exchange_rate_change_pct, exchange_rate_change_amt,
                                          usa_indices_json, night_futures, updated_at)
                        VALUES(1, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                          exchange_rate=excluded.exchange_rate,
                          exchange_rate_change_pct=excluded.exchange_rate_change_pct,
                          exchange_rate_change_amt=excluded.exchange_rate_change_amt,
                          usa_indices_json=excluded.usa_indices_json,
                          night_futures=excluded.night_futures,
                          updated_at=excluded.updated_at
                    """, (
                        data.get("exchange_rate"),
                        data.get("exchange_rate_change_pct"),
                        data.get("exchange_rate_change_amt"),
                        _json.dumps(usa_new, ensure_ascii=False),
                        data.get("night_futures"),
                        ts
                    ))
                else:
                    # usa_indices 없으면 환율·야간선물만 업데이트, usa_indices_json은 건드리지 않음
                    conn.execute("""
                        INSERT INTO macro(id, exchange_rate, exchange_rate_change_pct, exchange_rate_change_amt,
                                          usa_indices_json, night_futures, updated_at)
                        VALUES(1, ?, ?, ?, '{}', ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                          exchange_rate=excluded.exchange_rate,
                          exchange_rate_change_pct=excluded.exchange_rate_change_pct,
                          exchange_rate_change_amt=excluded.exchange_rate_change_amt,
                          night_futures=excluded.night_futures,
                          updated_at=excluded.updated_at
                    """, (
                        data.get("exchange_rate"),
                        data.get("exchange_rate_change_pct"),
                        data.get("exchange_rate_change_amt"),
                        data.get("night_futures"),
                        ts
                    ))
                conn.commit()
                # 캐시 무효화
                _MACRO_CACHE["data"] = None
                _MACRO_CACHE["mtime"] = 0
                logger.info("[macro-bg] 환율 갱신 완료: %s", data.get("exchange_rate"))
            finally:
                conn.close()
        except Exception as e:
            logger.warning("[macro-bg] 갱신 실패: %s", e)

    def _loop():
        time.sleep(5)
        _refresh()
        while True:
            time.sleep(600)  # 10분마다 갱신
            try:
                _refresh()
            except Exception as e:
                logger.warning("[macro-bg] loop error: %s", e)

    threading.Thread(target=_loop, daemon=True, name="macro-bg-poller").start()
    logger.info("[macro-bg] 매크로 폴러 시작 (10분 간격)")


# ─── GET /api/macro ───────────────────────────────────────────
@router.get("/api/macro")
def get_macro():
    mtime = _get_db_mtime()
    if _MACRO_CACHE["data"] and _MACRO_CACHE["mtime"] == mtime:
        return _MACRO_CACHE["data"]
    redis_key = f"stocks:macro:v{_mtime_token()}"
    redis_hit = redis_get_json(redis_key)
    if isinstance(redis_hit, dict):
        _MACRO_CACHE["data"] = redis_hit
        _MACRO_CACHE["mtime"] = mtime
        return redis_hit

    if not _stocks_db_available():
        return {}
    conn = get_stocks_conn()
    try:
        try:
            sync_credit_trend_from_xls(conn)
        except Exception:
            pass
        m = conn.execute("SELECT * FROM macro WHERE id=1").fetchone()
        if not m:
            m = {}
        else:
            m = dict(m)
            m["usa_indices"] = jl(m.pop("usa_indices_json", None)) or {}
        credit_trend = load_credit_trend_payload(conn)
        if credit_trend:
            m["credit_trend"] = credit_trend
        if not m:
            return {}
        _MACRO_CACHE["data"] = m
        _MACRO_CACHE["mtime"] = mtime
        redis_set_json(redis_key, m, ttl_seconds=600)
        return m
    finally:
        conn.close()


# ─── 뉴스 웜캐시 + 백그라운드 폴러 ──────────────────────────────
# 요청마다 DB를 치지 않고, 백그라운드가 MAX(id) 체크 후 변경분만 갱신한다.
# HTTP 핸들러: 메모리 → Redis(안정키) → DB(cold start 전용)

_NEWS_STABLE_REDIS_KEY = "news:warm:p1:l10:v4"  # mtime 무관 안정 키
_NEWS_DIRTY_SEQ_KEY = "news:dirty:seq:v1"

_NEWS_WARM: dict = {           # 메모리 웜캐시
    "data":   None,
    "max_id": -1,
    "ts":     0.0,
    "dirty_seq": 0,
}
_NEWS_BG_STARTED = False
_NEWS_BG_LOCK = threading.Lock()
_NEWS_REFRESH_LOCK = threading.Lock()


def _get_news_dirty_seq() -> int:
    try:
        hit = redis_get_json(_NEWS_DIRTY_SEQ_KEY)
        if isinstance(hit, dict):
            return int(hit.get("seq") or 0)
    except Exception:
        pass
    return 0


def _news_build_response(conn, page: int, limit: int) -> dict:
    """DB 연결을 받아 뉴스 응답 dict를 만든다 (price enrichment 포함)."""
    total_count = conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0]
    total_pages = (total_count + limit - 1) // limit
    offset = (page - 1) * limit

    rows = conn.execute(
        "SELECT * FROM news_events ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    news_list = [dict(r) for r in rows]

    for item in news_list:
        item.setdefault("source_url", "")

    # 연관 종목 현재가 일괄 조회
    all_codes: set = set()
    for item in news_list:
        try:
            pp = json.loads(item.get("publish_prices_json") or "{}")
            for v in pp.values():
                code = (v.get("code") or "").strip()
                if code:
                    all_codes.add(code)
        except Exception:
            pass

    current_prices: dict = {}
    if all_codes and _stocks_db_available():
        try:
            sc = get_stocks_conn()
            ph = ",".join("?" * len(all_codes))
            price_rows = sc.execute(
                f"SELECT code, current_price, change_pct FROM price_today WHERE code IN ({ph})",
                list(all_codes),
            ).fetchall()
            sc.close()
            current_prices = {
                r["code"]: {"current_price": r["current_price"], "change_pct": r["change_pct"]}
                for r in price_rows
            }
        except Exception:
            pass

    # 실시간 체결강도(strength) — KIS WS 캐시에서 직접 주입 (shared namespace)
    try:
        for code in all_codes:
            rt = _KIS_RT_CACHE.get(code)
            if rt and rt.get("strength") is not None:
                if code not in current_prices:
                    current_prices[code] = {}
                current_prices[code]["strength"] = rt["strength"]
    except Exception:
        pass

    for item in news_list:
        try:
            pp = json.loads(item.get("publish_prices_json") or "{}")
            for v in pp.values():
                code = (v.get("code") or "").strip()
                if code and code in current_prices:
                    cp = current_prices[code]
                    v["current_price"]      = cp.get("current_price")
                    v["current_change_pct"] = cp.get("change_pct")
                    if cp.get("strength") is not None:
                        v["current_strength"] = cp["strength"]
            item["publish_prices_json"] = json.dumps(pp, ensure_ascii=False)
        except Exception:
            pass

    return {
        "data": news_list,
        "total_count": total_count,
        "total_pages": total_pages,
        "current_page": page,
    }


def _news_bg_refresh_once():
    """MAX(id) 확인 후 신규 뉴스가 있을 때만 DB 쿼리 → 웜캐시 + Redis 갱신."""
    if not _news_db_available():
        return
    with _NEWS_REFRESH_LOCK:
        conn = get_news_conn()
        try:
            row = conn.execute("SELECT MAX(id) FROM news_events").fetchone()
            max_id = row[0] if row and row[0] is not None else 0
            dirty_seq = _get_news_dirty_seq()

            if (
                max_id <= _NEWS_WARM["max_id"]
                and _NEWS_WARM["data"] is not None
                and dirty_seq <= int(_NEWS_WARM.get("dirty_seq") or 0)
            ):
                return  # 변경 없음 — DB 쿼리 생략

            res = _news_build_response(conn, page=1, limit=10)
            _NEWS_WARM["data"] = res
            _NEWS_WARM["max_id"] = max_id
            _NEWS_WARM["ts"] = time.time()
            _NEWS_WARM["dirty_seq"] = max(dirty_seq, int(_NEWS_WARM.get("dirty_seq") or 0))
            redis_set_json(_NEWS_STABLE_REDIS_KEY, res, ttl_seconds=600)
            logger.info("[news-bg] 갱신 max_id=%s items=%s", max_id, len(res.get("data", [])))
        except Exception as e:
            logger.debug("[news-bg] refresh error: %s", e)
        finally:
            conn.close()


def start_news_background_poller():
    """서버 시작 시 1회 호출 — 뉴스 백그라운드 폴러를 시작한다."""
    global _NEWS_BG_STARTED
    with _NEWS_BG_LOCK:
        if _NEWS_BG_STARTED:
            return
        _NEWS_BG_STARTED = True

    def _loop():
        time.sleep(1)                  # 서버 완전 기동 후 시작
        _news_bg_refresh_once()        # cold start: 즉시 1회
        while True:
            t0 = time.time()
            try:
                _news_bg_refresh_once()
            except Exception as e:
                logger.debug("[news-bg] loop error: %s", e)
            m = get_market_status() if callable(get_market_status) else "closed"
            interval = 15 if m == "open" else 60
            elapsed = time.time() - t0
            time.sleep(max(0.5, interval - elapsed))

    threading.Thread(target=_loop, daemon=True, name="news-bg-poller").start()
    logger.info("[news-bg] 폴러 시작 (장중 15s, 장외 60s)")


# ─── GET /api/news ────────────────────────────────────────────
@router.get("/api/news")
def get_news(page: int = 1, limit: int = 20):
    # 뉴스 INSERT 프로세스가 dirty seq를 올렸으면 먼저 웜캐시 동기화
    try:
        dirty_seq = _get_news_dirty_seq()
        if dirty_seq > int(_NEWS_WARM.get("dirty_seq") or 0):
            _news_bg_refresh_once()
            _NEWS_WARM["dirty_seq"] = dirty_seq
    except Exception:
        pass

    # ① 메모리 웜캐시 — 즉시 반환 (page=1, limit=10 전용)
    if page == 1 and limit == 10 and _NEWS_WARM["data"] is not None:
        _touch_focus_from_payload(_NEWS_WARM["data"], ttl_sec=90.0, max_collect=240)
        return _NEWS_WARM["data"]

    # ② Redis 안정 키 (page=1 공통, 모든 limit)
    if page == 1:
        stable_key = f"news:warm:p1:l{limit}:v4"
        redis_hit = redis_get_json(stable_key)
        if isinstance(redis_hit, dict) and redis_hit.get("data") is not None:
            if limit == 10:
                _NEWS_WARM["data"] = redis_hit   # 메모리 웜캐시도 채움
            _touch_focus_from_payload(redis_hit, ttl_sec=90.0, max_collect=240)
            return redis_hit

    # ③ DB 직접 쿼리 (cold start 또는 page>1)
    if not _news_db_available():
        return {"data": [], "total_count": 0, "total_pages": 0, "current_page": page}

    conn = get_news_conn()
    try:
        res = _news_build_response(conn, page=page, limit=limit)
        if page == 1:
            stable_key = f"news:warm:p1:l{limit}:v4"
            redis_set_json(stable_key, res, ttl_seconds=600)
            if limit == 10:
                _NEWS_WARM["data"]   = res
                _NEWS_WARM["max_id"] = conn.execute(
                    "SELECT MAX(id) FROM news_events"
                ).fetchone()[0] or 0
                _NEWS_WARM["ts"]     = time.time()
        # 구버전 호환 mtime 캐시도 유지 (page>1 등)
        mtime = _get_db_mtime()
        stocks_mtime = _get_db_mtime() if _stocks_db_available() else 0
        _NEWS_CACHE[(page, limit)] = {"data": res, "mtime": mtime, "stocks_mtime": stocks_mtime}
        _touch_focus_from_payload(res, ttl_sec=90.0, max_collect=240)
        return res
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        conn.close()


# ─── GET /api/news/{news_id} — 단건 상세 ──────────────────────
@router.get("/api/news/{news_id}")
def get_news_item(news_id: str):
    """단건 뉴스 상세. 프론트 NewsDetailView 가 사용."""
    if not _news_db_available():
        raise HTTPException(503, "news db unavailable")
    conn = get_news_conn()
    try:
        # id 가 정수 PK 또는 event_id 문자열일 수 있음 — 둘 다 시도
        row = None
        try:
            id_int = int(str(news_id).strip())
            row = conn.execute(
                "SELECT id, timestamp, headline, source_type, sentiment, related_stocks, "
                "theme, reason, raw_content, sentiment_score, publish_prices_json, "
                "source_url, algo_view, algo_basis, algo_icon "
                "FROM news_events WHERE id=?",
                (id_int,),
            ).fetchone()
        except (ValueError, TypeError):
            row = None
        if not row:
            raise HTTPException(404, f"news not found: {news_id}")
        # 컬럼명 명시 매핑 (PgCompatCursor description 미지원 회피)
        keys = ("id", "timestamp", "headline", "source_type", "sentiment",
                "related_stocks", "theme", "reason", "raw_content", "sentiment_score",
                "publish_prices_json", "source_url", "algo_view", "algo_basis", "algo_icon")
        item = dict(zip(keys, row))
        return item
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        conn.close()


# ─── GET /api/health ──────────────────────────────────────────
@router.get("/api/health")
def health():
    db_ok  = _stocks_db_available()
    json_ok = os.path.exists(JSON_LATEST)
    # Postgres-only: no local file mtime, stamp the check itself.
    mtime = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if db_ok else None

    if db_ok:
        conn = get_stocks_conn()
        cnt = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
        conn.close()
    else:
        cnt = 0

    return {
        "status":      "ok" if db_ok else "no_db",
        "stocks_db":   db_ok,
        "json_exists": json_ok,
        "stock_count": cnt,
        "db_updated":  mtime,
    }


# ─── /price 인플라이트 디덕 (Phase 1) ─────────────────────
# 동일 code 에 대해 동시 요청이 쏟아질 때 KIS 호출을 1회로 제한.
# 리더만 실제 REST 호출, 팔로워는 이벤트 대기 후 캐시에서 읽음.
_LIVE_PRICE_INFLIGHT: dict[str, threading.Event] = {}
_LIVE_PRICE_INFLIGHT_LOCK = threading.Lock()
_LIVE_PRICE_INFLIGHT_TIMEOUT_SEC = max(0.5, float(os.getenv("LIVE_PRICE_INFLIGHT_TIMEOUT_SEC", "3.0")))
_LIVE_PRICE_DEDUP_ENABLED = str(os.getenv("KIS_INFLIGHT_DEDUP_ENABLED", "1")).strip().lower() not in ("0", "false", "no", "off")


# ─── GET /api/stocks/{code}/price ───────────────────────────
@router.get("/api/stocks/{code}/price")
def get_live_price(code: str):
    if not _is_valid_stock_code(code):
        return {"code": code, "error": "invalid_code", "current_price": None}
    import time
    now = time.time()

    # Phase 2 — WS tick 캐시 우선 조회 (5초 이내 신선한 tick 있으면 REST 생략)
    if _PRICE_HANDLER_WS_FIRST:
        rt = _rt_cache_get_fresh(code, _PRICE_HANDLER_WS_FRESH_SEC)
        if rt is not None:
            try:
                observe_cache_read("price", "ws_hit")
            except Exception:
                pass
            # open/high/low 는 WS 가 안 주므로 기존 REST 캐시에서 보강
            prev_data = (_LIVE_PRICE_CACHE.get(code) or {}).get("data") or {}
            ws_result = {
                "code": code,
                "current_price": rt.get("current_price"),
                "change_pct": rt.get("change_pct"),
                "change_amt": prev_data.get("change_amt"),
                "open_price": prev_data.get("open_price"),
                "high_price": prev_data.get("high_price"),
                "low_price": prev_data.get("low_price"),
                "volume": rt.get("trading_volume") or rt.get("acml_vol") or prev_data.get("volume"),
                "strength": rt.get("strength"),
                "updated_at": rt.get("updated_at") or datetime.now().strftime("%H:%M:%S"),
                "source": "kis_ws",
            }
            # WS hit 도 캐시에 업데이트 — 다음 요청에서 즉시 히트
            _cache_set(_LIVE_PRICE_CACHE, code, {"data": ws_result, "fetched_at": now})
            # 후속 유저 요청 대비 RT hub 에 구독 유지
            try:
                _ensure_rt_subscription(code)
            except Exception:
                pass
            return ws_result

    cached = _LIVE_PRICE_CACHE.get(code)
    # 장 중: 10초 / 장 외: 5분 (DB의 종가 데이터로 충분)
    price_ttl = 10 if _market_status() == "open" else 300
    if cached and now - cached["fetched_at"] < price_ttl:
        # Phase 2 — cache hit 시에도 구독 유지 (처음 hit 이후 자연 구독)
        try:
            _ensure_rt_subscription(code)
        except Exception:
            pass
        try:
            observe_cache_read("price", "rest_hit")
        except Exception:
            pass
        return cached["data"]

    # REST 경로로 들어왔다 — 이 code 를 다음부터 WS 에 태우기 위해 구독 요청
    try:
        _ensure_rt_subscription(code)
    except Exception:
        pass
    try:
        observe_cache_read("price", "miss")
    except Exception:
        pass

    # Inflight 디덕: 같은 code 에 이미 리더가 작업 중이면 대기 후 캐시 읽기.
    inflight_event = None
    is_leader = True
    if _LIVE_PRICE_DEDUP_ENABLED:
        with _LIVE_PRICE_INFLIGHT_LOCK:
            existing = _LIVE_PRICE_INFLIGHT.get(code)
            if existing is None:
                inflight_event = threading.Event()
                _LIVE_PRICE_INFLIGHT[code] = inflight_event
            else:
                inflight_event = existing
                is_leader = False

        if not is_leader:
            if inflight_event.wait(timeout=_LIVE_PRICE_INFLIGHT_TIMEOUT_SEC):
                after = _LIVE_PRICE_CACHE.get(code)
                if after and isinstance(after.get("data"), dict):
                    return after["data"]
            # 리더가 타임아웃 안에 발행 못 했으면 follower 도 자체 호출로 폴백

    try:
        try:
            sys.path.insert(0, ROOT_DIR)
            from collectors.kis_api import KISCollector
            collector = KISCollector()
            price_data = collector.get_price(code)

            # 체결강도: WS 실시간 캐시 우선, 없으면 REST 결과
            rt_strength = None
            try:
                rt = _KIS_RT_CACHE.get(code)
                if rt:
                    rt_strength = rt.get("strength")
            except Exception:
                pass

            result = {
                "code": code,
                "current_price": price_data.get("current_price"),
                "change_pct":    price_data.get("change_pct"),
                "change_amt":    price_data.get("change_amt"),
                "open_price":    price_data.get("open_price"),
                "high_price":    price_data.get("high_price"),
                "low_price":     price_data.get("low_price"),
                "volume":        price_data.get("volume"),
                "strength":      rt_strength,
                "updated_at":    datetime.now().strftime("%H:%M:%S"),
            }
        except Exception as e:
            result = {"code": code, "error": str(e), "current_price": None}

        # 캐시 먼저 채운 뒤 이벤트를 set 해야 팔로워가 wake-up 후 캐시 히트.
        _cache_set(_LIVE_PRICE_CACHE, code, {"data": result, "fetched_at": now})
    finally:
        # 리더는 항상 이벤트를 set 해서 팔로워가 더 이상 대기하지 않도록.
        if _LIVE_PRICE_DEDUP_ENABLED and is_leader and inflight_event is not None:
            with _LIVE_PRICE_INFLIGHT_LOCK:
                _LIVE_PRICE_INFLIGHT.pop(code, None)
            inflight_event.set()

    return result


_SCALPING_REFRESH_LOCK = threading.Lock()
_SCALPING_REFRESHING: set[str] = set()
_ORDERBOOK_REFRESH_LOCK = threading.Lock()
_ORDERBOOK_REFRESHING: set[str] = set()
_SCALPING_SOFT_TTL_OPEN = max(5, int(os.getenv("SCALPING_SOFT_TTL_OPEN_SEC", "60")))
_SCALPING_SOFT_TTL_CLOSED = max(60, int(os.getenv("SCALPING_SOFT_TTL_CLOSED_SEC", "1800")))
_SCALPING_HARD_TTL_OPEN = max(_SCALPING_SOFT_TTL_OPEN, int(os.getenv("SCALPING_HARD_TTL_OPEN_SEC", "900")))
_SCALPING_HARD_TTL_CLOSED = max(_SCALPING_SOFT_TTL_CLOSED, int(os.getenv("SCALPING_HARD_TTL_CLOSED_SEC", "21600")))
_ORDERBOOK_SOFT_TTL_OPEN = max(1.0, float(os.getenv("ORDERBOOK_SOFT_TTL_OPEN_SEC", "1.5")))
_ORDERBOOK_SOFT_TTL_CLOSED = max(2.0, float(os.getenv("ORDERBOOK_SOFT_TTL_CLOSED_SEC", "10.0")))
_ORDERBOOK_HARD_TTL_OPEN = max(_ORDERBOOK_SOFT_TTL_OPEN, float(os.getenv("ORDERBOOK_HARD_TTL_OPEN_SEC", "30.0")))
_ORDERBOOK_HARD_TTL_CLOSED = max(_ORDERBOOK_SOFT_TTL_CLOSED, float(os.getenv("ORDERBOOK_HARD_TTL_CLOSED_SEC", "120.0")))
_DETAIL_AUX_NONBLOCKING = str(os.getenv("DETAIL_AUX_NONBLOCKING", "1")).strip().lower() not in ("0", "false", "no", "off")


def _get_shared_kis_collector():
    collector = _get_news_ws_collector()
    if collector is not None:
        return collector
    try:
        sys.path.insert(0, ROOT_DIR)
        from collectors.kis_api import KISCollector
        return KISCollector()
    except Exception:
        return None


def _compute_scalping_payload(code: str) -> dict:
    collector = _get_shared_kis_collector()
    if collector is None:
        raise RuntimeError("kis_collector_unavailable")

    now_dt = datetime.now()
    all_minute_candles = _fetch_full_day_minutes(collector, code)
    candles = all_minute_candles

    data_source = "minute"
    if len(candles) < 5:
        try:
            daily = collector.get_daily_price(code, "D")
            if len(daily) >= 5:
                candles = [
                    {
                        "time": d.get("date", ""),
                        "open": d.get("open") or 0,
                        "high": d.get("high") or 0,
                        "low": d.get("low") or 0,
                        "close": d.get("close") or 0,
                        "volume": d.get("volume") or 0,
                    }
                    for d in reversed(daily)
                    if d.get("close")
                ]
                data_source = "daily"
        except Exception:
            pass

    result = _calc_scalping_index(candles)
    result["candles"] = candles[-30:]
    result["candles_30min"] = _aggregate_to_30min(all_minute_candles) if all_minute_candles else []
    result["data_source"] = data_source
    result["market_status"] = _market_status()
    result["updated_at"] = now_dt.strftime("%H:%M:%S")
    return result


def _compute_scalping_fallback(code: str) -> dict:
    try:
        conn = get_stocks_conn()
        rows = conn.execute(
            """SELECT date, open, high, low, close, volume
               FROM price_daily WHERE code=? ORDER BY date DESC LIMIT 30""",
            (code,),
        ).fetchall()
        conn.close()
        if len(rows) >= 5:
            candles = [
                {"time": r["date"], "open": r["open"] or 0, "high": r["high"] or 0,
                 "low": r["low"] or 0, "close": r["close"] or 0, "volume": r["volume"] or 0}
                for r in reversed(rows) if r["close"]
            ]
            result = _calc_scalping_index(candles)
            result["candles"] = candles[-30:]
            result["candles_30min"] = []
            result["data_source"] = "daily"
            result["market_status"] = _market_status()
            result["updated_at"] = datetime.now().strftime("%H:%M:%S")
            return result
    except Exception:
        pass
    return {
        "score": None, "label": "데이터 없음",
        "detail": {}, "candles": [], "candles_30min": [], "data_source": "none",
        "market_status": _market_status(),
        "updated_at": datetime.now().strftime("%H:%M:%S"),
    }


def _compute_orderbook_payload(code: str) -> dict:
    collector = _get_shared_kis_collector()
    if collector is None:
        raise RuntimeError("kis_collector_unavailable")
    rest_snapshot = _fetch_orderbook_snapshot_from_kis(collector, code)
    # Phase 3 shadow diff: WS 캐시가 있으면 REST 와 top-of-book 비교 로그
    try:
        _orderbook_shadow_log_diff(code, rest_snapshot)
    except Exception:
        pass
    return rest_snapshot


def _queue_scalping_refresh(code: str) -> bool:
    with _SCALPING_REFRESH_LOCK:
        if code in _SCALPING_REFRESHING:
            return False
        _SCALPING_REFRESHING.add(code)

    def _run():
        try:
            try:
                payload = _compute_scalping_payload(code)
            except Exception:
                payload = _compute_scalping_fallback(code)
            _cache_set(_SCALPING_CACHE, code, {"data": payload, "fetched_at": time.time()})
        finally:
            with _SCALPING_REFRESH_LOCK:
                _SCALPING_REFRESHING.discard(code)

    threading.Thread(target=_run, daemon=True, name=f"scalp-refresh-{code}").start()
    return True


def _queue_orderbook_refresh(code: str) -> bool:
    with _ORDERBOOK_REFRESH_LOCK:
        if code in _ORDERBOOK_REFRESHING:
            return False
        _ORDERBOOK_REFRESHING.add(code)

    def _run():
        try:
            try:
                payload = _compute_orderbook_payload(code)
            except Exception as e:
                payload = {
                    "asks": [], "bids": [],
                    "total_ask_qty": 0, "total_bid_qty": 0,
                    "market_status": _market_status(),
                    "updated_at": datetime.now().strftime("%H:%M:%S"),
                    "source_id": _STOCK_FLOW_WS_ORDERBOOK_SOURCE_ID,
                    "error": str(e),
                }
            _cache_set(_ORDERBOOK_CACHE, code, {"data": payload, "fetched_at": time.time()})
        finally:
            with _ORDERBOOK_REFRESH_LOCK:
                _ORDERBOOK_REFRESHING.discard(code)

    threading.Thread(target=_run, daemon=True, name=f"ob-refresh-{code}").start()
    return True


# ─── GET /api/stocks/{code}/scalping ────────────────────────
@router.get("/api/stocks/{code}/scalping")
def get_scalping_index(code: str):
    if not _is_valid_stock_code(code):
        return _compute_scalping_fallback(code)
    import time
    now = time.time()
    cached = _SCALPING_CACHE.get(code)
    ms = _market_status()
    soft_ttl = _SCALPING_SOFT_TTL_OPEN if ms == "open" else _SCALPING_SOFT_TTL_CLOSED
    hard_ttl = _SCALPING_HARD_TTL_OPEN if ms == "open" else _SCALPING_HARD_TTL_CLOSED

    if cached:
        age = now - float(cached.get("fetched_at") or 0.0)
        if age <= soft_ttl:
            return cached["data"]
        _queue_scalping_refresh(code)
        if age <= hard_ttl:
            return cached["data"]
        if _DETAIL_AUX_NONBLOCKING:
            stale = copy.deepcopy(cached["data"])
            stale["_stale"] = True
            stale["snapshot_age_sec"] = int(max(0.0, age))
            return stale

    if _DETAIL_AUX_NONBLOCKING:
        _queue_scalping_refresh(code)
        result = _compute_scalping_fallback(code)
        if isinstance(result, dict):
            result["_warming"] = True
        _cache_set(_SCALPING_CACHE, code, {"data": result, "fetched_at": now})
        return result

    try:
        result = _compute_scalping_payload(code)
    except Exception:
        result = _compute_scalping_fallback(code)

    _cache_set(_SCALPING_CACHE, code, {"data": result, "fetched_at": now})
    return result


# ─── GET /api/stocks/{code}/orderbook ───────────────────────
@router.get("/api/stocks/{code}/orderbook")
def get_orderbook(code: str):
    if not _is_valid_stock_code(code):
        return {
            "asks": [], "bids": [],
            "total_ask_qty": 0, "total_bid_qty": 0,
            "market_status": _market_status(),
            "updated_at": datetime.now().strftime("%H:%M:%S"),
            "source_id": _STOCK_FLOW_WS_ORDERBOOK_SOURCE_ID,
            "error": "invalid_code",
        }
    import time
    now = time.time()

    # Phase 3 — WS orderbook 캐시 우선 조회 (KIS_ORDERBOOK_WS_READ=1 일 때).
    # 이 code 의 다음 요청부터 WS tick 이 오도록 구독도 보장.
    try:
        _ensure_orderbook_subscription(code)
    except Exception:
        pass
    if _KIS_ORDERBOOK_WS_READ:
        ws_snap = _orderbook_ws_get_fresh(code)
        if isinstance(ws_snap, dict):
            # 캐시에도 동기화 (다음 REST fallback 경로의 stale 기준 통일)
            _cache_set(_ORDERBOOK_CACHE, code, {"data": ws_snap, "fetched_at": now})
            try:
                observe_cache_read("orderbook", "ws_hit")
            except Exception:
                pass
            return ws_snap

    cached = _ORDERBOOK_CACHE.get(code)
    ms = _market_status()
    soft_ttl = _ORDERBOOK_SOFT_TTL_OPEN if ms == "open" else _ORDERBOOK_SOFT_TTL_CLOSED
    hard_ttl = _ORDERBOOK_HARD_TTL_OPEN if ms == "open" else _ORDERBOOK_HARD_TTL_CLOSED
    if cached:
        try:
            observe_cache_read("orderbook", "rest_hit")
        except Exception:
            pass
        age = now - float(cached.get("fetched_at") or 0.0)
        if age <= soft_ttl:
            return cached["data"]
        _queue_orderbook_refresh(code)
        if age <= hard_ttl:
            return cached["data"]
        if _DETAIL_AUX_NONBLOCKING:
            stale = copy.deepcopy(cached["data"])
            stale["_stale"] = True
            stale["snapshot_age_sec"] = int(max(0.0, age))
            return stale

    if _DETAIL_AUX_NONBLOCKING:
        _queue_orderbook_refresh(code)
        result = {
            "asks": [], "bids": [],
            "total_ask_qty": 0, "total_bid_qty": 0,
            "market_status": _market_status(),
            "updated_at": datetime.now().strftime("%H:%M:%S"),
            "source_id": _STOCK_FLOW_WS_ORDERBOOK_SOURCE_ID,
            "error": "warming",
            "_warming": True,
        }
        _cache_set(_ORDERBOOK_CACHE, code, {"data": result, "fetched_at": now})
        return result

    try:
        result = _compute_orderbook_payload(code)
    except Exception as e:
        result = {
            "asks": [], "bids": [],
            "total_ask_qty": 0, "total_bid_qty": 0,
            "market_status": _market_status(),
            "updated_at": datetime.now().strftime("%H:%M:%S"),
            "source_id": _STOCK_FLOW_WS_ORDERBOOK_SOURCE_ID,
            "error": str(e),
        }

    _cache_set(_ORDERBOOK_CACHE, code, {"data": result, "fetched_at": now})
    return result


@router.get("/api/stocks/{code}/financials")
def get_financials_compat(code: str):
    if not _is_valid_stock_code(code):
        raise HTTPException(400, f"invalid stock code: {code}")
    return {
        "code": code,
        "deprecated": True,
        "income_statement": [],
        "balance_sheet": [],
        "cash_flow": [],
        "ratios": {},
    }


@router.get("/api/stocks/{code}/invest-opinion")
def get_invest_opinion_compat(code: str):
    if not _is_valid_stock_code(code):
        raise HTTPException(400, f"invalid stock code: {code}")
    return {
        "code": code,
        "deprecated": True,
        "score": None,
        "summary": "endpoint retired",
        "signals": [],
    }



