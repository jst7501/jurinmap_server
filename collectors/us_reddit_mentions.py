"""Reddit 서브레딧별 ticker 언급 랭킹 — apewisdom 스타일.

각 서브의 hot/new/rising 통합 풀에서 ticker 추출 + mention rank + sentiment.

지원 서브 (squeeze/meme/장기 관점 다양):
  wallstreetbets — meme/short squeeze
  stocks         — 일반 분석
  options        — 옵션 플레이
  investing      — 장기 가치
  StockMarket    — 시황
  Daytrading     — 단타
  pennystocks    — 페니
  Shortsqueeze   — squeeze 전용
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger("collectors.us_reddit_mentions")

_HEADERS = {"User-Agent": "JurinMapBot/1.0 (investment dashboard)"}
# hot + rising = top 인기 + 최근 부상. new 는 hot 과 겹치고 noise 많아 제외
_FEEDS = ["hot", "rising"]
_FEED_LIMIT = 100

DEFAULT_SUBS = [
    "wallstreetbets", "stocks", "options", "investing", "StockMarket",
    "pennystocks", "Daytrading", "Shortsqueeze", "ValueInvesting", "CryptoCurrency",
]
ALL_SUPPORTED_SUBS = DEFAULT_SUBS  # 동일

# 크립토 친화 sub (us_stocks 마스터에 없는 ticker 도 인정)
_CRYPTO_SUBS = {"CryptoCurrency"}

# 자주 등장하는 crypto 심볼 — us_stocks 검증 우회 (CryptoCurrency sub 한정)
_CRYPTO_WHITELIST = {
    "BTC", "ETH", "USDT", "USDC", "BNB", "XRP", "SOL", "ADA", "DOGE", "AVAX",
    "DOT", "MATIC", "SHIB", "TRX", "LTC", "BCH", "LINK", "ATOM", "ETC", "XLM",
    "ALGO", "FIL", "VET", "ICP", "NEAR", "APT", "ARB", "OP", "SUI", "INJ",
    "TIA", "SEI", "PEPE", "WLD", "FET", "RNDR", "FTM", "GRT", "AAVE", "MKR",
    "SAND", "MANA", "AXS", "GALA", "IMX", "BLUR", "JTO", "JUP", "PYTH", "BONK",
    "WIF", "FLOKI", "MEME", "STX", "EGLD", "HBAR", "QNT", "RUNE",
}

# ALL-CAPS 인식 시 무시할 일반 단어
_STOPWORDS = {
    "THE", "AND", "FOR", "YOU", "ARE", "HAVE", "WITH", "FROM", "THIS", "THAT", "WAS", "WHEN",
    "WHAT", "WHO", "WHY", "HOW", "BUT", "NOT", "ONE", "TWO", "ALL", "ANY", "CAN", "WILL",
    "DID", "DO", "DOES", "GET", "GOT", "OUR", "OUT", "NEW", "NOW", "OFF", "ON", "OR", "ITS",
    "IT", "TO", "IS", "OF", "IN", "AS", "MY", "ME", "WE", "BE", "BY", "AT", "AN", "SO", "UP",
    "WSB", "DD", "TA", "YOLO", "HODL", "FOMO", "FUD", "ATH", "ATL", "TLDR", "GUH", "PR", "PE",
    "EPS", "PT", "TP", "SL", "EV", "OG", "GOAT", "LOL", "LMAO", "ETF", "IPO", "CEO", "CFO",
    "CTO", "CMO", "COO", "SP", "CPI", "PPI", "GDP",
    "AI", "VR", "AR", "ML", "API", "URL", "USA", "US", "UK", "EU",
    "USD", "EUR", "GBP", "JPY", "CNY", "CAD", "AUD", "CHF", "INR", "KRW", "HKD",
    "NYSE", "NASDAQ", "SEC", "FED", "FOMC", "IRS", "FTC", "FBI", "CIA", "NSA",
    "AM", "PM", "EST", "PST", "ET", "UTC", "GMT",
    "BUY", "SELL", "OPEN", "PUT", "CALL", "ITM", "OTM", "ATM", "LONG", "SHORT",
}

# 일반 영어 단어이면서 ticker 인 ambiguous — 반드시 $ 접두 필요
_AMBIGUOUS_TICKERS = {
    "GO", "SEE", "RUN", "LOW", "WELL", "ALL", "ANY", "FOR", "ONE", "TWO", "NOW", "NEW", "OLD",
    "HIT", "BIG", "BAD", "GET", "GOT", "ARE", "HAS", "OUR", "OUT", "DAY", "WAS", "WHO", "WHY",
    "TOP", "EAT", "FUN", "JOY", "JOB", "BUY", "TRY", "REAL", "OPEN", "FREE",
    "EVER", "NICE", "GOOD", "MEAN", "MOON", "BUZZ", "FAST", "PLAY", "LIVE", "LOVE", "LAND",
    "FAIL", "PUSH", "TURN", "FORM", "MOVE", "ROAD", "HOPE", "EDGE", "PUMP", "TASK", "BANK",
    "BEAT", "BEST", "DOG", "CAR", "AIR", "CAT", "NET", "GAS", "OIL", "BIO", "CAP",
    "BIT", "TEN", "BAR", "MOM", "DAD", "TIP", "TAX", "WAY", "BOX",
    "ZERO", "FOUR", "FIVE", "FALL", "LAST", "MEME", "RICH",
    "AWAY", "GG", "LP", "RR", "BP",
}

# Sentiment heuristic ────────────────────────────────────────────────
# A. flair 가중치 (Reddit WSB 등의 link_flair_text)
_FLAIR_BIAS = {
    # 강한 긍정
    "gain": +1.0, "gains": +1.0, "yolo": +0.6,
    "moon": +1.0, "rocket": +0.8,
    # 강한 부정
    "loss": -1.0, "losses": -1.0, "loss porn": -1.0,
    # 중립/정보
    "dd": 0.0, "discussion": 0.0, "news": 0.0, "chart": 0.0,
    "earnings": 0.0, "technical analysis": 0.0,
    "options": 0.0, "shitpost": 0.0, "meme": 0.0,
    "daily discussion": 0.0,
    # 약간 부정 (불확실/걱정)
    "question": -0.1, "help": -0.2,
}

# B. ticker 주변 ±N 단어 키워드 카운트 (대소문자 무관 매칭 위해 lower-case)
_BULL_KW = {
    "calls", "call", "long", "buy", "bullish", "moon", "rocket", "rockets",
    "squeeze", "pump", "rip", "ripping", "send", "sending", "tendies",
    "breakout", "breakouts", "explode", "exploding", "soar", "soaring",
    "uptrend", "rally", "rallies", "winning", "winner", "gains", "gain",
    "lambo", "stonks", "yolo", "bullion",
}
_BEAR_KW = {
    "puts", "put", "short", "shorts", "shorting", "sell", "selling", "bearish",
    "dump", "dumping", "tank", "tanking", "crash", "crashing", "drop",
    "dropping", "plunge", "rugpull", "rug", "bagholder", "bag",
    "downtrend", "decline", "declining", "losing", "loser", "loss",
    "puts印", "diving", "redgreen",
}
_PROXIMITY_WINDOW = 6   # ticker 좌우 6단어 내 keyword 매칭

# 캐시
_CACHE: dict = {}
_CACHE_TTL = 90
_TICKER_CACHE_TTL = 3600


def _flair_score(flair: Optional[str]) -> float:
    """flair → -1.0~+1.0 점수. 모르는 flair 는 0."""
    if not flair:
        return 0.0
    fl = flair.lower().strip()
    if fl in _FLAIR_BIAS:
        return _FLAIR_BIAS[fl]
    # 부분 매칭 (flair 가 "DD - Tech" 같은 패턴일 때)
    for key, bias in _FLAIR_BIAS.items():
        if key in fl:
            return bias
    return 0.0


def _proximity_score(text: str, ticker: str) -> tuple[float, int, int]:
    """ticker 주변 ±N 단어 내 bullish/bearish 키워드 카운트.

    Returns: (normalized_score [-1.0~+1.0], bull_n, bear_n)
    """
    if not text or not ticker:
        return 0.0, 0, 0
    # 토큰화 — 공백 + 구두점 split, 원문 case 보존
    tokens = re.findall(r"[A-Za-z\$']+", text)
    if not tokens:
        return 0.0, 0, 0

    ticker_u = ticker.upper()
    bull = 0
    bear = 0
    for i, tok in enumerate(tokens):
        tok_u = tok.upper().lstrip("$")
        if tok_u != ticker_u:
            continue
        # ±N window
        lo = max(0, i - _PROXIMITY_WINDOW)
        hi = min(len(tokens), i + _PROXIMITY_WINDOW + 1)
        for j in range(lo, hi):
            if j == i:
                continue
            w = tokens[j].lower().lstrip("$")
            if w in _BULL_KW:
                bull += 1
            elif w in _BEAR_KW:
                bear += 1

    total = bull + bear
    if total == 0:
        return 0.0, 0, 0
    # bull - bear / total: -1 (전부 bear) ~ +1 (전부 bull)
    return (bull - bear) / total, bull, bear


def _fetch_feed(sub: str, feed: str = "hot", limit: int = 100, retries: int = 2) -> list[dict]:
    url = f"https://www.reddit.com/r/{sub}/{feed}.json"
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=_HEADERS, params={"limit": limit}, timeout=12)
            if r.status_code == 429:
                # exponential backoff: 2s, 4s
                wait = 2 ** (attempt + 1)
                logger.info("429 on %s/%s, waiting %ds (attempt %d/%d)", sub, feed, wait, attempt + 1, retries + 1)
                time.sleep(wait)
                continue
            r.raise_for_status()
        except Exception as exc:
            if attempt < retries:
                time.sleep(2)
                continue
            logger.warning("feed fetch failed (%s/%s): %s", sub, feed, exc)
            return []
        try:
            data = r.json()
            return [c["data"] for c in data.get("data", {}).get("children", [])]
        except Exception:
            return []
    return []


def _fetch_post_comments(post_id: str, limit: int = 50, retries: int = 1) -> list[dict]:
    """단일 글의 top-level 댓글 (재귀 X) fetch.

    Reddit /comments/{id}.json 응답은 2-element list: [post, comments].
    각 comment 의 body + score 만 추출. 429 재시도 1회.
    """
    if not post_id:
        return []
    url = f"https://www.reddit.com/comments/{post_id}.json"
    r = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=_HEADERS, params={"limit": limit, "depth": 1, "sort": "top"}, timeout=10)
            if r.status_code == 429:
                if attempt < retries:
                    time.sleep(3)
                    continue
                return []
            r.raise_for_status()
            break
        except Exception as exc:
            if attempt < retries:
                time.sleep(2)
                continue
            logger.debug("comments fetch failed (%s): %s", post_id, exc)
            return []
    if r is None:
        return []
    try:
        data = r.json()
        if not isinstance(data, list) or len(data) < 2:
            return []
        comment_listing = data[1]
        comments = []
        for c in comment_listing.get("data", {}).get("children", []):
            if c.get("kind") != "t1":
                continue
            d = c.get("data", {})
            body = (d.get("body") or "")[:1500]
            if not body or body == "[deleted]" or body == "[removed]":
                continue
            comments.append({
                "body": body,
                "score": int(d.get("score") or 0),
                "author": d.get("author"),
            })
        return comments
    except Exception:
        return []


def _fetch_sub_pool(sub: str) -> list[dict]:
    """단일 서브의 hot/new/rising 통합 풀 (캐시). 직렬 fetch + 사이 sleep."""
    key = f"pool:{sub}"
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached["_ts"] < _CACHE_TTL:
        return cached["data"]

    all_posts: list[dict] = []
    seen = set()
    # feed 직렬 fetch + 500ms 분산 (Reddit 60req/min 안전)
    for i, feed in enumerate(_FEEDS):
        posts = _fetch_feed(sub, feed, _FEED_LIMIT)
        for p in posts:
            pid = p.get("id")
            if pid and pid not in seen:
                seen.add(pid)
                all_posts.append(p)
        if i < len(_FEEDS) - 1:
            time.sleep(0.5)

    _CACHE[key] = {"_ts": now, "data": all_posts}
    return all_posts


def _load_valid_tickers() -> set[str]:
    """us_stocks 마스터 ticker set."""
    cached = _CACHE.get("tickers")
    if cached and time.time() - cached["_ts"] < _TICKER_CACHE_TTL:
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
    return {
        "id": p.get("id"),
        "title": (p.get("title") or "")[:200],
        "url": f"https://www.reddit.com{p.get('permalink', '')}",
        "subreddit": p.get("subreddit"),
        "score": int(p.get("score", 0)),
        "num_comments": int(p.get("num_comments", 0)),
        "upvote_ratio": float(p.get("upvote_ratio", 0.5)),
        "created_utc": int(p.get("created_utc", 0)),
        "author": p.get("author"),
    }


def _analyze_pool(
    posts: list[dict],
    valid_tickers: set[str],
    min_mentions: int = 1,
    is_crypto_sub: bool = False,
    fetch_comments_top_n: int = 0,
) -> list[dict]:
    """post pool 에서 ticker 별 mention 통계 + 강화된 sentiment.

    is_crypto_sub: True 면 _CRYPTO_WHITELIST 도 valid 로 인정 (us_stocks 마스터에 없는 BTC/ETH 등).
    fetch_comments_top_n: > 0 이면 score 상위 N개 글의 댓글까지 fetch → comment sentiment 반영.

    Sentiment 구성:
      댓글 sentiment 없을 때 (40% base + 30% flair + 30% post keyword)
      댓글 sentiment 있을 때 (30% base + 20% flair + 25% post keyword + 25% comment keyword)
    """
    # crypto sub 면 whitelist 합쳐서 valid 확장
    if is_crypto_sub:
        valid_tickers = (valid_tickers or set()) | _CRYPTO_WHITELIST
    stats: dict[str, dict] = defaultdict(lambda: {
        "symbol": "",
        "mention_count": 0,
        "explicit_count": 0,
        "score_sum": 0,
        "comment_sum": 0,
        "_ratio_total": 0.0,
        "_ratio_n": 0,
        "_flair_total": 0.0,
        "_flair_n": 0,
        "_kw_total": 0.0,
        "_kw_n": 0,
        "_bull_n": 0,
        "_bear_n": 0,
        # 댓글 sentiment
        "_comment_kw_total": 0.0,
        "_comment_kw_n": 0,
        "_comment_bull_n": 0,
        "_comment_bear_n": 0,
        "_comment_analyzed": 0,    # 분석된 댓글 수
        # top post 후보를 score 순으로 보관 (댓글 fetch 우선순위)
        "_top_score": -1,
        "top_post": None,
        "_top_candidates": [],   # [(score, post_id, text_for_proximity), ...]
    })

    for p in posts:
        title = p.get("title", "") or ""
        body = (p.get("selftext") or "")[:1200]
        text_raw = title + " " + body
        text_upper = text_raw.upper()
        flair = p.get("link_flair_text")
        flair_s = _flair_score(flair)

        seen_in_post: set[tuple[str, bool]] = set()

        # 1) $TICKER 명시
        for m in re.finditer(r"\$([A-Z]{1,5})\b", text_upper):
            t = m.group(1)
            if t and t not in _STOPWORDS:
                seen_in_post.add((t, True))

        # 2) ALL-CAPS (valid + non-ambiguous 만)
        for m in re.finditer(r"\b([A-Z]{2,5})\b", text_upper):
            t = m.group(1)
            if t in _STOPWORDS or t in _AMBIGUOUS_TICKERS:
                continue
            if valid_tickers and t in valid_tickers:
                if (t, True) in seen_in_post:
                    continue
                seen_in_post.add((t, False))

        # 글당 ticker 별로 1회만 카운트
        counted = set()
        for sym, is_explicit in seen_in_post:
            if sym in counted:
                continue
            counted.add(sym)
            s = stats[sym]
            s["symbol"] = sym
            s["mention_count"] += 1
            if is_explicit:
                s["explicit_count"] += 1
            s["score_sum"] += int(p.get("score", 0))
            s["comment_sum"] += int(p.get("num_comments", 0))
            s["_ratio_total"] += float(p.get("upvote_ratio", 0.5))
            s["_ratio_n"] += 1

            # A. flair (모든 mention 에 동일 적용 — 글 단위 신호)
            s["_flair_total"] += flair_s
            s["_flair_n"] += 1

            # B. proximity (ticker 주변 단어)
            prox_s, bull, bear = _proximity_score(text_raw, sym)
            if (bull + bear) > 0:
                s["_kw_total"] += prox_s
                s["_kw_n"] += 1
                s["_bull_n"] += bull
                s["_bear_n"] += bear

            if p.get("score", 0) > s["_top_score"]:
                s["_top_score"] = p.get("score", 0)
                s["top_post"] = _post_to_dict(p)

            # 댓글 fetch 후보 보관 (글 단위, 추후 score top N개만 댓글까지 분석)
            s["_top_candidates"].append((int(p.get("score", 0)), p.get("id"), text_raw))

    # ── 댓글 sentiment (선택: fetch_comments_top_n > 0) ──────────────
    if fetch_comments_top_n > 0:
        # mention 상위 ticker 만 — 모든 ticker 의 댓글까지 fetch 하면 부담 큼
        # mention_count 기준 정렬 후 top 10 만
        ranked_syms = sorted(stats.items(), key=lambda kv: kv[1]["mention_count"], reverse=True)[:10]
        fetched_post_ids: set = set()
        for sym, s in ranked_syms:
            if not s["_top_candidates"]:
                continue
            candidates = sorted(s["_top_candidates"], key=lambda x: x[0], reverse=True)
            for post_score, post_id, post_text in candidates[:fetch_comments_top_n]:
                if post_score < 5 or not post_id:
                    continue
                if post_id in fetched_post_ids:
                    continue   # 같은 글이 여러 ticker 의 top 일 수 있음 → 중복 fetch 회피
                fetched_post_ids.add(post_id)
                comments = _fetch_post_comments(post_id, limit=30)
                time.sleep(0.4)   # 댓글 fetch 사이 분산
                for c in comments:
                    if c["score"] < 2:
                        continue
                    prox, bull, bear = _proximity_score(c["body"], sym)
                    if (bull + bear) > 0:
                        s["_comment_kw_total"] += prox
                        s["_comment_kw_n"] += 1
                        s["_comment_bull_n"] += bull
                        s["_comment_bear_n"] += bear
                    s["_comment_analyzed"] += 1

    rows = []
    for sym, s in stats.items():
        if s["mention_count"] < min_mentions:
            continue
        avg_ratio = round(s["_ratio_total"] / s["_ratio_n"], 3) if s["_ratio_n"] else 0.5
        avg_flair = (s["_flair_total"] / s["_flair_n"]) if s["_flair_n"] else 0.0
        avg_kw = (s["_kw_total"] / s["_kw_n"]) if s["_kw_n"] else 0.0
        avg_comment_kw = (s["_comment_kw_total"] / s["_comment_kw_n"]) if s["_comment_kw_n"] else 0.0

        # 각 컴포넌트 0~100
        base_100 = avg_ratio * 100
        flair_100 = 50 + (avg_flair * 50)
        kw_100 = 50 + (avg_kw * 50)
        comment_100 = 50 + (avg_comment_kw * 50)

        # 가중 평균 — 신호 있는 컴포넌트만 weight 합산 (총 1.0 으로 정규화)
        components = [(base_100, 0.30)]
        if s["_flair_n"] > 0 and avg_flair != 0:
            components.append((flair_100, 0.20))
        if s["_kw_n"] > 0:
            components.append((kw_100, 0.25))
        if s["_comment_kw_n"] > 0:
            components.append((comment_100, 0.25))

        total_w = sum(w for _, w in components)
        sentiment = sum(v * w for v, w in components) / total_w if total_w else base_100
        sentiment_score = int(round(max(0, min(100, sentiment))))

        rows.append({
            "symbol": sym,
            "mention_count": s["mention_count"],
            "explicit_count": s["explicit_count"],
            "score_sum": s["score_sum"],
            "comment_sum": s["comment_sum"],
            "avg_upvote_ratio": avg_ratio,
            "sentiment_score": sentiment_score,
            "sentiment_components": {
                "base": round(base_100, 1),
                "flair": round(flair_100, 1) if s["_flair_n"] > 0 else None,
                "keyword": round(kw_100, 1) if s["_kw_n"] > 0 else None,
                "comment": round(comment_100, 1) if s["_comment_kw_n"] > 0 else None,
                "bull_n": s["_bull_n"],
                "bear_n": s["_bear_n"],
                "kw_matched_posts": s["_kw_n"],
                "comment_bull_n": s["_comment_bull_n"],
                "comment_bear_n": s["_comment_bear_n"],
                "comments_analyzed": s["_comment_analyzed"],
            },
            "top_post": s["top_post"],
        })

    rows.sort(key=lambda r: (r["mention_count"], r["explicit_count"], r["score_sum"]), reverse=True)
    return rows


def _is_crypto_sub(sub: str) -> bool:
    return sub in _CRYPTO_SUBS


def get_mentions_by_sub(
    sub: str,
    top_n: int = 25,
    min_mentions: int = 2,
    fetch_comments_top_n: int = 0,
) -> dict:
    """단일 서브의 ticker mention 랭킹.

    Returns:
      {
        "subreddit": "wallstreetbets",
        "data": [
          {"rank": 1, "symbol": "TSLA", "mention_count": 14, "explicit_count": 8,
           "score_sum": 4521, "comment_sum": 832, "avg_upvote_ratio": 0.78,
           "sentiment_score": 78, "top_post": {...}}
        ],
        "post_pool_size": N,
        "as_of": "2026-05-14T..."
      }
    """
    valid = _load_valid_tickers()
    posts = _fetch_sub_pool(sub)
    rows = _analyze_pool(
        posts, valid,
        min_mentions=min_mentions,
        is_crypto_sub=_is_crypto_sub(sub),
        fetch_comments_top_n=fetch_comments_top_n,
    )
    # rank 부여
    ranked = []
    for i, r in enumerate(rows[:top_n]):
        r["rank"] = i + 1
        ranked.append(r)
    return {
        "subreddit": sub,
        "data": ranked,
        "post_pool_size": len(posts),
        "min_mentions": min_mentions,
        "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def get_mentions_aggregated(
    subs: Optional[list[str]] = None,
    top_n: int = 25,
    min_mentions: int = 2,
    fetch_comments_top_n: int = 0,
) -> dict:
    """다중 서브 병렬 fetch → 합산 ticker 랭킹 + 서브별 breakdown."""
    subs = subs or DEFAULT_SUBS
    valid = _load_valid_tickers()

    # 서브별 pool 직렬 fetch (rate limit 보호) — 캐시 적중하면 sleep 없이 skip
    sub_posts: dict[str, list[dict]] = {}
    pool_sizes: dict[str, int] = {}
    for i, sub in enumerate(subs):
        was_cached = (_CACHE.get(f"pool:{sub}") and time.time() - _CACHE[f"pool:{sub}"]["_ts"] < _CACHE_TTL)
        try:
            posts = _fetch_sub_pool(sub)
        except Exception as exc:
            logger.warning("sub %s pool failed: %s", sub, exc)
            posts = []
        sub_posts[sub] = posts
        pool_sizes[sub] = len(posts)
        # sub 사이 sleep — 캐시 miss 였을 때만
        if i < len(subs) - 1 and not was_cached:
            time.sleep(0.8)

    # 서브별 통계 분석
    sub_stats: dict[str, dict[str, dict]] = {}
    for sub, posts in sub_posts.items():
        rows = _analyze_pool(
            posts, valid, min_mentions=1,
            is_crypto_sub=_is_crypto_sub(sub),
            fetch_comments_top_n=fetch_comments_top_n,
        )
        sub_stats[sub] = {r["symbol"]: r for r in rows}

    # 통합: ticker 별 sub 분포
    all_symbols = set()
    for sm in sub_stats.values():
        all_symbols.update(sm.keys())

    aggregated = []
    for sym in all_symbols:
        total_mentions = 0
        total_explicit = 0
        total_score = 0
        total_comments = 0
        ratio_total = 0.0
        ratio_n = 0
        sent_total = 0.0
        sent_n = 0
        bull_n = 0
        bear_n = 0
        kw_posts = 0
        comment_bull_n = 0
        comment_bear_n = 0
        comments_analyzed = 0
        by_sub: dict[str, int] = {}
        top_post = None
        top_score = -1
        for sub, sm in sub_stats.items():
            r = sm.get(sym)
            if not r:
                continue
            total_mentions += r["mention_count"]
            total_explicit += r["explicit_count"]
            total_score += r["score_sum"]
            total_comments += r["comment_sum"]
            ratio_total += r["avg_upvote_ratio"] * r["mention_count"]
            ratio_n += r["mention_count"]
            # sentiment 가중 합산 (mention weighted)
            sent_total += r["sentiment_score"] * r["mention_count"]
            sent_n += r["mention_count"]
            c = r.get("sentiment_components") or {}
            bull_n += c.get("bull_n", 0)
            bear_n += c.get("bear_n", 0)
            kw_posts += c.get("kw_matched_posts", 0)
            comment_bull_n += c.get("comment_bull_n", 0)
            comment_bear_n += c.get("comment_bear_n", 0)
            comments_analyzed += c.get("comments_analyzed", 0)
            by_sub[sub] = r["mention_count"]
            if r["top_post"] and r["top_post"]["score"] > top_score:
                top_score = r["top_post"]["score"]
                top_post = r["top_post"]

        if total_mentions < min_mentions:
            continue

        avg_ratio = round(ratio_total / ratio_n, 3) if ratio_n else 0.5
        avg_sentiment = int(round(sent_total / sent_n)) if sent_n else 50
        aggregated.append({
            "symbol": sym,
            "mention_count": total_mentions,
            "explicit_count": total_explicit,
            "score_sum": total_score,
            "comment_sum": total_comments,
            "avg_upvote_ratio": avg_ratio,
            "sentiment_score": avg_sentiment,
            "sentiment_components": {
                "bull_n": bull_n,
                "bear_n": bear_n,
                "kw_matched_posts": kw_posts,
                "comment_bull_n": comment_bull_n,
                "comment_bear_n": comment_bear_n,
                "comments_analyzed": comments_analyzed,
            },
            "by_sub": by_sub,
            "top_post": top_post,
        })

    aggregated.sort(key=lambda r: (r["mention_count"], r["explicit_count"], r["score_sum"]), reverse=True)
    ranked = []
    for i, r in enumerate(aggregated[:top_n]):
        r["rank"] = i + 1
        ranked.append(r)

    return {
        "subreddits": subs,
        "data": ranked,
        "post_pool_size": sum(pool_sizes.values()),
        "pool_by_sub": pool_sizes,
        "min_mentions": min_mentions,
        "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


if __name__ == "__main__":
    import json
    import os
    import sys
    # 스크립트 직접 실행 시 root path 추가 (server 모듈 import 가능하게)
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    if len(sys.argv) > 1 and sys.argv[1] != "all":
        res = get_mentions_by_sub(sys.argv[1])
        print(f"sub={res['subreddit']} pool={res['post_pool_size']}")
        for r in res["data"]:
            print(f"  #{r['rank']:2}  {r['symbol']:6}  mentions={r['mention_count']:3} (explicit={r['explicit_count']})  sent={r['sentiment_score']:3}  upvotes={r['score_sum']:5}")
    else:
        res = get_mentions_aggregated()
        print(f"aggregated subs={res['subreddits']} pool={res['post_pool_size']}")
        print(f"pool_by_sub={res['pool_by_sub']}")
        for r in res["data"][:15]:
            sub_str = " ".join(f"{s}={n}" for s, n in r["by_sub"].items())
            print(f"  #{r['rank']:2}  {r['symbol']:6}  mentions={r['mention_count']:3}  sent={r['sentiment_score']:3}  [{sub_str}]")
