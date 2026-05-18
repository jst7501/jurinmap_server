"""
시황 브리핑용 뉴스 크롤링 — 주요 증권 뉴스 사이트에서 최근 헤드라인 수집.

사용:
  python scripts/crawl_market_news.py                     # 기본 12시간, 25건
  python scripts/crawl_market_news.py --hours 6 --limit 20
  python scripts/crawl_market_news.py --hours 24 --limit 50 --sources naver,hk

결과: stdout으로 JSON 배열
  [{source, title, url, published_at, preview}, ...]

호출처: .claude/scheduled-tasks/jurinmap-briefing-{pre,post}market/SKILL.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko,en;q=0.9",
}


# ─── 소스별 크롤러 ─────────────────────────────────────────

def _fetch_naver_main() -> List[Dict[str, Any]]:
    """네이버 금융 주요 뉴스 메인 (증권 시황)."""
    url = "https://finance.naver.com/news/mainnews.naver"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.encoding = "euc-kr"
    except Exception as e:
        return []

    soup = BeautifulSoup(res.text, "html.parser")
    out: List[Dict[str, Any]] = []
    for item in soup.select(".mainNewsList li"):
        a = item.select_one("dt a")
        if not a:
            continue
        title = a.get_text(strip=True)
        href = a.get("href") or ""
        if href.startswith("/"):
            href = f"https://finance.naver.com{href}"
        preview_node = item.select_one(".lede")
        preview = preview_node.get_text(strip=True) if preview_node else ""
        time_node = item.select_one(".articleInfo .wdate")
        pub = time_node.get_text(strip=True) if time_node else ""
        out.append(
            {
                "source": "naver_finance_main",
                "title": title,
                "url": href,
                "published_at": pub,
                "preview": preview,
            }
        )
    return out


def _fetch_naver_sise() -> List[Dict[str, Any]]:
    """네이버 금융 > 증권 > 시황/전망 섹션."""
    url = (
        "https://finance.naver.com/news/news_list.naver"
        "?mode=LSS3_NEWS&section_id=101&section_id2=258&section_id3=403"
    )
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.encoding = "euc-kr"
    except Exception:
        return []

    soup = BeautifulSoup(res.text, "html.parser")
    out: List[Dict[str, Any]] = []
    for item in soup.select(".realtimeNewsList dl dt"):
        a = item.select_one("a")
        if not a:
            continue
        title = a.get_text(strip=True)
        href = a.get("href") or ""
        if href.startswith("/"):
            href = f"https://finance.naver.com{href}"
        # 발행 시각은 형제 dd.time/wdate 에 있음 (구조가 단순치 않아 가능하면 뽑음)
        parent_dl = item.find_parent("dl")
        pub = ""
        if parent_dl:
            t = parent_dl.select_one(".wdate, .time")
            if t:
                pub = t.get_text(strip=True)
        out.append(
            {
                "source": "naver_finance_sise",
                "title": title,
                "url": href,
                "published_at": pub,
                "preview": "",
            }
        )
    return out


def _fetch_yonhap_economy_rss() -> List[Dict[str, Any]]:
    """연합뉴스 경제 RSS."""
    url = "https://www.yna.co.kr/rss/economy.xml"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.encoding = "utf-8"
    except Exception:
        return []

    soup = BeautifulSoup(res.text, "xml")
    out: List[Dict[str, Any]] = []
    for item in soup.find_all("item"):
        title = (item.find("title").text if item.find("title") else "").strip()
        link = (item.find("link").text if item.find("link") else "").strip()
        pub = (item.find("pubDate").text if item.find("pubDate") else "").strip()
        desc = (item.find("description").text if item.find("description") else "").strip()
        out.append(
            {
                "source": "yonhap_economy",
                "title": title,
                "url": link,
                "published_at": pub,
                "preview": _strip_html(desc)[:200],
            }
        )
    return out


def _fetch_hankyung_finance_rss() -> List[Dict[str, Any]]:
    """한경 증권 RSS (금융/증권)."""
    url = "https://www.hankyung.com/feed/finance"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.encoding = "utf-8"
    except Exception:
        return []

    soup = BeautifulSoup(res.text, "xml")
    out: List[Dict[str, Any]] = []
    for item in soup.find_all("item"):
        title = (item.find("title").text if item.find("title") else "").strip()
        link = (item.find("link").text if item.find("link") else "").strip()
        pub = (item.find("pubDate").text if item.find("pubDate") else "").strip()
        desc = (item.find("description").text if item.find("description") else "").strip()
        out.append(
            {
                "source": "hankyung_finance",
                "title": title,
                "url": link,
                "published_at": pub,
                "preview": _strip_html(desc)[:200],
            }
        )
    return out


def _fetch_mk_stock_rss() -> List[Dict[str, Any]]:
    """매일경제 증권 RSS."""
    url = "https://www.mk.co.kr/rss/50200011/"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.encoding = "utf-8"
    except Exception:
        return []

    soup = BeautifulSoup(res.text, "xml")
    out: List[Dict[str, Any]] = []
    for item in soup.find_all("item"):
        title = (item.find("title").text if item.find("title") else "").strip()
        link = (item.find("link").text if item.find("link") else "").strip()
        pub = (item.find("pubDate").text if item.find("pubDate") else "").strip()
        desc = (item.find("description").text if item.find("description") else "").strip()
        out.append(
            {
                "source": "mk_stock",
                "title": title,
                "url": link,
                "published_at": pub,
                "preview": _strip_html(desc)[:200],
            }
        )
    return out


SOURCES = {
    "naver_main": _fetch_naver_main,
    "naver_sise": _fetch_naver_sise,
    "yonhap": _fetch_yonhap_economy_rss,
    "hk": _fetch_hankyung_finance_rss,
    "mk": _fetch_mk_stock_rss,
}


# ─── 헬퍼 ──────────────────────────────────────────────────

def _strip_html(s: str) -> str:
    if not s:
        return ""
    # CDATA 제거
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    # 태그 제거
    s = re.sub(r"<[^>]+>", " ", s)
    # 공백 정리
    return re.sub(r"\s+", " ", s).strip()


def _parse_published(pub_str: str) -> datetime | None:
    """다양한 날짜 포맷을 KST aware datetime으로 변환. 실패 시 None."""
    if not pub_str:
        return None
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(pub_str)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST)
    except Exception:
        pass

    # "YYYY-MM-DD HH:MM" 유형
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y.%m.%d %H:%M", "%Y.%m.%d"):
        try:
            dt = datetime.strptime(pub_str, fmt)
            return dt.replace(tzinfo=KST)
        except ValueError:
            continue
    return None


def _dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen_urls: set = set()
    seen_titles: set = set()
    out: List[Dict[str, Any]] = []
    for it in items:
        u = (it.get("url") or "").strip()
        t = (it.get("title") or "").strip()
        key_t = re.sub(r"\s+", " ", t)[:60]
        if u and u in seen_urls:
            continue
        if key_t and key_t in seen_titles:
            continue
        seen_urls.add(u)
        seen_titles.add(key_t)
        out.append(it)
    return out


# ─── 메인 ──────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=12, help="최근 N시간 이내만")
    ap.add_argument("--limit", type=int, default=25, help="최대 반환 건수")
    ap.add_argument(
        "--sources",
        default=",".join(SOURCES.keys()),
        help=f"쉼표 구분. 가능: {','.join(SOURCES.keys())}",
    )
    args = ap.parse_args()

    sources = [s.strip() for s in args.sources.split(",") if s.strip() in SOURCES]
    if not sources:
        print(json.dumps({"error": "no valid sources"}, ensure_ascii=False))
        return 2

    now_kst = datetime.now(KST)
    cutoff = now_kst - timedelta(hours=args.hours)

    all_items: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}

    for name in sources:
        fn = SOURCES[name]
        try:
            items = fn() or []
            all_items.extend(items)
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {e}"

    # 시간 필터 + 파싱된 시각 붙이기
    filtered: List[Dict[str, Any]] = []
    for it in all_items:
        pub_dt = _parse_published(it.get("published_at") or "")
        it["published_at_parsed"] = pub_dt.isoformat() if pub_dt else None
        if pub_dt and pub_dt < cutoff:
            continue
        filtered.append(it)

    # 중복 제거 + 최신순 정렬
    filtered = _dedupe(filtered)
    filtered.sort(
        key=lambda x: x.get("published_at_parsed") or "",
        reverse=True,
    )

    result = {
        "fetched_at": now_kst.isoformat(),
        "cutoff": cutoff.isoformat(),
        "hours": args.hours,
        "requested_sources": sources,
        "errors": errors,
        "count": min(len(filtered), args.limit),
        "items": filtered[: args.limit],
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
