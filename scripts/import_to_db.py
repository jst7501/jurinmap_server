"""
JSON → Postgres 임포터
top100_full_latest.json 을 읽어서 Postgres의 stocks 관련 테이블에 upsert합니다.
"""
import sys, os, json, re
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "server"))


JSON_PATH = os.path.join(ROOT_DIR, "data", "top100_full_latest.json")

from server.db.connections import get_stocks_conn  # noqa: E402


# ─── DB 연결 ─────────────────────────────────────────────────
def get_conn():
    return get_stocks_conn()


# ─── 스키마 초기화 ───────────────────────────────────────────
def _split_sql_statements(script: str) -> list[str]:
    """Postgres psycopg doesn't have executescript; split on top-level ';'."""
    # Strip SQL line comments first.
    cleaned = re.sub(r"--[^\n]*", "", script)
    stmts = [s.strip() for s in cleaned.split(";") if s.strip()]
    return stmts


def init_schema(conn):
    # Postgres already has these tables (db_compat translates AUTOINCREMENT etc).
    # CREATE TABLE IF NOT EXISTS is idempotent.
    script = """
    -- 종목 기본
    CREATE TABLE IF NOT EXISTS stocks (
        code        TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        market      TEXT,
        updated_at  TEXT
    );

    -- 당일 현재가 (+ _raw JSON 포함)
    CREATE TABLE IF NOT EXISTS price_today (
        code                TEXT PRIMARY KEY,
        current_price       INTEGER,
        change_pct          REAL,
        change_amt          INTEGER,
        trading_value       INTEGER,
        trading_volume      INTEGER,
        volume_turnover_rate REAL,
        market_cap          INTEGER,
        per                 TEXT,
        pbr                 TEXT,
        eps                 TEXT,
        foreign_hold_pct    REAL,
        listed_shares       INTEGER,
        raw_json            TEXT,
        updated_at          TEXT
    );

    -- 일별 OHLCV
    CREATE TABLE IF NOT EXISTS price_daily (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        code          TEXT NOT NULL,
        date          TEXT NOT NULL,
        open          INTEGER,
        high          INTEGER,
        low           INTEGER,
        close         INTEGER,
        volume        INTEGER,
        trading_value INTEGER,
        credit_rate   REAL,
        UNIQUE(code, date)
    );
    CREATE INDEX IF NOT EXISTS idx_price_daily_code ON price_daily(code);

    -- 투자자 수급 5일
    CREATE TABLE IF NOT EXISTS investor_flow (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        code                   TEXT NOT NULL,
        date                   TEXT NOT NULL,
        foreign_net            INTEGER,
        institution_net        INTEGER,
        individual_net         INTEGER,
        etc_org_net            INTEGER,
        program_net            INTEGER,
        foreign_net_amt        INTEGER,
        institution_net_amt    INTEGER,
        individual_net_amt     INTEGER,
        foreign_buy            INTEGER,
        foreign_sell           INTEGER,
        institution_buy        INTEGER,
        institution_sell       INTEGER,
        individual_buy         INTEGER,
        individual_sell        INTEGER,
        foreign_buy_amt        INTEGER,
        foreign_sell_amt       INTEGER,
        institution_buy_amt    INTEGER,
        institution_sell_amt   INTEGER,
        individual_buy_amt     INTEGER,
        individual_sell_amt    INTEGER,
        UNIQUE(code, date)
    );
    CREATE INDEX IF NOT EXISTS idx_investor_code ON investor_flow(code);

    -- 당일 수급 요약
    CREATE TABLE IF NOT EXISTS investor_today (
        code                TEXT PRIMARY KEY,
        date                TEXT,
        foreign_net         INTEGER,
        institution_net     INTEGER,
        individual_net      INTEGER,
        full_json           TEXT
    );

    -- 프로그램 매매 5일
    CREATE TABLE IF NOT EXISTS program_trade (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        code            TEXT NOT NULL,
        date            TEXT NOT NULL,
        program_buy     INTEGER,
        program_sell    INTEGER,
        program_net     INTEGER,
        program_net_amt INTEGER,
        UNIQUE(code, date)
    );

    -- 공매도 요약
    CREATE TABLE IF NOT EXISTS short_data (
        code                       TEXT PRIMARY KEY,
        short_enabled              INTEGER,
        short_selling_volume_ratio REAL,
        updated_at                 TEXT
    );

    -- 신용잔고
    CREATE TABLE IF NOT EXISTS credit_data (
        code       TEXT PRIMARY KEY,
        rate_today REAL,
        daily_json TEXT,
        updated_at TEXT
    );

    -- 재무비율
    CREATE TABLE IF NOT EXISTS finance_ratio (
        code            TEXT PRIMARY KEY,
        debt_ratio      TEXT,
        retention_ratio TEXT,
        roe             TEXT,
        roa             TEXT,
        bps             TEXT,
        eps             TEXT,
        updated_at      TEXT
    );

    -- DART 공시 (1:N)
    CREATE TABLE IF NOT EXISTS dart_disclosures (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        code       TEXT NOT NULL,
        date       TEXT,
        title      TEXT,
        type       TEXT,
        created_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_dart_disc_code ON dart_disclosures(code);

    -- DART 주요주주
    CREATE TABLE IF NOT EXISTS dart_shareholders (
        code      TEXT PRIMARY KEY,
        data_json TEXT,
        updated_at TEXT
    );

    -- 종토방 감성
    CREATE TABLE IF NOT EXISTS board_sentiment (
        code                TEXT PRIMARY KEY,
        score               INTEGER,
        mood                TEXT,
        grade               TEXT,
        raw_score           REAL,
        post_count          INTEGER,
        agree_ratio         REAL,
        euphoria_count      INTEGER,
        despair_count       INTEGER,
        top_euphoria_json   TEXT,
        top_despair_json    TEXT,
        hot_posts_json      TEXT,
        updated_at          TEXT
    );

    -- Gemini AI 분석
    CREATE TABLE IF NOT EXISTS ai_analysis (
        code                    TEXT PRIMARY KEY,
        human_indicator_score   INTEGER,
        sentiment_phase         TEXT,
        sentiment_phase_kor     TEXT,
        core_issue              TEXT,
        contrarian_signal       TEXT,
        contrarian_signal_kor   TEXT,
        summary                 TEXT,
        sentiment_keywords_json TEXT,
        issue_keywords_json     TEXT,
        updated_at              TEXT
    );

    -- 기술적 분석
    CREATE TABLE IF NOT EXISTS tech_analysis (
        code                   TEXT PRIMARY KEY,
        current_price          INTEGER,
        ma5                    REAL,
        ma20                   REAL,
        ma60                   REAL,
        ma120                  REAL,
        div_5                  REAL,
        div_20                 REAL,
        div_60                 REAL,
        div_120                REAL,
        rs_score               REAL,
        avg_20d_trading_value  INTEGER,
        volume_profile_json    TEXT,
        returns_json           TEXT,
        updated_at             TEXT
    );

    -- 수급 비중 요약
    CREATE TABLE IF NOT EXISTS ownership_summary (
        code                TEXT PRIMARY KEY,
        foreign_pct         REAL,
        institution_pct     REAL,
        individual_pct      REAL,
        full_json           TEXT,
        updated_at          TEXT
    );

    -- 매크로 (1행)
    CREATE TABLE IF NOT EXISTS macro (
        id                       INTEGER PRIMARY KEY CHECK(id=1),
        exchange_rate            TEXT,
        exchange_rate_change_pct REAL,
        exchange_rate_change_amt REAL,
        usa_indices_json         TEXT,
        night_futures            TEXT,
        updated_at               TEXT
    );
    """
    for stmt in _split_sql_statements(script):
        try:
            conn.execute(stmt)
        except Exception as e:
            # 이미 존재하는 테이블·인덱스는 넘어간다
            msg = str(e).lower()
            if "already exists" in msg:
                continue
            raise
    try:
        conn.commit()
    except Exception:
        pass
    print("✅ 스키마 초기화 완료")


# ─── 헬퍼 ────────────────────────────────────────────────────
def j(v):
    return json.dumps(v, ensure_ascii=False) if v is not None else None

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def upsert(conn, table, data: dict):
    cols = ", ".join(data.keys())
    vals = ", ".join(["?"] * len(data))
    updates = ", ".join(f"{k}=excluded.{k}" for k in data if k != "code")
    sql = f"INSERT INTO {table}({cols}) VALUES({vals}) ON CONFLICT(code) DO UPDATE SET {updates}"
    conn.execute(sql, list(data.values()))


# ─── 종목별 임포트 ────────────────────────────────────────────
def import_stock(conn, code: str, d: dict, ts: str):

    # stocks
    upsert(conn, "stocks", {
        "code": code, "name": d.get("name",""), "market": d.get("market",""), "updated_at": ts
    })

    # price_today
    pt = d.get("price_today") or {}
    if pt:
        upsert(conn, "price_today", {
            "code": code,
            "current_price": pt.get("current_price"),
            "change_pct":    pt.get("change_pct"),
            "change_amt":    pt.get("change_amt"),
            "trading_value": pt.get("trading_value"),
            "trading_volume": pt.get("trading_volume"),
            "volume_turnover_rate": pt.get("volume_turnover_rate"),
            "market_cap":    pt.get("market_cap"),
            "per":           str(pt.get("per", "")),
            "pbr":           str(pt.get("pbr", "")),
            "eps":           str(pt.get("eps", "")),
            "foreign_hold_pct": pt.get("foreign_hold_pct"),
            "listed_shares": pt.get("listed_shares"),
            "raw_json":      j(pt.get("_raw")),
            "updated_at":    ts,
        })

    # price_daily
    for r in (d.get("daily_ohlcv") or []):
        if not r.get("date"):
            continue
        conn.execute("""
        INSERT INTO price_daily(code,date,open,high,low,close,volume,trading_value,credit_rate)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(code,date) DO UPDATE SET
          open=excluded.open, high=excluded.high, low=excluded.low,
          close=excluded.close, volume=excluded.volume,
          trading_value=excluded.trading_value, credit_rate=excluded.credit_rate
        """, (code, r["date"], r.get("open"), r.get("high"), r.get("low"),
              r.get("close"), r.get("volume"), r.get("trading_value"), r.get("credit_rate")))

    # investor_flow (5d)
    for r in (d.get("investor_5d") or []):
        if not r.get("date"):
            continue
        conn.execute("""
        INSERT INTO investor_flow(code,date,
          foreign_net,institution_net,individual_net,etc_org_net,program_net,
          foreign_net_amt,institution_net_amt,individual_net_amt,
          foreign_buy,foreign_sell,institution_buy,institution_sell,
          individual_buy,individual_sell,
          foreign_buy_amt,foreign_sell_amt,institution_buy_amt,institution_sell_amt,
          individual_buy_amt,individual_sell_amt)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(code,date) DO UPDATE SET
          foreign_net=excluded.foreign_net,
          institution_net=excluded.institution_net,
          individual_net=excluded.individual_net
        """, (code, r["date"],
              r.get("foreign"), r.get("institution"), r.get("individual"),
              r.get("etc_org"), r.get("program"),
              r.get("foreign_net_amt"), r.get("institution_net_amt"), r.get("individual_net_amt"),
              r.get("foreign_buy"), r.get("foreign_sell"),
              r.get("institution_buy"), r.get("institution_sell"),
              r.get("individual_buy"), r.get("individual_sell"),
              r.get("foreign_buy_amt"), r.get("foreign_sell_amt"),
              r.get("institution_buy_amt"), r.get("institution_sell_amt"),
              r.get("individual_buy_amt"), r.get("individual_sell_amt")))

    # investor_today
    it = d.get("investor_today") or {}
    if it:
        upsert(conn, "investor_today", {
            "code": code,
            "date": it.get("date"),
            "foreign_net": it.get("foreign"),
            "institution_net": it.get("institution"),
            "individual_net": it.get("individual"),
            "full_json": j(it),
        })

    # program_trade
    for r in (d.get("program_5d") or []):
        if not r.get("date"):
            continue
        conn.execute("""
        INSERT INTO program_trade(code,date,program_buy,program_sell,program_net,program_net_amt)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(code,date) DO UPDATE SET
          program_buy=excluded.program_buy, program_sell=excluded.program_sell,
          program_net=excluded.program_net, program_net_amt=excluded.program_net_amt
        """, (code, r["date"], r.get("program_buy"), r.get("program_sell"),
              r.get("program_net"), r.get("program_net_amt")))

    # short_data
    sd = d.get("short_data") or {}
    if sd:
        upsert(conn, "short_data", {
            "code": code,
            "short_enabled": 1 if sd.get("short_enabled") else 0,
            "short_selling_volume_ratio": sd.get("short_selling_volume_ratio"),
            "updated_at": ts,
        })

    # credit_data
    cd = d.get("credit_data") or {}
    if isinstance(cd, dict):
        upsert(conn, "credit_data", {
            "code": code,
            "rate_today": cd.get("rate_today"),
            "daily_json": j(cd.get("daily")),
            "updated_at": ts,
        })
    elif isinstance(cd, list):
        rate = cd[0].get("credit_rate", 0) if cd else 0
        upsert(conn, "credit_data", {
            "code": code, "rate_today": rate, "daily_json": j(cd), "updated_at": ts
        })

    # finance_ratio
    fr = d.get("finance_ratio") or {}
    if fr:
        upsert(conn, "finance_ratio", {
            "code": code,
            "debt_ratio": fr.get("debt_ratio"),
            "retention_ratio": fr.get("retention_ratio"),
            "roe": fr.get("roe"), "roa": fr.get("roa"),
            "bps": fr.get("bps"), "eps": fr.get("eps"),
            "updated_at": ts,
        })

    # dart_disclosures
    conn.execute("DELETE FROM dart_disclosures WHERE code=?", (code,))
    for item in (d.get("dart_disclosures") or []):
        conn.execute(
            "INSERT INTO dart_disclosures(code,date,title,type,created_at) VALUES(?,?,?,?,?)",
            (code, item.get("date"), item.get("title"), item.get("type"), ts)
        )

    # dart_shareholders
    ds = d.get("dart_shareholders") or {}
    upsert(conn, "dart_shareholders", {"code": code, "data_json": j(ds), "updated_at": ts})

    # board_sentiment
    bs = d.get("board_sentiment") or {}
    if bs:
        upsert(conn, "board_sentiment", {
            "code": code,
            "score":         bs.get("score"),
            "mood":          bs.get("mood"),
            "grade":         bs.get("grade"),
            "raw_score":     bs.get("raw_score"),
            "post_count":    bs.get("post_count") or bs.get("total_posts_scanned"),
            "agree_ratio":   bs.get("agree_ratio"),
            "euphoria_count": bs.get("euphoria_count"),
            "despair_count": bs.get("despair_count"),
            "top_euphoria_json": j(bs.get("top_euphoria")),
            "top_despair_json":  j(bs.get("top_despair")),
            "hot_posts_json":    j(bs.get("top_hot_posts") or bs.get("hot_posts")),
            "updated_at": ts,
        })

    # ai_analysis
    ai = d.get("ai_analysis") or {}
    if ai:
        upsert(conn, "ai_analysis", {
            "code": code,
            "human_indicator_score": ai.get("human_indicator_score"),
            "sentiment_phase":       ai.get("sentiment_phase"),
            "sentiment_phase_kor":   ai.get("sentiment_phase_kor"),
            "core_issue":            ai.get("core_issue"),
            "contrarian_signal":     ai.get("contrarian_signal"),
            "contrarian_signal_kor": ai.get("contrarian_signal_kor"),
            "summary":               ai.get("summary"),
            "sentiment_keywords_json": j(ai.get("sentiment_keywords")),
            "issue_keywords_json":     j(ai.get("issue_keywords")),
            "updated_at": ts,
        })

    # tech_analysis
    ta = d.get("tech_analysis") or {}
    if ta:
        upsert(conn, "tech_analysis", {
            "code": code,
            "current_price":         ta.get("current_price"),
            "ma5":  ta.get("ma5"),   "ma20": ta.get("ma20"),
            "ma60": ta.get("ma60"),  "ma120": ta.get("ma120"),
            "div_5":  ta.get("div_5"),  "div_20": ta.get("div_20"),
            "div_60": ta.get("div_60"), "div_120": ta.get("div_120"),
            "rs_score": ta.get("rs_score"),
            "avg_20d_trading_value": ta.get("avg_20d_trading_value"),
            "volume_profile_json": j(ta.get("volume_profile")),
            "returns_json":        j(ta.get("returns")),
            "updated_at": ts,
        })

    # ownership_summary
    op = d.get("ownership_summary_pct") or {}
    if op:
        upsert(conn, "ownership_summary", {
            "code": code,
            "foreign_pct":     op.get("foreign"),
            "institution_pct": op.get("institution"),
            "individual_pct":  op.get("individual"),
            "full_json":       j(op),
            "updated_at":      ts,
        })


# ─── 매크로 임포트 ────────────────────────────────────────────
def import_macro(conn, macro: dict, ts: str):
    # 기존 컬럼 없으면 추가 (마이그레이션)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(macro)").fetchall()}
    if "exchange_rate_change_pct" not in existing:
        conn.execute("ALTER TABLE macro ADD COLUMN exchange_rate_change_pct REAL")
    if "exchange_rate_change_amt" not in existing:
        conn.execute("ALTER TABLE macro ADD COLUMN exchange_rate_change_amt REAL")

    conn.execute("""
    INSERT INTO macro(id, exchange_rate, exchange_rate_change_pct, exchange_rate_change_amt,
                      usa_indices_json, night_futures, updated_at)
    VALUES(1, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
      exchange_rate=excluded.exchange_rate,
      exchange_rate_change_pct=excluded.exchange_rate_change_pct,
      exchange_rate_change_amt=excluded.exchange_rate_change_amt,
      usa_indices_json=excluded.usa_indices_json,
      night_futures=excluded.night_futures,
      updated_at=excluded.updated_at
    """, (
        macro.get("exchange_rate"),
        macro.get("exchange_rate_change_pct"),
        macro.get("exchange_rate_change_amt"),
        j(macro.get("usa_indices")),
        macro.get("night_futures"),
        ts
    ))


# ─── MAIN ────────────────────────────────────────────────────
def main():
    print("=" * 58)
    print("📦 JSON → Postgres 임포터")
    print(f"   소스: {os.path.basename(JSON_PATH)}")
    print(f"   대상: Postgres")
    print("=" * 58)

    if not os.path.exists(JSON_PATH):
        print(f"❌ JSON 파일 없음: {JSON_PATH}"); return

    print("\n📖 JSON 로딩...", end=" ", flush=True)
    with open(JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)
    codes = [c for c in data if c != "_macro"]
    print(f"{len(codes)}개 종목")

    conn = get_conn()
    init_schema(conn)

    ts = now()
    import time
    start = time.time()

    # 매크로
    macro = data.get("_macro") or {}
    if macro:
        import_macro(conn, macro, ts)
        print(f"✅ 매크로 저장: 환율 {macro.get('exchange_rate')}")

    # 종목별
    print(f"\n📊 종목 데이터 저장 중...")
    for idx, code in enumerate(codes, 1):
        import_stock(conn, code, data[code], ts)
        if idx % 20 == 0 or idx == len(codes):
            print(f"  → {idx}/{len(codes)} 완료...")

    try:
        conn.commit()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass

    elapsed = time.time() - start
    print(f"\n✅ 임포트 완료!")
    print(f"  - 종목 수  : {len(codes)}개")
    print(f"  - 소요시간 : {elapsed:.1f}초")
    print(f"  - 저장소  : Postgres")


if __name__ == "__main__":
    main()
