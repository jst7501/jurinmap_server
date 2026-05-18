from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Any

import FinanceDataReader as fdr

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
from server.db.connections import get_stocks_conn


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        s = str(v).strip().replace(",", "")
        if s == "" or s.lower() == "nan":
            return None
        return int(float(s))
    except Exception:
        return None


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        s = str(v).strip().replace(",", "")
        if s == "" or s.lower() == "nan":
            return None
        return float(s)
    except Exception:
        return None


def _to_text(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kr_etf_master (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT,
            price BIGINT,
            rise_fall TEXT,
            change_amt BIGINT,
            change_rate DOUBLE PRECISION,
            nav DOUBLE PRECISION,
            earning_rate DOUBLE PRECISION,
            volume BIGINT,
            amount BIGINT,
            market_cap BIGINT,
            source TEXT NOT NULL DEFAULT 'fdr_etf_kr',
            asof_date TEXT NOT NULL,
            raw_json TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_kr_etf_master_asof
        ON kr_etf_master(asof_date)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_kr_etf_master_name
        ON kr_etf_master(name)
        """
    )
    conn.commit()


def main() -> int:
    now = datetime.now()
    asof = now.strftime("%Y%m%d")
    ts = now.strftime("%Y-%m-%d %H:%M:%S")

    df = fdr.StockListing("ETF/KR")
    if df is None or df.empty:
        print("ETF/KR source returned empty dataset")
        return 1

    rows = df.to_dict("records")
    conn = get_stocks_conn()
    try:
        _ensure_schema(conn)
        upserted = 0
        for r in rows:
            code = _to_text(r.get("Symbol"))
            name = _to_text(r.get("Name"))
            if not code or not name:
                continue
            if code.isdigit():
                code = code.zfill(6)

            conn.execute(
                """
                INSERT INTO kr_etf_master (
                    code, name, category, price, rise_fall, change_amt, change_rate,
                    nav, earning_rate, volume, amount, market_cap,
                    source, asof_date, raw_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name = excluded.name,
                    category = excluded.category,
                    price = excluded.price,
                    rise_fall = excluded.rise_fall,
                    change_amt = excluded.change_amt,
                    change_rate = excluded.change_rate,
                    nav = excluded.nav,
                    earning_rate = excluded.earning_rate,
                    volume = excluded.volume,
                    amount = excluded.amount,
                    market_cap = excluded.market_cap,
                    source = excluded.source,
                    asof_date = excluded.asof_date,
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                (
                    code,
                    name,
                    _to_text(r.get("Category")),
                    _to_int(r.get("Price")),
                    _to_text(r.get("RiseFall")),
                    _to_int(r.get("Change")),
                    _to_float(r.get("ChangeRate")),
                    _to_float(r.get("NAV")),
                    _to_float(r.get("EarningRate")),
                    _to_int(r.get("Volume")),
                    _to_int(r.get("Amount")),
                    _to_int(r.get("MarCap")),
                    "fdr_etf_kr",
                    asof,
                    json.dumps(r, ensure_ascii=False),
                    ts,
                ),
            )
            upserted += 1

        conn.commit()
        print(
            json.dumps(
                {
                    "status": "ok",
                    "asof_date": asof,
                    "source_rows": len(rows),
                    "upserted": upserted,
                    "table": "kr_etf_master",
                },
                ensure_ascii=False,
            )
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
