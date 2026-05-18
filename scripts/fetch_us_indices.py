"""
미국 3대 지수(S&P 500 / Nasdaq / Dow) 시세 수집 → Postgres macro.usa_indices_json

매일 KST 07:00 이후 (미국장 마감 후) 실행 권장.
크론 예:
  # 매일 한국시간 오전 7시 5분
  5 7 * * * cd /path/to/투자정보 && python scripts/fetch_us_indices.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Any, Dict

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "server"))


import yfinance as yf

from server.db.connections import get_stocks_conn

# 포맷: { 응답 키: (yfinance ticker, 표시명) }
INDEX_MAP = {
    "sp500": ("^GSPC", "S&P 500"),
    "nasdaq": ("^IXIC", "Nasdaq"),
    "dow": ("^DJI", "Dow"),
}


def fetch_one(ticker: str, label: str) -> Dict[str, Any] | None:
    """yfinance로 최근 2일치 종가를 받아 변화율 계산."""
    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period="5d", auto_adjust=False)
        if hist is None or hist.empty or len(hist) < 2:
            print(f"  [WARN] {label}({ticker}) 데이터 부족")
            return None
        last = hist.iloc[-1]
        prev = hist.iloc[-2]
        close = float(last["Close"])
        prev_close = float(prev["Close"])
        change_amt = close - prev_close
        change_pct = (change_amt / prev_close * 100.0) if prev_close else 0.0
        session_date = str(hist.index[-1].date())

        return {
            "label": label,
            "ticker": ticker,
            "price": f"{close:,.2f}",
            "price_numeric": round(close, 2),
            "change_pct": round(change_pct, 2),
            "change_amt": round(change_amt, 2),
            "session_date": session_date,
        }
    except Exception as e:
        print(f"  [ERR] {label}({ticker}): {type(e).__name__}: {e}")
        return None


def main() -> int:
    print("=" * 60)
    print(f"  미국 3대 지수 수집 시작 — {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    result: Dict[str, Any] = {}
    for key, (ticker, label) in INDEX_MAP.items():
        print(f"  fetching {label} ({ticker})...", end=" ", flush=True)
        data = fetch_one(ticker, label)
        if data:
            result[key] = data
            print(f"OK  close={data['price']}  chg={data['change_pct']:+.2f}%")

    if not result:
        print("\n[FAIL] 3개 전부 실패")
        return 1

    # Postgres macro 테이블 upsert (id=1 단일 행)
    conn = get_stocks_conn()
    try:
        # 기존 macro 행 조회
        row = conn.execute("SELECT id FROM macro WHERE id = 1").fetchone()
        if row:
            conn.execute(
                """UPDATE macro
                   SET usa_indices_json = ?, updated_at = ?
                   WHERE id = 1""",
                (
                    json.dumps(result, ensure_ascii=False),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
        else:
            conn.execute(
                """INSERT INTO macro (id, usa_indices_json, updated_at)
                   VALUES (1, ?, ?)""",
                (
                    json.dumps(result, ensure_ascii=False),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
        conn.commit()
        print(f"\n[OK] macro.usa_indices_json 업데이트 ({len(result)}개 지수)")
    except Exception as e:
        print(f"\n[DB ERR] {type(e).__name__}: {e}")
        return 2
    finally:
        try:
            conn.close()
        except Exception:
            pass

    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
