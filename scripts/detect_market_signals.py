"""
시장 신호 자동 감지 — brief SKILL 가 시작할 때 호출해 "강한 사건" 을 놓치지 않게.

출력: JSON 배열. 각 항목 = {type, severity, details}
type 종류:
  · sidecar          — 코스피200 선물 ±5% 등 사이드카 발동 조건 충족 (heuristic)
  · circuit_breaker  — 코스피 ±8% 등 서킷브레이커 조건
  · new_high_52w     — 코스피 지수가 52주 신고가 권 (-2% 이내)
  · new_low_52w      — 코스피 지수가 52주 신저가 권 (+2% 이내)
  · decoupling_us_kr — 어제 미국 vs 오늘 한국 방향 정반대 + 차이 ≥ 3%p
  · limit_up_surge   — 오늘 상한가 종목 ≥ 5일 평균 × 2
  · foreign_extreme_sell — 외인 5일 누적 ≤ -3조
  · foreign_extreme_buy  — 외인 5일 누적 ≥ +3조
  · low_volatility   — 코스피 일일 변동 ±0.5% 미만 + 거래대금 평소 80% 이하
  · high_volatility  — 코스피 일중 고가-저가 차이 ≥ 3% (whipsaw 후보)

각 SKILL 은 brief 작성 직전 이 스크립트 호출 → 본문에 사건 반영.
사용:
    python scripts/detect_market_signals.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("USE_POSTGRES", "1")

from server.db.connections import get_stocks_conn  # noqa: E402


def _safe_float(v) -> float:
    try:
        if v is None or str(v).strip() in ("", "-"):
            return 0.0
        return float(str(v).replace(",", ""))
    except Exception:
        return 0.0


def _detect_index_signals(conn) -> list[dict]:
    """global_indicators 의 KOSPI · KOSDAQ · 미국 지수 비교."""
    out = []
    try:
        rows = conn.execute(
            """
            SELECT symbol, display_name, price, change_pct
            FROM global_indicators
            WHERE symbol IN ('KOSPI','KOSDAQ','SP500','NASDAQ','DOW','SOX','VIX')
               OR display_name IN ('KOSPI','KOSDAQ','SP500','NASDAQ','DOW','SOX','VIX')
            """
        ).fetchall()
    except Exception:
        return out

    idx = {}
    for r in rows:
        d = dict(r) if hasattr(r, "keys") else {}
        key = d.get("symbol") or d.get("display_name")
        idx[key] = {
            "value": _safe_float(d.get("price")),
            "change_pct": _safe_float(d.get("change_pct")),
        }

    kospi = idx.get("KOSPI") or {}
    sp500 = idx.get("SP500") or {}

    # 사이드카 heuristic — 코스피 ±5% 이상 (실 KRX 사이드카는 코스피200 선물 ±5%·1분 지속)
    if kospi.get("change_pct") and abs(kospi["change_pct"]) >= 5:
        out.append({
            "type": "sidecar",
            "severity": "high",
            "details": {"kospi_change_pct": kospi["change_pct"]},
            "phrase_hint": f"코스피 {kospi['change_pct']:+.2f}% — 사이드카 발동 조건 부근",
        })

    # 서킷 브레이커 heuristic — 코스피 ±8%
    if kospi.get("change_pct") and abs(kospi["change_pct"]) >= 8:
        out.append({
            "type": "circuit_breaker",
            "severity": "extreme",
            "details": {"kospi_change_pct": kospi["change_pct"]},
            "phrase_hint": f"코스피 {kospi['change_pct']:+.2f}% — 서킷 브레이커 조건",
        })

    # 디커플링 — 미국 vs 한국 방향 정반대 + 차이 ≥ 3%p
    if sp500.get("change_pct") and kospi.get("change_pct"):
        us = sp500["change_pct"]
        kr = kospi["change_pct"]
        if us * kr < 0 and abs(us - kr) >= 3:
            out.append({
                "type": "decoupling_us_kr",
                "severity": "high",
                "details": {"sp500_change_pct": us, "kospi_change_pct": kr, "gap": abs(us - kr)},
                "phrase_hint": f"미국 {us:+.2f}% 와 한국 {kr:+.2f}% 가 정반대 — 흔치 않은 디커플링",
            })

    # 코스피 변동성 — 낮음(±0.5%↓) 또는 높음(일중 H-L ≥ 3%)
    if kospi.get("change_pct") is not None and abs(kospi["change_pct"]) < 0.5:
        out.append({
            "type": "low_volatility",
            "severity": "info",
            "details": {"kospi_change_pct": kospi["change_pct"]},
            "phrase_hint": f"코스피 {kospi['change_pct']:+.2f}% 보합권 — 잠잠한 날",
        })

    return out


def _detect_52w_signals(conn) -> list[dict]:
    """KOSPI 지수 자체 52주 신고/신저 근접."""
    out = []
    try:
        row = conn.execute(
            """
            SELECT MAX(close) AS hi, MIN(close) AS lo
            FROM price_daily
            WHERE date >= TO_CHAR(CURRENT_DATE - INTERVAL '365 days', 'YYYYMMDD')
              AND code = 'KOSPI'
            """
        ).fetchone()
        # 종목 단위 price_daily 에 KOSPI 자체가 없을 수 있음 — 지수는 global_indicators 사용
    except Exception:
        row = None

    # 폴백 — global_indicators 의 KOSPI 값을 보고 신고가/신저가 근접 추정은 불가 (52주 데이터 없음)
    # → 별도 KOSPI index history 필요. 일단 종목 풀에서 52주 신고가 근접 종목 수 카운트로 대체.
    try:
        row2 = conn.execute(
            """
            SELECT COUNT(*) FILTER (WHERE pt.current_price >= ne.high_52w * 0.98) AS near_high,
                   COUNT(*) FILTER (WHERE pt.current_price <= ne.low_52w * 1.02) AS near_low,
                   COUNT(*) AS total
            FROM price_today pt JOIN naver_extended ne USING (code)
            WHERE pt.current_price > 0 AND ne.high_52w > 0
            """
        ).fetchone()
        d = dict(row2) if hasattr(row2, "keys") else {}
        nh = int(d.get("near_high") or 0)
        nl = int(d.get("near_low") or 0)
        total = int(d.get("total") or 1)
        if nh >= max(50, total * 0.05):
            out.append({
                "type": "new_high_52w_breadth",
                "severity": "high",
                "details": {"near_high_count": nh, "total": total, "ratio": round(nh / total, 3)},
                "phrase_hint": f"52주 신고가 근접 {nh}개 종목 — 시장 폭넓게 강세",
            })
        if nl >= max(50, total * 0.05):
            out.append({
                "type": "new_low_52w_breadth",
                "severity": "high",
                "details": {"near_low_count": nl, "total": total, "ratio": round(nl / total, 3)},
                "phrase_hint": f"52주 신저가 근접 {nl}개 종목 — 약세 폭넓음",
            })
    except Exception:
        pass

    return out


def _detect_limit_up_surge(conn) -> list[dict]:
    """오늘 상한가 / 급등 종목 수 vs 평소 평균 (최근 20일)."""
    out = []
    try:
        today_count = conn.execute(
            "SELECT COUNT(*) AS n FROM price_today WHERE change_pct >= 29.0"
        ).fetchone()
        d = dict(today_count) if hasattr(today_count, "keys") else {}
        today_n = int(d.get("n") or 0)
    except Exception:
        return out

    # 평소 평균 — 최근 20 영업일 price_daily 에서 change 계산
    try:
        rows = conn.execute(
            """
            WITH daily_limits AS (
                SELECT date,
                       COUNT(*) FILTER (
                         WHERE close > 0 AND open > 0 AND (close - LAG(close) OVER (PARTITION BY code ORDER BY date)) / NULLIF(LAG(close) OVER (PARTITION BY code ORDER BY date), 0) * 100 >= 29
                       ) AS limit_up_count
                FROM price_daily
                WHERE date >= TO_CHAR(CURRENT_DATE - INTERVAL '30 days', 'YYYYMMDD')
                GROUP BY date
            )
            SELECT AVG(limit_up_count)::numeric(10,2) AS avg_n FROM daily_limits
            """
        ).fetchall()
        avg_n = float((dict(rows[0]) if rows else {}).get("avg_n") or 0)
    except Exception:
        avg_n = 0.0

    if today_n >= 5 and avg_n > 0 and today_n >= avg_n * 2:
        out.append({
            "type": "limit_up_surge",
            "severity": "high",
            "details": {"today": today_n, "avg_20d": round(avg_n, 1), "ratio": round(today_n / avg_n, 1)},
            "phrase_hint": f"오늘 상한가 {today_n}종목 — 평소 {avg_n:.0f}종목의 약 {today_n / max(avg_n, 1):.1f}배",
        })
    elif today_n >= 8:
        # 평균 못 구해도 8개 이상이면 surge 로 본다
        out.append({
            "type": "limit_up_surge",
            "severity": "high",
            "details": {"today": today_n, "avg_20d": None},
            "phrase_hint": f"오늘 상한가 {today_n}종목 — 평소 보기 드문 수준",
        })

    return out


def _detect_foreign_extreme(conn) -> list[dict]:
    """외인 5일 누적 ±3조 ↑ 극단치."""
    out = []
    try:
        row = conn.execute(
            """
            SELECT SUM(foreign_net_amt) AS sum_5d
            FROM (
                SELECT foreign_net_amt,
                       ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
                FROM investor_flow
            ) t WHERE rn <= 5
            """
        ).fetchone()
        d = dict(row) if hasattr(row, "keys") else {}
        # foreign_net_amt 단위 = 백만원. 3조원 = 3,000,000 백만원
        total_baekman = int(d.get("sum_5d") or 0)
        total_eok = total_baekman / 100  # 억원
    except Exception:
        return out

    if total_eok >= 30000:
        out.append({
            "type": "foreign_extreme_buy",
            "severity": "high",
            "details": {"foreign_5d_eok": int(total_eok)},
            "phrase_hint": f"외국인 5일 누적 +{int(total_eok / 1000):.1f}조 — 강한 매수 우위",
        })
    elif total_eok <= -30000:
        out.append({
            "type": "foreign_extreme_sell",
            "severity": "high",
            "details": {"foreign_5d_eok": int(total_eok)},
            "phrase_hint": f"외국인 5일 누적 -{int(abs(total_eok) / 1000):.1f}조 — 강한 매도 우위",
        })

    return out


def main():
    conn = get_stocks_conn()
    signals: list[dict] = []
    try:
        signals.extend(_detect_index_signals(conn))
        signals.extend(_detect_52w_signals(conn))
        signals.extend(_detect_limit_up_surge(conn))
        signals.extend(_detect_foreign_extreme(conn))
    finally:
        try:
            conn.close()
        except Exception:
            pass

    out = {
        "detected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(signals),
        "signals": signals,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
