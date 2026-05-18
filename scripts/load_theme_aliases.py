"""
테마 별칭/제외 매핑 로더
data/theme_aliases.json → stocks DB의 theme_aliases 테이블

스키마
  source_theme  TEXT PRIMARY KEY  (raw 입력 측 테마명, 예: 네이버 그대로)
  display_theme TEXT              (표시할 이름 — NULL이면 제외)
  note          TEXT              (메모)
  updated_at    TIMESTAMP

parse_themes.py 가 stock_themes에 INSERT할 때 이 테이블을 조회해
  · source 일치 + display=NULL → 스킵 (blocklist)
  · source 일치 + display=값  → display 값으로 변환 후 INSERT
  · 일치 없음                 → 원본 그대로 INSERT

실행: python scripts/load_theme_aliases.py
"""
import io
import json
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

from server.db.connections import get_stocks_conn  # noqa: E402

JSON_PATH = os.path.join(ROOT_DIR, "data", "theme_aliases.json")


def ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS theme_aliases (
            source_theme  TEXT PRIMARY KEY,
            display_theme TEXT,
            note          TEXT,
            updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_theme_aliases_display ON theme_aliases(display_theme)"
    )
    try:
        conn.commit()
    except Exception:
        pass


def upsert_aliases(conn, items: list) -> int:
    """JSON 배열을 그대로 받아 UPSERT. JSON에 없는 기존 행은 건드리지 않음 (유지)."""
    n = 0
    for it in items:
        src = str(it.get("source", "")).strip()
        if not src:
            continue
        disp = it.get("display")
        if disp is not None:
            disp = str(disp).strip() or None
        note = str(it.get("note", "")).strip() or None
        # Postgres 호환: ON CONFLICT 사용. db_compat가 SQLite UPSERT 흉내를 내지만
        # 가장 안전한 방법은 DELETE + INSERT (행 1건이라 비용 무시)
        conn.execute("DELETE FROM theme_aliases WHERE source_theme = ?", [src])
        conn.execute(
            "INSERT INTO theme_aliases (source_theme, display_theme, note) VALUES (?, ?, ?)",
            [src, disp, note],
        )
        n += 1
    try:
        conn.commit()
    except Exception:
        pass
    return n


def main() -> None:
    print("=" * 60)
    print("  theme_aliases 로더")
    print("=" * 60)

    if not os.path.exists(JSON_PATH):
        print(f"[ERROR] {JSON_PATH} 없음")
        sys.exit(1)

    with open(JSON_PATH, encoding="utf-8") as f:
        items = json.load(f)
    print(f"▶ JSON 항목 수: {len(items)}")

    conn = get_stocks_conn()
    ensure_schema(conn)
    n = upsert_aliases(conn, items)
    print(f"▶ UPSERT: {n}건")

    rows = conn.execute(
        "SELECT source_theme, display_theme, note FROM theme_aliases ORDER BY source_theme"
    ).fetchall()
    print(f"\n[현재 theme_aliases 테이블: {len(rows)}건]")
    for r in rows:
        d = dict(r)
        disp = d["display_theme"] if d["display_theme"] is not None else "<제외>"
        note = d.get("note") or ""
        print(f"  {d['source_theme']:<18} → {disp:<18} {note}")
    print("=" * 60)


if __name__ == "__main__":
    main()
