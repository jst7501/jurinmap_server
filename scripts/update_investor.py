"""
update_investor.py — 매일 실행하는 경량 수급 갱신 스크립트
=====================================================
main.py            : 주 1회 (전체 데이터 풀 수집, ~5분)
update_investor.py : 매일 (현재가 + 수급 전체 이력, ~45초)

수집 데이터:
  - 현재가 / 등락률 / 거래대금 / 신용잔고율
  - 외국인/기관/개인/기타법인/프로그램 순매수 + 매수/매도량 + 거래대금
  - 상기 데이터의 최대 20일치 이력 (FHKST01010900 output 배열 전체 활용)
  - 보유자별 보유 비중 (외국인 % 직접 제공, 기관·개인은 추정)

실행 방법:
  python scripts/update_investor.py
"""

import sys
import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import json
from datetime import datetime
from config.stock_list import TARGET_STOCKS
from collectors.kis_api import KISCollector, safe_float, safe_int
from collectors.investor_history import InvestorHistory

DEPLOY_PATH = os.path.join(ROOT_DIR, "dashboard", "public", "data.json")


def load_existing() -> dict:
    if os.path.exists(DEPLOY_PATH):
        try:
            with open(DEPLOY_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"  [경고] data.json 로드 실패: {e}")
    return {}


def save_deploy(data: dict):
    os.makedirs(os.path.dirname(DEPLOY_PATH), exist_ok=True)
    with open(DEPLOY_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"🚀 배포 완료 → {DEPLOY_PATH}")


def calc_individual(row: dict) -> dict:
    """개인 순매수가 0이면 역산으로 보완"""
    if row.get('individual', 0) == 0:
        f = row.get('foreign', 0) or 0
        inst = row.get('institution', 0) or 0
        etc = row.get('etc_org', 0) or 0
        prog = row.get('program', 0) or 0
        row['individual'] = -(f + inst + etc + prog)
    return row


def update_investor():
    date_str = datetime.now().strftime("%Y%m%d")
    print(f"\n[{date_str}] ▶ 수급 일일 갱신 시작")
    print("=" * 52)

    kis = KISCollector()
    inv_history = InvestorHistory()
    data = load_existing()

    if not data:
        print("⚠️  data.json 없음 — python main.py 먼저 실행 필요")
        return

    for code, info in TARGET_STOCKS.items():
        name = info["name"]
        print(f"\n─── {name} ({code})")
        stock = data.get(code, {})
        if not stock:
            print(f"  [스킵] 종목 데이터 없음")
            continue

        # ── 현재가 갱신 ─────────────────────────────────────────
        try:
            print("  > 현재가... ", end="", flush=True)
            price = kis.get_price(code)
            stock["price_today"] = price
            raw = price.get("_raw", {})

            # 신용잔고율 fallback
            cr = stock.get("credit_data", {})
            if not cr.get("rate_today"):
                fb = safe_float(raw.get("whol_loan_rmnd_rate", 0))
                if fb:
                    cr["rate_today"] = fb
                    stock["credit_data"] = cr

            # 보유자별 보유 비중 갱신
            listed = safe_int(raw.get("lstn_stcn", 0))    # 상장주식수
            foreign_pct = safe_float(raw.get("hts_frgn_ehrt", 0))  # 외국인 보유율%
            stock["ownership"] = {
                "listed_shares": listed,
                "foreign_pct": round(foreign_pct, 2),
                # 기관·개인 % 는 아래 수급 누적에서 근사치 계산
            }
            cp = price.get('current_price', 0)
            pct = price.get('change_pct', 0)
            print(f"완료 ({cp:,}원 {pct:+.2f}% | 외국인 보유 {foreign_pct:.2f}%)")
        except Exception as e:
            print(f"에러: {e}")
            listed = stock.get("price_today", {}).get("listed_shares", 0)

        # ── 투자자 수급 전체 이력 수집 ───────────────────────────
        try:
            print("  > 수급 이력(최대 20일)... ", end="", flush=True)
            hist_rows = kis.get_investor_history(code, max_days=20)

            saved = 0
            for row in hist_rows:
                row_date = row.get("date", "")
                if not row_date or row_date == "-":
                    continue
                row = calc_individual(row)
                # 히스토리에 저장 (날짜별로 이미 있으면 덮어쓰기)
                inv_history.update_today(code, row, row_date)
                saved += 1

            # 당일 데이터 (첫 번째 항목)
            if hist_rows:
                today_row = calc_individual(hist_rows[0].copy())
                stock["investor_today"] = today_row
                f_n = today_row.get("foreign", 0)
                i_n = today_row.get("institution", 0)
                p_n = today_row.get("individual", 0)
                b_f = today_row.get("foreign_buy", 0)
                s_f = today_row.get("foreign_sell", 0)
                print(f"완료 ({saved}일치 | 외={f_n:+,} 기={i_n:+,} 개={p_n:+,} | 외국인 매수={b_f:,}/매도={s_f:,})")
            else:
                print("0 (장 중 or 미수신)")

        except Exception as e:
            print(f"에러: {e}")

        # ── investor_5d 히스토리에서 재구성 ─────────────────────
        h5d = inv_history.get_5d(code)
        stock["investor_5d"] = [calc_individual(r.copy()) for r in h5d]

        # ── investor_20d (전체 이력 — 상세 차트용) ──────────────
        h20d = inv_history.get_nd(code, 20)
        stock["investor_20d"] = [calc_individual(r.copy()) for r in h20d]

        # ── 보유 비중 근사 계산 (누적 순매수 기반) ──────────────
        if listed and h20d:
            cum_foreign = sum(r.get("foreign", 0) for r in h20d)
            cum_inst = sum(r.get("institution", 0) for r in h20d)
            cum_indiv = sum(r.get("individual", 0) for r in h20d)
            cum_etc = sum(r.get("etc_org", 0) for r in h20d)
            ownership = stock.get("ownership", {})
            ownership["20d_cum_foreign"] = cum_foreign
            ownership["20d_cum_institution"] = cum_inst
            ownership["20d_cum_individual"] = cum_indiv
            ownership["20d_cum_etc"] = cum_etc
            ownership["20d_foreign_flow_pct"] = round(cum_foreign / listed * 100, 3) if listed else 0
            ownership["20d_institution_flow_pct"] = round(cum_inst / listed * 100, 3) if listed else 0
            ownership["20d_individual_flow_pct"] = round(cum_indiv / listed * 100, 3) if listed else 0
            stock["ownership"] = ownership

        data[code] = stock

    # 히스토리 영구 저장
    inv_history.save()
    print(f"\n💾 히스토리 저장 → data/investor_history.json")

    save_deploy(data)
    print(f"\n✅ 수급 갱신 완료!")


if __name__ == "__main__":
    update_investor()
