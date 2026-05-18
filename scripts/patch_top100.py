"""
top100_full_latest.json 패치 스크립트
기존 수집 데이터에 빠진 필드들을 추가합니다:

[계산만 (API 없음)] 빠름
  - tech_analysis     : MA5/20/60/120, RS스코어, 거래량 프로파일, returns
  - ownership_summary_pct : 수급 비중 요약
  - market_change_pct : 전일 대비 등락률
  - short_data 개선   : short_enabled 추가

[외부 API 필요] 종목당 1~2초
  - _macro            : 환율 + 미국 지수 (1회)
  - dart_shareholders : DART 주요주주 현황 (종목당 1회)
"""
import sys, os, json, time, requests
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from config.settings import DART_API_KEY

LATEST_PATH = os.path.join(ROOT_DIR, "data", "top100_full_latest.json")


# ─── 1) MACRO 데이터 (1회) ──────────────────────────────────
def fetch_macro() -> dict:
    macro = {"night_futures": None, "exchange_rate": None, "usa_indices": {}}

    # 환율 (USD/KRW) — Yahoo Finance 무료 API
    try:
        res = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/USDKRW=X",
            params={"interval": "1d", "range": "2d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=7
        )
        meta = res.json()["chart"]["result"][0]["meta"]
        fx_price      = meta.get("regularMarketPrice") or 0
        fx_prev_close = meta.get("chartPreviousClose") or 0
        fx_change_pct = round((fx_price - fx_prev_close) / fx_prev_close * 100, 2) if fx_prev_close else None
        fx_change_amt = round(fx_price - fx_prev_close, 2) if fx_prev_close else None
        macro["exchange_rate"]          = f"{fx_price:,.2f}"
        macro["exchange_rate_change_pct"] = fx_change_pct
        macro["exchange_rate_change_amt"] = fx_change_amt
    except Exception:
        pass

    # 미국 주요 지수 — Yahoo Finance
    indices = {
        "SPX": "^GSPC",    # S&P 500
        "NDX": "^IXIC",    # 나스닥
        "DJI": "^DJI",     # 다우
        "VIX": "^VIX",     # 공포지수
    }
    for name, symbol in indices.items():
        try:
            res = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"interval": "1d", "range": "2d"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=7
            )
            info = res.json()["chart"]["result"][0]["meta"]
            macro["usa_indices"][name] = {
                "price": info.get("regularMarketPrice"),
                "prev_close": info.get("chartPreviousClose"),
                "change_pct": round(
                    (info["regularMarketPrice"] - info["chartPreviousClose"])
                    / info["chartPreviousClose"] * 100, 2
                ) if info.get("chartPreviousClose") else None
            }
        except Exception:
            pass

    return macro


# ─── 2) DART 주요주주 ───────────────────────────────────────
class DartShareholders:
    def __init__(self):
        self.api_key = DART_API_KEY
        self._corp_map = None

    def _load_map(self):
        if self._corp_map:
            return
        import zipfile, io, xml.etree.ElementTree as ET
        print("  [DART] 기업코드 마스터 로딩...", end=" ", flush=True)
        url = "https://opendart.fss.or.kr/api/corpCode.xml"
        res = requests.get(url, params={"crtfc_key": self.api_key}, timeout=15)
        m = {}
        if res.status_code == 200:
            with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                with z.open("CORPCODE.xml") as f:
                    import xml.etree.ElementTree as ET2
                    root = ET2.parse(f).getroot()
                    for lst in root.findall("list"):
                        sc = lst.find("stock_code").text
                        cc = lst.find("corp_code").text
                        if sc and sc.strip():
                            m[sc.strip()] = cc
        self._corp_map = m
        print(f"완료 ({len(m)}개)")

    def fetch(self, stock_code: str) -> dict:
        self._load_map()
        corp_code = self._corp_map.get(stock_code)
        if not corp_code:
            return {}
        try:
            # 최근 연도 주요주주 보고서
            year = str(datetime.now().year - 1)
            res = requests.get(
                "https://opendart.fss.or.kr/api/majorstock.json",
                params={"crtfc_key": self.api_key, "corp_code": corp_code, "bsns_year": year, "reprt_code": "11011"},
                timeout=10
            )
            data = res.json()
            return {"status": data.get("status"), "message": data.get("message"), "list": data.get("list", [])}
        except Exception:
            return {}


# ─── 3) TECH ANALYSIS (계산) ───────────────────────────────
def calc_tech_analysis(ohlcv: list, current_price: int) -> dict:
    if not ohlcv:
        return {}

    closes = [r["close"] for r in ohlcv if r.get("close")]
    volumes = [r["volume"] for r in ohlcv if r.get("volume") is not None]

    def ma(n):
        if len(closes) < n:
            return None
        return round(sum(closes[:n]) / n, 2)

    def div(price, avg):
        if avg is None or avg == 0:
            return None
        return round((price - avg) / avg * 100, 2)

    # 상대강도 스코어: 1주/4주 수익률 평균으로 단순 계산
    r1d  = round((closes[0] - closes[1]) / closes[1] * 100, 2) if len(closes) >= 2 else None
    r5d  = round((closes[0] - closes[4]) / closes[4] * 100, 2) if len(closes) >= 5 else None
    r20d = round((closes[0] - closes[19]) / closes[19] * 100, 2) if len(closes) >= 20 else None
    rs_score = round((r5d + (r20d or 0)) / 2, 2) if r5d is not None else None

    # 거래량 프로파일 (10구간)
    vp = []
    if closes and volumes and len(closes) == len(volumes):
        lo, hi = min(closes), max(closes)
        if hi > lo:
            bucket_size = (hi - lo) / 10
            for i in range(10):
                plo = lo + i * bucket_size
                phi = lo + (i + 1) * bucket_size
                vol = sum(volumes[j] for j, c in enumerate(closes) if plo <= c < phi)
                vp.append({"price_lo": round(plo), "price_hi": round(phi), "volume": vol})

    # 20일 평균 거래대금
    trading_values = [r.get("trading_value", 0) for r in ohlcv[:20] if r.get("trading_value")]
    avg_20d_tv = int(sum(trading_values) / len(trading_values)) if trading_values else 0

    ma5 = ma(5); ma20 = ma(20); ma60 = ma(60); ma120 = ma(120)

    return {
        "current_price": current_price,
        "ma5":   ma5,
        "ma20":  ma20,
        "ma60":  ma60,
        "ma120": ma120,
        "div_5":   div(current_price, ma5),
        "div_20":  div(current_price, ma20),
        "div_60":  div(current_price, ma60),
        "div_120": div(current_price, ma120),
        "rs_score": rs_score,
        "avg_20d_trading_value": avg_20d_tv,
        "volume_profile": vp,
        "returns": {"1d": r1d, "5d": r5d, "20d": r20d},
    }


# ─── 4) 수급 비중 요약 (계산) ──────────────────────────────
def calc_ownership_summary(investor_5d: list) -> dict:
    """최근 5일 수급을 합산하여 비중 요약"""
    if not investor_5d:
        return {}
    keys = [
        "foreign", "institution", "individual", "etc_org", "program",
        "foreign_net_amt", "institution_net_amt", "individual_net_amt",
        "foreign_buy", "institution_buy", "individual_buy",
        "foreign_sell", "institution_sell", "individual_sell",
    ]
    totals = {k: sum(d.get(k, 0) or 0 for d in investor_5d) for k in keys}
    total_vol = abs(totals.get("foreign", 0)) + abs(totals.get("institution", 0)) + abs(totals.get("individual", 0))
    result = {}
    for k, v in totals.items():
        result[k] = round(v / total_vol * 100, 2) if total_vol else 0.0
    return result


# ─── MAIN PATCH ─────────────────────────────────────────────
def main():
    print("=" * 60)
    print("🔧 top100_full_latest.json 패치 스크립트")
    print("=" * 60)

    # 기존 JSON 로드
    with open(LATEST_PATH, encoding="utf-8") as f:
        data = json.load(f)
    print(f"✅ 기존 데이터 로드: {len(data)}개 종목")

    # ── MACRO (1회) ─────────────────────────────────────────
    print("\n[1/3] 매크로 데이터 수집 (환율, 미국 지수)...")
    macro = fetch_macro()
    print(f"  환율: {macro.get('exchange_rate')} | 지수: {list(macro['usa_indices'].keys())}")
    data["_macro"] = macro

    # ── DART 주요주주 로더 ───────────────────────────────────
    print("\n[2/3] DART 주요주주 수집 준비...")
    dart_sh = DartShareholders()
    dart_sh._load_map()

    # ── 종목별 패치 ─────────────────────────────────────────
    print(f"\n[3/3] 종목별 계산 필드 + DART 주요주주 패치 중...\n")
    codes = [c for c in data.keys() if c != "_macro"]
    total = len(codes)
    start = time.time()

    for idx, code in enumerate(codes, 1):
        d = data[code]

        # tech_analysis (OHLCV 기반 계산)
        ohlcv = d.get("daily_ohlcv", [])
        cur_price = (d.get("price_today") or {}).get("current_price", 0)
        d["tech_analysis"] = calc_tech_analysis(ohlcv, cur_price)

        # ownership_summary_pct (investor_5d 기반 계산)
        d["ownership_summary_pct"] = calc_ownership_summary(d.get("investor_5d", []))

        # market_change_pct (price_today에서 추출)
        d["market_change_pct"] = (d.get("price_today") or {}).get("change_pct", 0)

        # short_data: short_enabled 추가 (_raw.ssts_yn에서)
        raw = (d.get("price_today") or {}).get("_raw", {})
        sd = d.get("short_data") or {}
        sd["short_enabled"] = raw.get("ssts_yn") == "Y"
        # daily 배열 제거 (불필요)
        sd.pop("daily", None)
        d["short_data"] = sd

        # credit_data: {rate_today, daily} 구조로 정리
        cd = d.get("credit_data", [])
        if isinstance(cd, list):
            rate_today = cd[0].get("credit_rate", 0) if cd else 0
            d["credit_data"] = {"rate_today": rate_today, "daily": cd}

        # dart_shareholders (DART API)
        d["dart_shareholders"] = dart_sh.fetch(code)
        time.sleep(0.2)  # DART rate limit

        elapsed = time.time() - start
        eta = (elapsed / idx) * (total - idx)
        sys.stdout.write(
            f"\r⏳ [{(idx/total)*100:>5.1f}%] {idx}/{total} | "
            f"{d.get('name',''):14s} | 경과 {elapsed/60:.1f}분 | ETA {eta/60:.1f}분"
        )
        sys.stdout.flush()

    sys.stdout.write("\n")

    # 저장
    today = datetime.now().strftime("%Y%m%d_%H%M")
    backup = LATEST_PATH.replace("latest.json", f"latest_before_patch_{today}.json")

    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    size_mb = os.path.getsize(LATEST_PATH) / 1024 / 1024
    print(f"\n✅ 패치 완료!")
    print(f"  - 종목 수  : {len(codes)}개")
    print(f"  - 소요시간 : {(time.time()-start)/60:.1f}분")
    print(f"  - 파일크기 : {size_mb:.2f} MB")
    print(f"  - 저장위치 : {LATEST_PATH}")
    print(f"\n  추가된 필드:")
    print(f"    ✓ _macro (환율 + 미국지수)")
    print(f"    ✓ tech_analysis (MA/RS스코어/거래량프로파일)")
    print(f"    ✓ ownership_summary_pct (수급비중)")
    print(f"    ✓ market_change_pct")
    print(f"    ✓ short_data.short_enabled")
    print(f"    ✓ credit_data 구조 개선 (rate_today + daily)")
    print(f"    ✓ dart_shareholders (주요주주)")


if __name__ == "__main__":
    main()
