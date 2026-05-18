import io
import json
import re
from datetime import datetime

import pandas as pd
import requests


def _num_list(text):
    if text is None:
        return []
    s = str(text).replace("\xa0", " ")
    tokens = re.findall(r"-?\d[\d,]*\.?\d*", s)
    out = []
    for token in tokens:
        t = token.replace(",", "")
        try:
            out.append(float(t))
        except Exception:
            continue
    return out


def _to_int(value):
    nums = _num_list(value)
    if not nums:
        return None
    return int(round(nums[0]))


def _to_float(value):
    nums = _num_list(value)
    if not nums:
        return None
    return float(nums[0])


def _to_records(df):
    if df is None or df.empty:
        return []
    tmp = df.copy()
    tmp = tmp.where(pd.notnull(tmp), None)
    return tmp.to_dict(orient="records")


class NaverExtendedCollector:
    def __init__(self):
        self.session = requests.Session()
        # Ignore shell proxy env. This environment sets a blocked proxy by default.
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

    def _fetch_main_html(self, code: str):
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        response = self.session.get(url, timeout=15)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        return response.text

    def _fetch_polling(self, code: str):
        url = f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{code}"
        response = self.session.get(url, timeout=10)
        response.raise_for_status()
        payload = response.json()
        areas = (((payload or {}).get("result") or {}).get("areas") or [])
        if not areas:
            return {}
        datas = (areas[0] or {}).get("datas") or []
        return datas[0] if datas else {}

    def _fetch_basic(self, code: str):
        url = f"https://m.stock.naver.com/api/stock/{code}/basic"
        response = self.session.get(url, timeout=10)
        response.raise_for_status()
        payload = response.json() if response.content else {}
        if not isinstance(payload, dict):
            return {}
        return payload

    def _parse_investor_trend(self, tables):
        if len(tables) <= 3:
            return []
        df = tables[3]
        if df.empty:
            return []
        # expected cols: 날짜, 종가, 전일비, 외국인, 기관
        out = []
        for _, row in df.iterrows():
            date_text = str(row.iloc[0]) if len(row) > 0 else ""
            if "/" not in date_text:
                continue
            out.append({
                "date": date_text.strip(),
                "close": _to_int(row.iloc[1]) if len(row) > 1 else None,
                "foreign_net": _to_int(row.iloc[3]) if len(row) > 3 else None,
                "institution_net": _to_int(row.iloc[4]) if len(row) > 4 else None,
            })
        return out

    def _parse_broker_top(self, tables):
        """매수/매도 상위 증권사. tables[2] 위치 기반 파싱 (컬럼명 인코딩 무관)."""
        if len(tables) <= 2:
            return []
        df = tables[2]
        if df.empty or df.shape[1] < 4:
            return []
        out = []
        for _, row in df.iterrows():
            buy_broker  = str(row.iloc[0]).strip() if len(row) > 0 else ""
            buy_amt     = _to_int(row.iloc[1]) if len(row) > 1 else None
            sell_broker = str(row.iloc[2]).strip() if len(row) > 2 else ""
            sell_amt    = _to_int(row.iloc[3]) if len(row) > 3 else None
            # 헤더 행 건너뜀
            if not buy_broker or buy_broker in ("nan", "NaN") or buy_amt is None:
                continue
            out.append({
                "buy_broker": buy_broker,
                "buy_amt": buy_amt,
                "sell_broker": sell_broker,
                "sell_amt": sell_amt,
            })
        return out

    def _parse_peer_compare(self, tables):
        """동종업종 비교. tables[5] 위치 기반 파싱."""
        if len(tables) <= 5:
            return []
        df = tables[5]
        if df.empty or df.shape[1] < 2:
            return []
        # 첫 번째 열 = 지표명(행 레이블), 나머지 열 = 종목별 값
        # 첫 행이 종목명, 이후 행이 지표값 패턴
        records = []
        # 컬럼 헤더 (종목명)는 첫 행에서 읽음
        header_row = df.iloc[0]
        metric_names = [str(df.iloc[r, 0]).strip() for r in range(1, len(df))]
        for col_idx in range(1, df.shape[1]):
            stock_name = str(header_row.iloc[col_idx]).strip()
            if not stock_name or stock_name in ("nan", "NaN"):
                continue
            rec = {"name": stock_name}
            for row_idx, metric in enumerate(metric_names, start=1):
                if row_idx < len(df):
                    val = _to_float(df.iloc[row_idx, col_idx])
                    if metric and metric not in ("nan", "NaN"):
                        rec[metric] = val
            records.append(rec)
        return records

    def _extract_analyst_count(self, text):
        if not text:
            return None
        s = str(text).replace(",", "")
        m = re.search(r"추정기관수\s*([0-9]+)", s)
        if m:
            return _to_int(m.group(1))
        nums = _num_list(s)
        if len(nums) >= 3:
            # common shape: [투자의견, 목표주가, 추정기관수]
            return int(round(nums[2]))
        return None

    def get_snapshot(self, code: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            html = self._fetch_main_html(code)
            tables = pd.read_html(io.StringIO(html))
            polling = self._fetch_polling(code)
            basic = self._fetch_basic(code)

            market_cap_text = None
            listed_shares = None
            foreign_own_pct = None
            if len(tables) > 6:
                t6 = tables[6]
                if not t6.empty:
                    market_cap_text = str(t6.iloc[0, 1]) if t6.shape[1] > 1 else None
                    listed_shares = _to_int(t6.iloc[2, 1]) if t6.shape[0] > 2 and t6.shape[1] > 1 else None

            if len(tables) > 7:
                t7 = tables[7]
                if not t7.empty and t7.shape[0] > 2 and t7.shape[1] > 1:
                    foreign_own_pct = _to_float(t7.iloc[2, 1])

            invest_opinion = None
            invest_label = None
            target_price = None
            consensus_analyst_count = None
            high_52w = None
            low_52w = None
            if len(tables) > 8:
                t8 = tables[8]
                if not t8.empty and t8.shape[1] > 1:
                    v0 = str(t8.iloc[0, 1]) if t8.shape[0] > 0 else ""
                    nums0 = _num_list(v0)
                    if nums0:
                        invest_opinion = float(nums0[0])
                    if len(nums0) >= 2:
                        target_price = int(round(nums0[1]))
                    consensus_analyst_count = self._extract_analyst_count(v0)
                    m = re.search(r"([가-힣A-Za-z]+)", v0)
                    invest_label = m.group(1) if m else None

                    v1 = str(t8.iloc[1, 1]) if t8.shape[0] > 1 else ""
                    nums1 = _num_list(v1)
                    if nums1:
                        high_52w = int(round(nums1[0]))
                    if len(nums1) >= 2:
                        low_52w = int(round(nums1[1]))

            per_ttm = None
            eps_ttm = None
            est_per = None
            est_eps = None
            pbr = None
            bps = None
            dividend_yield = None
            if len(tables) > 9:
                t9 = tables[9]
                if not t9.empty and t9.shape[1] > 1:
                    row0 = _num_list(t9.iloc[0, 1]) if t9.shape[0] > 0 else []
                    row1 = _num_list(t9.iloc[1, 1]) if t9.shape[0] > 1 else []
                    row2 = _num_list(t9.iloc[2, 1]) if t9.shape[0] > 2 else []
                    row3 = _num_list(t9.iloc[3, 1]) if t9.shape[0] > 3 else []

                    if row0:
                        per_ttm = float(row0[0])
                    if len(row0) >= 2:
                        eps_ttm = float(row0[1])
                    if row1:
                        est_per = float(row1[0])
                    if len(row1) >= 2:
                        est_eps = float(row1[1])
                    if row2:
                        pbr = float(row2[0])
                    if len(row2) >= 2:
                        bps = float(row2[1])
                    if row3:
                        dividend_yield = float(row3[0])

            payload = {
                "code": code,
                "collected_at": ts,
                "market_cap_text": market_cap_text,
                "listed_shares": listed_shares,
                "foreign_ownership_pct": foreign_own_pct,
                "investment_opinion_score": invest_opinion,
                "investment_opinion_label": invest_label,
                "target_price": target_price,
                "consensus_analyst_count": consensus_analyst_count,
                "high_52w": high_52w,
                "low_52w": low_52w,
                "per_ttm": per_ttm,
                "eps_ttm": eps_ttm,
                "est_per": est_per,
                "est_eps": est_eps,
                "pbr": pbr,
                "bps": bps,
                "dividend_yield": dividend_yield,
                "consensus_eps": _to_float((polling or {}).get("cnsEps")),
                "investor_trend_7d": self._parse_investor_trend(tables),
                "broker_top": self._parse_broker_top(tables),
                "peer_compare": self._parse_peer_compare(tables),
                "polling": polling,
                "stock_end_type": basic.get("stockEndType"),
                "item_logo_url": basic.get("itemLogoUrl"),
                "item_logo_png_url": basic.get("itemLogoPngUrl"),
                "status": "ok",
                "error": None,
            }
            return payload
        except Exception as e:
            return {
                "code": code,
                "collected_at": ts,
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "polling": {},
                "investor_trend_7d": [],
                "broker_top": [],
                "peer_compare": [],
            }
