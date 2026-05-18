import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import re

class MacroCollector:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

    def get_night_futures(self):
        """
        KOSPI 200 야간 선물 지수 크롤링
        """
        # 1. Investing.com (헤더 보강)
        url = "https://www.investing.com/indices/kospi-200-futures"
        try:
            res = requests.get(url, headers=self.headers, timeout=10)
            soup = BeautifulSoup(res.text, "html.parser")
            price_tag = soup.find(attrs={"data-test": "instrument-price-last"})
            change_tag = soup.find(attrs={"data-test": "instrument-price-change-percent"})
            if price_tag:
                return {"price": price_tag.text.strip(), "change_pct": change_tag.text.strip()}
        except: pass

        # 2. Yahoo Finance (KOSPI 200 지수 대체용 - 야간 반영 가능성)
        try:
            url = "https://finance.yahoo.com/quote/%5EKS200" # KOSPI 200
            res = requests.get(url, headers=self.headers, timeout=10)
            soup = BeautifulSoup(res.text, "html.parser")
            price_tag = soup.find("fin-streamer", {"data-field": "regularMarketPrice"})
            change_tag = soup.find("fin-streamer", {"data-field": "regularMarketChangePercent"})
            if price_tag:
                return {"price": price_tag.text.strip(), "change_pct": change_tag.text.strip()}
        except: pass
        return None

    def get_usa_indices(self):
        """
        미국 3대 지수 (S&P500, Nasdaq, Dow) — yfinance 사용.
        scripts/fetch_us_indices.py 와 동일 로직·포맷. 실패 시 빈 dict.
        """
        try:
            import yfinance as yf
            out = {}
            for key, ticker, label in [
                ("sp500", "^GSPC", "S&P 500"),
                ("nasdaq", "^IXIC", "Nasdaq"),
                ("dow", "^DJI", "Dow"),
            ]:
                try:
                    tk = yf.Ticker(ticker)
                    hist = tk.history(period="5d", auto_adjust=False)
                    if hist is None or hist.empty or len(hist) < 2:
                        continue
                    last = hist.iloc[-1]
                    prev = hist.iloc[-2]
                    close = float(last["Close"])
                    prev_close = float(prev["Close"])
                    change_amt = close - prev_close
                    change_pct = (change_amt / prev_close * 100.0) if prev_close else 0.0
                    out[key] = {
                        "label": label,
                        "ticker": ticker,
                        "price": f"{close:,.2f}",
                        "price_numeric": round(close, 2),
                        "change_pct": round(change_pct, 2),
                        "change_amt": round(change_amt, 2),
                        "session_date": str(hist.index[-1].date()),
                    }
                except Exception:
                    continue
            return out
        except Exception:
            return {}

    def _get_exchange_rate_kis(self):
        """KIS 해외주식-012 (FHKST03030100) — USD/KRW 환율 조회"""
        try:
            from config.settings import KIS_DOMAIN, KIS_APP_KEY, KIS_APP_SECRET
            from utils.helpers import get_kis_token
            token = get_kis_token()
            if not token:
                return None
            today = datetime.now().strftime("%Y%m%d")
            ago = (datetime.now() - timedelta(days=5)).strftime("%Y%m%d")
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {token}",
                "appkey": KIS_APP_KEY,
                "appsecret": KIS_APP_SECRET,
                "tr_id": "FHKST03030100",
                "custtype": "P",
            }
            params = {
                "FID_COND_MRKT_DIV_CODE": "X",
                "FID_INPUT_ISCD": "FX@KRW",
                "FID_INPUT_DATE_1": ago,
                "FID_INPUT_DATE_2": today,
                "FID_PERIOD_DIV_CODE": "D",
            }
            res = requests.get(
                f"{KIS_DOMAIN}/uapi/overseas-price/v1/quotations/inquire-daily-chartprice",
                headers=headers, params=params, timeout=5,
            )
            if res.status_code != 200:
                return None
            data = res.json()
            if data.get("rt_cd") != "0":
                return None
            out1 = data.get("output1") or {}
            out2 = data.get("output2") or []
            price = float(out1.get("ovrs_nmix_prpr") or out1.get("stck_prpr") or 0)
            if not price and out2:
                price = float(out2[0].get("ovrs_nmix_prpr") or out2[0].get("clos") or 0)
            prev_close = float(out1.get("ovrs_nmix_prdy_clpr") or 0)
            if not prev_close and len(out2) >= 2:
                prev_close = float(out2[1].get("ovrs_nmix_prpr") or out2[1].get("clos") or 0)
            if not price:
                return None
            change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else None
            change_amt = round(price - prev_close, 2) if prev_close else None
            return {
                "price": f"{price:,.2f}",
                "change_pct": change_pct,
                "change_amt": change_amt,
            }
        except Exception:
            return None

    def _get_exchange_rate_yahoo(self):
        """Yahoo Finance 폴백"""
        try:
            res = requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/USDKRW=X",
                params={"interval": "1d", "range": "2d"},
                headers=self.headers,
                timeout=7,
            )
            meta = res.json()["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice") or 0
            prev_close = meta.get("chartPreviousClose") or 0
            change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else None
            change_amt = round(price - prev_close, 2) if prev_close else None
            return {
                "price": f"{price:,.2f}",
                "change_pct": change_pct,
                "change_amt": change_amt,
            }
        except Exception:
            return None

    def get_exchange_rate(self):
        """KIS API 우선 → Yahoo Finance 폴백"""
        return self._get_exchange_rate_kis() or self._get_exchange_rate_yahoo()

    def get_us_overnight_events(self, lookback_hours: int = 30, min_importance: str = "medium"):
        """전날(KST 기준) 미국 매크로 이벤트 — Trading Economics 캘린더.
        아침 시황 brief 작성 시 "간밤 미국에서 PPI/CPI/Fed 발언 등이 있었는지" 컨텍스트.
        실패·빈 결과 시 빈 list."""
        try:
            from .us_econ_calendar import get_us_overnight_events as _fetch
            return _fetch(lookback_hours=lookback_hours, min_importance=min_importance)
        except Exception:
            return []

    def get_all_macros(self):
        fx = self.get_exchange_rate() or {}
        return {
            "night_futures":             self.get_night_futures(),
            "exchange_rate":             fx.get("price"),
            "exchange_rate_change_pct":  fx.get("change_pct"),
            "exchange_rate_change_amt":  fx.get("change_amt"),
            "usa_indices":               self.get_usa_indices(),
            # 사용자 피드백(2026-05-14): 아침 브리핑이 전날 미국 PPI/CPI/Fed 같은
            # 매크로 이벤트를 다루지 않음. 컨텍스트에 추가해 brief 작성 시 활용.
            "us_overnight_events":       self.get_us_overnight_events(),
        }

if __name__ == "__main__":
    collector = MacroCollector()
    print(collector.get_all_macros())
