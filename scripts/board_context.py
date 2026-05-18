"""
종토방 컨텍스트 수집 — 네이버 종토방 크롤링 + 기본 키워드 점수.

Claude Code 루틴이 각 종목에 대해 이 스크립트 호출 → 나온 JSON 을 컨텍스트로
받아 **스스로 민심 분석** 수행. 외부 AI API 없음.

사용법:
    python scripts/board_context.py --code 000660
    python scripts/board_context.py --code 000660 --pages 3

출력 (stdout, JSON):
{
  "code": "000660",
  "pages": 3,
  "posts_total": 60,
  "titles_recent": ["...", ...],         # 최근 20개 제목
  "titles_hot":    [{"title":"...", "replies":N, "good":N}, ...],  # 댓글·공감 TOP 10
  "keyword_score": 62,                    # 0-100 (50=중립, euphoria-despair 비율 기반)
  "mood_label":    "낙관",                # "환희"/"낙관"/"중립"/"불안"/"공포"
  "euphoria_count": N,
  "despair_count":  N,
  "top_euphoria":  [["가즈아", 5], ...],
  "top_despair":   [["물림", 3], ...],
  "updated_at":    "YYYY-MM-DD HH:MM:SS"
}
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


from scrapers.naver_board_scraper import NaverBoardScraper  # noqa: E402


def collect_context(code: str, pages: int = 3) -> dict:
    scraper = NaverBoardScraper()
    # scraper 가 stdout 에 진행 로그 print — JSON 오염 방지용 redirect
    with contextlib.redirect_stdout(io.StringIO()):
        posts = scraper.scrape(code, pages=pages) or []
        sentiment = scraper.analyze_sentiment(posts) if posts else None

    titles_recent = [p.get("title", "") for p in posts[:20]]
    titles_hot_raw = sorted(
        posts,
        key=lambda p: (int(p.get("replies") or 0) * 2 + int(p.get("good") or 0)),
        reverse=True,
    )[:10]
    titles_hot = [
        {
            "title": p.get("title", ""),
            "replies": int(p.get("replies") or 0),
            "good": int(p.get("good") or 0),
        }
        for p in titles_hot_raw
    ]

    result: dict = {
        "code": code,
        "pages": pages,
        "posts_total": len(posts),
        "titles_recent": titles_recent,
        "titles_hot": titles_hot,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    if sentiment:
        result.update({
            "keyword_score": sentiment.get("score"),
            "mood_label": sentiment.get("mood"),
            "euphoria_count": sentiment.get("euphoria_count", 0),
            "despair_count": sentiment.get("despair_count", 0),
            "top_euphoria": sentiment.get("top_euphoria", [])[:5],
            "top_despair": sentiment.get("top_despair", [])[:5],
        })
    else:
        result.update({
            "keyword_score": None,
            "mood_label": "데이터 없음",
            "euphoria_count": 0,
            "despair_count": 0,
            "top_euphoria": [],
            "top_despair": [],
        })

    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True, help="6자리 종목 코드")
    ap.add_argument("--pages", type=int, default=3, help="크롤링 페이지 수 (기본 3, 약 60개 포스트)")
    args = ap.parse_args()

    code = args.code.strip().zfill(6)
    ctx = collect_context(code, pages=args.pages)
    print(json.dumps(ctx, ensure_ascii=False))


if __name__ == "__main__":
    main()
