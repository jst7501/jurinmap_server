"""
코스피/코스닥 TOP 191개 종목 풀 데이터 수집기
- 종목 리스트: data/top100_stock_list.json (빌드된 파일)
- KIS API: 현재가, 수급, OHLCV, 프로그램매매, 공매도, 신용잔고, 재무비율
- DART: 최근 공시 5건
- 네이버 종토방: 감성 분석 스코어
"""
import sys, os, json, time, requests
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from collectors.kis_api import KISCollector
from scrapers.naver_board_scraper import NaverBoardScraper
from scrapers.ai_analysis_engine import HumanIndicatorAI
from config.settings import DART_API_KEY

# ─── DART 최근 공시 수집기 ──────────────────────────────────
class DartDisc:
    def __init__(self):
        self.api_key = DART_API_KEY
        self._corp_map = None

    def load(self):
        if self._corp_map is not None:
            return
        import zipfile, io, xml.etree.ElementTree as ET
        print("  [DART] 기업코드 마스터 로딩...", end=" ")
        url = "https://opendart.fss.or.kr/api/corpCode.xml"
        res = requests.get(url, params={"crtfc_key": self.api_key}, timeout=15)
        m = {}
        if res.status_code == 200:
            with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                with z.open('CORPCODE.xml') as f:
                    root = __import__('xml.etree.ElementTree', fromlist=['parse']).parse(f).getroot()
                    for lst in root.findall('list'):
                        sc = lst.find('stock_code').text
                        cc = lst.find('corp_code').text
                        if sc and sc.strip():
                            m[sc.strip()] = cc
        self._corp_map = m
        print(f"완료 ({len(m)}개)")

    def get(self, code: str, count=5) -> list:
        self.load()
        corp_code = self._corp_map.get(code)
        if not corp_code:
            return []
        try:
            res = requests.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={"crtfc_key": self.api_key, "corp_code": corp_code,
                        "bgn_de": "20251001", "page_no": "1", "page_count": str(count)},
                timeout=10
            )
            data = res.json()
            if data.get("status") != "000":
                return []
            return [{"date": i.get("rcept_dt",""), "title": i.get("report_nm",""),
                     "type": i.get("pblntf_ty","")} for i in data.get("list",[])[:count]]
        except Exception:
            return []


# ─── 메인 ───────────────────────────────────────────────────
def main():
    print("=" * 62)
    print("📊 코스피/코스닥 TOP 191 풀 데이터 수집기")
    print("   [KIS 시세/수급] + [DART 공시] + [종토방 감성]")
    print("=" * 62)

    # 0. 종목 리스트 로드
    list_path = os.path.join(ROOT_DIR, "data", "top100_stock_list.json")
    if not os.path.exists(list_path):
        print(f"❌ 종목 리스트 파일 없음: {list_path}")
        print("   먼저 python scripts/build_top100_list.py 를 실행하세요.")
        return
    with open(list_path, encoding="utf-8") as f:
        stock_meta = json.load(f)

    all_stocks = {}
    for item in stock_meta.get("kospi", []) + stock_meta.get("kosdaq", []):
        code = item["code"]
        all_stocks[code] = {"name": item["name"], "market": item["market"]}
    stock_list = list(all_stocks.keys())
    total = len(stock_list)
    print(f"\n✅ 종목 리스트 로드 완료: {total}개")

    # 1. KIS
    print("\n[Step 1] KIS API 인증 중...")
    try:
        kis = KISCollector()
    except Exception as e:
        print(f"❌ KIS 초기화 실패: {e}"); return

    # 2. DART
    print("[Step 2] DART 공시 수집기 준비...")
    dart = DartDisc()
    dart.load()

    # 3. 종토방 + AI
    print("[Step 3] 네이버 종토방 스크래퍼 + Gemini AI 분석기 준비...")
    naver = NaverBoardScraper()
    ai    = HumanIndicatorAI()

    print(f"\n🚀 전체 수집 시작! (총 {total}개 × 10개 항목)\n")

    def safe(func, *args, default=None):
        try:
            return func(*args)
        except Exception:
            return default if default is not None else {}

    result = {}
    start_time = time.time()

    for idx, code in enumerate(stock_list, 1):
        name   = all_stocks[code]["name"]
        market = all_stocks[code]["market"]

        d = {"name": name, "market": market}

        # KIS: 7개 엔드포인트
        d["price_today"]    = safe(kis.get_price, code)
        d["daily_ohlcv"]    = safe(kis.get_daily_price, code, "D", default=[])
        inv_hist            = safe(kis.get_investor_history, code, 5, default=[])
        d["investor_5d"]    = inv_hist
        d["investor_today"] = inv_hist[0] if inv_hist else {}
        d["program_5d"]     = safe(kis.get_program_trade_5d, code, default=[])
        d["short_data"]     = safe(kis.get_short_sale, code)
        d["credit_data"]    = safe(kis.get_credit_balance, code, default=[])
        d["finance_ratio"]  = safe(kis.get_finance_ratio, code)

        # DART: 최근 공시 5건
        d["dart_disclosures"] = dart.get(code, count=5)

        # 종토방: 200개 게시글 수집 (10페이지 × 20개)
        posts = safe(naver.scrape, code, 10, default=[])

        # 기존 감성 분석 (키워드 기반)
        if posts:
            sentiment = naver.analyze_sentiment(posts)
        else:
            sentiment = {"score": 50, "mood": "데이터없음", "total_posts_scanned": 0}
        d["board_sentiment"] = sentiment

        # Gemini AI 심층 분석 (제목 200개 기반)
        titles = [p.get("title","") for p in posts if p.get("title")]
        ai_result = safe(ai.analyze, titles, code, name, default=None)
        d["ai_analysis"] = ai_result  # None이면 AI 실패

        result[code] = d

        # 실시간 로딩바
        elapsed = time.time() - start_time
        eta     = (elapsed / idx) * (total - idx)
        ai_ok   = "✓" if ai_result else "✗"
        sys.stdout.write(
            f"\r⏳ [{(idx/total)*100:>5.1f}%] {idx}/{total} | "
            f"{name:16s} | AI:{ai_ok} | 경과 {elapsed/60:.1f}분 | ETA {eta/60:.1f}분"
        )
        sys.stdout.flush()

    sys.stdout.write("\n")

    # 저장
    today = datetime.now().strftime("%Y%m%d_%H%M")
    out_dir = os.path.join(ROOT_DIR, "data")
    out_path     = os.path.join(out_dir, f"top100_full_{today}.json")
    latest_path  = os.path.join(out_dir, "top100_full_latest.json")

    for path in [out_path, latest_path]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, separators=(",", ":"))

    size_mb   = os.path.getsize(out_path) / 1024 / 1024
    total_min = (time.time() - start_time) / 60

    print(f"\n✅ 수집 완료!")
    print(f"  - 종목 수  : {len(result)}개")
    print(f"  - 소요시간 : {total_min:.1f}분")
    print(f"  - 파일크기 : {size_mb:.2f} MB")
    print(f"  - 최신파일 : {latest_path}")


if __name__ == "__main__":
    main()
