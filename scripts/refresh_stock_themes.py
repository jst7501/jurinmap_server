"""
네이버 finance 테마 그룹 페이지 일괄 스크래핑 → stock_themes 테이블 보강.

흐름:
  1. https://finance.naver.com/sise/theme.naver?page=N (1~10) 순회 → 테마 no/이름 수집
  2. /sise/sise_group_detail.naver?type=theme&no=N → 종목 코드 수집
  3. stock_themes 에 (code, theme) UPSERT (PK 충돌 무시)

옵션:
  --dry-run         DB 쓰기 안 함, 결과만 출력
  --replace         기존 stock_themes 전체 삭제 후 재작성 (기본: 추가만)
  --max-themes N    상한 (테스트용)
  --sleep SEC       페이지 간 sleep (기본 0.3)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("USE_POSTGRES", "1")

from server.db.connections import get_stocks_conn  # noqa: E402

BASE = "https://finance.naver.com"
LIST_URL = BASE + "/sise/theme.naver?page={page}"
DETAIL_URL = BASE + "/sise/sise_group_detail.naver?type=theme&no={no}"
HEADERS = {"User-Agent": "Mozilla/5.0 (jurinmap-scraper)"}


def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.encoding = "euc-kr"
    return r.text


def parse_theme_list(html: str) -> list[tuple[str, str]]:
    """리스트 페이지 → [(theme_no, theme_name), ...] 반환"""
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=lambda h: h and "sise_group_detail" in h):
        href = a.get("href") or ""
        m = re.search(r"no=(\d+)", href)
        if not m:
            continue
        name = a.get_text(strip=True)
        if not name:
            continue
        out.append((m.group(1), name))
    return out


def parse_theme_codes(html: str) -> list[str]:
    """테마 detail 페이지 → 종목 코드 리스트 (dedup)"""
    codes = re.findall(r"/item/main\.naver\?code=(\d{6})", html)
    return list(dict.fromkeys(codes))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--replace", action="store_true", help="기존 stock_themes 전체 삭제 후 재작성")
    ap.add_argument("--max-themes", type=int, default=None)
    ap.add_argument("--max-list-pages", type=int, default=12, help="theme list 페이지 상한")
    ap.add_argument("--sleep", type=float, default=0.3)
    args = ap.parse_args()

    # ── Step 1: 테마 목록 수집 (페이지 순회) ─────────────────────
    themes: list[tuple[str, str]] = []
    seen_no: set[str] = set()
    for page in range(1, args.max_list_pages + 1):
        try:
            html = fetch(LIST_URL.format(page=page))
        except Exception as e:
            print(f"[list] page={page} fetch failed: {e}", file=sys.stderr)
            break
        rows = parse_theme_list(html)
        new = [(n, name) for n, name in rows if n not in seen_no]
        if not new:
            # 더 이상 새 테마 없음 → 페이지네이션 종료
            print(f"[list] page={page} 새 테마 없음 — 종료")
            break
        for n, name in new:
            seen_no.add(n)
            themes.append((n, name))
        print(f"[list] page={page} 새 테마 {len(new)}개 (누적 {len(themes)})")
        time.sleep(args.sleep)
        if args.max_themes and len(themes) >= args.max_themes:
            themes = themes[: args.max_themes]
            break

    print(f"\n총 테마 {len(themes)}개 수집")

    # ── Step 2: 테마별 종목 코드 수집 ────────────────────────────
    pairs: list[tuple[str, str]] = []  # (code, theme_name)
    fail_count = 0
    for i, (no, name) in enumerate(themes, 1):
        try:
            html = fetch(DETAIL_URL.format(no=no))
            codes = parse_theme_codes(html)
        except Exception as e:
            print(f"[detail] no={no} '{name}' 실패: {e}", file=sys.stderr)
            fail_count += 1
            continue
        for c in codes:
            pairs.append((c, name))
        if i % 25 == 0:
            print(f"[detail] {i}/{len(themes)} (페어 누적 {len(pairs)}, 실패 {fail_count})")
        time.sleep(args.sleep)

    print(f"\n총 페어 {len(pairs)}개 (실패 테마 {fail_count}개)")

    # 종목당 평균 / 테마당 평균
    code_count: dict[str, int] = {}
    theme_count: dict[str, int] = {}
    for c, t in pairs:
        code_count[c] = code_count.get(c, 0) + 1
        theme_count[t] = theme_count.get(t, 0) + 1
    if code_count:
        avg_per_code = sum(code_count.values()) / max(1, len(code_count))
        print(f"커버 종목 {len(code_count)}개, 종목당 평균 {avg_per_code:.2f} 테마")
    if theme_count:
        avg_per_theme = sum(theme_count.values()) / max(1, len(theme_count))
        print(f"테마당 평균 {avg_per_theme:.1f} 종목")

    if args.dry_run:
        print("\n--dry-run — DB 쓰기 안 함")
        return 0

    # ── Step 3: DB UPSERT ──────────────────────────────────────
    conn = get_stocks_conn()
    try:
        if args.replace:
            conn.execute("DELETE FROM stock_themes")
            print("[db] 기존 stock_themes 삭제")

        # PK = (code, theme) 가정 (스키마에 따라 ON CONFLICT 동작 다를 수 있음)
        # 일단 try-insert + 충돌 시 무시 패턴
        inserted = 0
        skipped = 0
        for code, theme in pairs:
            try:
                conn.execute(
                    "INSERT INTO stock_themes (code, theme) VALUES (?, ?) ON CONFLICT DO NOTHING",
                    (code, theme),
                )
                inserted += 1
            except Exception:
                skipped += 1
        conn.commit()
        print(f"[db] 시도 {inserted}건, 스킵 {skipped}건")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
