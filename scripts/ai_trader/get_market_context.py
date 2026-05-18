"""
ai_trader 시장 컨텍스트 — 슬롯 진입 시 시장 분위기 파악용.

출력:
{
  "fetched_at": "...",
  "latest_brief_kospi": {...},   # market_briefings 최근 KOSPI
  "latest_brief_nasdaq": {...},
  "indices": {
    "KOSPI": {"price": ..., "change_pct": ..., "as_of": ...},
    "KOSDAQ": {...}
  },
  "market_flow_today": {        # 시장 단위 외인/기관 누적 (백만원)
    "KOSPI": {"foreign_net_uk": ..., "institution_net_uk": ..., "date": "..."},
    "KOSDAQ": {...}
  },
  "top_themes_today": [...],     # 평균 등락률 상위 5
  "top_trading_value": [...],    # 거래대금 상위 20 (code/name/price/chg_pct/value)
  "macro": {                     # 매크로 — DB에 있으면
    "usd_krw": ..., "vix": ..., "sp500_after_hours_chg": ...
  }
}

CLI:
  python scripts/ai_trader/get_market_context.py
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# Windows cp949 콘솔에서 한글·em-dash 출력 가능하도록
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from server.db.connections import get_stocks_conn  # noqa: E402


def _safe_row(row):
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return dict(row)


def _rb(conn):
    """PG: 트랜잭션 에러 후 rollback — 후속 쿼리 가능하게."""
    try:
        conn.rollback()
    except Exception:
        pass


def _parse_ctx(ctx_raw):
    if not ctx_raw:
        return None
    try:
        return json.loads(ctx_raw)
    except Exception:
        return None


def _latest_brief(conn, market: str) -> dict | None:
    """latest_brief — context_json 의미 있는 (len>=50) 첫 행."""
    rows = conn.execute(
        """
        SELECT slot, briefing_date, slot_time, summary, context_json, created_at
          FROM market_briefings
         WHERE market=?
         ORDER BY briefing_date DESC, slot_time DESC, id DESC LIMIT 5
        """,
        (market,),
    ).fetchall()
    for r in rows:
        d = _safe_row(r)
        ctx = _parse_ctx(d.get("context_json"))
        if not ctx:
            continue
        ss = (ctx.get("summary_structured") or {})
        return {
            "slot": d.get("slot"),
            "briefing_date": d.get("briefing_date"),
            "slot_time": d.get("slot_time"),
            "headline": ss.get("headline") or "",
            "bullets": ss.get("bullets") or [],
            "closing": ss.get("closing") or "",
            "summary_first_line": (d.get("summary") or "").splitlines()[0] if d.get("summary") else "",
        }
    return None


def _indices(conn) -> dict:
    """지수 — daily_market_indices 또는 비슷한 테이블 시도. 없으면 빈."""
    out = {}
    for code, name in [("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")]:
        for sql in [
            "SELECT price, change_pct, as_of FROM market_indices WHERE name=? "
            "ORDER BY as_of DESC LIMIT 1",
            "SELECT price, change_pct, updated_at AS as_of FROM index_snapshots WHERE name=? "
            "ORDER BY updated_at DESC LIMIT 1",
        ]:
            try:
                r = conn.execute(sql, (name,)).fetchone()
                if r:
                    out[code] = _safe_row(r)
                    break
            except Exception:
                _rb(conn)
                continue
    return out


def _market_flow_today(conn) -> dict:
    """오늘 (또는 최근 거래일) 시장 단위 외인/기관 — fetch_market_flow 결과 캐시되는 표가 없으니
    가장 최근 market_briefings 의 context_json.flow_today 를 신뢰."""
    out = {}
    for market in ("KOSPI", "KOSDAQ"):
        try:
            row = conn.execute(
                "SELECT context_json FROM market_briefings WHERE market=? "
                "ORDER BY briefing_date DESC, slot_time DESC LIMIT 3",
                (market,),
            ).fetchall()
        except Exception:
            _rb(conn)
            continue
        for r in row:
            ctx = _parse_ctx((r["context_json"] if hasattr(r, "keys") else r[0]))
            if not ctx:
                continue
            ft = ctx.get("flow_today")
            if ft and isinstance(ft, dict):
                out[market] = ft
                break
    return out


def _top_themes(conn, limit: int = 5) -> list:
    """오늘 평균 등락률 상위 테마."""
    try:
        rows = conn.execute(
            """
            SELECT theme, avg_change_pct, member_count, top_stocks_json
              FROM theme_daily_summary
             WHERE summary_date = (SELECT MAX(summary_date) FROM theme_daily_summary)
             ORDER BY avg_change_pct DESC NULLS LAST
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_safe_row(r) for r in rows]
    except Exception:
        _rb(conn)
    # fallback — themes 테이블이 다를 수 있어 안전한 빈 응답
    return []


def _top_trading_value(conn, limit: int = 20) -> list:
    """거래대금 상위 — 가능한 컬럼 명 여러 개 시도."""
    candidates = [
        """
        SELECT code, name, current_price AS price, change_pct, trading_value
          FROM price_today
         WHERE trading_value > 0
         ORDER BY trading_value DESC NULLS LAST
         LIMIT ?
        """,
        """
        SELECT code, name, price, change_pct, trading_value
          FROM stocks_today
         WHERE trading_value > 0
         ORDER BY trading_value DESC
         LIMIT ?
        """,
        """
        SELECT s.code, s.name,
               pt.current_price AS price,
               pt.change_pct,
               pt.trading_value
          FROM price_today pt
          JOIN stocks s ON s.code = pt.code
         ORDER BY pt.trading_value DESC NULLS LAST
         LIMIT ?
        """,
    ]
    for sql in candidates:
        try:
            rows = conn.execute(sql, (limit,)).fetchall()
            if rows:
                return [_safe_row(r) for r in rows]
        except Exception:
            _rb(conn)
            continue
    return []


def _macro(conn) -> dict:
    """매크로 — global_indicators 풍부한 데이터 다 가져오기.
    원자재(WTI/BRENT/GOLD/COPPER/NATGAS) + 채권(US10Y/US2Y) + 글로벌(SP500/NASDAQ/VIX/DXY/SOX/NIKKEI/FEAR_GREED) + 한국(KOSPI/KOSDAQ/KRW_USD) + 암호화폐(BTC).
    지정학·인플레·금리 변동을 AI가 인지하려면 이 데이터가 핵심.

    응답에 `_freshness` 키 포함 — 데이터가 얼마나 stale 한지 AI 가 인지하도록.
    """
    from datetime import datetime as _dt
    out = {}
    most_recent = None
    most_stale = None
    try:
        rows = conn.execute(
            "SELECT symbol, display_name, category, price, change_pct, updated_at "
            "FROM global_indicators "
            "WHERE symbol IN ('WTI','BRENT','GOLD','COPPER','NATGAS','SILVER',"
            "'US10Y','US2Y','SP500','NASDAQ','SOX','VIX','DXY','NIKKEI','FEAR_GREED',"
            "'KOSPI','KOSDAQ','KRW_USD','BTC') "
            "ORDER BY category, symbol"
        ).fetchall()
        by_cat = {}
        for r in rows:
            row = _safe_row(r)
            if not row:
                continue
            cat = row.get("category") or "other"
            by_cat.setdefault(cat, []).append({
                "symbol": row["symbol"],
                "name": row.get("display_name"),
                "price": row.get("price"),
                "change_pct": row.get("change_pct"),
                "updated_at": row.get("updated_at"),
            })
            # freshness 트래킹
            upd_raw = row.get("updated_at")
            try:
                if isinstance(upd_raw, str):
                    upd = _dt.strptime(upd_raw.split(".")[0], "%Y-%m-%d %H:%M:%S")
                else:
                    upd = upd_raw
                if upd:
                    if most_recent is None or upd > most_recent:
                        most_recent = upd
                    if most_stale is None or upd < most_stale:
                        most_stale = upd
            except Exception:
                pass
        out = by_cat
        # _freshness 메타 — AI 가 인지해야 하는 stale 경고
        if most_recent:
            now_ts = _dt.now()
            recent_age_h = (now_ts - most_recent).total_seconds() / 3600
            stale_age_h = (now_ts - most_stale).total_seconds() / 3600 if most_stale else recent_age_h
            warning = None
            if recent_age_h > 12:
                warning = (
                    f"⚠️ 매크로 데이터가 {recent_age_h:.1f}시간 묵었음. "
                    f"미국 종가/원자재/금리 정보가 오래된 상태 — 이 컨텍스트로 매매 결정 시 stale 위험. "
                    f"WebSearch 로 SP500/WTI/VIX 현재가 직접 확인 권장."
                )
            elif recent_age_h > 2:
                warning = f"매크로 데이터 {recent_age_h:.1f}시간 묵음 — 큰 변동 시 갱신 늦을 수 있음."
            out["_freshness"] = {
                "most_recent_at": most_recent.strftime("%Y-%m-%d %H:%M:%S"),
                "most_stale_at": most_stale.strftime("%Y-%m-%d %H:%M:%S") if most_stale else None,
                "recent_age_hours": round(recent_age_h, 2),
                "stale_age_hours": round(stale_age_h, 2),
                "warning": warning,
            }
    except Exception:
        _rb(conn)
    return out


# 지정학·매크로 키워드 — AI 트레이더가 인지해야 하는 시그널 카테고리
GEO_KEYWORDS = {
    "middle_east": ["중동", "이스라엘", "이란", "가자", "팔레스타인", "호르무즈", "헤즈볼라", "하마스", "사우디"],
    "ukraine_russia": ["우크라이나", "러시아", "푸틴", "젤렌스키"],
    "trade_war": ["관세", "상호관세", "트럼프", "무역갈등", "수출통제", "제재"],
    "fed_inflation": ["FOMC", "Fed", "연준", "파월", "CPI", "PPI", "PCE", "인플레", "금리 인상", "금리 인하", "긴축"],
    "korea_geo": ["북한", "도발", "미사일", "사드", "한미"],
    "energy": ["유가", "원유", "WTI", "OPEC", "감산", "증산"],
}


def _geopolitical_news(conn, hours: int = 36, per_category: int = 3) -> dict:
    """news_events 에서 최근 36시간 지정학·매크로 키워드 매칭 헤드라인 추출.
    AI가 이걸 보고 시장 영향을 thinking 에 반영하도록.
    """
    out = {}
    try:
        from datetime import datetime, timedelta
        since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return out

    for cat, keywords in GEO_KEYWORDS.items():
        try:
            like_clauses = " OR ".join(["headline LIKE %s" for _ in keywords])
            params = tuple(f"%{k}%" for k in keywords) + (since,)
            sql = (
                f"SELECT id, timestamp, headline, sentiment, sentiment_score, source_url "
                f"FROM news_events "
                f"WHERE ({like_clauses}) AND timestamp >= %s "
                f"ORDER BY timestamp DESC LIMIT {per_category}"
            )
            rows = conn.execute(sql, params).fetchall()
            hits = [_safe_row(r) for r in rows] if rows else []
            if hits:
                out[cat] = hits
        except Exception:
            _rb(conn)
            continue
    return out


def _macro_pulse(conn) -> dict:
    """자동 매크로 시그널 감지 — 큰 변동 자동 플래그.
    AI 가 매번 일일이 확인 안 해도 핵심 변동은 놓치지 않게 자동 표시.
    """
    pulse = []
    try:
        rows = conn.execute(
            "SELECT symbol, display_name, price, change_pct "
            "FROM global_indicators "
            "WHERE symbol IN ('WTI','BRENT','GOLD','VIX','US10Y','DXY','BTC','SOX','KRW_USD','FEAR_GREED','COPPER')"
        ).fetchall()
    except Exception:
        _rb(conn)
        return {"signals": pulse}

    # 시그널 임계
    THRESHOLDS = {
        "WTI":   {"big_up": 3.0,  "big_down": -3.0, "desc_up": "유가 급등 — 중동 긴장·OPEC 감산 등 가능, 인플레 압력·코스피 외인 매도 압력",
                  "desc_down": "유가 급락 — 수요 둔화 우려, 정유주·항공주 영향"},
        "BRENT": {"big_up": 3.0,  "big_down": -3.0, "desc_up": "Brent 급등 — 유럽·중동 공급 우려", "desc_down": "Brent 급락"},
        "GOLD":  {"big_up": 1.5,  "big_down": -2.0, "desc_up": "금 상승 — 안전자산 유입, 지정학/인플레 우려 시그널",
                  "desc_down": "금 하락 — 위험자산 선호"},
        "VIX":   {"abs_high": 25, "abs_critical": 30, "desc_high": "VIX 공포 영역 진입 — 미국 시장 변동성 확대",
                  "desc_critical": "VIX 패닉 영역 — 코스피 외인 매도 압력 큼"},
        "US10Y": {"big_up": 0.07, "big_down": -0.07, "desc_up": "미국 10년 금리 급등 — 인플레/긴축 우려, 성장주 압박",
                  "desc_down": "미국 10년 금리 급락 — 안전자산 유입 또는 경기침체 우려"},
        "DXY":   {"big_up": 0.7,  "big_down": -0.7, "desc_up": "달러 강세 — 한국 외인 매도 압력", "desc_down": "달러 약세 — 신흥국 호재"},
        "BTC":   {"big_up": 5.0,  "big_down": -5.0, "desc_up": "비트코인 급등 — 위험자산 선호", "desc_down": "비트코인 급락 — 위험회피"},
        "SOX":   {"big_up": 2.0,  "big_down": -2.0, "desc_up": "필라델피아 반도체 강세 — SK하이닉스·삼성전자 출발 우호적",
                  "desc_down": "필라델피아 반도체 약세 — 한국 반도체 출발 부담"},
        "KRW_USD": {"big_up": 0.5, "big_down": -0.5, "desc_up": "원/달러 급등 — 외인 매도 압력",
                    "desc_down": "원/달러 급락 — 외인 매수 우호"},
        "FEAR_GREED": {"abs_low": 25, "abs_critical": 15, "desc_low": "공포&탐욕 공포 영역 진입",
                       "desc_critical": "공포&탐욕 극단 공포 — 역발상 매수 신호 가능"},
        "COPPER": {"big_up": 2.5, "big_down": -2.5, "desc_up": "구리 급등 — 경기 회복 신호", "desc_down": "구리 급락 — 경기 둔화 우려"},
    }

    for r in rows:
        row = _safe_row(r) if not isinstance(r, dict) else r
        if not row:
            continue
        sym = row.get("symbol")
        chg = row.get("change_pct")
        price = row.get("price")
        rules = THRESHOLDS.get(sym)
        if not rules:
            continue
        # 절대값 기반 (VIX, FEAR_GREED) 우선
        if "abs_critical" in rules and price is not None and Number_above(price, rules.get("abs_critical", 9e9)):
            pulse.append({"symbol": sym, "name": row.get("display_name"), "level": "critical",
                          "value": price, "change_pct": chg, "note": rules["desc_critical"]})
            continue
        if "abs_high" in rules and price is not None and Number_above(price, rules.get("abs_high", 9e9)):
            pulse.append({"symbol": sym, "name": row.get("display_name"), "level": "high",
                          "value": price, "change_pct": chg, "note": rules["desc_high"]})
            continue
        if "abs_critical" in rules and "abs_low" not in rules and False:
            pass  # placeholder
        if "abs_low" in rules and price is not None and Number_below(price, rules.get("abs_critical", -9e9)):
            pulse.append({"symbol": sym, "name": row.get("display_name"), "level": "critical",
                          "value": price, "change_pct": chg, "note": rules["desc_critical"]})
            continue
        if "abs_low" in rules and price is not None and Number_below(price, rules.get("abs_low", -9e9)):
            pulse.append({"symbol": sym, "name": row.get("display_name"), "level": "low",
                          "value": price, "change_pct": chg, "note": rules["desc_low"]})
            continue
        # 변동률 기반
        if chg is None:
            continue
        if "big_up" in rules and chg >= rules["big_up"]:
            pulse.append({"symbol": sym, "name": row.get("display_name"), "level": "up",
                          "value": price, "change_pct": chg, "note": rules["desc_up"]})
        elif "big_down" in rules and chg <= rules["big_down"]:
            pulse.append({"symbol": sym, "name": row.get("display_name"), "level": "down",
                          "value": price, "change_pct": chg, "note": rules["desc_down"]})
    return {"signals": pulse, "total": len(pulse)}


def Number_above(v, threshold):
    try:
        return float(v) >= float(threshold)
    except Exception:
        return False


def Number_below(v, threshold):
    try:
        return float(v) <= float(threshold)
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-themes", type=int, default=5)
    ap.add_argument("--top-trading", type=int, default=20)
    args = ap.parse_args()

    conn = get_stocks_conn()
    try:
        out = {
            "fetched_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "latest_brief_kospi": _latest_brief(conn, "KOSPI"),
            "latest_brief_nasdaq": _latest_brief(conn, "NASDAQ"),
            "indices": _indices(conn),
            "market_flow_today": _market_flow_today(conn),
            "top_themes_today": _top_themes(conn, args.top_themes),
            "top_trading_value": _top_trading_value(conn, args.top_trading),
            "macro": _macro(conn),
            # v29 — 지정학·매크로 시그널 (중동/우크라이나/관세/Fed/유가 등)
            "macro_pulse": _macro_pulse(conn),
            "geopolitical_news": _geopolitical_news(conn, hours=36, per_category=3),
        }
    finally:
        conn.close()

    print(json.dumps(out, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
