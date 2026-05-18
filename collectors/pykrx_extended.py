import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta

import pandas as pd
from pykrx import stock


@contextmanager
def _without_proxy():
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    old = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# pykrx 한글 컬럼명 → 영문 매핑
_KR_COL_MAP = {
    "날짜": "date", "일자": "date",
    "시가": "open", "고가": "high", "저가": "low", "종가": "close",
    "거래량": "volume", "거래대금": "trading_value",
    "시가총액": "market_cap", "상장주식수": "listed_shares",
    "BPS": "bps", "PER": "per", "EPS": "eps", "PBR": "pbr", "DIV": "div", "DPS": "dps",
    "공매도잔고": "short_balance", "공매도잔고금액": "short_balance_amt",
    "공매도거래량": "short_volume", "공매도거래대금": "short_value",
    "매수": "buy", "매도": "sell", "순매수": "net_buy",
    "기관합계": "institution", "외국인": "foreign", "개인": "individual",
    "소진율": "exhaustion_rate", "보유수량": "hold_qty", "상장수량": "listed_qty",
}

def _rename_cols(df: pd.DataFrame) -> pd.DataFrame:
    """한글 컬럼명을 영문으로 치환. 매핑 없는 컬럼은 그대로 유지."""
    return df.rename(columns=lambda c: _KR_COL_MAP.get(str(c).strip(), str(c)))


def _df_to_records(df: pd.DataFrame, max_rows: int = 40):
    if df is None or df.empty:
        return []
    tmp = df.reset_index()
    tmp = _rename_cols(tmp)
    tmp = tmp.where(pd.notnull(tmp), None)
    if len(tmp) > max_rows:
        tmp = tmp.tail(max_rows)
    return tmp.to_dict(orient="records")


class PykrxExtendedCollector:
    def __init__(self):
        self._cached_business_date = None

    def _safe_call(self, fn, *args, **kwargs):
        try:
            with _without_proxy():
                result = fn(*args, **kwargs)
            return result, None
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    def find_recent_business_date(self):
        if self._cached_business_date:
            return self._cached_business_date
        today = datetime.now()
        for i in range(0, 15):
            d = (today - timedelta(days=i)).strftime("%Y%m%d")
            df, err = self._safe_call(stock.get_market_ohlcv_by_date, d, d, "005930")
            if err:
                continue
            if df is not None and not df.empty:
                self._cached_business_date = d
                return d
        self._cached_business_date = today.strftime("%Y%m%d")
        return self._cached_business_date

    def collect_for_ticker(self, code: str, market: str = "KOSPI", lookback_days: int = 20):
        asof = self.find_recent_business_date()
        start = (datetime.strptime(asof, "%Y%m%d") - timedelta(days=max(lookback_days, 5) * 3)).strftime("%Y%m%d")

        errors = {}
        status = "ok"

        fundamental_df, err = self._safe_call(stock.get_market_fundamental_by_date, start, asof, code)
        if err:
            errors["fundamental_by_date"] = err
            status = "partial"

        ohlcv_df, err = self._safe_call(stock.get_market_ohlcv_by_date, start, asof, code)
        if err:
            errors["ohlcv_by_date"] = err
            status = "partial"

        market_cap_df, err = self._safe_call(stock.get_market_cap_by_date, start, asof, code)
        if err:
            errors["market_cap_by_date"] = err
            status = "partial"

        short_balance_df, err = self._safe_call(stock.get_shorting_balance_by_date, start, asof, code)
        if err:
            errors["shorting_balance_by_date"] = err
            status = "partial"

        short_volume_df, err = self._safe_call(stock.get_shorting_volume_by_date, start, asof, code)
        if err:
            errors["shorting_volume_by_date"] = err
            status = "partial"

        short_value_df, err = self._safe_call(stock.get_shorting_value_by_date, start, asof, code)
        if err:
            errors["shorting_value_by_date"] = err
            status = "partial"

        trading_value_df, err = self._safe_call(stock.get_market_trading_value_by_date, start, asof, code)
        if err:
            errors["market_trading_value_by_date"] = err
            status = "partial"

        trading_volume_df, err = self._safe_call(stock.get_market_trading_volume_by_date, start, asof, code)
        if err:
            errors["market_trading_volume_by_date"] = err
            status = "partial"

        foreign_exhaustion_df, err = self._safe_call(stock.get_exhaustion_rates_of_foreign_investment_by_date, start, asof, code)
        if err:
            errors["foreign_exhaustion_by_date"] = err
            status = "partial"

        # NOTE: by_investor APIs need ticker, not market label.
        market_investor_value_df, err = self._safe_call(stock.get_market_trading_value_by_investor, start, asof, code)
        if err:
            errors["market_trading_value_by_investor"] = err
            status = "partial"
        else:
            if market_investor_value_df is not None and not market_investor_value_df.empty:
                market_investor_value_df = market_investor_value_df.head(20)

        market_investor_volume_df, err = self._safe_call(stock.get_market_trading_volume_by_investor, start, asof, code)
        if err:
            errors["market_trading_volume_by_investor"] = err
            status = "partial"
        else:
            if market_investor_volume_df is not None and not market_investor_volume_df.empty:
                market_investor_volume_df = market_investor_volume_df.head(20)

        investor_net_by_ticker = {}
        for investor in ("개인", "기관합계", "외국인"):
            df, err = self._safe_call(
                stock.get_market_net_purchases_of_equities_by_ticker,
                start,
                asof,
                market,
                investor,
            )
            key = f"net_purchases_by_ticker_{investor}"
            if err:
                errors[key] = err
                status = "partial"
                continue
            if df is None or df.empty:
                investor_net_by_ticker[investor] = []
                continue
            row = df[df.index.astype(str) == str(code)]
            investor_net_by_ticker[investor] = _df_to_records(row, max_rows=1)

        payload = {
            "code": code,
            "market": market,
            "asof": asof,
            "start": start,
            "status": status,
            "errors": errors,
            "fundamental": _df_to_records(fundamental_df),
            "ohlcv": _df_to_records(ohlcv_df),
            "market_cap": _df_to_records(market_cap_df),
            "short_balance": _df_to_records(short_balance_df),
            "short_volume": _df_to_records(short_volume_df),
            "short_value": _df_to_records(short_value_df),
            "trading_value": _df_to_records(trading_value_df),
            "trading_volume": _df_to_records(trading_volume_df),
            "foreign_exhaustion": _df_to_records(foreign_exhaustion_df),
            "market_investor_value_top20": _df_to_records(market_investor_value_df, max_rows=20),
            "market_investor_volume_top20": _df_to_records(market_investor_volume_df, max_rows=20),
            "investor_net_by_ticker": investor_net_by_ticker,
        }

        has_any_data = any(
            [
                payload["fundamental"],
                payload["ohlcv"],
                payload["market_cap"],
                payload["short_balance"],
                payload["short_volume"],
                payload["short_value"],
                payload["trading_value"],
                payload["trading_volume"],
                payload["foreign_exhaustion"],
                payload["market_investor_value_top20"],
                payload["market_investor_volume_top20"],
                any(payload["investor_net_by_ticker"].values()),
            ]
        )

        if errors and has_any_data:
            payload["status"] = "partial"
        elif errors and not has_any_data:
            payload["status"] = "error"
        else:
            payload["status"] = "ok"

        return payload
