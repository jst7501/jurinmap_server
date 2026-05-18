"""Reddit 실시간 검색 — 임의 ticker/키워드를 sub-by-sub 검색.

기존 us_wsb_sentiment.py 는 hot/new/rising 사전 풀에서 매칭 → 풀에 없으면 0건.
이 모듈은 Reddit search API 를 직접 호출 → 풀 의존 X.

Reddit search.json 특성:
  - restrict_sr=on 으로 단일 sub 안에서만 검색
  - 다중 sub combined "r/a+b/search.json" 은 결과 매우 제한적 (API 한계)
  - 따라서 sub 별 병렬 fetch 후 합치는 방식 사용
  - t=week + sort=relevance 가 가장 안정적인 결과 양/품질

기본 sub 묶음 (squeeze/meme 트레이더 관점):
  wallstreetbets — 메인
  stocks         — 더 정제된 분석
  options        — 옵션 플레이 관점
  investing      — 장기 관점
  StockMarket    — 시황
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger("collectors.us_reddit_search")

_HEADERS = {"User-Agent": "JurinMapBot/1.0 (investment dashboard)"}
_TIMEOUT = 12

DEFAULT_SUBS = ["wallstreetbets", "stocks", "options", "investing", "StockMarket"]
ALLOWED_SORTS = {"relevance", "new", "top", "hot"}
ALLOWED_TIMES = {"hour", "day", "week", "month", "year", "all"}

# (query_key, sub, sort, time) 단위 캐시 — 90초 TTL
_CACHE: dict[tuple, dict] = {}
_CACHE_TTL = 90


def _fetch_sub(
    sub: str, query: str, sort: str = "relevance", time_range: str = "week", limit: int = 25,
) -> list[dict]:
    """단일 sub 에서 query 검색."""
    url = f"https://www.reddit.com/r/{sub}/search.json"
    params = {
        "q": query,
        "restrict_sr": "on",
        "sort": sort,
        "t": time_range,
        "limit": min(limit, 100),
    }
    try:
        r = requests.get(url, headers=_HEADERS, params=params, timeout=_TIMEOUT)
        r.raise_for_status()
    except Exception as exc:
        logger.warning("reddit search failed (%s, q=%s): %s", sub, query, exc)
        return []

    try:
        data = r.json()
        return [c["data"] for c in data.get("data", {}).get("children", [])]
    except Exception:
        return []


def _normalize_post(p: dict) -> dict:
    """raw Reddit post → 슬림 dict."""
    text = (p.get("title", "") or "") + " " + (p.get("selftext", "") or "")[:300]
    return {
        "id": p.get("id"),
        "title": (p.get("title") or "")[:200],
        "subreddit": p.get("subreddit"),
        "url": f"https://www.reddit.com{p.get('permalink', '')}",
        "score": int(p.get("score", 0)),
        "num_comments": int(p.get("num_comments", 0)),
        "upvote_ratio": float(p.get("upvote_ratio", 0.5)),
        "created_utc": int(p.get("created_utc", 0)),
        "author": p.get("author"),
        "flair": p.get("link_flair_text"),
        "is_video": bool(p.get("is_video")),
        "selftext_preview": (p.get("selftext") or "")[:200],
    }


def search_reddit(
    query: str,
    subs: Optional[list[str]] = None,
    sort: str = "relevance",
    time_range: str = "week",
    limit_per_sub: int = 25,
    total_limit: int = 50,
) -> dict:
    """다중 sub 병렬 검색 → 점수순 통합.

    Args:
      query: 검색어 (예: "TSLA", "$NVDA", "calls TSLA", "earnings AAPL")
      subs: 검색 대상 sub list. None 이면 DEFAULT_SUBS
      sort: relevance / new / top / hot
      time_range: hour / day / week / month / year / all
      limit_per_sub: 각 sub 당 최대 fetch
      total_limit: 최종 결과 최대 개수

    Returns:
      {
        "query": "TSLA",
        "subs": ["wallstreetbets", ...],
        "sort": "relevance",
        "time_range": "week",
        "count": N,
        "data": [
          {
            "id": "...",
            "title": "...",
            "subreddit": "wallstreetbets",
            "url": "https://reddit.com/...",
            "score": 374,
            "num_comments": 74,
            "upvote_ratio": 0.92,
            "created_utc": 1715000000,
            ...
          }
        ],
        "by_sub": {"wallstreetbets": 5, "stocks": 3, ...},
        "as_of": "2026-05-14T..."
      }
    """
    if not query or not query.strip():
        return {"query": "", "data": [], "count": 0, "as_of": None}

    query = query.strip()
    subs = subs or DEFAULT_SUBS
    sort = sort if sort in ALLOWED_SORTS else "relevance"
    time_range = time_range if time_range in ALLOWED_TIMES else "week"

    cache_key = (query.lower(), tuple(sorted(subs)), sort, time_range, limit_per_sub)
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and now - cached["_ts"] < _CACHE_TTL:
        return cached["payload"]

    # 병렬 fetch
    seen_ids: set[str] = set()
    all_posts: list[dict] = []
    by_sub: dict[str, int] = {s: 0 for s in subs}

    with ThreadPoolExecutor(max_workers=min(len(subs), 5)) as pool:
        futures = {
            pool.submit(_fetch_sub, s, query, sort, time_range, limit_per_sub): s
            for s in subs
        }
        for fut in as_completed(futures, timeout=_TIMEOUT * 2):
            sub = futures[fut]
            try:
                raw_posts = fut.result()
            except Exception as exc:
                logger.warning("sub %s search failed: %s", sub, exc)
                continue
            for p in raw_posts:
                pid = p.get("id")
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                normalized = _normalize_post(p)
                all_posts.append(normalized)
                by_sub[sub] = by_sub.get(sub, 0) + 1

    # 정렬: sort 기준
    if sort == "new":
        all_posts.sort(key=lambda x: x["created_utc"], reverse=True)
    elif sort == "top":
        all_posts.sort(key=lambda x: x["score"], reverse=True)
    else:
        # relevance / hot — score + 최근성 가중치
        now_ts = datetime.now(timezone.utc).timestamp()
        for p in all_posts:
            age_hours = max(1, (now_ts - p["created_utc"]) / 3600)
            p["_rel_score"] = p["score"] / (age_hours ** 0.4)  # 시간감쇠
        all_posts.sort(key=lambda x: x.get("_rel_score", 0), reverse=True)
        for p in all_posts:
            p.pop("_rel_score", None)

    all_posts = all_posts[:total_limit]

    # 요약 통계
    score_sum = sum(p["score"] for p in all_posts)
    comment_sum = sum(p["num_comments"] for p in all_posts)
    avg_ratio = (sum(p["upvote_ratio"] for p in all_posts) / len(all_posts)) if all_posts else 0.0

    payload = {
        "query": query,
        "subs": subs,
        "sort": sort,
        "time_range": time_range,
        "count": len(all_posts),
        "data": all_posts,
        "by_sub": by_sub,
        "summary": {
            "score_sum": score_sum,
            "comment_sum": comment_sum,
            "avg_upvote_ratio": round(avg_ratio, 3),
        },
        "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    _CACHE[cache_key] = {"_ts": now, "payload": payload}
    return payload


def search_for_symbol(
    symbol: str,
    subs: Optional[list[str]] = None,
    time_range: str = "week",
    limit: int = 25,
) -> dict:
    """단일 종목 ticker 검색 헬퍼.

    Reddit relevance 가 broad 라 "TSLA" 검색에 S&P500 글이 섞임 → post-filter 로
    실제 ticker 가 title/selftext 에 명확히 등장하는 글만 통과.
    """
    import re
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"query": "", "data": [], "count": 0, "as_of": None}
    # $TSLA + TSLA 둘 다 fetch — Reddit search 는 OR 보다 따옴표 양쪽이 더 많이 잡음
    query = f'"{sym}" OR "${sym}"'
    raw = search_reddit(query, subs=subs, sort="relevance", time_range=time_range, total_limit=limit * 3)

    # post-filter: title/selftext_preview 에 ticker 가 word-boundary 로 등장
    pat_dollar = re.compile(rf"\${re.escape(sym)}\b", re.IGNORECASE)
    pat_word = re.compile(rf"\b{re.escape(sym)}\b")
    filtered = []
    explicit_n = 0
    for p in raw.get("data", []):
        text = (p.get("title", "") or "") + " " + (p.get("selftext_preview", "") or "")
        has_dollar = bool(pat_dollar.search(text))
        has_word = bool(pat_word.search(text.upper()))
        if has_dollar or has_word:
            p["mention_type"] = "explicit" if has_dollar else "implicit"
            filtered.append(p)
            if has_dollar:
                explicit_n += 1
        if len(filtered) >= limit:
            break

    # by_sub 재계산
    by_sub: dict[str, int] = {}
    for p in filtered:
        s = p.get("subreddit") or "?"
        by_sub[s] = by_sub.get(s, 0) + 1

    score_sum = sum(p["score"] for p in filtered)
    comment_sum = sum(p["num_comments"] for p in filtered)
    avg_ratio = (sum(p["upvote_ratio"] for p in filtered) / len(filtered)) if filtered else 0.0

    return {
        **raw,
        "symbol": sym,
        "data": filtered,
        "count": len(filtered),
        "explicit_count": explicit_n,
        "by_sub": by_sub,
        "summary": {
            "score_sum": score_sum,
            "comment_sum": comment_sum,
            "avg_upvote_ratio": round(avg_ratio, 3),
        },
        "pre_filter_count": raw.get("count", 0),
    }


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) < 2:
        print("usage: python collectors/us_reddit_search.py <query>")
        sys.exit(1)
    q = sys.argv[1]
    t = sys.argv[2] if len(sys.argv) > 2 else "week"
    res = search_reddit(q, time_range=t)
    print(f"query={res['query']!r} count={res['count']} by_sub={res['by_sub']}")
    print(f"summary: {res['summary']}")
    print()
    for p in res["data"][:10]:
        age_h = (datetime.now(timezone.utc).timestamp() - p["created_utc"]) / 3600
        print(f"  [{p['subreddit']:18}] {age_h:5.1f}h ago | s={p['score']:5} c={p['num_comments']:4} | {p['title'][:70]}")
