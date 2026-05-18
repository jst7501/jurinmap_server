"""
gen_etf_brand_logos.py — ETF 브랜드별 식별 로고 생성 (2026-05-18)

ETF 는 네이버가 개별 로고를 안 줌. 운용사 브랜드(KODEX/TIGER/RISE 등) 별로
시그니처 컬러 + 이니셜 배지 SVG 를 자체 생성해 매칭.
(실제 브랜드 로고 복제가 아니라 식별용 자체 디자인 — 단순 도형 + 텍스트)

저장: data/company_logos/<etf_code>.svg
같은 브랜드 ETF 는 동일 디자인 (KODEX 233개 모두 같은 KODEX 배지).

사용:
  python scripts/gen_etf_brand_logos.py
  python scripts/gen_etf_brand_logos.py --dry-run
"""
import sys
import io
import os
import argparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db.connections import get_stocks_conn  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGO_DIR = os.path.join(ROOT, "data", "company_logos")
EXTS = ("png", "svg", "jpg", "jpeg", "webp")

# 브랜드 prefix → (이니셜, 배경색). 색은 운용사 시그니처 컬러 계열.
BRAND_MAP = {
    "KODEX":      ("K",  "#1428A0"),  # 삼성자산운용
    "TIGER":      ("T",  "#E0701B"),  # 미래에셋자산운용
    "RISE":       ("R",  "#F5A623"),  # KB자산운용
    "ACE":        ("A",  "#E60012"),  # 한국투자신탁운용
    "PLUS":       ("P",  "#F37021"),  # 한화자산운용
    "SOL":        ("S",  "#0046FF"),  # 신한자산운용
    "KIWOOM":     ("K",  "#C8102E"),  # 키움투자자산운용
    "HANARO":     ("H",  "#00A651"),  # NH아문디자산운용
    "1Q":         ("1Q", "#008485"),  # 하나자산운용
    "KoAct":      ("KA", "#1428A0"),  # 삼성액티브자산운용
    "TIME":       ("TF", "#1A2B4A"),  # 타임폴리오자산운용
    "WON":        ("W",  "#0067AC"),  # 우리자산운용
    "에셋플러스":  ("E+", "#2E7D32"),  # 에셋플러스자산운용
    "BNK":        ("B",  "#ED1C24"),  # BNK자산운용
    "HK":         ("HK", "#C8102E"),  # 흥국자산운용
    "FOCUS":      ("F",  "#5C6BC0"),  # 브이아이자산운용
    "마이티":      ("M",  "#00857C"),  # DB자산운용
    "파워":        ("PW", "#6A1B9A"),  # 교보악사자산운용
    "UNICORN":    ("U",  "#7E57C2"),
    "DAISHIN343": ("D",  "#003876"),  # 대신자산운용
    "ITF":        ("IT", "#5C6BC0"),
    "마이다스":    ("MD", "#00897B"),  # 마이다스에셋자산운용
    "TREX":       ("TX", "#00838F"),  # 유리자산운용
    "TRUSTON":    ("TR", "#37474F"),  # 트러스톤자산운용
    "VITA":       ("V",  "#00897B"),
    "KCGI":       ("KC", "#455A64"),  # KCGI자산운용
    "더제이":      ("J",  "#5D4037"),  # 더제이자산운용
    "아이엠에셋":  ("IM", "#1565C0"),  # 아이엠에셋자산운용
}


def make_svg(initials, color):
    n = len(initials)
    font_size = 19 if n == 1 else 15 if n == 2 else 12
    return (
        '<svg width="40" height="40" viewBox="0 0 40 40" '
        'xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="40" height="40" rx="11" fill="{color}"/>'
        f'<text x="20" y="21" font-family="Helvetica, Arial, sans-serif" '
        f'font-size="{font_size}" font-weight="700" fill="#FFFFFF" '
        f'text-anchor="middle" dominant-baseline="central" '
        f'letter-spacing="0.5">{initials}</text>'
        "</svg>"
    )


def existing_codes():
    have = set()
    for fn in os.listdir(LOGO_DIR):
        if "." in fn:
            base, ext = fn.rsplit(".", 1)
            if ext.lower() in EXTS:
                have.add(base)
    return have


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    os.makedirs(LOGO_DIR, exist_ok=True)
    have = existing_codes()

    conn = get_stocks_conn()
    cur = conn.cursor()
    cur.execute("SELECT code, name FROM stocks WHERE market = 'ETF' ORDER BY name")
    etfs = [(r["code"], r["name"]) for r in cur.fetchall()]
    cur.close()
    conn.close()

    # SVG 캐시 (브랜드당 1회 생성)
    svg_cache = {}
    done, skip_have, skip_nobrand = 0, 0, 0
    nobrand = []
    by_brand = {}

    for code, name in etfs:
        if code in have:
            skip_have += 1
            continue
        first = (name or "").strip().split()[0] if name else ""
        spec = BRAND_MAP.get(first)
        if not spec:
            skip_nobrand += 1
            nobrand.append((code, name))
            continue
        initials, color = spec
        if first not in svg_cache:
            svg_cache[first] = make_svg(initials, color)
        by_brand[first] = by_brand.get(first, 0) + 1
        if not args.dry_run:
            with open(os.path.join(LOGO_DIR, f"{code}.svg"), "w", encoding="utf-8") as f:
                f.write(svg_cache[first])
        done += 1

    print(f"[ETF logo] {'(dry-run) ' if args.dry_run else ''}생성 {done}, 이미보유 {skip_have}, 브랜드미상 {skip_nobrand}")
    print("브랜드별:")
    for brand, cnt in sorted(by_brand.items(), key=lambda x: -x[1]):
        ini, col = BRAND_MAP[brand]
        print(f"  {brand:14} {cnt:4}  배지='{ini}' {col}")
    if nobrand:
        print("브랜드 매핑 안 된 ETF:")
        for c, n in nobrand[:20]:
            print(f"  {c} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
