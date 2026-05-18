"""
테마 데이터 파서 & DB 로더 (Postgres 전용)
data/theme_raw.txt → Postgres의 stock_themes 테이블

실행: python scripts/parse_themes.py
"""
import os, sys, re

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "server"))


from server.db.connections import get_stocks_conn  # noqa: E402

THEME_RAW = os.path.join(ROOT_DIR, "data", "theme_raw.txt")


def get_conn():
    return get_stocks_conn()


# ─── 1. theme_raw.txt 파싱 ──────────────────────────────────
def parse_theme_file(path: str) -> dict:
    """
    반환: {theme_name: [stock_name, ...]}
    """
    themes = {}
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            # [카테고리] 헤더 건너뜀
            if line.startswith("[") and line.endswith("]"):
                continue
            # "테마명: 종목1, 종목2, ..." 형식
            if ":" not in line:
                continue
            theme_part, stocks_part = line.split(":", 1)
            theme = theme_part.strip()
            stocks = [s.strip() for s in stocks_part.split(",") if s.strip()]
            if theme and stocks:
                themes[theme] = stocks
    return themes


# ─── 2. 종목명 → 코드 조회 ──────────────────────────────────
def build_name_code_map(conn) -> dict:
    rows = conn.execute("SELECT code, name FROM stocks").fetchall()
    return {row[1]: row[0] for row in rows}


# ─── 2.5. theme_aliases 로드 (rename / 제외) ────────────────
def load_alias_map(conn) -> dict:
    """
    Returns: {source_theme: display_theme_or_None}
      · None이면 제외 (blocklist)
      · 값이면 rename
      · 키가 없으면 원본 그대로 (apply_alias에서 처리)
    """
    try:
        rows = conn.execute(
            "SELECT source_theme, display_theme FROM theme_aliases"
        ).fetchall()
    except Exception:
        # theme_aliases 테이블 미존재 시 비어있는 매핑 반환 (legacy 호환)
        return {}
    return {dict(r)["source_theme"]: dict(r)["display_theme"] for r in rows}


def apply_alias(alias_map: dict, theme: str):
    """alias 적용 결과 반환. None이면 제외."""
    if theme in alias_map:
        return alias_map[theme]  # None or replacement
    return theme  # 원본 유지


# ─── 3. stock_themes 테이블 생성 및 INSERT ──────────────────
def load_themes_to_db(themes: dict, name_map: dict, conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_themes (
            code  TEXT NOT NULL,
            theme TEXT NOT NULL,
            PRIMARY KEY (code, theme)
        )
    """)
    conn.execute("DELETE FROM stock_themes")  # 기존 데이터 초기화

    alias_map = load_alias_map(conn)
    if alias_map:
        n_block = sum(1 for v in alias_map.values() if v is None)
        n_rename = len(alias_map) - n_block
        print(f"▶ theme_aliases 적용: rename {n_rename}건 / 제외 {n_block}건")

    inserted = 0
    skipped_alias = 0
    missed_names = set()

    for theme, stock_names in themes.items():
        mapped_theme = apply_alias(alias_map, theme)
        if mapped_theme is None:
            skipped_alias += len(stock_names)
            continue
        for sname in stock_names:
            code = name_map.get(sname)
            if code:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO stock_themes (code, theme) VALUES (?, ?)",
                        (code, mapped_theme)
                    )
                    inserted += 1
                except Exception as e:
                    print(f"  INSERT 실패: {sname}({code}) / {mapped_theme} → {e}")
            else:
                missed_names.add(sname)

    try:
        conn.commit()
    except Exception:
        pass
    if skipped_alias:
        print(f"▶ alias 제외로 스킵된 종목-테마 쌍: {skipped_alias}건")
    return inserted, missed_names


# ─── 메인 ───────────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  테마 데이터 파싱 & DB 로드 (Postgres)")
    print("=" * 55)

    # 파싱
    themes = parse_theme_file(THEME_RAW)
    print(f"\n▶ 파싱된 테마 수: {len(themes)}")
    total_stocks = sum(len(v) for v in themes.values())
    print(f"▶ 테마 내 종목 항목 총계: {total_stocks}")

    # DB 연결
    conn = get_conn()

    # 이름-코드 맵
    name_map = build_name_code_map(conn)
    print(f"▶ DB 종목 수: {len(name_map)}")

    # 로드
    inserted, missed = load_themes_to_db(themes, name_map, conn)
    try:
        conn.close()
    except Exception:
        pass

    print(f"\n[OK] stock_themes 삽입 완료: {inserted}건")

    if missed:
        print(f"\n[WARN] DB에서 못 찾은 종목명 ({len(missed)}개):")
        for nm in sorted(missed):
            print(f"   - {nm}")
    else:
        print(">> 미매핑 종목 없음")

    # 결과 확인
    conn2 = get_conn()
    cnt = conn2.execute("SELECT COUNT(*) FROM stock_themes").fetchone()[0]
    themes_cnt = conn2.execute("SELECT COUNT(DISTINCT theme) FROM stock_themes").fetchone()[0]
    try:
        conn2.close()
    except Exception:
        pass
    print(f"\n[결과] stock_themes: {cnt}행 / {themes_cnt}개 테마")
    print("=" * 55)


if __name__ == "__main__":
    main()
