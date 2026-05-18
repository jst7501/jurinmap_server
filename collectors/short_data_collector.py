from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

import requests
from bs4 import BeautifulSoup

from server.db.connections import get_stocks_conn
from collectors.yfinance_collector import YFinanceCollector


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if text in ("", "-", "None", "nan", "NaN", "null"):
        return None
    text = text.replace(",", "")
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        return float(text)
    except Exception:
        return None


def _normalize_percent(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    if 0 < value <= 1:
        return round(value * 100.0, 4)
    return round(value, 4)


def _to_int(value: Any) -> Optional[int]:
    v = _to_float(value)
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    if isinstance(row, dict):
        return dict(row)
    return {}


def _iter_dicts(payload: Any):
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from _iter_dicts(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_dicts(item)


def _find_numeric(payload: Any, candidate_keys: Iterable[str], percent: bool = False) -> Optional[float]:
    normalized = [k.lower().replace("_", "") for k in candidate_keys]
    for obj in _iter_dicts(payload):
        for key, raw_value in obj.items():
            key_norm = str(key).lower().replace("_", "")
            if any((key_norm == c or c in key_norm) for c in normalized):
                value = _to_float(raw_value)
                if value is not None:
                    return _normalize_percent(value) if percent else value
    return None


def _parse_compact_number(text: str) -> Optional[float]:
    raw = str(text or "").strip().upper().replace(",", "")
    if not raw:
        return None
    mult = 1.0
    if raw.endswith("K"):
        mult = 1_000.0
        raw = raw[:-1]
    elif raw.endswith("M"):
        mult = 1_000_000.0
        raw = raw[:-1]
    elif raw.endswith("B"):
        mult = 1_000_000_000.0
        raw = raw[:-1]
    elif raw.endswith("T"):
        mult = 1_000_000_000_000.0
        raw = raw[:-1]
    v = _to_float(raw)
    if v is None:
        return None
    return float(v) * mult


class ShortDataCollector:
    def __init__(self) -> None:
        self.finnhub_api_key = os.getenv("FINNHUB_API_KEY", "").strip()
        self.fmp_api_key = os.getenv("FMP_API_KEY", "").strip()
        self.http_timeout = int(os.getenv("SHORT_DATA_TIMEOUT_SEC", "8"))
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        self._yfinance_collector: Optional[YFinanceCollector] = None
        self._quote_summary_cache: Dict[str, Dict[str, Any]] = {}
        self._quote_summary_cache_lock = threading.Lock()
        self._quote_summary_ttl_sec = int(os.getenv("YAHOO_QS_TTL_SEC", "900"))
        self._yf_info_cache: Dict[str, Dict[str, Any]] = {}
        self._yf_info_cache_lock = threading.Lock()
        self._yf_info_ttl_sec = int(os.getenv("YFINANCE_INFO_TTL_SEC", "900"))
        self._finviz_cache: Dict[str, Dict[str, Any]] = {}
        self._finviz_cache_lock = threading.Lock()
        self._finviz_ttl_sec = int(os.getenv("FINVIZ_TTL_SEC", "900"))

    def _get_yfinance_collector(self) -> Optional[YFinanceCollector]:
        if self._yfinance_collector is not None:
            return self._yfinance_collector
        try:
            self._yfinance_collector = YFinanceCollector()
            return self._yfinance_collector
        except Exception:
            return None

    def _get_yfinance_short_ownership(
        self, symbol: str, force_refresh: bool = False
    ) -> Optional[Dict[str, Any]]:
        ticker = str(symbol or "").strip().upper()
        if not ticker:
            return None
        now_ts = datetime.now(timezone.utc).timestamp()
        with self._yf_info_cache_lock:
            cached = self._yf_info_cache.get(ticker)
            if (
                cached
                and not force_refresh
                and (now_ts - float(cached.get("ts") or 0.0) < float(self._yf_info_ttl_sec))
            ):
                return cached.get("payload")

        collector = self._get_yfinance_collector()
        if collector is None:
            return None
        try:
            payload = collector.get_short_ownership_from_info(ticker)
        except Exception:
            return None

        with self._yf_info_cache_lock:
            self._yf_info_cache[ticker] = {"ts": now_ts, "payload": payload}
        return payload

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            conn = get_stocks_conn()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS us_short_interest_daily (
                        symbol TEXT NOT NULL,
                        as_of_date TEXT NOT NULL,
                        short_float_pct REAL,
                        short_interest_shares REAL,
                        borrow_fee_pct REAL,
                        days_to_cover REAL,
                        source TEXT,
                        payload_json TEXT,
                        fetched_at TEXT NOT NULL,
                        PRIMARY KEY (symbol, as_of_date)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS us_ownership_daily (
                        symbol TEXT NOT NULL,
                        as_of_date TEXT NOT NULL,
                        institutional_ownership_pct REAL,
                        insider_ownership_pct REAL,
                        source TEXT,
                        payload_json TEXT,
                        fetched_at TEXT NOT NULL,
                        PRIMARY KEY (symbol, as_of_date)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS us_short_borrow_daily (
                        symbol TEXT NOT NULL,
                        as_of_date TEXT NOT NULL,
                        available_shares BIGINT,
                        borrow_fee_pct REAL,
                        rebate_rate_pct REAL,
                        source TEXT,
                        payload_json TEXT,
                        fetched_at TEXT NOT NULL,
                        PRIMARY KEY (symbol, as_of_date)
                    )
                    """
                )
                conn.commit()
                self._schema_ready = True
            finally:
                conn.close()

    def _request_json(self, url: str, params: Dict[str, Any]) -> Optional[Any]:
        try:
            response = requests.get(url, params=params, timeout=self.http_timeout)
            if response.status_code != 200:
                return None
            return response.json()
        except Exception:
            return None

    def _get_finviz_text(self, symbol: str, force_refresh: bool = False) -> Optional[str]:
        ticker = str(symbol or "").strip().upper()
        if not ticker:
            return None
        now_ts = datetime.now(timezone.utc).timestamp()
        with self._finviz_cache_lock:
            cached = self._finviz_cache.get(ticker)
            if (
                cached
                and not force_refresh
                and (now_ts - float(cached.get("ts") or 0.0) < float(self._finviz_ttl_sec))
            ):
                return cached.get("text")

        url = f"https://finviz.com/quote.ashx?t={ticker}&p=d"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://finviz.com/",
        }
        try:
            response = requests.get(url, headers=headers, timeout=self.http_timeout)
            if response.status_code != 200:
                return None
            soup = BeautifulSoup(response.text, "lxml")
            text = soup.get_text(" ", strip=True)
            if not text:
                return None
        except Exception:
            return None

        with self._finviz_cache_lock:
            self._finviz_cache[ticker] = {"ts": now_ts, "text": text}
        return text

    def _find_pct_from_text(self, text: str, label: str) -> Optional[float]:
        pattern = rf"{re.escape(label)}\s+(-?[0-9]+(?:\.[0-9]+)?)\s*%"
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if not m:
            return None
        return _to_float(m.group(1))

    def _find_num_from_text(self, text: str, label: str) -> Optional[float]:
        pattern = rf"{re.escape(label)}\s+(-?[0-9]+(?:\.[0-9]+)?)"
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if not m:
            return None
        return _to_float(m.group(1))

    def _find_compact_num_from_text(self, text: str, label: str) -> Optional[float]:
        pattern = rf"{re.escape(label)}\s+([0-9]+(?:\.[0-9]+)?[KMBT]?)"
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if not m:
            return None
        return _parse_compact_number(m.group(1))

    def _qs_pick_raw(self, payload: Dict[str, Any], *path: str) -> Optional[float]:
        cur: Any = payload
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur.get(key)
        if isinstance(cur, dict):
            raw = cur.get("raw")
            if raw is not None:
                return _to_float(raw)
        return _to_float(cur)

    def _get_quote_summary_payload(self, symbol: str, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        ticker = str(symbol or "").strip().upper()
        if not ticker:
            return None

        now_ts = datetime.now(timezone.utc).timestamp()
        with self._quote_summary_cache_lock:
            cached = self._quote_summary_cache.get(ticker)
            if (
                cached
                and not force_refresh
                and (now_ts - float(cached.get("ts") or 0.0) < float(self._quote_summary_ttl_sec))
            ):
                return cached.get("payload")

        url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
        params = {
            "modules": "defaultKeyStatistics,financialData,price,summaryDetail",
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        }

        try:
            response = requests.get(url, params=params, headers=headers, timeout=self.http_timeout)
            if response.status_code != 200:
                return None
            body = response.json() or {}
            result = ((body.get("quoteSummary") or {}).get("result") or [])
            if not isinstance(result, list) or not result:
                return None
            payload = result[0] if isinstance(result[0], dict) else None
            if not payload:
                return None
        except Exception:
            return None

        with self._quote_summary_cache_lock:
            self._quote_summary_cache[ticker] = {"ts": now_ts, "payload": payload}
        return payload

    def _load_short_cache(self, symbol: str) -> Dict[str, Any]:
        conn = get_stocks_conn()
        try:
            row = conn.execute(
                """
                SELECT symbol, as_of_date, short_float_pct, short_interest_shares,
                       borrow_fee_pct, days_to_cover, source, payload_json, fetched_at
                FROM us_short_interest_daily
                WHERE symbol = ?
                ORDER BY as_of_date DESC
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()
            return _row_to_dict(row)
        finally:
            conn.close()

    def _load_ownership_cache(self, symbol: str) -> Dict[str, Any]:
        conn = get_stocks_conn()
        try:
            row = conn.execute(
                """
                SELECT symbol, as_of_date, institutional_ownership_pct,
                       insider_ownership_pct, source, payload_json, fetched_at
                FROM us_ownership_daily
                WHERE symbol = ?
                ORDER BY as_of_date DESC
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()
            return _row_to_dict(row)
        finally:
            conn.close()

    def _load_borrow_history_cache(self, symbol: str, days: int = 7) -> list[Dict[str, Any]]:
        conn = get_stocks_conn()
        try:
            rows = conn.execute(
                """
                SELECT symbol, as_of_date, available_shares, borrow_fee_pct,
                       rebate_rate_pct, source, payload_json, fetched_at
                FROM us_short_borrow_daily
                WHERE symbol = ?
                ORDER BY as_of_date DESC
                LIMIT ?
                """,
                (symbol, max(1, int(days))),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def _upsert_short(self, data: Dict[str, Any]) -> None:
        conn = get_stocks_conn()
        try:
            conn.execute(
                """
                INSERT INTO us_short_interest_daily (
                    symbol, as_of_date, short_float_pct, short_interest_shares,
                    borrow_fee_pct, days_to_cover, source, payload_json, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, as_of_date) DO UPDATE SET
                    short_float_pct = excluded.short_float_pct,
                    short_interest_shares = excluded.short_interest_shares,
                    borrow_fee_pct = excluded.borrow_fee_pct,
                    days_to_cover = excluded.days_to_cover,
                    source = excluded.source,
                    payload_json = excluded.payload_json,
                    fetched_at = excluded.fetched_at
                """,
                (
                    data.get("symbol"),
                    data.get("as_of_date"),
                    data.get("short_float_pct"),
                    data.get("short_interest_shares"),
                    data.get("borrow_fee_pct"),
                    data.get("days_to_cover"),
                    data.get("source"),
                    data.get("payload_json"),
                    data.get("fetched_at"),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _upsert_ownership(self, data: Dict[str, Any]) -> None:
        conn = get_stocks_conn()
        try:
            conn.execute(
                """
                INSERT INTO us_ownership_daily (
                    symbol, as_of_date, institutional_ownership_pct,
                    insider_ownership_pct, source, payload_json, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, as_of_date) DO UPDATE SET
                    institutional_ownership_pct = excluded.institutional_ownership_pct,
                    insider_ownership_pct = excluded.insider_ownership_pct,
                    source = excluded.source,
                    payload_json = excluded.payload_json,
                    fetched_at = excluded.fetched_at
                """,
                (
                    data.get("symbol"),
                    data.get("as_of_date"),
                    data.get("institutional_ownership_pct"),
                    data.get("insider_ownership_pct"),
                    data.get("source"),
                    data.get("payload_json"),
                    data.get("fetched_at"),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _upsert_borrow_history(self, rows: list[Dict[str, Any]]) -> None:
        if not rows:
            return
        conn = get_stocks_conn()
        try:
            params = []
            for row in rows:
                params.append(
                    (
                        row.get("symbol"),
                        row.get("as_of_date"),
                        row.get("available_shares"),
                        row.get("borrow_fee_pct"),
                        row.get("rebate_rate_pct"),
                        row.get("source"),
                        row.get("payload_json"),
                        row.get("fetched_at"),
                    )
                )
            conn.executemany(
                """
                INSERT INTO us_short_borrow_daily (
                    symbol, as_of_date, available_shares, borrow_fee_pct,
                    rebate_rate_pct, source, payload_json, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, as_of_date) DO UPDATE SET
                    available_shares = excluded.available_shares,
                    borrow_fee_pct = excluded.borrow_fee_pct,
                    rebate_rate_pct = excluded.rebate_rate_pct,
                    source = excluded.source,
                    payload_json = excluded.payload_json,
                    fetched_at = excluded.fetched_at
                """,
                params,
            )
            conn.commit()
        finally:
            conn.close()

    def _fetch_borrow_history_from_iborrowdesk(self, symbol: str, days: int = 7) -> list[Dict[str, Any]]:
        ticker = str(symbol or "").strip().upper()
        if not ticker:
            return []

        url = f"https://www.iborrowdesk.com/api/ticker/{ticker}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": f"https://iborrowdesk.com/report/{ticker}",
            "Accept": "application/json",
        }
        try:
            response = requests.get(url, headers=headers, timeout=self.http_timeout)
            if response.status_code != 200:
                return []
            payload = response.json() or {}
        except Exception:
            return []

        daily = payload.get("daily")
        if not isinstance(daily, list) or not daily:
            return []

        rows: list[Dict[str, Any]] = []
        for item in daily:
            if not isinstance(item, dict):
                continue
            as_of_date = str(item.get("date") or "").strip()
            if not as_of_date:
                continue
            rows.append(
                {
                    "symbol": ticker,
                    "as_of_date": as_of_date,
                    "available_shares": _to_int(item.get("available")),
                    "borrow_fee_pct": _to_float(item.get("fee")),
                    "rebate_rate_pct": _to_float(item.get("rebate")),
                    "source": "iborrowdesk",
                    "payload_json": json.dumps(item, ensure_ascii=False),
                    "fetched_at": _utc_now_iso(),
                }
            )

        if not rows:
            return []

        rows.sort(key=lambda x: str(x.get("as_of_date") or ""))
        return rows[-max(1, int(days)) :]

    def _fetch_short_from_fmp(self, symbol: str) -> Optional[Dict[str, Any]]:
        if not self.fmp_api_key:
            return None

        candidates = [
            ("https://financialmodelingprep.com/api/v4/short_interest", {"symbol": symbol}),
            ("https://financialmodelingprep.com/api/v3/quote-short/" + symbol, {}),
            ("https://financialmodelingprep.com/api/v4/shares_float", {"symbol": symbol}),
        ]

        for url, params in candidates:
            payload = self._request_json(url, {**params, "apikey": self.fmp_api_key})
            if payload in (None, {}, []):
                continue
            short_float = _find_numeric(payload, ["shortFloat", "shortFloatPercent", "shortPercentOfFloat"], percent=True)
            borrow_fee = _find_numeric(payload, ["borrowFee", "borrowFeeRate", "shortBorrowRate"], percent=True)
            days_to_cover = _find_numeric(payload, ["daysToCover", "shortRatio"])
            short_shares = _find_numeric(payload, ["shortInterest", "shortShares", "shortInterestShares"])

            if all(v is None for v in (short_float, borrow_fee, days_to_cover, short_shares)):
                continue

            return {
                "symbol": symbol,
                "as_of_date": _today_iso(),
                "short_float_pct": short_float,
                "short_interest_shares": short_shares,
                "borrow_fee_pct": borrow_fee,
                "days_to_cover": days_to_cover,
                "source": "fmp",
                "payload_json": json.dumps(payload, ensure_ascii=False),
                "fetched_at": _utc_now_iso(),
            }
        return None

    def _fetch_short_from_finnhub(self, symbol: str) -> Optional[Dict[str, Any]]:
        if not self.finnhub_api_key:
            return None

        endpoints = [
            ("https://finnhub.io/api/v1/stock/short-interest", {"symbol": symbol, "token": self.finnhub_api_key}),
            ("https://finnhub.io/api/v1/stock/metric", {"symbol": symbol, "metric": "all", "token": self.finnhub_api_key}),
        ]

        for url, params in endpoints:
            payload = self._request_json(url, params)
            if payload in (None, {}, []):
                continue

            short_float = _find_numeric(payload, ["shortFloat", "shortPercentOfFloat", "shortInterestPercent"], percent=True)
            borrow_fee = _find_numeric(payload, ["borrowFee", "borrowFeeRate", "shortBorrowRate"], percent=True)
            days_to_cover = _find_numeric(payload, ["daysToCover", "shortRatio"])
            short_shares = _find_numeric(payload, ["shortInterest", "shortShares", "shortInterestShares"])

            if all(v is None for v in (short_float, borrow_fee, days_to_cover, short_shares)):
                continue

            return {
                "symbol": symbol,
                "as_of_date": _today_iso(),
                "short_float_pct": short_float,
                "short_interest_shares": short_shares,
                "borrow_fee_pct": borrow_fee,
                "days_to_cover": days_to_cover,
                "source": "finnhub",
                "payload_json": json.dumps(payload, ensure_ascii=False),
                "fetched_at": _utc_now_iso(),
            }
        return None

    def _fetch_short_from_scrape(self, symbol: str) -> Optional[Dict[str, Any]]:
        url = f"https://fintel.io/ss/us/{symbol}"
        try:
            response = requests.get(url, timeout=self.http_timeout)
            if response.status_code != 200:
                return None
            soup = BeautifulSoup(response.text, "lxml")
            text = soup.get_text(" ", strip=True)
        except Exception:
            return None

        def _find(pattern: str) -> Optional[float]:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                return None
            return _to_float(match.group(1))

        short_float = _normalize_percent(_find(r"Short\s+Interest\s*(?:%\s*Float|Float)\s*[:\-]?\s*([0-9]+(?:\.[0-9]+)?)%?"))
        borrow_fee = _normalize_percent(_find(r"Borrow\s+Fee\s*(?:Rate)?\s*[:\-]?\s*([0-9]+(?:\.[0-9]+)?)%?"))
        days_to_cover = _find(r"Days\s+to\s+Cover\s*[:\-]?\s*([0-9]+(?:\.[0-9]+)?)")

        if all(v is None for v in (short_float, borrow_fee, days_to_cover)):
            return None

        return {
            "symbol": symbol,
            "as_of_date": _today_iso(),
            "short_float_pct": short_float,
            "short_interest_shares": None,
            "borrow_fee_pct": borrow_fee,
            "days_to_cover": days_to_cover,
            "source": "scrape:fintel",
            "payload_json": json.dumps({"url": url}, ensure_ascii=False),
            "fetched_at": _utc_now_iso(),
        }

    def _fetch_short_from_yfinance(self, symbol: str) -> Optional[Dict[str, Any]]:
        data = self._get_yfinance_short_ownership(symbol)
        if not data:
            return None

        short_float = _to_float(data.get("short_float_pct"))
        short_shares = _to_float(data.get("short_interest_shares"))
        days_to_cover = _to_float(data.get("days_to_cover"))

        if all(v is None for v in (short_float, short_shares, days_to_cover)):
            return None

        return {
            "symbol": symbol,
            "as_of_date": _today_iso(),
            "short_float_pct": short_float,
            "short_interest_shares": short_shares,
            "borrow_fee_pct": None,
            "days_to_cover": days_to_cover,
            "source": "yfinance",
            "payload_json": json.dumps(data, ensure_ascii=False),
            "fetched_at": _utc_now_iso(),
        }

    def _fetch_short_from_yahoo_quote_summary(self, symbol: str) -> Optional[Dict[str, Any]]:
        payload = self._get_quote_summary_payload(symbol)
        if not payload:
            return None

        short_float_raw = (
            self._qs_pick_raw(payload, "defaultKeyStatistics", "shortPercentOfFloat")
            or self._qs_pick_raw(payload, "defaultKeyStatistics", "sharesPercentSharesOut")
        )
        short_float = _normalize_percent(short_float_raw)
        short_shares = self._qs_pick_raw(payload, "defaultKeyStatistics", "sharesShort")
        days_to_cover = (
            self._qs_pick_raw(payload, "defaultKeyStatistics", "shortRatio")
            or self._qs_pick_raw(payload, "financialData", "shortRatio")
        )

        if all(v is None for v in (short_float, short_shares, days_to_cover)):
            return None

        return {
            "symbol": str(symbol or "").strip().upper(),
            "as_of_date": _today_iso(),
            "short_float_pct": short_float,
            "short_interest_shares": short_shares,
            "borrow_fee_pct": None,
            "days_to_cover": days_to_cover,
            "source": "yahoo_quote_summary",
            "payload_json": json.dumps(payload, ensure_ascii=False),
            "fetched_at": _utc_now_iso(),
        }

    def _fetch_short_from_finviz(self, symbol: str) -> Optional[Dict[str, Any]]:
        text = self._get_finviz_text(symbol)
        if not text:
            return None

        short_float = self._find_pct_from_text(text, "Short Float")
        days_to_cover = self._find_num_from_text(text, "Short Ratio")
        short_shares = self._find_compact_num_from_text(text, "Shs Short")

        if all(v is None for v in (short_float, short_shares, days_to_cover)):
            return None

        return {
            "symbol": str(symbol or "").strip().upper(),
            "as_of_date": _today_iso(),
            "short_float_pct": short_float,
            "short_interest_shares": short_shares,
            "borrow_fee_pct": None,
            "days_to_cover": days_to_cover,
            "source": "finviz",
            "payload_json": json.dumps({"snippet": text[:1000]}, ensure_ascii=False),
            "fetched_at": _utc_now_iso(),
        }

    def _fetch_ownership_from_fmp(self, symbol: str) -> Optional[Dict[str, Any]]:
        if not self.fmp_api_key:
            return None

        endpoints = [
            (
                "https://financialmodelingprep.com/api/v4/institutional-ownership/symbol-ownership",
                {"symbol": symbol, "apikey": self.fmp_api_key},
            ),
            (
                f"https://financialmodelingprep.com/api/v3/institutional-holder/{symbol}",
                {"apikey": self.fmp_api_key},
            ),
            (
                f"https://financialmodelingprep.com/api/v3/profile/{symbol}",
                {"apikey": self.fmp_api_key},
            ),
        ]

        for url, params in endpoints:
            payload = self._request_json(url, params)
            if payload in (None, {}, []):
                continue

            inst_pct = _find_numeric(
                payload,
                [
                    "institutionalOwnershipPercentage",
                    "institutionalOwnership",
                    "institutionOwnership",
                    "institutionalPercent",
                ],
                percent=True,
            )
            insider_pct = _find_numeric(payload, ["insiderOwnership", "insiderOwnershipPercentage"], percent=True)

            if inst_pct is None and isinstance(payload, list) and payload:
                total_shares = 0.0
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    shares = _to_float(item.get("sharesNumber") or item.get("shares") or item.get("share"))
                    if shares:
                        total_shares += shares
                outstanding = _find_numeric(payload, ["sharesOutstanding", "outstandingShares", "shareOutstanding"])
                if outstanding and outstanding > 0:
                    inst_pct = _normalize_percent(total_shares / outstanding)

            if inst_pct is None and insider_pct is None:
                continue

            return {
                "symbol": symbol,
                "as_of_date": _today_iso(),
                "institutional_ownership_pct": inst_pct,
                "insider_ownership_pct": insider_pct,
                "source": "fmp",
                "payload_json": json.dumps(payload, ensure_ascii=False),
                "fetched_at": _utc_now_iso(),
            }
        return None

    def _fetch_ownership_from_finnhub(self, symbol: str) -> Optional[Dict[str, Any]]:
        if not self.finnhub_api_key:
            return None

        endpoints = [
            ("https://finnhub.io/api/v1/stock/ownership", {"symbol": symbol, "limit": 50, "token": self.finnhub_api_key}),
            ("https://finnhub.io/api/v1/stock/metric", {"symbol": symbol, "metric": "all", "token": self.finnhub_api_key}),
        ]

        for url, params in endpoints:
            payload = self._request_json(url, params)
            if payload in (None, {}, []):
                continue

            inst_pct = _find_numeric(
                payload,
                [
                    "institutionalOwnership",
                    "institutionalOwnershipPercentage",
                    "institutionOwnership",
                ],
                percent=True,
            )
            insider_pct = _find_numeric(payload, ["insiderOwnership", "insiderOwnershipPercentage"], percent=True)

            if inst_pct is None and insider_pct is None:
                continue

            return {
                "symbol": symbol,
                "as_of_date": _today_iso(),
                "institutional_ownership_pct": inst_pct,
                "insider_ownership_pct": insider_pct,
                "source": "finnhub",
                "payload_json": json.dumps(payload, ensure_ascii=False),
                "fetched_at": _utc_now_iso(),
            }
        return None

    def _fetch_ownership_from_yfinance(self, symbol: str) -> Optional[Dict[str, Any]]:
        data = self._get_yfinance_short_ownership(symbol)
        if not data:
            return None

        inst_pct = _to_float(data.get("institutional_ownership_pct"))
        insider_pct = _to_float(data.get("insider_ownership_pct"))
        if inst_pct is None and insider_pct is None:
            return None

        return {
            "symbol": symbol,
            "as_of_date": _today_iso(),
            "institutional_ownership_pct": inst_pct,
            "insider_ownership_pct": insider_pct,
            "source": "yfinance",
            "payload_json": json.dumps(data, ensure_ascii=False),
            "fetched_at": _utc_now_iso(),
        }

    def _fetch_ownership_from_yahoo_quote_summary(self, symbol: str) -> Optional[Dict[str, Any]]:
        payload = self._get_quote_summary_payload(symbol)
        if not payload:
            return None

        inst_raw = self._qs_pick_raw(payload, "defaultKeyStatistics", "heldPercentInstitutions")
        insider_raw = self._qs_pick_raw(payload, "defaultKeyStatistics", "heldPercentInsiders")
        inst_pct = _normalize_percent(inst_raw)
        insider_pct = _normalize_percent(insider_raw)

        if inst_pct is None and insider_pct is None:
            return None

        return {
            "symbol": str(symbol or "").strip().upper(),
            "as_of_date": _today_iso(),
            "institutional_ownership_pct": inst_pct,
            "insider_ownership_pct": insider_pct,
            "source": "yahoo_quote_summary",
            "payload_json": json.dumps(payload, ensure_ascii=False),
            "fetched_at": _utc_now_iso(),
        }

    def _fetch_ownership_from_finviz(self, symbol: str) -> Optional[Dict[str, Any]]:
        text = self._get_finviz_text(symbol)
        if not text:
            return None

        inst_pct = self._find_pct_from_text(text, "Inst Own")
        insider_pct = self._find_pct_from_text(text, "Insider Own")
        if inst_pct is None and insider_pct is None:
            return None

        return {
            "symbol": str(symbol or "").strip().upper(),
            "as_of_date": _today_iso(),
            "institutional_ownership_pct": inst_pct,
            "insider_ownership_pct": insider_pct,
            "source": "finviz",
            "payload_json": json.dumps({"snippet": text[:1000]}, ensure_ascii=False),
            "fetched_at": _utc_now_iso(),
        }

    def get_short_metrics(self, symbol: str, force_refresh: bool = False) -> Dict[str, Any]:
        self._ensure_schema()
        ticker = str(symbol or "").strip().upper()
        if not ticker:
            return {
                "symbol": "",
                "short_float_pct": None,
                "borrow_fee_pct": None,
                "short_interest_shares": None,
                "days_to_cover": None,
                "source": "invalid_symbol",
                "as_of_date": None,
                "is_stale": True,
            }

        cached = self._load_short_cache(ticker)
        today = _today_iso()
        if cached and not force_refresh and str(cached.get("as_of_date")) == today:
            cached["is_stale"] = False
            cached["source"] = f"cache:{cached.get('source') or 'unknown'}"
            return cached

        live = (
            self._fetch_short_from_fmp(ticker)
            or self._fetch_short_from_finnhub(ticker)
            or self._fetch_short_from_scrape(ticker)
            or self._fetch_short_from_yahoo_quote_summary(ticker)
            or self._fetch_short_from_finviz(ticker)
            or self._fetch_short_from_yfinance(ticker)
        )

        if live:
            self._upsert_short(live)
            live["is_stale"] = False
            return live

        if cached:
            cached["is_stale"] = True
            cached["source"] = f"stale:{cached.get('source') or 'unknown'}"
            return cached

        return {
            "symbol": ticker,
            "short_float_pct": None,
            "borrow_fee_pct": None,
            "short_interest_shares": None,
            "days_to_cover": None,
            "source": "unavailable",
            "as_of_date": None,
            "is_stale": True,
        }

    def get_ownership_metrics(self, symbol: str, force_refresh: bool = False) -> Dict[str, Any]:
        self._ensure_schema()
        ticker = str(symbol or "").strip().upper()
        if not ticker:
            return {
                "symbol": "",
                "institutional_ownership_pct": None,
                "insider_ownership_pct": None,
                "source": "invalid_symbol",
                "as_of_date": None,
                "is_stale": True,
            }

        cached = self._load_ownership_cache(ticker)
        today = _today_iso()
        if cached and not force_refresh and str(cached.get("as_of_date")) == today:
            cached["is_stale"] = False
            cached["source"] = f"cache:{cached.get('source') or 'unknown'}"
            return cached

        live = (
            self._fetch_ownership_from_fmp(ticker)
            or self._fetch_ownership_from_finnhub(ticker)
            or self._fetch_ownership_from_yahoo_quote_summary(ticker)
            or self._fetch_ownership_from_finviz(ticker)
            or self._fetch_ownership_from_yfinance(ticker)
        )

        if live:
            self._upsert_ownership(live)
            live["is_stale"] = False
            return live

        if cached:
            cached["is_stale"] = True
            cached["source"] = f"stale:{cached.get('source') or 'unknown'}"
            return cached

        return {
            "symbol": ticker,
            "institutional_ownership_pct": None,
            "insider_ownership_pct": None,
            "source": "unavailable",
            "as_of_date": None,
            "is_stale": True,
        }

    def check_squeeze_warning(self, symbol: str) -> bool:
        metrics = self.get_short_metrics(symbol)
        short_float = _to_float(metrics.get("short_float_pct")) or 0.0
        borrow_fee = _to_float(metrics.get("borrow_fee_pct")) or 0.0
        return short_float >= 15.0 and borrow_fee >= 10.0

    def get_borrow_history(self, symbol: str, days: int = 7, force_refresh: bool = False) -> Dict[str, Any]:
        self._ensure_schema()
        ticker = str(symbol or "").strip().upper()
        keep_days = max(1, min(int(days or 7), 90))
        if not ticker:
            return {
                "symbol": "",
                "days": keep_days,
                "source": "invalid_symbol",
                "latest": None,
                "data": [],
            }

        cached = self._load_borrow_history_cache(ticker, days=keep_days)
        if cached and not force_refresh:
            latest = cached[0] if cached else None
            return {
                "symbol": ticker,
                "days": keep_days,
                "source": f"cache:{latest.get('source') if latest else 'unknown'}",
                "latest": latest,
                "data": list(reversed(cached)),
            }

        live_rows = self._fetch_borrow_history_from_iborrowdesk(ticker, days=keep_days)
        if live_rows:
            self._upsert_borrow_history(live_rows)
            cached_after = self._load_borrow_history_cache(ticker, days=keep_days)
            latest = cached_after[0] if cached_after else (live_rows[-1] if live_rows else None)
            return {
                "symbol": ticker,
                "days": keep_days,
                "source": "iborrowdesk",
                "latest": latest,
                "data": list(reversed(cached_after)) if cached_after else live_rows,
            }

        if cached:
            latest = cached[0]
            return {
                "symbol": ticker,
                "days": keep_days,
                "source": f"stale:{latest.get('source') or 'unknown'}",
                "latest": latest,
                "data": list(reversed(cached)),
            }

        return {
            "symbol": ticker,
            "days": keep_days,
            "source": "unavailable",
            "latest": None,
            "data": [],
        }

    def get_snapshot(self, symbol: str, force_refresh: bool = False) -> Dict[str, Any]:
        short_metrics = self.get_short_metrics(symbol, force_refresh=force_refresh)
        ownership = self.get_ownership_metrics(symbol, force_refresh=force_refresh)
        return {
            "symbol": str(symbol or "").strip().upper(),
            "short": short_metrics,
            "ownership": ownership,
            "is_squeeze_warning": self.check_squeeze_warning(symbol),
        }
