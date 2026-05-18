# ─── 미국 테마 API ───────────────────────────────────────────
#
# 의존성 (Postgres 테이블):
#   - stock_themes_us (ticker, theme)
#   - us_stocks (ticker, name, exchange, sector, industry, market_cap, updated_at)
#   - price_today_us (ticker, current_price, change_pct, change_amt, prev_close,
#                     open_price, day_high, day_low, trading_volume, trading_value,
#                     market_cap, updated_at)
#
# 제공 엔드포인트:
#   GET /api/themes/us              — rising / hot / falling 랭킹
#   GET /api/themes/us/{theme}      — 특정 테마 상세 (구성종목 리스트)
#   GET /api/us-stocks/{ticker}     — 단일 티커 스냅샷

import server.state as _state

_US_THEMES_CACHE = _state._US_THEMES_CACHE


def _us_themes_available() -> bool:
    """stock_themes_us 테이블 존재 여부 — SELECT 1 시도로 판정."""
    if not _stocks_db_available():
        return False
    conn = get_stocks_conn()
    try:
        # 존재 체크는 그냥 SELECT 해보고 예외 발생 여부로 판단
        conn.execute("SELECT 1 FROM stock_themes_us LIMIT 1").fetchone()
        return True
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


@router.get("/api/themes/us")
def get_us_themes(top: int = 15):
    """미국 테마 랭킹 — rising(상승 평균), hot(거래대금), falling(하락 평균)."""
    mtime = _get_db_mtime()
    if (
        _US_THEMES_CACHE.get("data")
        and _US_THEMES_CACHE.get("mtime") == mtime
        and _US_THEMES_CACHE.get("top") == top
    ):
        return _US_THEMES_CACHE["data"]

    redis_key = f"stocks:themes_us:top:{top}:v{_mtime_token()}"
    redis_hit = redis_get_json(redis_key)
    if isinstance(redis_hit, dict):
        _US_THEMES_CACHE["data"] = redis_hit
        _US_THEMES_CACHE["mtime"] = mtime
        _US_THEMES_CACHE["top"] = top
        return redis_hit

    if not _us_themes_available():
        raise HTTPException(503, "미국 테마 DB 없음. scripts/parse_themes_us.py 실행 후 collectors/us_theme_price_collector.py 실행 필요.")

    conn = get_stocks_conn()
    try:
        # 평균/합계는 시세 있는 종목만 반영 (delisted/누락 종목이 0%로 평균 희석되는 문제 회피)
        rows = conn.execute("""
            SELECT
                st.theme,
                COUNT(*)                                  AS stock_count,
                COUNT(pt.current_price)                   AS priced_count,
                AVG(pt.change_pct)                        AS avg_change,
                SUM(COALESCE(pt.trading_value, 0))        AS total_value,
                GROUP_CONCAT(
                    COALESCE(us.name, st.ticker) || ':' || st.ticker || ':' || COALESCE(pt.change_pct, 0)
                )                                         AS members
            FROM stock_themes_us st
            LEFT JOIN us_stocks       us ON us.ticker = st.ticker
            LEFT JOIN price_today_us  pt ON pt.ticker = st.ticker
            GROUP BY st.theme
            HAVING COUNT(pt.current_price) >= 2
        """).fetchall()

        themes = []
        for r in rows:
            r = dict(r)
            members_raw = r.pop("members", "") or ""
            members = []
            for part in members_raw.split(","):
                segs = part.rsplit(":", 2)
                if len(segs) != 3:
                    continue
                nm, tk, pct_s = segs
                try:
                    pct_v = float(pct_s)
                except ValueError:
                    pct_v = 0.0
                members.append({"name": nm, "ticker": tk, "pct": pct_v})
            themes.append({
                "name":         r["theme"],
                "stock_count":  r["stock_count"],
                "priced_count": r.get("priced_count", r["stock_count"]),
                "avg_change":   round(r["avg_change"] or 0, 2),
                "total_value":  r["total_value"] or 0,
                "members":      members,
            })

        rising = sorted(
            [t for t in themes if t["avg_change"] > 0],
            key=lambda t: t["avg_change"], reverse=True
        )[:top]

        falling = sorted(
            [t for t in themes if t["avg_change"] < 0],
            key=lambda t: t["avg_change"]
        )[:top]

        hot = sorted(
            themes,
            key=lambda t: t["total_value"], reverse=True
        )[:top]

        res = {
            "rising": rising,
            "falling": falling,
            "hot": hot,
            "total_themes": len(themes),
        }
        _US_THEMES_CACHE["data"] = res
        _US_THEMES_CACHE["mtime"] = mtime
        _US_THEMES_CACHE["top"] = top
        redis_set_json(redis_key, res, ttl_seconds=900)
        return res
    finally:
        conn.close()


@router.get("/api/themes/us/{theme_name:path}")
def get_us_theme_detail(theme_name: str):
    """미국 테마 상세 — 구성종목 + 통계."""
    redis_key = f"stocks:theme_us_detail:{_search_key_token(theme_name)}:v{_mtime_token()}"
    redis_hit = redis_get_json(redis_key)
    if isinstance(redis_hit, dict):
        return redis_hit

    if not _us_themes_available():
        raise HTTPException(503, "미국 테마 DB 없음.")

    conn = get_stocks_conn()
    try:
        rows = conn.execute("""
            SELECT
                st.ticker,
                us.name, us.exchange, us.sector, us.industry, us.market_cap AS meta_market_cap,
                pt.current_price, pt.change_pct, pt.change_amt, pt.prev_close,
                pt.open_price, pt.day_high, pt.day_low,
                pt.trading_volume, pt.trading_value,
                pt.updated_at
            FROM stock_themes_us st
            LEFT JOIN us_stocks       us ON us.ticker = st.ticker
            LEFT JOIN price_today_us  pt ON pt.ticker = st.ticker
            WHERE st.theme = ?
            ORDER BY COALESCE(pt.trading_value, 0) DESC
        """, (theme_name,)).fetchall()

        if not rows:
            raise HTTPException(404, f"미국 테마 없음: {theme_name}")

        stocks = []
        total_value = 0.0
        change_list = []
        for r in rows:
            d = dict(r) if hasattr(r, "keys") else {}
            stocks.append({
                "ticker":          d.get("ticker"),
                "name":            d.get("name") or d.get("ticker"),
                "exchange":        d.get("exchange"),
                "sector":          d.get("sector"),
                "industry":        d.get("industry"),
                "market_cap":      d.get("meta_market_cap"),
                "current_price":   d.get("current_price"),
                "change_pct":      d.get("change_pct"),
                "change_amt":      d.get("change_amt"),
                "prev_close":      d.get("prev_close"),
                "open_price":      d.get("open_price"),
                "day_high":        d.get("day_high"),
                "day_low":         d.get("day_low"),
                "trading_volume":  d.get("trading_volume"),
                "trading_value":   d.get("trading_value"),
                "updated_at":      d.get("updated_at"),
            })
            tv = d.get("trading_value")
            if isinstance(tv, (int, float)):
                total_value += tv
            cp = d.get("change_pct")
            if isinstance(cp, (int, float)):
                change_list.append(cp)

        avg_change = round(sum(change_list) / len(change_list), 2) if change_list else None
        up = sum(1 for c in change_list if c > 0)
        down = sum(1 for c in change_list if c < 0)
        flat = len(change_list) - up - down

        stats = {
            "stock_count":       len(stocks),
            "avg_change_pct":    avg_change,
            "total_value_today": total_value,
            "up_count":          up,
            "down_count":        down,
            "flat_count":        flat,
        }

        result = {
            "theme":  theme_name,
            "stats":  stats,
            "stocks": stocks,
        }
        redis_set_json(redis_key, result, ttl_seconds=600)
        return sanitize_floats(result)
    finally:
        conn.close()


@router.get("/api/us-stocks/{ticker}")
def get_us_stock_detail(ticker: str):
    """단일 미국 티커 스냅샷 — 테마 리스트 포함."""
    tk = (ticker or "").strip().upper()
    if not tk:
        raise HTTPException(400, "ticker required")
    if not _us_themes_available():
        raise HTTPException(503, "미국 테마 DB 없음.")

    conn = get_stocks_conn()
    try:
        row = conn.execute("""
            SELECT
                us.ticker, us.name, us.exchange, us.sector, us.industry, us.market_cap,
                pt.current_price, pt.change_pct, pt.change_amt, pt.prev_close,
                pt.open_price, pt.day_high, pt.day_low,
                pt.trading_volume, pt.trading_value, pt.updated_at
            FROM us_stocks us
            LEFT JOIN price_today_us pt ON pt.ticker = us.ticker
            WHERE us.ticker = ?
        """, (tk,)).fetchone()
        if not row:
            raise HTTPException(404, f"미국 종목 없음: {tk}")

        d = dict(row) if hasattr(row, "keys") else {}
        themes = [
            r[0] for r in conn.execute(
                "SELECT theme FROM stock_themes_us WHERE ticker = ? ORDER BY theme",
                (tk,),
            ).fetchall()
        ]
        d["themes"] = themes
        return sanitize_floats(d)
    finally:
        conn.close()


# ─── 한국↔미국 테마 매핑 API ─────────────────────────────────

def _theme_map_available() -> bool:
    if not _stocks_db_available():
        return False
    conn = get_stocks_conn()
    try:
        conn.execute("SELECT 1 FROM theme_map_kr_us LIMIT 1").fetchone()
        return True
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _kr_theme_stats(conn, theme: str) -> dict:
    """한국 테마 집계 (stock_themes + price_today)."""
    row = conn.execute("""
        SELECT
            COUNT(*)                        AS stock_count,
            COUNT(pt.change_pct)            AS priced_count,
            AVG(pt.change_pct)              AS avg_change,
            SUM(COALESCE(pt.trading_value,0)) AS total_value
        FROM stock_themes st
        LEFT JOIN price_today pt ON pt.code = st.code
        WHERE st.theme = ?
    """, (theme,)).fetchone()
    if not row:
        return {}
    d = dict(row) if hasattr(row, "keys") else {}
    return {
        "stock_count":  d.get("stock_count") or 0,
        "priced_count": d.get("priced_count") or 0,
        "avg_change":   round((d.get("avg_change") or 0), 2),
        "total_value":  d.get("total_value") or 0,
    }


def _us_theme_stats(conn, theme: str) -> dict:
    """미국 테마 집계."""
    row = conn.execute("""
        SELECT
            COUNT(*)                        AS stock_count,
            COUNT(pt.change_pct)            AS priced_count,
            AVG(pt.change_pct)              AS avg_change,
            SUM(COALESCE(pt.trading_value,0)) AS total_value
        FROM stock_themes_us st
        LEFT JOIN price_today_us pt ON pt.ticker = st.ticker
        WHERE st.theme = ?
    """, (theme,)).fetchone()
    if not row:
        return {}
    d = dict(row) if hasattr(row, "keys") else {}
    return {
        "stock_count":  d.get("stock_count") or 0,
        "priced_count": d.get("priced_count") or 0,
        "avg_change":   round((d.get("avg_change") or 0), 2),
        "total_value":  d.get("total_value") or 0,
    }


def _us_theme_exists(conn, theme: str) -> bool:
    r = conn.execute("SELECT 1 FROM stock_themes_us WHERE theme = ? LIMIT 1", (theme,)).fetchone()
    return r is not None


def _kr_theme_exists(conn, theme: str) -> bool:
    r = conn.execute("SELECT 1 FROM stock_themes WHERE theme = ? LIMIT 1", (theme,)).fetchone()
    return r is not None


@router.get("/api/themes/kr-us-match/{theme_name:path}")
def get_kr_theme_us_match(theme_name: str):
    """한국 테마에 매칭되는 미국 테마들 + 양쪽 통계.

    매칭 소스 2가지:
      1) 동명(same-name) — KR/US 양쪽에 같은 이름의 테마가 있으면 자동 exact 매치
      2) theme_map_kr_us 테이블 — cross-name 매핑

    매칭 없어도 200 반환 (matches=[]). 클라이언트 콘솔 에러 회피.
    """
    conn = get_stocks_conn()
    try:
        kr_stats = _kr_theme_stats(conn, theme_name)
        matches = []
        seen = set()

        # 1) 동명 매칭
        if _us_theme_exists(conn, theme_name):
            matches.append({
                "us_theme":   theme_name,
                "confidence": "exact",
                "same_name":  True,
                "stats":      _us_theme_stats(conn, theme_name),
            })
            seen.add(theme_name)

        # 2) cross-name 매핑 테이블
        if _theme_map_available():
            rows = conn.execute(
                "SELECT us_theme, confidence FROM theme_map_kr_us WHERE kr_theme = ? ORDER BY CASE confidence WHEN 'exact' THEN 1 WHEN 'strong' THEN 2 WHEN 'partial' THEN 3 ELSE 4 END",
                (theme_name,),
            ).fetchall()
            for r in rows:
                us_theme = r[0]
                if us_theme in seen:
                    continue
                seen.add(us_theme)
                matches.append({
                    "us_theme":   us_theme,
                    "confidence": r[1],
                    "same_name":  False,
                    "stats":      _us_theme_stats(conn, us_theme),
                })

        return sanitize_floats({
            "kr_theme":  theme_name,
            "kr_stats":  kr_stats,
            "matches":   matches,
        })
    finally:
        conn.close()


@router.get("/api/themes/us-kr-match/{theme_name:path}")
def get_us_theme_kr_match(theme_name: str):
    """미국 테마에 매칭되는 한국 테마들 + 양쪽 통계.

    매칭 없어도 200 반환 (matches=[]). 클라이언트 콘솔 에러 회피.
    """
    conn = get_stocks_conn()
    try:
        us_stats = _us_theme_stats(conn, theme_name)
        matches = []
        seen = set()

        if _kr_theme_exists(conn, theme_name):
            matches.append({
                "kr_theme":   theme_name,
                "confidence": "exact",
                "same_name":  True,
                "stats":      _kr_theme_stats(conn, theme_name),
            })
            seen.add(theme_name)

        if _theme_map_available():
            rows = conn.execute(
                "SELECT kr_theme, confidence FROM theme_map_kr_us WHERE us_theme = ? ORDER BY CASE confidence WHEN 'exact' THEN 1 WHEN 'strong' THEN 2 WHEN 'partial' THEN 3 ELSE 4 END",
                (theme_name,),
            ).fetchall()
            for r in rows:
                kr_theme = r[0]
                if kr_theme in seen:
                    continue
                seen.add(kr_theme)
                matches.append({
                    "kr_theme":   kr_theme,
                    "confidence": r[1],
                    "same_name":  False,
                    "stats":      _kr_theme_stats(conn, kr_theme),
                })

        return sanitize_floats({
            "us_theme":  theme_name,
            "us_stats":  us_stats,
            "matches":   matches,
        })
    finally:
        conn.close()


@router.get("/api/themes/pairs")
def get_theme_pairs(confidence: str = ""):
    """전체 매핑 테이블 — rising/falling 사이드바이사이드용."""
    if not _theme_map_available():
        raise HTTPException(503, "theme_map_kr_us 테이블 없음.")

    conn = get_stocks_conn()
    try:
        if confidence:
            rows = conn.execute(
                "SELECT kr_theme, us_theme, confidence FROM theme_map_kr_us WHERE confidence = ? ORDER BY kr_theme",
                (confidence,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT kr_theme, us_theme, confidence FROM theme_map_kr_us ORDER BY kr_theme, CASE confidence WHEN 'exact' THEN 1 WHEN 'strong' THEN 2 WHEN 'partial' THEN 3 ELSE 4 END"
            ).fetchall()

        pairs = []
        for r in rows:
            kr = r[0]
            us = r[1]
            conf = r[2]
            kr_stats = _kr_theme_stats(conn, kr)
            us_stats = _us_theme_stats(conn, us)
            pairs.append({
                "kr_theme":   kr,
                "us_theme":   us,
                "confidence": conf,
                "kr_avg_change":    kr_stats.get("avg_change"),
                "us_avg_change":    us_stats.get("avg_change"),
                "kr_stock_count":   kr_stats.get("stock_count"),
                "us_stock_count":   us_stats.get("stock_count"),
                "kr_total_value":   kr_stats.get("total_value"),
                "us_total_value":   us_stats.get("total_value"),
            })
        return sanitize_floats({"count": len(pairs), "pairs": pairs})
    finally:
        conn.close()
