"""
미국 테마 데이터 파서 & DB 로더
data/theme_raw_us.txt → Postgres의 stock_themes_us 테이블

실행: python scripts/parse_themes_us.py
"""
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

THEME_RAW = os.path.join(ROOT_DIR, "data", "theme_raw_us.txt")


def _get_conn():
    """server.db.connections.get_stocks_conn() 을 재사용 (Postgres)."""
    from server.db.connections import get_stocks_conn
    return get_stocks_conn()


def parse_theme_file(path: str) -> dict:
    themes: dict[str, list[str]] = {}
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                continue
            if ":" not in line:
                continue
            theme_part, tickers_part = line.split(":", 1)
            theme = theme_part.strip()
            tickers = [t.strip().upper() for t in tickers_part.split(",") if t.strip()]
            if theme and tickers:
                themes[theme] = tickers
    return themes


def ensure_schema(conn) -> None:
    """Postgres 스키마 (CREATE TABLE IF NOT EXISTS, idempotent)."""
    stmts = [
        """
        CREATE TABLE IF NOT EXISTS us_stocks (
            ticker       TEXT PRIMARY KEY,
            name         TEXT,
            exchange     TEXT,
            sector       TEXT,
            industry     TEXT,
            market_cap   DOUBLE PRECISION,
            updated_at   TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS price_today_us (
            ticker          TEXT PRIMARY KEY,
            current_price   DOUBLE PRECISION,
            change_pct      DOUBLE PRECISION,
            change_amt      DOUBLE PRECISION,
            prev_close      DOUBLE PRECISION,
            open_price      DOUBLE PRECISION,
            day_high        DOUBLE PRECISION,
            day_low         DOUBLE PRECISION,
            trading_volume  DOUBLE PRECISION,
            trading_value   DOUBLE PRECISION,
            market_cap      DOUBLE PRECISION,
            updated_at      TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS stock_themes_us (
            ticker TEXT NOT NULL,
            theme  TEXT NOT NULL,
            PRIMARY KEY (ticker, theme)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_stock_themes_us_theme ON stock_themes_us(theme)",
    ]
    for s in stmts:
        conn.execute(s)
    try:
        conn.commit()
    except Exception:
        pass


def load_themes(themes: dict, conn) -> tuple[int, set]:
    conn.execute("DELETE FROM stock_themes_us")
    all_tickers: set[str] = set()
    inserted = 0
    for theme, tickers in themes.items():
        for t in tickers:
            all_tickers.add(t)
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO stock_themes_us (ticker, theme) VALUES (?, ?)",
                    (t, theme),
                )
                inserted += 1
            except Exception as e:
                print(f"  INSERT 실패: {t} / {theme} → {e}")
    for t in all_tickers:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO us_stocks (ticker, name) VALUES (?, ?)",
                (t, t),
            )
        except Exception:
            pass
    try:
        conn.commit()
    except Exception:
        pass
    return inserted, all_tickers


def main():
    print("=" * 55)
    print("  미국 테마 데이터 파싱 & DB 로드")
    print("=" * 55)

    themes = parse_theme_file(THEME_RAW)
    print(f"\n▶ 파싱된 테마 수: {len(themes)}")
    total = sum(len(v) for v in themes.values())
    print(f"▶ 테마 내 티커 항목 총계: {total}")

    conn = _get_conn()
    ensure_schema(conn)
    inserted, tickers = load_themes(themes, conn)
    conn.close()

    print(f"\n[OK] stock_themes_us 삽입: {inserted}건")
    print(f"[OK] 고유 티커 수: {len(tickers)}")

    conn2 = _get_conn()
    cnt = conn2.execute("SELECT COUNT(*) FROM stock_themes_us").fetchone()[0]
    themes_cnt = conn2.execute("SELECT COUNT(DISTINCT theme) FROM stock_themes_us").fetchone()[0]
    conn2.close()
    print(f"\n[결과] stock_themes_us: {cnt}행 / {themes_cnt}개 테마")
    print("=" * 55)


if __name__ == "__main__":
    main()
