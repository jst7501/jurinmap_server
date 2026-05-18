"""
sync_global_indicators.py
─────────────────────────────────────────────────────────────
홈 "글로벌 매크로 대시보드" 용 지표를 배치 수집해 Postgres
`global_indicators` 테이블에 upsert.

소스:
  - Yahoo Finance (yfinance): 지수·선물·원자재·크립토·환율·채권
  - alternative.me: Fear & Greed 지수 (crypto 기반, 시장 심리 프록시)

Usage:
  python scripts/sync_global_indicators.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Optional

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import requests
import yfinance as yf

from server.db.connections import get_stocks_conn


# ─── 심볼 카탈로그 ──────────────────────────────────────────────────
# symbol(내부 id) / display_name(UI) / emoji / yahoo source_symbol / category
CATALOG = [
    # 한국 (korea)
    ("KOSPI",         "코스피",        "🇰🇷", "^KS11",    "korea"),
    ("KOSDAQ",        "코스닥",        "🇰🇷", "^KQ11",    "korea"),
    ("KRW_USD",       "원/달러",       "💱", "KRW=X",    "korea"),
    ("KODEX200",      "KODEX 200",   "📊", "069500.KS","korea"),

    # 글로벌 지수/선물 (global)
    ("NASDAQ",        "나스닥",        "🇺🇸", "^IXIC",    "global"),
    ("NASDAQ_FUT",    "나스닥 선물",    "🇺🇸", "NQ=F",     "global"),
    ("SP500",         "S&P 500",     "🇺🇸", "^GSPC",    "global"),
    ("SP500_FUT",     "S&P 500 선물", "🇺🇸", "ES=F",     "global"),
    ("SOX",           "반도체(SOX)",   "🇺🇸", "^SOX",     "global"),
    ("VIX",           "VIX 공포지수",   "📊", "^VIX",     "global"),
    ("NIKKEI",        "니케이225",     "🇯🇵", "^N225",    "global"),
    ("EWY",           "EWY (한국ETF)", "🇰🇷", "EWY",      "global"),
    ("KORU",          "KORU (3X 한국)","🇰🇷", "KORU",     "global"),
    ("DXY",           "달러인덱스",     "💵", "DX=F",     "global"),

    # 채권/금리 (bonds)
    ("US10Y",         "미국 10년물",   "📈", "^TNX",     "bonds"),
    ("US2Y",          "미국 2년물",    "📉", "^FVX",     "bonds"),  # TNX=10Y, FVX=5Y, IRX=13주 — US 2Y 직접 심볼 없음. FVX(5Y)로 근사

    # 원자재 (commodity)
    ("GOLD",          "금",          "🥇", "GC=F",     "commodity"),
    ("SILVER",        "은",          "🥈", "SI=F",     "commodity"),
    ("COPPER",        "구리",         "🔶", "HG=F",     "commodity"),
    ("WTI",           "WTI",         "🛢", "CL=F",     "commodity"),
    ("BRENT",         "브렌트",        "🛢", "BZ=F",     "commodity"),
    ("NATGAS",        "천연가스",       "🔥", "NG=F",     "commodity"),

    # 크립토 (crypto)
    ("BTC",           "비트코인",      "₿", "BTC-USD",  "crypto"),
    ("ETH",           "이더리움",      "Ξ", "ETH-USD",  "crypto"),
    ("XRP",           "리플",         "✕", "XRP-USD",  "crypto"),
    ("SOL",           "솔라나",        "◎", "SOL-USD",  "crypto"),
]


def fetch_kis_index(index_code: str) -> Optional[dict]:
    """KIS 일별 지수차트로 KOSPI/KOSDAQ 현재가·등락률 조회.

    yfinance(^KS11/^KQ11) 가 stale 한 경우의 1차 소스. 첫 행 = 가장 최근 영업일이라
    장중에는 실시간 반영(보통 30~60초 지연), 마감 후엔 당일 종가.

    KOSDAQ 코드 fallback: 1001 → 1002 (collectors.kis_api 의 fetch_market_flow 코드
    주석 참조 — KIS 가 KOSDAQ 종합지수에 1001/1002 둘 다 받지만 일부 TR ID 에서
    1001 이 rt_cd=9 실패). 안전하게 둘 다 시도.
    """
    try:
        from collectors.kis_api import KISCollector
    except Exception as e:
        print(f"  [kis-index] collector import failed: {e}", flush=True)
        return None

    try:
        c = KISCollector()
    except Exception as e:
        print(f"  [kis-index] collector init failed: {e}", flush=True)
        return None

    candidates = [index_code]
    if index_code == "1001":
        candidates.append("1002")  # KOSDAQ 폴백 코드

    rows = []
    for code in candidates:
        try:
            rows = c.get_index_daily(code) or []
        except Exception as e:
            print(f"  [kis-index] get_index_daily({code}) error: {e}", flush=True)
            rows = []
        if rows:
            break

    if not rows:
        return None

    r = rows[0]
    close = r.get("close")
    change_pct = r.get("change_pct")
    if close is None or close <= 0:
        return None

    # change_amt 는 collector 가 안 노출 — close * pct/100 로 역산
    change_amt = None
    try:
        if change_pct is not None:
            change_amt = round(float(close) * float(change_pct) / 100.0, 2)
    except Exception:
        change_amt = None

    return {
        "price": float(close),
        "change_pct": float(change_pct) if change_pct is not None else None,
        "change_amt": change_amt,
        "extra": {"as_of_date": r.get("date"), "source_detail": "kis_inquire-daily-indexchartprice"},
    }


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fetch_yahoo_quote(symbol: str) -> Optional[dict]:
    """단일 심볼 현재가·전일종가·변동률 조회. 실패 시 None."""
    try:
        t = yf.Ticker(symbol)
        # fast_info 가 가장 빠름. 실패 시 history fallback
        fi = t.fast_info
        price = None
        prev = None
        try:
            price = float(fi.get("last_price") if hasattr(fi, "get") else getattr(fi, "last_price", None))
        except Exception:
            price = None
        try:
            prev = float(fi.get("previous_close") if hasattr(fi, "get") else getattr(fi, "previous_close", None))
        except Exception:
            prev = None

        if price is None or prev is None or prev == 0:
            # fallback: 최근 5일 종가
            hist = t.history(period="5d", auto_adjust=False)
            if hist is None or len(hist) < 2:
                return None
            price = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
            if prev == 0:
                return None

        change_amt = price - prev
        change_pct = (change_amt / prev) * 100.0
        return {
            "price": price,
            "prev": prev,
            "change_amt": change_amt,
            "change_pct": change_pct,
        }
    except Exception as e:
        return None


def fetch_fear_and_greed() -> Optional[dict]:
    """alternative.me 에서 F&G (crypto 기반이지만 공포·탐욕 전반 심리 프록시)."""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=2", timeout=8)
        r.raise_for_status()
        data = r.json().get("data") or []
        if len(data) < 1:
            return None
        now_val = float(data[0].get("value") or 0)
        prev_val = float(data[1].get("value") or now_val) if len(data) > 1 else now_val
        change_amt = now_val - prev_val
        change_pct = (change_amt / prev_val * 100.0) if prev_val else 0.0
        classification = data[0].get("value_classification") or ""
        return {
            "price": now_val,
            "change_amt": change_amt,
            "change_pct": change_pct,
            "extra": {"classification": classification},
        }
    except Exception:
        return None


# ─── 공탐지수 자체 계산기 (CNN F&G 방식 KOSPI 로컬라이징) ────────────
#
# 2026-04-21 사용자 요청: alternative.me는 **암호화폐** 공포탐욕 지수.
# 한국 주식 시장용으로 5개 서브지표를 합성해 0-100 점수 산출.
#
# 서브지표 (각 0-100 점수화 후 가중평균):
#   1. Momentum  (20%): KOSPI 현재가 vs 125일 MA 편차
#   2. Volatility(20%): KOSPI 30일 수익률 표준편차 vs 1년 평균 표준편차
#   3. SafeHaven (20%): KOSPI 20일 누적 수익률 (양수=탐욕)
#   4. Strength  (20%): 52주 신고가 근접 종목 vs 신저가 근접 종목 비율
#   5. Breadth   (20%): 오늘 상승 종목 / (상승+하락) 비율
#
# 0 = 극한 공포, 100 = 극한 탐욕.

def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _compute_momentum_score(closes) -> Optional[float]:
    """KOSPI vs 125일 MA. +10% → 85, -10% → 15 선형."""
    if len(closes) < 125:
        return None
    ma125 = closes.tail(125).mean()
    if ma125 <= 0:
        return None
    gap_pct = (float(closes.iloc[-1]) / float(ma125) - 1.0) * 100.0
    return _clamp(50.0 + gap_pct * 3.5)


def _compute_volatility_score(closes) -> Optional[float]:
    """30일 변동성 vs 1년 평균 변동성. 낮을수록 탐욕."""
    if len(closes) < 60:
        return None
    returns = closes.pct_change().dropna()
    if len(returns) < 60:
        return None
    vol_30 = float(returns.tail(30).std())
    vol_avg = float(returns.std())
    if vol_avg <= 0:
        return None
    ratio = vol_30 / vol_avg
    # ratio=1 → 50, ratio=1.5 → 25, ratio=0.5 → 75
    return _clamp(100.0 - (ratio - 1.0) * 50.0)


def _compute_safehaven_score(closes) -> Optional[float]:
    """20일 누적 수익률. +5% → 75, -5% → 25."""
    if len(closes) < 21:
        return None
    ret20 = (float(closes.iloc[-1]) / float(closes.iloc[-21]) - 1.0) * 100.0
    return _clamp(50.0 + ret20 * 5.0)


def _compute_strength_score(conn) -> Optional[float]:
    """52주 신고가 근접(>=95%) 종목 수 vs 신저가 근접(<=105%) 종목 수.
    신고가 많을수록 탐욕 구간.
    """
    try:
        row = conn.execute(
            """
            SELECT
              SUM(CASE WHEN pt.price >= ne.high_52w * 0.95 THEN 1 ELSE 0 END) AS near_high,
              SUM(CASE WHEN pt.price <= ne.low_52w  * 1.05 THEN 1 ELSE 0 END) AS near_low
            FROM price_today pt
            JOIN naver_extended ne ON ne.code = pt.code
            WHERE ne.high_52w IS NOT NULL
              AND ne.low_52w  IS NOT NULL
              AND pt.price    IS NOT NULL
            """
        ).fetchone()
        high_cnt = int(row[0] or 0)
        low_cnt = int(row[1] or 0)
        denom = high_cnt + low_cnt
        if denom < 5:
            return None
        return _clamp((high_cnt / denom) * 100.0)
    except Exception:
        return None


def _compute_breadth_score(conn) -> Optional[float]:
    """오늘 상승 종목 / (상승+하락) 비율. 50 → 50, 60 → 70."""
    try:
        row = conn.execute(
            """
            SELECT
              SUM(CASE WHEN COALESCE(change_pct,0) > 0 THEN 1 ELSE 0 END) AS up,
              SUM(CASE WHEN COALESCE(change_pct,0) < 0 THEN 1 ELSE 0 END) AS down
            FROM price_today
            """
        ).fetchone()
        up = int(row[0] or 0)
        down = int(row[1] or 0)
        denom = up + down
        if denom < 10:
            return None
        ratio = up / denom  # 0.0 ~ 1.0
        # 0.5 → 50점, 0.6 → 70점, 0.4 → 30점 (선형)
        return _clamp(50.0 + (ratio - 0.5) * 200.0)
    except Exception:
        return None


def _classify_fg(score: float) -> str:
    if score < 20:
        return "극한 공포"
    if score < 40:
        return "공포"
    if score < 60:
        return "중립"
    if score < 80:
        return "탐욕"
    return "극한 탐욕"


def compute_kospi_fear_greed(conn) -> Optional[dict]:
    """CNN F&G 방식을 KOSPI에 맞춘 5지표 합성 공탐지수."""
    try:
        kospi = yf.Ticker("^KS11").history(period="1y", auto_adjust=False)
        if kospi is None or len(kospi) < 125:
            return None
        closes = kospi["Close"]
    except Exception:
        return None

    subs = {
        "momentum":  _compute_momentum_score(closes),
        "volatility": _compute_volatility_score(closes),
        "safehaven": _compute_safehaven_score(closes),
        "strength":  _compute_strength_score(conn),
        "breadth":   _compute_breadth_score(conn),
    }
    valid = [v for v in subs.values() if v is not None]
    if len(valid) < 3:
        return None
    score = sum(valid) / len(valid)

    # 전일 점수 조회해서 change 계산 (global_indicators 테이블 과거값)
    prev = None
    try:
        row = conn.execute(
            "SELECT price FROM global_indicators WHERE symbol = 'KOSPI_FG'"
        ).fetchone()
        if row and row[0] is not None:
            prev = float(row[0])
    except Exception:
        prev = None

    change_amt = (score - prev) if prev is not None else 0.0
    change_pct = (change_amt / prev * 100.0) if prev else 0.0

    return {
        "price": round(score, 1),
        "change_amt": round(change_amt, 1),
        "change_pct": round(change_pct, 2),
        "extra": {
            "classification": _classify_fg(score),
            "subs": {k: (round(v, 1) if v is not None else None) for k, v in subs.items()},
            "note": "CNN F&G 방식 KOSPI 로컬라이징 (5지표 평균)",
        },
    }


# ─── 커플링지수 (KOSPI × S&P 500 30일 상관계수) ─────────────────────
#
# 한국 주식이 미국 시장을 얼마나 따라가는지. 양수=동조, 음수=탈동조.
# -100 ~ +100 범위.

def compute_coupling_index() -> Optional[dict]:
    try:
        kospi = yf.Ticker("^KS11").history(period="90d", auto_adjust=False)
        sp500 = yf.Ticker("^GSPC").history(period="90d", auto_adjust=False)
        if kospi is None or sp500 is None or len(kospi) < 40 or len(sp500) < 40:
            return None
        kr = kospi["Close"].pct_change().dropna()
        us = sp500["Close"].pct_change().dropna()

        # 날짜 정규화 (timezone 제거)
        kr.index = kr.index.tz_localize(None) if kr.index.tz else kr.index
        us.index = us.index.tz_localize(None) if us.index.tz else us.index
        # 한국은 미국보다 하루 먼저 개장 → 같은 날짜 기준 단순 corr
        common = kr.index.intersection(us.index)
        if len(common) < 20:
            return None
        kr_a = kr.reindex(common).tail(30)
        us_a = us.reindex(common).tail(30)
        if len(kr_a) < 20 or len(us_a) < 20:
            return None
        corr = float(kr_a.corr(us_a))
        if corr != corr:  # NaN check
            return None
        score = round(corr * 100.0, 1)  # -100 ~ +100
    except Exception:
        return None

    return {
        "price": score,
        "change_amt": 0,
        "change_pct": 0,
        "extra": {
            "period_days": len(kr_a),
            "corr": round(corr, 4),
            "note": "KOSPI × S&P 500 일수익률 30일 상관계수 × 100",
        },
    }


def upsert_indicator(conn, symbol, display_name, category, emoji, source, source_symbol, q, ts):
    extra_json = json.dumps(q.get("extra") or {}, ensure_ascii=False) if q else None
    conn.execute(
        """
        INSERT INTO global_indicators(
            symbol, display_name, category, emoji, source, source_symbol,
            price, change_pct, change_amt, currency, extra_json, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(symbol) DO UPDATE SET
            display_name=excluded.display_name,
            category=excluded.category,
            emoji=excluded.emoji,
            source=excluded.source,
            source_symbol=excluded.source_symbol,
            price=excluded.price,
            change_pct=excluded.change_pct,
            change_amt=excluded.change_amt,
            currency=excluded.currency,
            extra_json=excluded.extra_json,
            updated_at=excluded.updated_at
        """,
        (
            symbol,
            display_name,
            category,
            emoji,
            source,
            source_symbol,
            (q.get("price") if q else None),
            (q.get("change_pct") if q else None),
            (q.get("change_amt") if q else None),
            None,
            extra_json,
            ts,
        ),
    )


def main():
    ts = now_ts()
    print(f"[global_indicators] start {ts}", flush=True)
    ok = 0
    fail = 0

    conn = get_stocks_conn()
    try:
        # KOSPI/KOSDAQ 는 KIS 우선 (yfinance ^KS11/^KQ11 가 자주 stale).
        # KIS 응답 0 → yahoo 폴백.
        KIS_INDEX_OVERRIDES = {"KOSPI": "0001", "KOSDAQ": "1001"}

        for entry in CATALOG:
            symbol, display_name, emoji, yahoo_sym, category = entry

            kis_code = KIS_INDEX_OVERRIDES.get(symbol)
            q = None
            source = "yahoo"
            source_sym = yahoo_sym

            if kis_code:
                q = fetch_kis_index(kis_code)
                if q is not None:
                    source = "kis"
                    source_sym = kis_code
                else:
                    print(f"  [kis-fallback] {symbol} → yahoo {yahoo_sym}", flush=True)

            if q is None:
                q = fetch_yahoo_quote(yahoo_sym)

            if q is None:
                print(f"  [fail] {symbol:14s} (kis={kis_code or '-'}, yahoo={yahoo_sym})", flush=True)
                fail += 1
                # 실패해도 row 유지: 값만 None 으로 넣고 source/name 업데이트
                upsert_indicator(conn, symbol, display_name, category, emoji,
                                 source, source_sym, {}, ts)
                continue
            upsert_indicator(conn, symbol, display_name, category, emoji,
                             source, source_sym, q, ts)
            ok += 1
            pct = q.get("change_pct") or 0
            tag = "kis" if source == "kis" else "yh "
            print(f"  [ok-{tag}] {symbol:14s} {q['price']:>12,.2f}  {pct:+.2f}%", flush=True)

        # Fear & Greed (alternative.me) — 크립토 기반 공포탐욕 (글로벌 심리 프록시)
        fg = fetch_fear_and_greed()
        if fg:
            upsert_indicator(conn, "FEAR_GREED", "공포&탐욕 (크립토)", "global", "😱",
                             "alternative.me", "fng", fg, ts)
            ok += 1
            cls = (fg.get("extra") or {}).get("classification", "")
            print(f"  [ok]   FEAR_GREED     {fg['price']:>12,.0f}  ({cls})", flush=True)
        else:
            print(f"  [fail] FEAR_GREED", flush=True)
            fail += 1

        # 2026-04-21 롤백: KOSPI_FG / COUPLING 호출 보류. 프론트 롤백과 짝 맞춤.
        # compute_kospi_fear_greed / compute_coupling_index 함수는 남겨두고, 재도입 시 아래 블록 해제.
        #
        # kfg = compute_kospi_fear_greed(conn)
        # if kfg:
        #     upsert_indicator(conn, "KOSPI_FG", "공탐지수", "korea", None,
        #                      "internal", "cnn_fg_localized", kfg, ts)
        #     ok += 1
        #     cls = (kfg.get("extra") or {}).get("classification", "")
        #     print(f"  [ok]   KOSPI_FG       {kfg['price']:>12,.1f}  ({cls})", flush=True)
        # else:
        #     print(f"  [fail] KOSPI_FG", flush=True)
        #     fail += 1
        #
        # cpl = compute_coupling_index()
        # if cpl:
        #     upsert_indicator(conn, "COUPLING", "커플링지수", "korea", None,
        #                      "internal", "kospi_sp_corr30", cpl, ts)
        #     ok += 1
        #     print(f"  [ok]   COUPLING       {cpl['price']:>12,.1f}", flush=True)
        # else:
        #     print(f"  [fail] COUPLING", flush=True)
        #     fail += 1

        conn.commit()
    finally:
        conn.close()

    print(f"[global_indicators] done ok={ok} fail={fail}", flush=True)


if __name__ == "__main__":
    main()
