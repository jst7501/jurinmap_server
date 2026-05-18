"""Reddit WallStreetBets (r/wallstreetbets) ticker mention 분석.

Reddit public JSON API 사용 (인증 없음, rate limit ~60/min).
WSB 의 hot/rising/new 게시물에서 ticker 언급 추출 + 점수/코멘트/upvote_ratio 가중.

핵심 사용처:
  1. 미국 종목 상세 페이지 → 이 종목이 WSB 에서 얼마나 자주 언급되는지 + 최근 post
  2. 미국 홈 → "WSB 핫 종목 TOP 10" (squeeze 후보 발굴)

Mention 인식 우선순위:
  $TICKER (명시적, 신뢰도 100%)
  ALL-CAPS TICKER (us_stocks 마스터에 존재할 때만 — false positive 방지)
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable, Optional

import requests

logger = logging.getLogger("collectors.us_wsb_sentiment")

_HEADERS = {"User-Agent": "JurinMapBot/1.0 (investment dashboard)"}
_FEEDS = ["hot", "new", "rising"]   # 3개 피드 통합
_FEED_LIMIT = 100                    # 각 피드 최대 100개
_BASE_URL = "https://www.reddit.com/r/wallstreetbets/{feed}.json"

# 영어 일반 단어이면서 동시에 ticker 인 ambigous set — 반드시 $ 접두사 있을 때만 인정
_AMBIGUOUS_TICKERS = {
    "GO", "SEE", "RUN", "LOW", "WELL", "ALL", "ANY", "FOR", "ONE", "TWO", "NOW", "NEW", "OLD",
    "HIT", "BIG", "BAD", "GET", "GOT", "ARE", "HAS", "OUR", "OUT", "DAY", "WAS", "WHO", "WHY",
    "TOP", "EAT", "FUN", "JOY", "JOB", "BUY", "TRY", "REAL", "REAL", "OPEN", "FREE", "BAD",
    "EVER", "NICE", "GOOD", "MEAN", "MOON", "BUZZ", "FAST", "PLAY", "LIVE", "LOVE", "LAND",
    "FAIL", "PUSH", "TURN", "FORM", "MOVE", "ROAD", "HOPE", "EDGE", "PUMP", "TASK", "BANK",
    "BEAT", "BEST", "DOG", "CAR", "AIR", "CAT", "NET", "GAS", "OIL", "FUN", "BIO", "CAP",
    "BIT", "TEN", "TWO", "BAR", "MOM", "DAD", "FUN", "TIP", "TAX", "WAY", "BOX",
    "ZERO", "FOUR", "FIVE", "TURN", "FALL", "LAST", "MEME", "RICH",
    "TWO", "WAY", "AWAY",
    "PR", "PT", "GG", "LP", "PE", "RR", "BP",
}

# ALL-CAPS 추출 시 무시할 일반 단어
_STOPWORDS = {
    # 영문 일반
    "THE", "AND", "FOR", "YOU", "ARE", "HAVE", "WITH", "FROM", "THIS", "THAT", "WAS", "WHEN",
    "WHAT", "WHO", "WHY", "HOW", "BUT", "NOT", "ONE", "TWO", "ALL", "ANY", "CAN", "WILL",
    "DID", "DO", "DOES", "GET", "GOT", "OUR", "OUT", "NEW", "NOW", "OFF", "ON", "OR", "ITS",
    "IT", "TO", "IS", "OF", "IN", "AS", "MY", "ME", "WE", "BE", "BY", "AT", "AN", "SO", "UP",
    # 투자 슬랭
    "WSB", "DD", "TA", "YOLO", "HODL", "FOMO", "FUD", "ATH", "ATL", "TLDR", "GUH", "PR", "PE",
    "EPS", "PT", "TP", "SL", "EV", "OG", "GOAT", "LOL", "LMAO", "ETF", "IPO", "CEO", "CFO",
    "CTO", "CMO", "COO", "SP", "CPI", "PPI", "GDP", "QQQ", "SPY", "IWM", "DIA", "VIX", "VXX",
    "AI", "VR", "AR", "ML", "API", "URL", "USA", "US", "UK", "EU", "EV", "ETF", "OEM",
    # 통화
    "USD", "EUR", "GBP", "JPY", "CNY", "CAD", "AUD", "CHF", "INR", "KRW", "HKD",
    # 거래소
    "NYSE", "NASDAQ", "SEC", "FED", "FOMC", "IRS", "FTC", "FBI", "CIA", "NSA",
    # 시간
    "AM", "PM", "EST", "PST", "ET", "UTC", "GMT",
    # 행동
    "BUY", "SELL", "OPEN", "PUT", "CALL", "ITM", "OTM", "ATM", "LONG", "SHORT",
    # 1글자
}

_TICKER_PAT = re.compile(r"\$([A-Z]{1,5})\b|\b([A-Z]{2,5})\b")

# 캐시 (60초 TTL — Reddit rate limit 보호)
_CACHE: dict = {}
_CACHE_TTL = 60


def _fetch_feed(feed: str = "hot", limit: int = 100) -> list[dict]:
    """단일 피드에서 게시물 list 반환."""
    url = _BASE_URL.format(feed=feed)
    try:
        r = requests.get(url, headers=_HEADERS, params={"limit": limit}, timeout=15)
        r.raise_for_status()
    except Exception as exc:
        logger.warning("WSB feed fetch failed (%s): %s", feed, exc)
        return []

    try:
        data = r.json()
        return [c["data"] for c in data.get("data", {}).get("children", [])]
    except Exception:
        return []


def _fetch_all_posts(feeds: Iterable[str] = _FEEDS) -> list[dict]:
    """캐시된 통합 피드. 60초 TTL."""
    now = time.time()
    cached = _CACHE.get("posts")
    if cached and now - cached["_ts"] < _CACHE_TTL:
        return cached["data"]

    all_posts: list[dict] = []
    seen_ids = set()
    for feed in feeds:
        for p in _fetch_feed(feed, _FEED_LIMIT):
            pid = p.get("id")
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                all_posts.append(p)

    _CACHE["posts"] = {"_ts": now, "data": all_posts}
    return all_posts


def _extract_tickers(text: str, valid_tickers: set[str]) -> list[str]:
    """텍스트에서 ticker 후보 추출. $ 접두 우선, ALL-CAPS 는 valid_tickers 에 있을 때만."""
    found: list[tuple[str, bool]] = []  # (ticker, is_explicit)
    for m in _TICKER_PAT.finditer(text):
        dollar_tk, raw_tk = m.group(1), m.group(2)
        if dollar_tk:
            t = dollar_tk.upper()
            if t and t not in _STOPWORDS:
                found.append((t, True))
        elif raw_tk:
            t = raw_tk.upper()
            if t in _STOPWORDS:
                continue
            # ALL-CAPS 는 known ticker 일 때만 인정
            if valid_tickers and t in valid_tickers:
                found.append((t, False))
    return [t for t, _ in found]


def _load_valid_tickers() -> set[str]:
    """us_stocks 마스터에서 ticker set 로드. 캐시 사용."""
    cached = _CACHE.get("tickers")
    if cached and time.time() - cached["_ts"] < 3600:
        return cached["data"]

    try:
        from server.db.connections import get_stocks_conn
        conn = get_stocks_conn()
        try:
            cur = conn.execute("SELECT ticker FROM us_stocks")
            tickers = {r[0].upper() for r in cur.fetchall() if r[0]}
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("us_stocks fetch failed: %s", exc)
        tickers = set()

    _CACHE["tickers"] = {"_ts": time.time(), "data": tickers}
    return tickers


def _post_to_dict(p: dict) -> dict:
    """Reddit post raw → 슬림 dict."""
    return {
        "id": p.get("id"),
        "title": p.get("title", "")[:200],
        "url": f"https://www.reddit.com{p.get('permalink', '')}",
        "score": int(p.get("score", 0)),
        "num_comments": int(p.get("num_comments", 0)),
        "upvote_ratio": float(p.get("upvote_ratio", 0.5)),
        "created_utc": int(p.get("created_utc", 0)),
        "author": p.get("author"),
        "flair": p.get("link_flair_text"),
        "is_video": bool(p.get("is_video")),
    }


def get_wsb_top_mentions(
    top_n: int = 15,
    min_mentions: int = 2,
) -> dict:
    """WSB hot/new/rising 통합에서 ticker 언급 랭킹.

    Returns:
      {
        "data": [
          {
            "symbol": "TSLA",
            "mention_count": 12,
            "explicit_count": 8,         # $TSLA 명시 횟수
            "score_sum": 4521,           # 언급된 글들의 upvote 합
            "comment_sum": 832,
            "avg_upvote_ratio": 0.78,
            "top_post": {...}            # score 가장 높은 글
          }
        ],
        "as_of": "2026-05-14T...",
        "post_pool_size": 245
      }
    """
    valid = _load_valid_tickers()
    posts = _fetch_all_posts()

    stats: dict[str, dict] = defaultdict(lambda: {
        "symbol": "",
        "mention_count": 0,
        "explicit_count": 0,
        "score_sum": 0,
        "comment_sum": 0,
        "_ratio_total": 0.0,
        "_ratio_n": 0,
        "_top_score": -1,
        "top_post": None,
    })

    for p in posts:
        text = (p.get("title", "") + " " + (p.get("selftext") or "")[:800]).upper()
        tickers_in_post = set()
        # 명시적 $ 검출
        for m in re.finditer(r"\$([A-Z]{1,5})\b", text):
            t = m.group(1)
            if t and t not in _STOPWORDS:
                tickers_in_post.add(("$" + t, True))
        # ALL-CAPS 검출 (valid 한 ticker 만, ambiguous 는 제외)
        for m in re.finditer(r"\b([A-Z]{2,5})\b", text):
            t = m.group(1)
            if t in _STOPWORDS or t in _AMBIGUOUS_TICKERS:
                continue
            if valid and t in valid:
                # 이미 $ 로 잡혔으면 skip
                if ("$" + t, True) in tickers_in_post:
                    continue
                tickers_in_post.add((t, False))

        # 중복 제거 (한 글에 같은 ticker 여러 번이라도 +1만)
        seen = set()
        for tag, is_explicit in tickers_in_post:
            sym = tag.lstrip("$")
            if sym in seen:
                continue
            seen.add(sym)
            s = stats[sym]
            s["symbol"] = sym
            s["mention_count"] += 1
            if is_explicit:
                s["explicit_count"] += 1
            s["score_sum"] += int(p.get("score", 0))
            s["comment_sum"] += int(p.get("num_comments", 0))
            s["_ratio_total"] += float(p.get("upvote_ratio", 0.5))
            s["_ratio_n"] += 1
            if p.get("score", 0) > s["_top_score"]:
                s["_top_score"] = p.get("score", 0)
                s["top_post"] = _post_to_dict(p)

    # min_mentions 미만 필터링 + 정렬
    rows = []
    for sym, s in stats.items():
        if s["mention_count"] < min_mentions:
            continue
        avg_ratio = round(s["_ratio_total"] / s["_ratio_n"], 3) if s["_ratio_n"] else 0.5
        rows.append({
            "symbol": sym,
            "mention_count": s["mention_count"],
            "explicit_count": s["explicit_count"],
            "score_sum": s["score_sum"],
            "comment_sum": s["comment_sum"],
            "avg_upvote_ratio": avg_ratio,
            "top_post": s["top_post"],
        })

    # 정렬: explicit > mention > score
    rows.sort(key=lambda r: (r["explicit_count"], r["mention_count"], r["score_sum"]), reverse=True)
    rows = rows[:top_n]

    return {
        "data": rows,
        "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "post_pool_size": len(posts),
    }


def get_wsb_for_symbol(symbol: str, limit: int = 10) -> dict:
    """단일 종목의 최근 WSB 게시물 목록."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"symbol": "", "data": [], "as_of": None}

    posts = _fetch_all_posts()
    pat_dollar = re.compile(rf"\${re.escape(sym)}\b", re.IGNORECASE)
    pat_word = re.compile(rf"\b{re.escape(sym)}\b")

    matched: list[dict] = []
    for p in posts:
        text = (p.get("title", "") + " " + (p.get("selftext") or "")[:800])
        text_upper = text.upper()
        has_dollar = bool(pat_dollar.search(text))
        has_word = bool(pat_word.search(text_upper))
        if has_dollar or has_word:
            d = _post_to_dict(p)
            d["mention_type"] = "explicit" if has_dollar else "implicit"
            matched.append(d)

    matched.sort(key=lambda x: x["score"], reverse=True)
    matched = matched[:limit]

    # 요약 통계
    if matched:
        avg_ratio = sum(p["upvote_ratio"] for p in matched) / len(matched)
        score_sum = sum(p["score"] for p in matched)
        comment_sum = sum(p["num_comments"] for p in matched)
        explicit_n = sum(1 for p in matched if p["mention_type"] == "explicit")
    else:
        avg_ratio = 0.0
        score_sum = 0
        comment_sum = 0
        explicit_n = 0

    return {
        "symbol": sym,
        "mention_count": len(matched),
        "explicit_count": explicit_n,
        "score_sum": score_sum,
        "comment_sum": comment_sum,
        "avg_upvote_ratio": round(avg_ratio, 3),
        "data": matched,
        "post_pool_size": len(posts),
        "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) > 1 and sys.argv[1] != "top":
        print(json.dumps(get_wsb_for_symbol(sys.argv[1]), indent=2, default=str, ensure_ascii=False))
    else:
        print(json.dumps(get_wsb_top_mentions(), indent=2, default=str, ensure_ascii=False))
