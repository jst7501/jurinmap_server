"""
Naver + pykrx 확장 데이터를 수집하여 Postgres에 upsert 합니다.

테이블:
- naver_extended
- pykrx_extended
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "server"))


from collectors.naver_extended import NaverExtendedCollector
from collectors.pykrx_extended import PykrxExtendedCollector
from server.db.connections import get_stocks_conn

JSON_PATH = os.path.join(ROOT_DIR, "data", "top100_full_latest.json")


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def j(v):
    return json.dumps(v, ensure_ascii=False, default=str) if v is not None else None


def get_conn():
    return get_stocks_conn()


def _add_col(conn, table, col, col_type="TEXT"):
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}")
    except Exception:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except Exception:
            pass


def ensure_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS naver_extended (
            code                         TEXT PRIMARY KEY,
            collected_at                 TEXT,
            market_cap_text              TEXT,
            listed_shares                INTEGER,
            foreign_ownership_pct        REAL,
            investment_opinion_score     REAL,
            investment_opinion_label     TEXT,
            target_price                 INTEGER,
            consensus_analyst_count      INTEGER,
            high_52w                     INTEGER,
            low_52w                      INTEGER,
            per_ttm                      REAL,
            eps_ttm                      REAL,
            est_per                      REAL,
            est_eps                      REAL,
            pbr                          REAL,
            bps                          REAL,
            dividend_yield               REAL,
            consensus_eps                REAL,
            investor_trend_7d_json       TEXT,
            broker_top_json              TEXT,
            peer_compare_json            TEXT,
            polling_json                 TEXT,
            stock_end_type               TEXT,
            item_logo_url                TEXT,
            item_logo_png_url            TEXT,
            status                       TEXT,
            error                        TEXT,
            updated_at                   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_naver_extended_status ON naver_extended(status);

        CREATE TABLE IF NOT EXISTS pykrx_extended (
            code                                TEXT PRIMARY KEY,
            market                              TEXT,
            asof                                TEXT,
            start                               TEXT,
            status                              TEXT,
            errors_json                         TEXT,
            fundamental_json                    TEXT,
            ohlcv_json                          TEXT,
            market_cap_json                     TEXT,
            short_balance_json                  TEXT,
            short_volume_json                   TEXT,
            short_value_json                    TEXT,
            trading_value_json                  TEXT,
            trading_volume_json                 TEXT,
            foreign_exhaustion_json             TEXT,
            market_investor_value_top20_json    TEXT,
            market_investor_volume_top20_json   TEXT,
            investor_net_by_ticker_json         TEXT,
            updated_at                          TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pykrx_extended_status ON pykrx_extended(status);
        """
    )
    _add_col(conn, "pykrx_extended", "ohlcv_json")
    _add_col(conn, "naver_extended", "stock_end_type")
    _add_col(conn, "naver_extended", "item_logo_url")
    _add_col(conn, "naver_extended", "item_logo_png_url")
    conn.commit()


def load_universe(conn):
    rows = conn.execute("SELECT code, market FROM stocks ORDER BY code").fetchall()
    if rows:
        return [{"code": r[0], "market": r[1]} for r in rows]

    if not os.path.exists(JSON_PATH):
        return []

    with open(JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)

    out = []
    for code, payload in data.items():
        if code.startswith("_"):
            continue
        out.append({"code": code, "market": payload.get("market", "")})
    return out


def to_pykrx_market(market_name):
    m = str(market_name or "").strip()
    if "닥" in m.upper():
        return "KOSDAQ"
    if "PI" in m.upper():
        return "KOSPI"
    if "코스닥" in m:
        return "KOSDAQ"
    return "KOSPI"


def upsert_naver(conn, payload, ts):
    data = {
        "code": payload.get("code"),
        "collected_at": payload.get("collected_at"),
        "market_cap_text": payload.get("market_cap_text"),
        "listed_shares": payload.get("listed_shares"),
        "foreign_ownership_pct": payload.get("foreign_ownership_pct"),
        "investment_opinion_score": payload.get("investment_opinion_score"),
        "investment_opinion_label": payload.get("investment_opinion_label"),
        "target_price": payload.get("target_price"),
        "consensus_analyst_count": payload.get("consensus_analyst_count"),
        "high_52w": payload.get("high_52w"),
        "low_52w": payload.get("low_52w"),
        "per_ttm": payload.get("per_ttm"),
        "eps_ttm": payload.get("eps_ttm"),
        "est_per": payload.get("est_per"),
        "est_eps": payload.get("est_eps"),
        "pbr": payload.get("pbr"),
        "bps": payload.get("bps"),
        "dividend_yield": payload.get("dividend_yield"),
        "consensus_eps": payload.get("consensus_eps"),
        "investor_trend_7d_json": j(payload.get("investor_trend_7d") or []),
        "broker_top_json": j(payload.get("broker_top") or []),
        "peer_compare_json": j(payload.get("peer_compare") or []),
        "polling_json": j(payload.get("polling") or {}),
        "stock_end_type": payload.get("stock_end_type"),
        "item_logo_url": payload.get("item_logo_url"),
        "item_logo_png_url": payload.get("item_logo_png_url"),
        "status": payload.get("status"),
        "error": payload.get("error"),
        "updated_at": ts,
    }

    cols = ", ".join(data.keys())
    vals = ", ".join(["?"] * len(data))
    updates = ", ".join(f"{k}=excluded.{k}" for k in data if k != "code")
    sql = f"INSERT INTO naver_extended({cols}) VALUES({vals}) ON CONFLICT(code) DO UPDATE SET {updates}"
    conn.execute(sql, list(data.values()))


def upsert_pykrx(conn, payload, ts):
    data = {
        "code": payload.get("code"),
        "market": payload.get("market"),
        "asof": payload.get("asof"),
        "start": payload.get("start"),
        "status": payload.get("status"),
        "errors_json": j(payload.get("errors") or {}),
        "fundamental_json": j(payload.get("fundamental") or []),
        "ohlcv_json": j(payload.get("ohlcv") or []),
        "market_cap_json": j(payload.get("market_cap") or []),
        "short_balance_json": j(payload.get("short_balance") or []),
        "short_volume_json": j(payload.get("short_volume") or []),
        "short_value_json": j(payload.get("short_value") or []),
        "trading_value_json": j(payload.get("trading_value") or []),
        "trading_volume_json": j(payload.get("trading_volume") or []),
        "foreign_exhaustion_json": j(payload.get("foreign_exhaustion") or []),
        "market_investor_value_top20_json": j(payload.get("market_investor_value_top20") or []),
        "market_investor_volume_top20_json": j(payload.get("market_investor_volume_top20") or []),
        "investor_net_by_ticker_json": j(payload.get("investor_net_by_ticker") or {}),
        "updated_at": ts,
    }

    cols = ", ".join(data.keys())
    vals = ", ".join(["?"] * len(data))
    updates = ", ".join(f"{k}=excluded.{k}" for k in data if k != "code")
    sql = f"INSERT INTO pykrx_extended({cols}) VALUES({vals}) ON CONFLICT(code) DO UPDATE SET {updates}"
    conn.execute(sql, list(data.values()))


def build_pykrx_skip_payload(code, market, reason):
    return {
        "code": code,
        "market": market,
        "asof": None,
        "start": None,
        "status": "error",
        "errors": {"connectivity": reason},
        "fundamental": [],
        "ohlcv": [],
        "market_cap": [],
        "short_balance": [],
        "short_volume": [],
        "short_value": [],
        "trading_value": [],
        "trading_volume": [],
        "foreign_exhaustion": [],
        "market_investor_value_top20": [],
        "market_investor_volume_top20": [],
        "investor_net_by_ticker": {},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="테스트용 종목 수 제한")
    parser.add_argument("--lookback", type=int, default=20, help="pykrx 일별 조회 lookback (영업일 기준)")
    args = parser.parse_args()

    conn = get_conn()
    ensure_schema(conn)
    universe = load_universe(conn)
    if args.limit and args.limit > 0:
        universe = universe[: args.limit]
    if not universe:
        print("수집 대상 종목이 없습니다.")
        conn.close()
        return

    naver_col = NaverExtendedCollector()
    pykrx_col = PykrxExtendedCollector()

    stats = {
        "total": len(universe),
        "naver_ok": 0,
        "naver_error": 0,
        "pykrx_ok": 0,
        "pykrx_partial": 0,
        "pykrx_error": 0,
        "institution_forecast_available": 0,
        "institution_forecast_missing": 0,
        "pykrx_short_balance_nonempty": 0,
        "pykrx_foreign_exhaustion_nonempty": 0,
        "pykrx_ohlcv_nonempty": 0,
    }

    print("=" * 64)
    print("Naver + pykrx 확장 데이터 DB 적재 시작")
    print(f"대상 종목: {len(universe)}개")
    print("=" * 64)

    pykrx_available = True
    pykrx_skip_reason = ""
    probe_code = "005930"
    probe_market = "KOSPI"
    probe = pykrx_col.collect_for_ticker(probe_code, probe_market, lookback_days=max(5, args.lookback // 2))
    probe_errors = probe.get("errors") or {}
    probe_empty = not any(
        [
            probe.get("fundamental"),
            probe.get("ohlcv"),
            probe.get("market_cap"),
            probe.get("short_balance"),
            probe.get("short_volume"),
            probe.get("short_value"),
            probe.get("trading_value"),
            probe.get("trading_volume"),
            probe.get("foreign_exhaustion"),
        ]
    )
    if probe_empty and len(probe_errors) >= 5:
        pykrx_available = False
        pykrx_skip_reason = "KRX/pykrx endpoint 접근 실패(환경/네트워크 제한 가능성)"
        print(f"[WARN] pykrx 사전 점검 실패: {pykrx_skip_reason}")
    else:
        print(f"[INFO] pykrx 사전 점검 성공 (status={probe.get('status')}, errors={len(probe_errors)})")

    started = time.time()
    ts = now()

    for idx, item in enumerate(universe, 1):
        code = item["code"]
        market = to_pykrx_market(item.get("market"))

        naver_data = naver_col.get_snapshot(code)
        upsert_naver(conn, naver_data, ts)
        if naver_data.get("status") == "ok":
            stats["naver_ok"] += 1
        else:
            stats["naver_error"] += 1

        has_inst_forecast = any(
            [
                naver_data.get("target_price") is not None,
                naver_data.get("est_eps") is not None,
                naver_data.get("consensus_eps") is not None,
                naver_data.get("consensus_analyst_count") is not None,
            ]
        )
        if has_inst_forecast:
            stats["institution_forecast_available"] += 1
        else:
            stats["institution_forecast_missing"] += 1

        if pykrx_available:
            pykrx_data = pykrx_col.collect_for_ticker(code, market=market, lookback_days=args.lookback)
        else:
            pykrx_data = build_pykrx_skip_payload(code, market, pykrx_skip_reason)
        upsert_pykrx(conn, pykrx_data, ts)

        py_status = pykrx_data.get("status")
        if py_status == "ok":
            stats["pykrx_ok"] += 1
        elif py_status == "partial":
            stats["pykrx_partial"] += 1
        else:
            stats["pykrx_error"] += 1

        if pykrx_data.get("short_balance"):
            stats["pykrx_short_balance_nonempty"] += 1
        if pykrx_data.get("foreign_exhaustion"):
            stats["pykrx_foreign_exhaustion_nonempty"] += 1
        if pykrx_data.get("ohlcv"):
            stats["pykrx_ohlcv_nonempty"] += 1

        if idx % 10 == 0 or idx == len(universe):
            conn.commit()
        if idx % 20 == 0 or idx == len(universe):
            elapsed = time.time() - started
            print(f"  - {idx}/{len(universe)} 처리 완료 ({elapsed:.1f}s)")

    conn.commit()
    conn.close()

    elapsed = time.time() - started
    print("\n" + "=" * 64)
    print("적재 완료")
    print(f"소요 시간: {elapsed:.1f}초")
    print(f"네이버 성공/실패: {stats['naver_ok']} / {stats['naver_error']}")
    print(
        f"pykrx 성공/부분/실패: "
        f"{stats['pykrx_ok']} / {stats['pykrx_partial']} / {stats['pykrx_error']}"
    )
    print(
        f"기관 예측치 확보/미확보: "
        f"{stats['institution_forecast_available']} / {stats['institution_forecast_missing']}"
    )
    print(f"pykrx OHLCV 비어있지 않은 종목 수: {stats['pykrx_ohlcv_nonempty']}")
    print(f"pykrx 공매도잔고 비어있지 않은 종목 수: {stats['pykrx_short_balance_nonempty']}")
    print(f"pykrx 외국인소진율 비어있지 않은 종목 수: {stats['pykrx_foreign_exhaustion_nonempty']}")
    print("=" * 64)
    print("JSON_SUMMARY=" + j(stats))


if __name__ == "__main__":
    main()
