"""
미국 테마 DB 일일 갱신 — 테마 테이블 재로드 + yfinance 시세/메타 갱신.

- data/theme_raw_us.txt 이 바뀌면 stock_themes_us 재구성
- price_today_us 시세 일괄 갱신
- us_stocks 메타(name/sector/market_cap) 갱신

크론 예:
  # 매일 한국시간 오전 7시 (미국장 마감 후)
  0 7 * * * cd /path/to/투자정보 && python scripts/refresh_us_themes.py
"""
import os
import sys
import time

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)


def main():
    print("=" * 60)
    print(f"  US 테마 리프레시 시작 — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 1) 테마 매핑 재로드
    from scripts.parse_themes_us import main as parse_main
    parse_main()

    # 2) yfinance 시세 + 메타 갱신
    from collectors.us_theme_price_collector import run as collect_run
    summary = collect_run(fill_meta=True, meta_sleep=0.05)

    print(f"\n[완료] 대상 {summary['tickers']} | 시세 {summary['priced']} | 메타 {summary['meta']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
