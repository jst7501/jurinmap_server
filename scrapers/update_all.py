#!/usr/bin/env python3
"""
🚀 Pulse Terminal 전체 데이터 통합 파이프라인
==================================================
1. KRX CSV 파싱 → 투자자별 순매수, 공매도 잔고
2. 네이버 종토방 → 전 종목 감성 분석
3. public/data.json 에 전체 머지 (UTF-8 완벽 처리)

사용법:
  python scrapers/update_all.py              # 전체 실행 (종토방 5페이지)
  python scrapers/update_all.py --pages 3    # 종토방 3페이지
  python scrapers/update_all.py --no-board   # 종토방 제외
"""
import json
import re
import sys
import time
import argparse
import datetime
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import requests
from bs4 import BeautifulSoup
from scrapers.ai_analysis_engine import HumanIndicatorAI
DATA_DIR = ROOT / "data"
PUBLIC_JSON = ROOT / "dashboard" / "public" / "data.json"

# ────────────────────────────────────────────────────────────────────
# 헬퍼
# ────────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_public_json() -> dict:
    with open(PUBLIC_JSON, encoding="utf-8") as f:
        return json.load(f)


def save_public_json(data: dict):
    with open(PUBLIC_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"✅ public/data.json 저장 완료")


# ────────────────────────────────────────────────────────────────────
# 1. KRX CSV 파서
# ────────────────────────────────────────────────────────────────────

def parse_investor_csv() -> dict:
    """투자자별순매수.csv → 투자자 구분별 순매수 dict 반환"""
    csv_path = ROOT / "data" / "투자자별순매수.csv"
    if not csv_path.exists():
        log("⚠️  투자자별순매수.csv 없음 - 건너뜀")
        return {}

    df = pd.read_csv(csv_path, encoding="cp949")
    result = {}
    for _, row in df.iterrows():
        investor_type = str(row.get("투자자구분", "")).strip()
        if not investor_type:
            continue
        result[investor_type] = {
            "sell_qty": _num(row.get("거래량_매도")),
            "buy_qty":  _num(row.get("거래량_매수")),
            "net_qty":  _num(row.get("거래량_순매수")),
            "sell_amt": _num(row.get("거래대금_매도")),
            "buy_amt":  _num(row.get("거래대금_매수")),
            "net_amt":  _num(row.get("거래대금_순매수")),
        }
    log(f"  ✓ 투자자별순매수: {len(result)}개 투자자 유형")
    return result


def parse_short_csv() -> dict:
    """개별공매도.csv → {종목코드: 공매도 정보} dict 반환"""
    csv_path = ROOT / "data" / "개별공매도.csv"
    if not csv_path.exists():
        log("⚠️  개별공매도.csv 없음 - 건너뜀")
        return {}

    df = pd.read_csv(csv_path, encoding="cp949")
    result = {}
    for _, row in df.iterrows():
        code_raw = str(row.get("종목코드", "")).strip()
        # KRX 종목코드는 6자리 zero-padding
        code = code_raw.zfill(6)
        result[code] = {
            "name":          str(row.get("종목명", "")).strip(),
            "short_balance": _num(row.get("수량_공매도순보유잔고수량")),
            "listed_shares": _num(row.get("수량_상장주식수")),
            "short_amount":  _num(row.get("금액_공매도순보유잔고금액")),
            "market_cap":    _num(row.get("금액_시가총액")),
            "short_ratio":   float(row.get("비중", 0) or 0),
        }
    log(f"  ✓ 개별공매도: {len(result)}개 종목")
    return result


def parse_kosdaq_program_csv() -> dict:
    """코스닥프로그램매매.csv → 프로그램 매매 집계 dict"""
    csv_path = ROOT / "data" / "코스닥프로그램매매.csv"
    if not csv_path.exists():
        log("⚠️  코스닥프로그램매매.csv 없음 - 건너뜀")
        return {}
    try:
        df = pd.read_csv(csv_path, encoding="cp949")
        cols = df.columns.tolist()
        sell_col = next((c for c in cols if "매도" in c and "순" not in c), None)
        buy_col  = next((c for c in cols if "매수" in c and "순" not in c), None)
        net_col  = next((c for c in cols if "순매수" in c), None)
        result = {}
        for _, row in df.iterrows():
            category = str(row.get("구분", "")).strip()
            if not category:
                continue
            result[category] = {
                "sell_qty": _num(row.get(sell_col)) if sell_col else None,
                "buy_qty":  _num(row.get(buy_col))  if buy_col  else None,
                "net_qty":  _num(row.get(net_col))  if net_col  else None,
            }
        log(f"  ✓ 코스닥 프로그램 매매: {len(result)}개 구분 (cols: {sell_col}/{buy_col}/{net_col})")
        return result
    except Exception as e:
        log(f"  [!] 코스닥프로그램매매 파싱 오류: {e}")
        return {}


def parse_short_ratio_top_csv() -> list:
    """공매도.csv → 공매도 비중 상위 종목 Top10"""
    csv_path = ROOT / "data" / "공매도.csv"
    if not csv_path.exists():
        log("⚠️  공매도.csv 없음 - 건너뜀")
        return []
    try:
        df = pd.read_csv(csv_path, encoding="utf-8", sep="\t")
        cols = df.columns.tolist()
        # 수량_비중 또는 금액_비중 우선 탐색
        ratio_col = next((c for c in cols if "금액_비중" in c), None) \
                 or next((c for c in cols if "수량_비중" in c), None) \
                 or next((c for c in cols if "비중" in c), None)
        name_col  = next((c for c in cols if "종목명" in c), None)
        code_col  = next((c for c in cols if "종목코드" in c), None)
        if not ratio_col:
            log("  [!] 공매도 비중 컬럼 없음")
            return []
        df[ratio_col] = pd.to_numeric(df[ratio_col], errors="coerce")
        df = df.dropna(subset=[ratio_col])
        top = df.nlargest(10, ratio_col)
        result = []
        for _, row in top.iterrows():
            result.append({
                "name":  str(row.get(name_col, "")) if name_col else "",
                "code":  str(row.get(code_col, "")).strip().zfill(6) if code_col else "",
                "ratio": float(row[ratio_col]),
            })
        log(f"  ✓ 공매도 비중 상위 {len(result)}개 (컬럼: {ratio_col})")
        return result
    except Exception as e:
        log(f"  [!] 공매도 파싱 오류: {e}")
        return []


def parse_top_net_buyers_csv() -> list:
    """투자자별 순매수 상위.csv → 코스닥 순매수 상위 Top10"""
    csv_path = ROOT / "data" / "투자자별 순매수 상위.csv"
    if not csv_path.exists():
        log("⚠️  투자자별 순매수 상위.csv 없음 - 건너뜀")
        return []
    try:
        df = pd.read_csv(csv_path, encoding="cp949")
        cols = df.columns.tolist()
        name_col    = next((c for c in cols if "종목명" in c), None)
        code_col    = next((c for c in cols if "종목코드" in c), None)
        net_amt_col = next((c for c in cols if "거래대금_순매수" in c or "순매수금액" in c), None)
        net_qty_col = next((c for c in cols if "거래량_순매수" in c), None)
        if not net_amt_col:
            log("  [!] 투자자별순매수 순매수금액 컬럼 없음")
            return []
        df[net_amt_col] = pd.to_numeric(df[net_amt_col].astype(str).str.replace(",","").str.replace(" ",""), errors="coerce")
        df = df.dropna(subset=[net_amt_col])
        # 순매수 양수만 (매수 우위 종목)
        df_buy = df[df[net_amt_col] > 0]
        top10 = df_buy.nlargest(10, net_amt_col)
        result = []
        for _, row in top10.iterrows():
            net_qty = None
            if net_qty_col:
                net_qty = _num(row.get(net_qty_col))
            result.append({
                "code":    str(row.get(code_col, "")).strip().zfill(6) if code_col else "",
                "name":    str(row.get(name_col, "")) if name_col else "",
                "net_amt": float(row[net_amt_col]),
                "net_qty": net_qty,
            })
        log(f"  ✓ 코스닥 순매수 상위 {len(result)}개")
        return result
    except Exception as e:
        log(f"  [!] 투자자별순매수 파싱 오류: {e}")
        return []


def parse_top_net_sellers_csv() -> list:
    """투자자별 순매수 상위.csv → 코스닥 순매도 상위 Top10 (net_amt < 0)"""
    csv_path = ROOT / "data" / "투자자별 순매수 상위.csv"
    if not csv_path.exists():
        log("⚠️  투자자별 순매수 상위.csv 없음 - 건너뜀")
        return []
    try:
        df = pd.read_csv(csv_path, encoding="cp949")
        cols = df.columns.tolist()
        name_col    = next((c for c in cols if "종목명" in c), None)
        code_col    = next((c for c in cols if "종목코드" in c), None)
        net_amt_col = next((c for c in cols if "거래대금_순매수" in c or "순매수금액" in c), None)
        net_qty_col = next((c for c in cols if "거래량_순매수" in c), None)
        if not net_amt_col:
            return []
        df[net_amt_col] = pd.to_numeric(df[net_amt_col].astype(str).str.replace(",","").str.replace(" ",""), errors="coerce")
        df = df.dropna(subset=[net_amt_col])
        # 순매도 음수만 (매도 우위 종목)
        df_sell = df[df[net_amt_col] < 0]
        top10 = df_sell.nsmallest(10, net_amt_col)
        result = []
        for _, row in top10.iterrows():
            result.append({
                "code":    str(row.get(code_col, "")).strip().zfill(6) if code_col else "",
                "name":    str(row.get(name_col, "")) if name_col else "",
                "net_amt": float(row[net_amt_col]),
                "net_qty": _num(row.get(net_qty_col)) if net_qty_col else None,
            })
        log(f"  ✓ 코스닥 순매도 상위 {len(result)}개")
        return result
    except Exception as e:
        log(f"  [!] 투자자별순매도 파싱 오류: {e}")
        return []


def parse_short_top3_detail() -> list:
    """공매도.csv Top3 종목에 공매도지분.csv(공시 의무자) 데이터를 조인하여 상세 반환"""
    try:
        # 1) 비중 상위 Top3 종목 코드 추출
        df_short = pd.read_csv(ROOT / "data" / "공매도.csv", encoding="utf-8", sep="\t")
        cols = df_short.columns.tolist()
        ratio_col = next((c for c in cols if "금액_비중" in c), None) \
                 or next((c for c in cols if "수량_비중" in c), None) \
                 or next((c for c in cols if "비중" in c), None)
        if not ratio_col:
            return []
        df_short[ratio_col] = pd.to_numeric(df_short[ratio_col], errors="coerce")
        top3 = df_short.dropna(subset=[ratio_col]).nlargest(3, ratio_col)

        # 2) 공매도지분.csv (공시 의무자 정보)
        disclose_path = ROOT / "data" / "공매도지분.csv"
        df_dis = pd.read_csv(disclose_path, encoding="cp949") if disclose_path.exists() else pd.DataFrame()

        result = []
        for rank, (_, row_s) in enumerate(top3.iterrows(), 1):
            code = str(row_s.get("종목코드", "")).strip().zfill(6)
            name = str(row_s.get("종목명", ""))

            # 기본 공매도 수치
            item = {
                "rank":           rank,
                "code":           code,
                "name":           name,
                "ratio_qty":      float(row_s.get("수량_비중", 0) or 0),
                "ratio_amt":      float(row_s.get(ratio_col, 0) or 0),
                "short_qty":      _num(row_s.get("수량_공매도거래량_전체")),
                "total_qty":      _num(row_s.get("수량_거래량")),
                "short_amt":      _num(row_s.get("금액_공매도거래대금_전체")),
                "total_amt":      _num(row_s.get("금액_거래대금")),
                "uptick_qty":     _num(row_s.get("수량_공매도거래량_업틱룰적용")),
                "uptick_ex_qty":  _num(row_s.get("수량_공매도거래량_업틱룰예외")),
                "disclosure":     [],
            }

            # 공시 의무자 조인
            if not df_dis.empty:
                matches = df_dis[df_dis["종목코드"].astype(str).str.zfill(6) == code]
                for _, dr in matches.iterrows():
                    item["disclosure"].append({
                        "reporter":    str(dr.get("성명(법인명)", "")),
                        "biz_no":      str(dr.get("생년월일(사업자번호 등)", "")),
                        "nationality": str(dr.get("국적", "")),
                        "address":     str(dr.get("주소", "")),
                        "agent":       str(dr.get("대리인성명", "")),
                        "report_date": str(dr.get("공시의무발생일", "")),
                        "first_date":  str(dr.get("최초의무발생일", "")),
                    })
            result.append(item)
            log(f"  ✓ [{rank}위] {name} ({code}) 비중:{item['ratio_amt']:.2f}% 공시:{len(item['disclosure'])}건")

        return result
    except Exception as e:
        log(f"  [!] 공매도 Top3 상세 파싱 오류: {e}")
        return []


def _num(val):
    try:
        v = str(val).replace(",", "").strip()
        if v in ("", "nan", "NaN", "-"):
            return None
        return float(v)
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────────
# 2. 네이버 종토방 스크레이퍼
# ────────────────────────────────────────────────────────────────────

EUPHORIA_KW = [
    "가즈아", "가자", "상한가", "풀매수", "대박", "축제", "호재",
    "점상", "폭등", "익절", "저점매수", "줍줍", "불꽃", "쭉가자",
    "존버", "매집", "목표가", "급등", "강추", "올라", "뚫어", "갑니다",
]
DESPAIR_KW = [
    "한강", "망함", "손절", "개미지옥", "상폐", "떡락", "물림",
    "지옥", "설거지", "패닉", "곡소리", "살려줘", "털림", "개박살",
    "반토막", "물렸다", "개같다", "하락", "폭락", "쓰레기", "ㅠㅠ",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}


def _fetch_board_page(code: str, page: int) -> list[dict]:
    url = f"https://finance.naver.com/item/board.naver?code={code}&page={page}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        # ✅ r.content(바이트 원본)을 from_encoding 지정하여 이중 디코딩 방지
        soup = BeautifulSoup(r.content, "html.parser", from_encoding="cp949")
    except Exception as e:
        log(f"    [!] {code} p{page} 오류: {e}")
        return []

    posts = []
    for row in soup.select("table.type2 tr"):
        td_title = row.select_one("td.title a")
        if not td_title:
            continue
        cells = row.select("td")
        if len(cells) < 5:
            continue

        raw_title = td_title.text.strip()
        reply_m   = re.search(r"\[(\d+)\]$", raw_title)
        replies   = int(reply_m.group(1)) if reply_m else 0
        title     = re.sub(r"\s*\[\d+\]\s*$", "", raw_title).strip()

        # ✅ 실제 네이버 종토방 td 구조 (검증 완료):
        # cells[0]=날짜  cells[1]=제목(td.title)  cells[2]=닉네임  cells[3]=조회수  cells[4]=공감  cells[5]=비공감
        date_str = cells[0].text.strip() if len(cells) > 0 else ""
        nickname = cells[2].text.strip() if len(cells) > 2 else ""
        views_s  = cells[3].text.strip().replace(",", "") if len(cells) > 3 else "0"
        views    = int(views_s) if views_s.isdigit() else 0
        good     = cells[4].text.strip() if len(cells) > 4 else "0"
        bad      = cells[5].text.strip() if len(cells) > 5 else "0"

        posts.append({
            "title":    title,
            "date":     date_str,
            "nickname": nickname,
            "views":    views,
            "replies":  replies,
            "good":     good,
            "bad":      bad,
        })
    return posts


def fetch_stock_news(code: str, count: int = 10) -> list:
    """네이버 금융 뉴스에서 종목 최신 헤드라인 추출"""
    url = f"https://finance.naver.com/item/news_news.naver?code={code}&page=1"
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        soup = BeautifulSoup(r.content, "html.parser", from_encoding="euc-kr")
        headlines = []
        for a in soup.select("td.title a"):
            title = a.text.strip()
            if title and len(title) > 5 and title not in headlines:
                headlines.append(title)
        return headlines[:count]
    except Exception:
        return []


def scrape_board(code: str, name: str, pages: int = 5) -> dict:
    all_posts = []
    for pg in range(1, pages + 1):
        posts = _fetch_board_page(code, pg)
        all_posts.extend(posts)
        time.sleep(0.25)   # 서버 부하 방지

    titles = [p["title"] for p in all_posts]
    eu, de = 0, 0
    eu_cnt, de_cnt = {}, {}

    for t in titles:
        for kw in EUPHORIA_KW:
            if kw in t:
                eu += 1
                eu_cnt[kw] = eu_cnt.get(kw, 0) + 1
        for kw in DESPAIR_KW:
            if kw in t:
                de += 1
                de_cnt[kw] = de_cnt.get(kw, 0) + 1

    total = eu + de
    score = int(eu / total * 100) if total > 0 else 50

    mood = "중립"
    if   score >= 75: mood = "환희 🔥"
    elif score >= 60: mood = "낙관 😊"
    elif score <= 25: mood = "공포 😱"
    elif score <= 40: mood = "불안 😰"

    top_by_views = sorted(all_posts, key=lambda x: x["views"], reverse=True)[:8]

    # ✅ 무의미한 제목 필터링 (너무 짧거나 단순 감탄사/숫자만 있는 제목 제거)
    JUNK_PATTERNS = re.compile(
        r'^[\d\s\.\,\~\!\?\^\-\_\+\=]+$'  # 숫자/기호만
        r'|^(ㅋ+|ㅠ+|ㅜ+|ㅎ+|헐+|와+|아+|어+|오+|이+|음+|흠+|음+|진짜|과연|아멘|역시|그냥|좋아|맞아|단순)+$'
    )
    def is_quality_title(t):
        t = t.strip()
        if len(t) < 7:           return False   # 너무 짧은 제목
        if JUNK_PATTERNS.match(t): return False  # 기호/감탄사만
        return True

    quality_titles = [t for t in titles if is_quality_title(t)]

    result = {
        "score":               score,
        "mood":                mood,
        "total_posts_scanned": len(all_posts),
        "euphoria_count":      eu,
        "despair_count":       de,
        "top_titles":          quality_titles[:8],
        "top_hot_posts":       top_by_views,
        "top_euphoria":        sorted(eu_cnt.items(), key=lambda x: x[1], reverse=True),
        "top_despair":         sorted(de_cnt.items(), key=lambda x: x[1], reverse=True),
        "updated_at":          datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 🤖 Gemini AI 분석 레이어 추가
    try:
        news_headlines = fetch_stock_news(code, count=10)
        if news_headlines:
            log(f"    📰 [{code}] 뉴스 {len(news_headlines)}건 수집")
        ai = HumanIndicatorAI()
        ai_result = ai.analyze(titles, code, name, news_headlines=news_headlines)
        if ai_result:
            result["ai_insight"] = ai_result
            result["news_headlines"] = news_headlines  # 뉴스도 결과에 저장
            log(f"    🌟 [AI] {name} 인간지표 분석 완료 ({ai_result['sentiment_phase_kor']}, {ai_result['contrarian_signal']})")
    except Exception as e:
        log(f"    [!] {name} AI 분석 실패: {e}")

    log(f"  ✓ [{code}] {name}: {len(all_posts)}개 수집, 감성점수={score} ({mood})")
    return result


# ────────────────────────────────────────────────────────────────────
# 3. 메인 파이프라인
# ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pulse Terminal 데이터 통합 파이프라인")
    parser.add_argument("--pages",    type=int, default=10,    help="종토방 수집 페이지 수 (기본 10, 약 200개 제목)")
    parser.add_argument("--no-board", action="store_true",     help="종토방 수집 건너뜀")
    args = parser.parse_args()

    log("=" * 55)
    log("🚀 Pulse Terminal 전체 데이터 파이프라인 시작")
    log("=" * 55)

    # ── 데이터 로드 ──────────────────────────────────────────────
    data = load_public_json()
    codes = [k for k in data if k != "_macro"]
    log(f"📋 대상 종목 {len(codes)}개: {', '.join(codes)}")

    # ── 1. KRX CSV 파싱 ──────────────────────────────────────────
    log("\n[1/3] KRX CSV 파싱 중...")
    investor_data   = parse_investor_csv()
    short_data      = parse_short_csv()
    kosdaq_program  = parse_kosdaq_program_csv()
    short_ratio_top = parse_short_ratio_top_csv()
    top_net_buyers  = parse_top_net_buyers_csv()
    top_net_sellers = parse_top_net_sellers_csv()
    short_top3      = parse_short_top3_detail()

    # 공매도 데이터를 각 종목에 삽입
    for code in codes:
        if code in short_data:
            data[code]["krx_short_balance"] = short_data[code]
            log(f"  ✓ [{code}] 공매도 잔고 업데이트")

    # 전체 투자자 수급은 _macro 에 저장
    if "_macro" not in data:
        data["_macro"] = {}
    if investor_data:
        data["_macro"]["investor_trading"] = investor_data
        log(f"  ✓ 시장 전체 투자자 거래 → _macro.investor_trading")
    if kosdaq_program:
        data["_macro"]["kosdaq_program"] = kosdaq_program
        log(f"  ✓ 코스닥 프로그램 매매 → _macro.kosdaq_program")
    if short_ratio_top:
        data["_macro"]["short_ratio_top"] = short_ratio_top
        log(f"  ✓ 공매도 지분 상위 → _macro.short_ratio_top")
    if top_net_buyers:
        data["_macro"]["top_net_buyers"] = top_net_buyers
        log(f"  ✓ 코스닥 순매수 상위 → _macro.top_net_buyers")
    if top_net_sellers:
        data["_macro"]["top_net_sellers"] = top_net_sellers
        log(f"  ✓ 코스닥 순매도 상위 → _macro.top_net_sellers")
    if short_top3:
        data["_macro"]["short_top3"] = short_top3
        log(f"  ✓ 공매도 Top3 상세 → _macro.short_top3")

    # ── 2. 종토방 ────────────────────────────────────────────────
    if not args.no_board:
        log(f"\n[2/3] 네이버 종토방 수집 중 ({args.pages}페이지/종목)...")
        for code in codes:
            name = data[code].get("name", code)
            sentiment = scrape_board(code, name, pages=args.pages)
            data[code]["board_sentiment"] = sentiment
            # 각 종목 별도 저장 (백업용)
            out = DATA_DIR / f"board_sentiment_{code}.json"
            with open(out, "w", encoding="utf-8") as f:
                json.dump(sentiment, f, ensure_ascii=False, indent=2)
    else:
        log("\n[2/3] 종토방 수집 건너뜀 (--no-board)")

    # ── 3. 저장 ──────────────────────────────────────────────────
    log("\n[3/3] public/data.json 저장 중...")
    save_public_json(data)

    # 요약 리포트
    log("\n" + "=" * 55)
    log("🎉 파이프라인 완료!")
    log(f"   종목 수     : {len(codes)}개")
    log(f"   공매도 매핑 : {sum(1 for c in codes if 'krx_short_balance' in data[c])}개")
    if not args.no_board:
        log(f"   종토방 수집 : {len(codes)}개 × {args.pages}페이지")
    log("=" * 55)


if __name__ == "__main__":
    main()
