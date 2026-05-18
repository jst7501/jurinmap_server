"""
한국 테마 ↔ 미국 테마 매핑 로더
data/theme_mapping_kr_us.json → stocks DB의 theme_map_kr_us 테이블

실행: python scripts/load_theme_mapping.py
"""
import json
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

MAPPING_JSON = os.path.join(ROOT_DIR, "data", "theme_mapping_kr_us.json")


def get_conn():
    from server.db.connections import get_stocks_conn
    return get_stocks_conn()


def ensure_schema(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS theme_map_kr_us (
            kr_theme   TEXT NOT NULL,
            us_theme   TEXT NOT NULL,
            confidence TEXT DEFAULT 'partial',
            PRIMARY KEY (kr_theme, us_theme)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_theme_map_kr ON theme_map_kr_us(kr_theme)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_theme_map_us ON theme_map_kr_us(us_theme)")
    try:
        conn.commit()
    except Exception:
        pass


def load_mapping(conn, pairs: list) -> int:
    conn.execute("DELETE FROM theme_map_kr_us")
    inserted = 0
    for p in pairs:
        kr = str(p.get("kr", "")).strip()
        us = str(p.get("us", "")).strip()
        conf = str(p.get("confidence", "partial")).strip()
        if not kr or not us:
            continue
        try:
            conn.execute(
                "INSERT OR IGNORE INTO theme_map_kr_us (kr_theme, us_theme, confidence) VALUES (?, ?, ?)",
                (kr, us, conf),
            )
            inserted += 1
        except Exception as e:
            print(f"  삽입 실패: {kr} ↔ {us} → {e}")
    try:
        conn.commit()
    except Exception:
        pass
    return inserted


def main():
    print("=" * 55)
    print("  한국↔미국 테마 매핑 로더")
    print("=" * 55)
    with open(MAPPING_JSON, encoding="utf-8") as f:
        data = json.load(f)
    pairs = data.get("pairs", [])
    print(f"\n▶ 매핑 쌍 수: {len(pairs)}")

    conn = get_conn()
    ensure_schema(conn)
    inserted = load_mapping(conn, pairs)
    conn.close()

    conn2 = get_conn()
    total = conn2.execute("SELECT COUNT(*) FROM theme_map_kr_us").fetchone()[0]
    kr_distinct = conn2.execute("SELECT COUNT(DISTINCT kr_theme) FROM theme_map_kr_us").fetchone()[0]
    us_distinct = conn2.execute("SELECT COUNT(DISTINCT us_theme) FROM theme_map_kr_us").fetchone()[0]
    conn2.close()

    print(f"\n[OK] 삽입: {inserted}건")
    print(f"[결과] 매핑 {total}행 / KR {kr_distinct}개 / US {us_distinct}개")
    print("=" * 55)


if __name__ == "__main__":
    main()
