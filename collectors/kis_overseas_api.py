from __future__ import annotations

from typing import Any, Dict, Iterable, List

import requests

from config.settings import KIS_APP_KEY, KIS_APP_SECRET, KIS_DOMAIN
from utils.helpers import get_kis_token
from utils.market_utils import get_us_market_status


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value).strip()
        if text in ("", "-", "None", "nan", "NaN"):
            return default
        return float(text.replace(",", ""))
    except Exception:
        return default


def _pick_float(row: Dict[str, Any], keys: Iterable[str], default: float = 0.0) -> float:
    for key in keys:
        if key in row and str(row.get(key, "")).strip() not in ("", "-", "None"):
            return _safe_float(row.get(key), default)
    return default


def _pick_str(row: Dict[str, Any], keys: Iterable[str], default: str = "") -> str:
    for key in keys:
        value = str(row.get(key, "")).strip()
        if value:
            return value
    return default


class KISOverseasCollector:
    VALID_EXCD = {"NAS", "NYS", "AMS"}

    def __init__(self) -> None:
        self.base_url = KIS_DOMAIN.rstrip("/")
        self.token = get_kis_token()

    def _refresh_token(self) -> None:
        self.token = get_kis_token()

    def _build_headers(self, tr_id: str) -> Dict[str, str]:
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.token}",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def _get(self, path: str, params: Dict[str, Any], tr_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = self._build_headers(tr_id)
        try:
            response = requests.get(url, headers=headers, params=params, timeout=10)
            if response.status_code != 200:
                return {"rt_cd": "9", "msg1": f"HTTP {response.status_code}"}
            payload = response.json()
            if str(payload.get("rt_cd", "")) == "0":
                return payload

            self._refresh_token()
            retry_headers = self._build_headers(tr_id)
            retry_response = requests.get(url, headers=retry_headers, params=params, timeout=10)
            if retry_response.status_code != 200:
                return {"rt_cd": "9", "msg1": f"HTTP {retry_response.status_code}"}
            return retry_response.json()
        except Exception as exc:
            return {"rt_cd": "9", "msg1": str(exc)}

    def _get_with_fallback(self, paths: Iterable[str], params: Dict[str, Any], tr_id: str) -> Dict[str, Any]:
        last_response: Dict[str, Any] = {"rt_cd": "9", "msg1": "No response"}
        for path in paths:
            response = self._get(path, params, tr_id)
            last_response = response
            if str(response.get("rt_cd", "")) == "0":
                return response
            msg = str(response.get("msg1", ""))
            if "HTTP 404" not in msg:
                return response
        return last_response

    def _normalize_excd(self, excd: str) -> str:
        code = str(excd or "").strip().upper()
        alias = {
            "NASDAQ": "NAS",
            "NASDAQM": "NAS",
            "NYSE": "NYS",
            "AMEX": "AMS",
        }
        code = alias.get(code, code)
        if code not in self.VALID_EXCD:
            raise ValueError(f"Unsupported EXCD '{excd}'. Use one of NAS/NYS/AMS.")
        return code

    def get_price(self, excd: str, symbol: str) -> Dict[str, Any]:
        return self.get_quote(excd, symbol)

    def get_quote(self, excd: str, symbol: str) -> Dict[str, Any]:
        exchange = self._normalize_excd(excd)
        ticker = str(symbol or "").strip().upper()
        if not ticker:
            raise ValueError("symbol is required")

        paths = (
            "/uapi/overseas-stock/v1/quotations/inquire-price",
            "/uapi/overseas-stock/v1/quotations/price",
            "/uapi/overseas-price/v1/quotations/inquire-price",
            "/uapi/overseas-price/v1/quotations/price",
        )
        params = {
            "AUTH": "",
            "EXCD": exchange,
            "SYMB": ticker,
        }
        res = self._get_with_fallback(paths, params, "HHDFS00000300")
        if str(res.get("rt_cd", "")) != "0":
            raise RuntimeError(res.get("msg1") or "KIS overseas quote request failed")

        output = res.get("output") or {}
        return {
            "symbol": ticker,
            "exchange": exchange,
            "market_status": get_us_market_status(),
            "timestamp": _pick_str(output, ("t_time", "xhms", "trdt", "tdtm"), ""),
            "current_price": _pick_float(output, ("last", "ovrs_nmix_prpr", "stck_prpr")),
            "change_amt": _pick_float(output, ("diff", "prdy_vrss")),
            "change_pct": _pick_float(output, ("rate", "prdy_ctrt")),
            "prev_close": _pick_float(output, ("base", "prev", "prdy_clpr")),
            "open_price": _pick_float(output, ("open", "oprc")),
            "high": _pick_float(output, ("high", "hgpr")),
            "low": _pick_float(output, ("low", "lwpr")),
            "ask_price": _pick_float(output, ("askp", "ask1")),
            "bid_price": _pick_float(output, ("bidp", "bid1")),
            "trading_volume": _pick_float(output, ("tvol", "acml_vol", "vol")),
            "trading_value": _pick_float(output, ("tamt", "acml_tr_pbmn", "amt")),
            "raw": output,
        }

    def get_minute_chart(
        self,
        excd: str,
        symbol: str,
        nmin: int = 1,
        nrec: int = 240,
        next_key: str = "",
        fill: str = "",
    ) -> List[Dict[str, Any]]:
        exchange = self._normalize_excd(excd)
        ticker = str(symbol or "").strip().upper()
        if not ticker:
            raise ValueError("symbol is required")

        minute = max(1, min(int(nmin or 1), 60))
        records = max(1, min(int(nrec or 240), 500))

        paths = (
            "/uapi/overseas-stock/v1/quotations/inquire-time-itemchartprice",
            "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice",
        )
        params = {
            "AUTH": "",
            "EXCD": exchange,
            "SYMB": ticker,
            "NMIN": str(minute),
            "PINC": "1",
            "NEXT": str(next_key or ""),
            "KEYB": str(next_key or ""),
            "NREC": str(records),
            "FILL": str(fill or ""),
        }
        res = self._get_with_fallback(paths, params, "HHDFS76950200")
        if str(res.get("rt_cd", "")) != "0":
            raise RuntimeError(res.get("msg1") or "KIS overseas minute chart request failed")

        rows = res.get("output2") or []
        if isinstance(rows, dict):
            rows = [rows]

        candles: List[Dict[str, Any]] = []
        for row in rows:
            date_value = _pick_str(row, ("xymd", "date", "trdt"), "")
            time_value = _pick_str(row, ("xhms", "time", "tm"), "")
            if not date_value and not time_value:
                continue

            candles.append(
                {
                    "date": date_value,
                    "time": time_value,
                    "open": _pick_float(row, ("open", "oprc")),
                    "high": _pick_float(row, ("high", "hgpr")),
                    "low": _pick_float(row, ("low", "lwpr")),
                    "close": _pick_float(row, ("last", "close", "prpr")),
                    "volume": _pick_float(row, ("evol", "vol", "tvol")),
                    "raw": row,
                }
            )

        candles.sort(key=lambda x: (x.get("date") or "", x.get("time") or ""))
        return candles
