"""
backfill_company_logos.py — 로고 없는 종목 로고 backfill (2026-05-18)

전략:
  1) 우선주 (이름이 우/우B/N우 로 끝남) → 본주 로고 파일을 우선주 코드로 복사
  2) 나머지 누락 종목 → 네이버 로고 URL (Stock<code>.svg) 직접 다운로드
     200 + 실제 이미지(SVG/PNG) 면 저장, 404·빈 응답이면 스킵

저장 위치: data/company_logos/<code>.<ext>
API(part01_realtime_base._get_local_logo_url) 가 자동으로 logo_local_url 로 노출.

사용:
  python scripts/backfill_company_logos.py            # 전체
  python scripts/backfill_company_logos.py --prefs-only   # 우선주만
  python scripts/backfill_company_logos.py --limit 50     # 네이버 다운 N개만 (테스트)
"""
import sys
import io
import os
import re
import time
import shutil
import argparse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db.connections import get_stocks_conn  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGO_DIR = os.path.join(ROOT, "data", "company_logos")
EXTS = ("png", "svg", "jpg", "jpeg", "webp")
NAVER_LOGO = "https://ssl.pstatic.net/imgstock/fn/real/logo/stock/Stock{code}.svg"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 우선주 이름 → 본주 이름 (접미사 제거)
PREF_SUFFIX = re.compile(r"(\d*우B?)$")


def existing_codes():
    have = {}
    for fn in os.listdir(LOGO_DIR):
        if "." not in fn:
            continue
        base, ext = fn.rsplit(".", 1)
        if ext.lower() in EXTS:
            have[base] = fn
    return have


def find_logo_file(code, have):
    """code 의 로고 파일명 반환 (없으면 None)"""
    return have.get(code)


def download_naver_logo(code, dest_path):
    """네이버 로고 SVG 다운로드. 성공 시 True."""
    url = NAVER_LOGO.format(code=code)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status != 200:
                return False
            data = resp.read()
            # 실제 이미지인지 — SVG 면 '<svg' 포함, 최소 100바이트
            if len(data) < 100:
                return False
            head = data[:200].lower()
            if b"<svg" not in head and b"<?xml" not in head and not data[:8].startswith((b"\x89PNG", b"\xff\xd8")):
                return False
            with open(dest_path, "wb") as f:
                f.write(data)
            return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefs-only", action="store_true", help="우선주 본주 복사만")
    ap.add_argument("--limit", type=int, default=0, help="네이버 다운 최대 N개 (0=무제한)")
    ap.add_argument("--sleep", type=float, default=0.15, help="네이버 요청 간 sleep")
    args = ap.parse_args()

    os.makedirs(LOGO_DIR, exist_ok=True)
    have = existing_codes()
    print(f"[start] 로컬 로고 보유: {len(have)}")

    conn = get_stocks_conn()
    cur = conn.cursor()
    cur.execute("SELECT code, name, market FROM stocks ORDER BY code")
    rows = [(r["code"], r["name"], r["market"]) for r in cur.fetchall()]
    # 본주 이름 → 코드 인덱스
    name_to_code = {}
    for code, name, market in rows:
        nm = (name or "").strip()
        if nm:
            name_to_code.setdefault(nm, code)
    cur.close()
    conn.close()

    missing = [(c, n, m) for c, n, m in rows if c not in have]
    print(f"[start] 누락 종목: {len(missing)}")

    # ── 1단계: 우선주 본주 로고 복사 ──
    pref_done, pref_fail = 0, 0
    pref_remaining = []
    for code, name, market in missing:
        nm = (name or "").strip()
        if not nm or "우" not in nm[-3:]:
            continue
        base_name = PREF_SUFFIX.sub("", nm).strip()
        if not base_name or base_name == nm:
            continue
        base_code = name_to_code.get(base_name)
        if not base_code:
            pref_remaining.append((code, name, "본주 코드 못 찾음: " + base_name))
            pref_fail += 1
            continue
        base_file = find_logo_file(base_code, have)
        if not base_file:
            pref_remaining.append((code, name, f"본주({base_code}) 로고 파일 없음"))
            pref_fail += 1
            continue
        ext = base_file.rsplit(".", 1)[1]
        src = os.path.join(LOGO_DIR, base_file)
        dst = os.path.join(LOGO_DIR, f"{code}.{ext}")
        try:
            shutil.copyfile(src, dst)
            have[code] = f"{code}.{ext}"
            pref_done += 1
            print(f"  [우선주] {code} {name} ← 본주 {base_code} {base_name} ({ext})")
        except Exception as e:
            pref_fail += 1
            pref_remaining.append((code, name, f"복사 실패: {e}"))
    print(f"[우선주] 복사 완료 {pref_done}, 실패 {pref_fail}")
    for c, n, reason in pref_remaining:
        print(f"  [우선주 미해결] {c} {n} — {reason}")

    if args.prefs_only:
        print("[done] --prefs-only 모드 종료")
        return 0

    # ── 2단계: 나머지 누락 → 네이버 로고 다운 ──
    remain = [(c, n, m) for c, n, m in missing if c not in have]
    if args.limit:
        remain = remain[: args.limit]
    print(f"[네이버] 다운로드 시도 대상: {len(remain)}")
    dl_ok, dl_fail = 0, 0
    for i, (code, name, market) in enumerate(remain):
        dst = os.path.join(LOGO_DIR, f"{code}.svg")
        if download_naver_logo(code, dst):
            dl_ok += 1
            if dl_ok <= 40 or dl_ok % 50 == 0:
                print(f"  [다운 OK] {code} {name}")
        else:
            dl_fail += 1
        if args.sleep:
            time.sleep(args.sleep)
        if (i + 1) % 200 == 0:
            print(f"  ... 진행 {i + 1}/{len(remain)} (성공 {dl_ok})")

    print(f"[네이버] 다운 성공 {dl_ok}, 실패(로고 없음) {dl_fail}")
    print(f"[done] 우선주 {pref_done} + 네이버 {dl_ok} = 총 {pref_done + dl_ok}개 로고 backfill")
    return 0


if __name__ == "__main__":
    sys.exit(main())
