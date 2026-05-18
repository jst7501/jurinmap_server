from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        text = str(value).strip()
        if text in ("", "-", "None", "nan", "NaN"):
            return None
        return float(text.replace(",", ""))
    except Exception:
        return None


def _normalize_pct(value: Any) -> Optional[float]:
    v = _to_float(value)
    if v is None:
        return None
    if 0 < v <= 1:
        return round(v * 100.0, 4)
    return round(v, 4)


class YFinanceCollector:
    def __init__(self) -> None:
        try:
            import yfinance as yf
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("yfinance is not installed. Run pip install yfinance") from exc
        self.yf = yf

    def _ticker(self, symbol: str):
        ticker = str(symbol or "").strip().upper()
        if not ticker:
            raise ValueError("symbol is required")
        return self.yf.Ticker(ticker)

    def get_quick_quote(self, symbol: str) -> Dict[str, Any]:
        """KIS get_quote() 호환 경량 quote — fast_info 만 사용. 50-200ms.

        KIS 사용 불가 (약관) 대체용. 공개 페이지에서 quote 표시 시 이걸 호출.
        """
        ticker = self._ticker(symbol)
        sym_u = str(symbol).strip().upper()
        fi: Dict[str, Any] = {}
        try:
            raw = ticker.fast_info
            fi = dict(raw) if raw else {}
        except Exception:
            fi = {}

        last = _to_float(fi.get("lastPrice") or fi.get("last_price"))
        prev = _to_float(fi.get("previousClose") or fi.get("previous_close"))
        change_amt = None
        change_pct = None
        if last is not None and prev is not None and prev > 0:
            change_amt = round(last - prev, 6)
            change_pct = round((last - prev) / prev * 100, 4)

        return {
            "symbol": sym_u,
            "exchange": fi.get("exchange"),
            "market_status": fi.get("marketState"),
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "current_price": last,
            "prev_close": prev,
            "change_amt": change_amt,
            "change_pct": change_pct,
            "open_price": _to_float(fi.get("open") or fi.get("regularMarketOpen")),
            "high": _to_float(fi.get("dayHigh") or fi.get("day_high")),
            "low": _to_float(fi.get("dayLow") or fi.get("day_low")),
            "ask_price": _to_float(fi.get("ask")),
            "bid_price": _to_float(fi.get("bid")),
            "trading_volume": _to_float(fi.get("lastVolume") or fi.get("last_volume")),
            "trading_value": None,  # yfinance fast_info 미제공
            "pre_market_price": _to_float(fi.get("preMarketPrice")),
            "post_market_price": _to_float(fi.get("postMarketPrice")),
            "raw": fi,
            "source": "yfinance",
        }

    def get_minute_history(self, symbol: str, nmin: int = 1, nrec: int = 240) -> List[Dict[str, Any]]:
        """KIS get_minute_chart() 호환 경량 분봉 — yfinance Ticker.history."""
        ticker = self._ticker(symbol)
        period_map = {1: "2d", 5: "5d", 10: "5d", 15: "5d", 30: "1mo", 60: "3mo"}
        interval_map = {1: "1m", 5: "5m", 10: "15m", 15: "15m", 30: "30m", 60: "60m"}
        nmin = max(1, int(nmin or 1))
        # nearest valid interval
        valid_intervals = sorted(interval_map.keys())
        chosen_nmin = min(valid_intervals, key=lambda x: abs(x - nmin))
        try:
            hist = ticker.history(
                period=period_map[chosen_nmin],
                interval=interval_map[chosen_nmin],
                prepost=True,
                auto_adjust=False,
            )
        except Exception:
            return []
        if hist is None or len(hist) == 0:
            return []
        out: List[Dict[str, Any]] = []
        for idx, row in hist.iterrows():
            try:
                d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                t = idx.strftime("%H%M%S") if hasattr(idx, "strftime") else ""
                out.append({
                    "date": d,
                    "time": t,
                    "open": _to_float(row.get("Open")),
                    "high": _to_float(row.get("High")),
                    "low": _to_float(row.get("Low")),
                    "close": _to_float(row.get("Close")),
                    "volume": _to_float(row.get("Volume")),
                })
            except Exception:
                continue
        return out[-int(nrec or 240):]

    def _safe_history(
        self,
        ticker,
        period: str,
        interval: str,
        prepost: bool,
        limit: int,
    ) -> List[Dict[str, Any]]:
        try:
            df = ticker.history(period=period, interval=interval, prepost=prepost, auto_adjust=False)
        except Exception:
            return []

        if df is None or len(df) == 0:
            return []

        rows: List[Dict[str, Any]] = []
        for idx, row in df.tail(max(1, limit)).iterrows():
            try:
                ts = idx.isoformat() if hasattr(idx, "isoformat") else str(idx)
            except Exception:
                ts = str(idx)
            rows.append(
                {
                    "ts": ts,
                    "open": _to_float(row.get("Open")),
                    "high": _to_float(row.get("High")),
                    "low": _to_float(row.get("Low")),
                    "close": _to_float(row.get("Close")),
                    "volume": _to_float(row.get("Volume")),
                }
            )
        return rows

    def _safe_news(self, ticker, limit: int = 10) -> List[Dict[str, Any]]:
        try:
            news_items = ticker.news or []
        except Exception:
            return []

        result: List[Dict[str, Any]] = []
        for item in news_items[: max(1, limit)]:
            result.append(
                {
                    "title": item.get("title"),
                    "publisher": item.get("publisher"),
                    "link": item.get("link") or item.get("url"),
                    "published": item.get("providerPublishTime") or item.get("pubDate"),
                    "type": item.get("type"),
                }
            )
        return result

    def _safe_dataframe_rows(self, df, limit: int = 10) -> List[Dict[str, Any]]:
        if df is None:
            return []
        try:
            if len(df) == 0:
                return []
            tmp = df.head(max(1, limit)).copy()
            tmp = tmp.reset_index()
            rows = []
            for _, row in tmp.iterrows():
                item = {}
                for col, value in row.items():
                    if hasattr(value, "isoformat"):
                        item[str(col)] = value.isoformat()
                    elif isinstance(value, (int, float, bool)):
                        item[str(col)] = value
                    else:
                        item[str(col)] = str(value)
                rows.append(item)
            return rows
        except Exception:
            return []

    def _safe_ticker_df_rows(self, ticker, attr_name: str, limit: int) -> List[Dict[str, Any]]:
        try:
            df = getattr(ticker, attr_name, None)
        except Exception:
            return []
        return self._safe_dataframe_rows(df, limit=limit)

    def get_snapshot(self, symbol: str, history_period: str = "5d", history_interval: str = "1m") -> Dict[str, Any]:
        ticker = self._ticker(symbol)
        symbol_u = str(symbol).strip().upper()

        info: Dict[str, Any] = {}
        fast_info: Dict[str, Any] = {}

        try:
            info = ticker.info or {}
        except Exception:
            info = {}

        try:
            # fast_info can be dict-like object
            fi = ticker.fast_info
            fast_info = dict(fi) if fi else {}
        except Exception:
            fast_info = {}

        quote = {
            "symbol": symbol_u,
            "currency": info.get("currency") or fast_info.get("currency"),
            "exchange": info.get("exchange") or fast_info.get("exchange"),
            "market_state": info.get("marketState") or fast_info.get("marketState"),
            "price": _to_float(info.get("currentPrice") or fast_info.get("lastPrice")),
            "prev_close": _to_float(info.get("previousClose") or fast_info.get("previousClose")),
            "open": _to_float(info.get("open") or fast_info.get("open")),
            "day_high": _to_float(info.get("dayHigh") or fast_info.get("dayHigh")),
            "day_low": _to_float(info.get("dayLow") or fast_info.get("dayLow")),
            "volume": _to_float(info.get("volume") or fast_info.get("lastVolume")),
            "avg_volume": _to_float(info.get("averageVolume") or fast_info.get("tenDayAverageVolume")),
            "market_cap": _to_float(info.get("marketCap") or fast_info.get("marketCap")),
            "fifty_two_week_high": _to_float(info.get("fiftyTwoWeekHigh") or fast_info.get("yearHigh")),
            "fifty_two_week_low": _to_float(info.get("fiftyTwoWeekLow") or fast_info.get("yearLow")),
            "pre_market_price": _to_float(info.get("preMarketPrice")),
            "post_market_price": _to_float(info.get("postMarketPrice")),
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }

        profile = {
            "long_name": info.get("longName"),
            "short_name": info.get("shortName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "country": info.get("country"),
            "website": info.get("website"),
            "employees": info.get("fullTimeEmployees"),
            "summary": info.get("longBusinessSummary"),
        }

        valuation = {
            "trailing_pe": _to_float(info.get("trailingPE")),
            "forward_pe": _to_float(info.get("forwardPE")),
            "peg_ratio": _to_float(info.get("pegRatio")),
            "price_to_book": _to_float(info.get("priceToBook")),
            "beta": _to_float(info.get("beta")),
            "dividend_yield_pct": _normalize_pct(info.get("dividendYield")),
            "profit_margin_pct": _normalize_pct(info.get("profitMargins")),
            "operating_margin_pct": _normalize_pct(info.get("operatingMargins")),
            "roe_pct": _normalize_pct(info.get("returnOnEquity")),
            "roa_pct": _normalize_pct(info.get("returnOnAssets")),
            "revenue_growth_pct": _normalize_pct(info.get("revenueGrowth")),
            "earnings_growth_pct": _normalize_pct(info.get("earningsGrowth")),
        }

        share_stats = {
            "shares_outstanding": _to_float(info.get("sharesOutstanding") or fast_info.get("shares")),
            "float_shares": _to_float(info.get("floatShares")),
            "shares_short": _to_float(info.get("sharesShort")),
            "short_ratio": _to_float(info.get("shortRatio")),
            "short_percent_float_pct": _normalize_pct(info.get("shortPercentOfFloat")),
            "short_percent_outstanding_pct": _normalize_pct(info.get("sharesPercentSharesOut")),
            "institutional_ownership_pct": _normalize_pct(info.get("heldPercentInstitutions")),
            "insider_ownership_pct": _normalize_pct(info.get("heldPercentInsiders")),
        }

        analyst = {
            "target_mean_price": _to_float(info.get("targetMeanPrice")),
            "target_high_price": _to_float(info.get("targetHighPrice")),
            "target_low_price": _to_float(info.get("targetLowPrice")),
            "recommendation_key": info.get("recommendationKey"),
            "recommendation_mean": _to_float(info.get("recommendationMean")),
            "number_of_analysts": _to_float(info.get("numberOfAnalystOpinions")),
        }

        holders = {
            "major_holders": self._safe_ticker_df_rows(ticker, "major_holders", limit=10),
            "institutional_holders": self._safe_ticker_df_rows(ticker, "institutional_holders", limit=20),
            "insider_transactions": self._safe_ticker_df_rows(ticker, "insider_transactions", limit=20),
        }

        financials = {
            "income_stmt": self._safe_ticker_df_rows(ticker, "financials", limit=12),
            "balance_sheet": self._safe_ticker_df_rows(ticker, "balance_sheet", limit=12),
            "cashflow": self._safe_ticker_df_rows(ticker, "cashflow", limit=12),
        }

        calendar_rows = self._safe_ticker_df_rows(ticker, "calendar", limit=10)
        recommendations_rows = self._safe_ticker_df_rows(ticker, "recommendations", limit=20)

        history = self._safe_history(
            ticker=ticker,
            period=history_period,
            interval=history_interval,
            prepost=True,
            limit=390,
        )

        return {
            "symbol": symbol_u,
            "source": "yfinance",
            "quote": quote,
            "profile": profile,
            "valuation": valuation,
            "share_stats": share_stats,
            "analyst": analyst,
            "calendar": calendar_rows,
            "recommendations": recommendations_rows,
            "holders": holders,
            "financials": financials,
            "history": history,
            "news": self._safe_news(ticker, limit=10),
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        }

    def get_history(
        self,
        symbol: str,
        period: str = "5d",
        interval: str = "1m",
        prepost: bool = True,
        limit: int = 390,
    ) -> Dict[str, Any]:
        ticker = self._ticker(symbol)
        rows = self._safe_history(ticker, period=period, interval=interval, prepost=prepost, limit=limit)
        return {
            "symbol": str(symbol).strip().upper(),
            "source": "yfinance",
            "period": period,
            "interval": interval,
            "prepost": prepost,
            "count": len(rows),
            "data": rows,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        }

    def get_short_ownership_from_info(self, symbol: str) -> Dict[str, Any]:
        ticker = self._ticker(symbol)
        try:
            info = ticker.info or {}
        except Exception:
            info = {}

        return {
            "symbol": str(symbol).strip().upper(),
            "source": "yfinance",
            "short_float_pct": _normalize_pct(info.get("shortPercentOfFloat") or info.get("sharesPercentSharesOut")),
            "short_interest_shares": _to_float(info.get("sharesShort")),
            "days_to_cover": _to_float(info.get("shortRatio")),
            "borrow_fee_pct": None,
            "institutional_ownership_pct": _normalize_pct(info.get("heldPercentInstitutions")),
            "insider_ownership_pct": _normalize_pct(info.get("heldPercentInsiders")),
            "float_shares": _to_float(info.get("floatShares")),
            "shares_outstanding": _to_float(info.get("sharesOutstanding")),
        }

    # ───────────────────────────────────────────────────────────
    # Phase 2 — options / earnings / dividend
    # ───────────────────────────────────────────────────────────
    def get_options_summary(self, symbol: str, max_expirations: int = 3) -> Dict[str, Any]:
        """가까운 N개 만기의 옵션 chain 을 합쳐 P/C ratio + IV 평균 계산.

        무료(yfinance) 옵션 데이터는 만기별 chain (calls/puts DataFrame). 만기마다
        호출이 따로 가야 해 비용이 큼 → max_expirations 로 제한 (기본 3개 만기).
        """
        ticker = self._ticker(symbol)
        try:
            expirations = list(ticker.options or [])
        except Exception:
            expirations = []

        if not expirations:
            return {
                "symbol": str(symbol).strip().upper(),
                "source": "yfinance",
                "expirations_used": [],
                "put_call_ratio_oi": None,   # open interest 기반 P/C
                "put_call_ratio_vol": None,  # 거래량 기반 P/C
                "avg_iv_pct": None,          # 30일 가까운 만기 IV 평균
                "total_call_oi": None,
                "total_put_oi": None,
                "unusual_volume": False,
            }

        targets = expirations[: max(1, int(max_expirations))]
        total_call_oi = 0.0
        total_put_oi = 0.0
        total_call_vol = 0.0
        total_put_vol = 0.0
        iv_samples: List[float] = []
        used: List[str] = []

        for exp in targets:
            try:
                chain = ticker.option_chain(exp)
            except Exception:
                continue
            calls_df = getattr(chain, "calls", None)
            puts_df = getattr(chain, "puts", None)
            if calls_df is None or puts_df is None:
                continue
            try:
                total_call_oi += float(calls_df["openInterest"].fillna(0).sum())
                total_put_oi += float(puts_df["openInterest"].fillna(0).sum())
                total_call_vol += float(calls_df["volume"].fillna(0).sum())
                total_put_vol += float(puts_df["volume"].fillna(0).sum())
                # ATM 근처 IV — 단순 평균 (개선 여지 있음)
                if "impliedVolatility" in calls_df.columns:
                    for v in calls_df["impliedVolatility"].dropna().tolist()[:5]:
                        iv_samples.append(float(v))
                if "impliedVolatility" in puts_df.columns:
                    for v in puts_df["impliedVolatility"].dropna().tolist()[:5]:
                        iv_samples.append(float(v))
            except Exception:
                continue
            used.append(exp)

        pc_oi = (total_put_oi / total_call_oi) if total_call_oi > 0 else None
        pc_vol = (total_put_vol / total_call_vol) if total_call_vol > 0 else None
        avg_iv = (sum(iv_samples) / len(iv_samples) * 100.0) if iv_samples else None
        # "이상 거래량" 추정: 풋·콜 합산 거래량이 OI 합산의 30% 이상이면 활발
        unusual = False
        try:
            total_oi = total_call_oi + total_put_oi
            total_vol = total_call_vol + total_put_vol
            if total_oi > 0 and total_vol > 0:
                unusual = (total_vol / total_oi) >= 0.30
        except Exception:
            pass

        return {
            "symbol": str(symbol).strip().upper(),
            "source": "yfinance",
            "expirations_used": used,
            "put_call_ratio_oi": round(pc_oi, 3) if pc_oi is not None else None,
            "put_call_ratio_vol": round(pc_vol, 3) if pc_vol is not None else None,
            "avg_iv_pct": round(avg_iv, 2) if avg_iv is not None else None,
            "total_call_oi": int(total_call_oi) if total_call_oi else 0,
            "total_put_oi": int(total_put_oi) if total_put_oi else 0,
            "total_call_volume": int(total_call_vol) if total_call_vol else 0,
            "total_put_volume": int(total_put_vol) if total_put_vol else 0,
            "unusual_volume": unusual,
        }

    def get_earnings_history(self, symbol: str, limit: int = 8) -> Dict[str, Any]:
        """최근 어닝 EPS 실적/추정/서프라이즈."""
        ticker = self._ticker(symbol)
        rows: List[Dict[str, Any]] = []
        try:
            df = getattr(ticker, "earnings_history", None)
        except Exception:
            df = None
        rows = self._safe_dataframe_rows(df, limit=max(1, int(limit)))
        # 가장 최근 1건
        last = None
        if rows:
            r = rows[0] if rows else {}
            last = {
                "period": r.get("index") or r.get("Date") or r.get("date"),
                "eps_estimate": _to_float(r.get("epsEstimate") or r.get("EpsEstimate")),
                "eps_actual": _to_float(r.get("epsActual") or r.get("EpsActual")),
                "eps_difference": _to_float(r.get("epsDifference") or r.get("EpsDifference")),
                "surprise_pct": _to_float(r.get("surprisePercent") or r.get("SurprisePercent")),
            }
        return {
            "symbol": str(symbol).strip().upper(),
            "source": "yfinance",
            "last": last,
            "history": rows,
        }

    def get_dividend_info(self, symbol: str) -> Dict[str, Any]:
        """배당 정보 (rate, yield, ex-date, payment date, frequency)."""
        ticker = self._ticker(symbol)
        try:
            info = ticker.info or {}
        except Exception:
            info = {}

        # frequency 추정: 최근 dividends 시계열 간격
        frequency = None
        try:
            divs = ticker.dividends
            if divs is not None and len(divs) >= 4:
                # 최근 4건 간격 평균(일)
                idx = list(divs.tail(5).index)
                if len(idx) >= 2:
                    gaps = [(idx[i] - idx[i - 1]).days for i in range(1, len(idx))]
                    avg_gap = sum(gaps) / len(gaps) if gaps else 0
                    if 25 <= avg_gap <= 40:
                        frequency = "monthly"
                    elif 80 <= avg_gap <= 100:
                        frequency = "quarterly"
                    elif 175 <= avg_gap <= 195:
                        frequency = "semi-annual"
                    elif 350 <= avg_gap <= 380:
                        frequency = "annual"
        except Exception:
            pass

        return {
            "symbol": str(symbol).strip().upper(),
            "source": "yfinance",
            "dividend_rate": _to_float(info.get("dividendRate")),  # 연간 USD
            "dividend_yield_pct": _normalize_pct(info.get("dividendYield")),
            "five_year_avg_dividend_yield_pct": _to_float(info.get("fiveYearAvgDividendYield")),
            "payout_ratio_pct": _normalize_pct(info.get("payoutRatio")),
            "ex_dividend_date": info.get("exDividendDate"),  # epoch sec
            "last_dividend_value": _to_float(info.get("lastDividendValue")),
            "last_dividend_date": info.get("lastDividendDate"),
            "frequency": frequency,
        }
