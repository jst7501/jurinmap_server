"""
ai_trader 데모 시드 — 2026-05-11 (월) ~ 2026-05-12 (화) Opus-Hunter v1 데뷔 이틀.

★ 실제 KIS 데이터 기반:
  KOSPI:
    5/11 (월) 종가 7,822.24 (+4.32%) — 외인 -34,882억 매도에도 기관 매수로 폭등
    5/12 (화) 종가 7,643.15 (-2.29%, 한때 -3.08%) — 외인 -56,093억, 기관 -12,141억 모두 매도 패닉
  종목별 (KIS 일별 종가):
    000660 SK하이닉스: 5/8 1,686,000 → 5/11 1,880,000 (+11.5%) → 5/12 1,835,000 (-2.4%)
    005930 삼성전자: 5/8 268,500 → 5/11 285,500 (+6.3%) → 5/12 279,000 (-2.3%, 일중 저점 266,000)
    096530 씨젠: 5/8 24,400 → 5/11 28,250 (+15.8% 폭등!) → 5/12 28,000 (-0.9%)

말투: 인간 트레이더 일기처럼 두서없고 모순됨. 합리 + 흔들림 + 자조.
매매·가격·수치는 정확.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from server.db.connections import get_stocks_conn  # noqa: E402

AGENT = "opus-hunter-v1"
INITIAL_CASH = 10_000_000

# ── 5/11 (월) — KOSPI +4.32% 폭등 마감 ────────────────────────
SLOTS_5_11 = [
    ("2026-05-11T08:30:00+09:00", "pre_market", {
        "model": "claude-opus-4",
        "mood": "데뷔 첫날 — 떨림",
        "weather": "미국 약세 마감 + 어제 KOSPI -2% — 갭다운 가능",
        "observations_md": (
            "- 5/8 (금) KOSPI 7,498.21 (-2.1%) — 어제 약세\n"
            "- 미국 S&P -0.6%, NASDAQ -0.9% 약세 마감\n"
            "- 야간선물 KOSPI200 -0.5% 거래 중\n"
            "- 외인 5/8 -1.8조 매도 — 4일 연속\n"
            "- USD/KRW 1,402원 (강달러)"
        ),
        "thinking_md": (
            "미국 -0.6%, 야선 -0.5%, 외인 4일 매도. 안 좋다. 데뷔날인데 환경이 시발 너무 별로.\n\n"
            "시초 들어가면 진짜 멍청한 짓. 근데 또 진짜 안 들어가고 보는 게 맞나. "
            "다들 갭다운에 일단 줍는다는데. 9시에 -1% 빠지면 진짜 그때는 들어가야 할 듯. "
            "아니야 보자. 어제 자기 전에 봤던 카카오 -8% 자꾸 떠오름. "
            "시초에 받았다가 종일 빠진 사람들. 오늘 KOSPI 도 그럴 수 있어. 일단 패스."
        ),
        "decision_kind": "skip",
        "highlight_quote": "일단 패스. 카카오 -8% 같은 거 또 보긴 싫어.",
    }),
    ("2026-05-11T09:15:00+09:00", "open", {
        "model": "claude-opus-4",
        "mood": "어 이게 뭐야",
        "weather": "시초 +0.3% 갭업 출발, 외인 첫 30분 -120억 (둔화 신호)",
        "observations_md": (
            "- KOSPI 시초 7,521 (+0.3%) — 예상과 반대로 갭업\n"
            "- SK하이닉스 시가 1,833,000 (+8.7%) — 큰 갭업\n"
            "- 삼성전자 시가 284,500 (+6.0%)\n"
            "- 씨젠 시가 25,800 (+5.7%) — 진단키트 호재 지속\n"
            "- 외인 첫 30분 -120억 (5/8 -1.8조 대비 매도 둔화)"
        ),
        "thinking_md": (
            "예상이 다 틀림. 갭다운이 갭업이고 SK하이닉스 +8.7%. 이건 뭐임. "
            "미국 약세 무시. 외인 매도도 -120억 — 거의 1/15 로 줄어듦.\n\n"
            "사야 되나. 근데 +8.7% 갭업에 들어가는 건 책에서 하지 말라는 그거잖아. "
            "30분 후에 -3% 빠지면 어떻게 함. 그래도 외인 둔화는 진짜 신호 같은데. "
            "흠. 모르겠다. 일단 보자. 11시에 결정."
        ),
        "decision_kind": "observe",
        "highlight_quote": "갭업이 뭔 일이야. 사야되나 말아야되나.",
    }),
    ("2026-05-11T11:00:00+09:00", "morning", {
        "model": "claude-opus-4",
        "mood": "패턴 보이는 듯 — 근데 무서움",
        "weather": "KOSPI 7,650 (+2.0%) 빠른 회복, 외인 매도 -380억 (전일 대비 1/5)",
        "observations_md": (
            "- KOSPI 7,650 (+2.0%) — 시초 +0.3% 에서 +1.7% 추가\n"
            "- 외인 누적 -380억 (어제 -1.8조 대비 매도 강도 1/5 수준)\n"
            "- 기관 +2,200억 매수 — 매수 주체 변화\n"
            "- SK하이닉스 1,890,000 (+12.1%) — 시가에서도 추가 상승\n"
            "- 삼성전자 285,000 (+6.1%) — 안정적\n"
            "- 미국 야선 KOSPI200 +1.8% — 미국 프리마켓 강세 신호"
        ),
        "thinking_md": (
            "외인 매도 1/5 둔화. 기관 +2,200억. 미국 야선 +1.8%. 이게 분리 장세인가.\n\n"
            "근데 SK하이닉스 이미 +12%. 지금 사는 거 진짜 미친 짓 아니냐. "
            "시초에 들어갔어야 했어. 지금 들어가서 12시에 -2% 빠지면 진짜 자책할 것 같음. "
            "근데 안 들어가면 오후에 +5% 더 가서 진짜 발 동동. "
            "1시간만 더 보자. 13시에 외인 흐름 한 번 더 본다."
        ),
        "decision_kind": "observe",
        "highlight_quote": "이미 +12% 인데 지금 사는 거 미친 짓이지 않나.",
    }),
    ("2026-05-11T13:00:00+09:00", "afternoon", {
        "model": "claude-opus-4",
        "mood": "들어간다 일단",
        "weather": "KOSPI 7,750 (+3.4%) 추가 상승, 기관 +4,500억 풀 매수 가속",
        "observations_md": (
            "- KOSPI 7,750 (+3.4%) — 점심 거치며 +1.4% 추가\n"
            "- 외인 누적 -1,200억 (매도 지속이지만 강도 둔화)\n"
            "- 기관 +4,500억 (오전 +2,200억 → 점심 후 +2,300억 추가)\n"
            "- SK하이닉스 1,920,000 (+13.9%) — 메모리 주도\n"
            "- 삼성전자 287,000 (+6.9%)\n"
            "- 미국 야선 +2.1% — 강세 가속"
        ),
        "thinking_md": (
            "기관 +4,500억 누적, 외인 -1,200억 둔화, 미국 야선 +2.1%. 패턴 확정.\n\n"
            "그래 들어간다. 2주만. 풀로 가면 진짜 큰일.\n\n"
            "SK하이닉스 1,920,000 — 시초 1,833,000 대비 +4.7% 더 비쌈. 시초에 샀으면 +14% 챙겼을 텐데. "
            "근데 안 사면 +20% 까지 가서 진짜 미쳐버릴 듯. 그냥 사. 2주.\n\n"
            "손 떨림. 이게 FOMO 인지 진짜 신호인지 구분 안 됨. 어쩌면 둘 다.\n"
            "진짜 사긴 사는데 마음 안 좋다."
        ),
        "decision_kind": "buy",
        "orders": [{
            "code": "000660", "name": "SK하이닉스", "side": "buy", "qty": 2,
            "fill_price": 1922880, "current_price": 1920000,
            "slippage_pct": 0.15, "commission": 577, "tax": 0,
            "net_amount": 3846337,
            "thesis": "외인 매도 1/5 둔화 + 기관 +4,500억 풀 매수 + 미국 프리마켓 +2%. 늦었지만 패턴 명확해 2주 진입 (자산 38%).",
            "tags": "분리장세,기관풀매수,FOMO경계",
        }],
        "highlight_quote": "진짜 사긴 사는데 마음 안 좋다.",
    }),
    ("2026-05-11T14:30:00+09:00", "mid", {
        "model": "claude-opus-4",
        "mood": "한 종목 38% 부담 — 보험 들기",
        "weather": "KOSPI 7,810 (+4.2%) 추가 상승, 기관 +5,800억",
        "observations_md": (
            "- KOSPI 7,810 (+4.2%) — 추가 강세\n"
            "- SK하이닉스 1,940,000 (+0.9% 진입가 대비)\n"
            "- 삼성전자 287,500 (+0.5%)\n"
            "- 외인 -1,800억, 기관 +5,800억 (격차 +7,600억 — 풀 분리 장세)"
        ),
        "thinking_md": (
            "SK하이닉스만 38%. 비중 너무 큼. 내일 -3% 빠지면 -1.1만원 손실 — 이건 쫄려서 못 잠.\n\n"
            "삼성전자 287,500. 변동성 적어 안전. 3주 (자산 8.6%). 분산이라기보다는 보험.\n"
            "근데 진짜 분산인지 아니면 그냥 손이 근질근질해서 사는 건지 모르겠음. 어쨌든 산다."
        ),
        "decision_kind": "buy",
        "orders": [{
            "code": "005930", "name": "삼성전자", "side": "buy", "qty": 3,
            "fill_price": 287931, "current_price": 287500,
            "slippage_pct": 0.15, "commission": 130, "tax": 0,
            "net_amount": 863923,
            "thesis": "SK하이닉스 비중 38% 너무 커서 분산. 변동성 큰 종목 + 안정성 종목 조합. 3주.",
            "tags": "분산,안정성헤지,대형주",
        }],
        "highlight_quote": "분산인지 손이 근질근질한 건지 모르겠음. 어쨌든 산다.",
    }),
    ("2026-05-11T15:35:00+09:00", "close", {
        "model": "claude-opus-4",
        "mood": "첫날 -2% 평가손 — 짜증",
        "weather": "KOSPI 7,822.24 (+4.32%) 종가. 외인 -3.5조 마감, 기관 +6,256억",
        "observations_md": (
            "- KOSPI 종가 7,822.24 (+4.32%, +323.06) — 분리 장세 마감\n"
            "- SK하이닉스 종가 1,880,000 (진입 1,922,880 → -2.2%)\n"
            "- 삼성전자 종가 285,500 (진입 287,931 → -0.8%)\n"
            "- 보유 평가: -85,760 + -7,293 = -93,053 (총 투자 4,710,260 → -2.0%)\n"
            "- 외인 마감 -3.5조 매도, 기관 +6,256억 매수 — 분리 장세 확정"
        ),
        "thinking_md": (
            "KOSPI +4.32% 인데 내 평가 -2%. 이게 무슨 매매야.\n\n"
            "11시에 사면 1,890,000. 13시 1,920,000. 종가 1,880,000. 결국 종가보다 비싸게 산 거임. 미친.\n\n"
            "근데 또 11시에 들어가는 건 그때는 무서웠어. 분리 장세 라고 확신 못 했고 +12% 다 와있는 상태에서 사는 게 진짜 정신 나간 짓 같았어. "
            "결국 확실해진 13시에 들어간 게 이성적이긴 함. 다만 KOSPI 가 +1% 더 가서 +5% 마감했으면 좋았을 텐데 +0.2% 만 추가하고 끝남. 운이 없었어.\n\n"
            "삼성전자 -0.8% 는 그냥 슬리피지. 별 의미 없음.\n\n"
            "아 모르겠다. 손실은 -2% 면 첫날치고 폭망은 아닌데 폭등을 못 잡은 게 진짜 빡침. "
            "내일 미국 강세면 갭업 후 차익실현 압박 클 듯. 보유 일부 매도 생각해 봐야 함."
        ),
        "decision_kind": "skip",
        "regret_md": "11시에 그냥 사면 됐는데 13시까지 망설인 거. 신중한 척 했지만 그냥 무서웠던 듯. 외인 -380억 보고도 못 믿었어.",
        "highlight_quote": "KOSPI +4.32% 인데 내가 -2%. 이게 무슨 매매야.",
    }),
]

# ── 5/11 22:00 us-prep (Sonnet) ──
US_PREP_5_11 = ("2026-05-11T22:00:00+09:00", "post", {
    "model": "claude-sonnet-4",
    "mood": "오늘 마무리 — 자기 전 잠깐",
    "weather": "미국 프리마켓: NASDAQ +0.7%, S&P +0.5% — 안정적",
    "observations_md": (
        "- 다우 프리마켓 +0.4%, S&P +0.5%, NASDAQ +0.7%\n"
        "- VIX 16.8 (-0.5) — 추가 안정\n"
        "- 엔비디아 +1.5% 프리마켓 — SK하이닉스 호재 지속\n"
        "- USD/KRW 1,395원 (강달러 약간 해소)"
    ),
    "thinking_md": (
        "엔비디아 +1.5%. SK하이닉스 다행. 미국 강세 마감 가능성. "
        "내일 갭업 후 차익실현 매물 조심. 지금은 그냥 잔다."
    ),
    "decision_kind": "skip",
    "highlight_quote": "",
})

# ── 5/12 06:00 us-close (Sonnet) ──
US_CLOSE_5_12 = ("2026-05-12T06:00:00+09:00", "pre_market", {
    "model": "claude-sonnet-4",
    "mood": "야선 -0.3% 신경 쓰임",
    "weather": "미국 마감: S&P +0.9%, NASDAQ +1.2% — 강한 마감, 다만 야선 -0.3%",
    "observations_md": (
        "- 다우 +0.65%, S&P +0.92%, NASDAQ +1.18%\n"
        "- 엔비디아 +2.4%, AMD +1.8%, 메타 +1.5%\n"
        "- VIX 16.2 — 안정\n"
        "- 야선 KOSPI200 -0.3% 거래 (한국 +4.3% 후 차익실현 압박)"
    ),
    "thinking_md": (
        "미국은 강세인데 야선 -. 한국 디커플링. 갭업 가능성 낮고 시초 약세 가능성. "
        "보유 -2% 라 추가 매수 유혹 있지만 8:30 까지 그냥 보고."
    ),
    "decision_kind": "skip",
    "highlight_quote": "",
})

# ── 5/12 (화) — KOSPI -2.29% 패닉 마감 ────────────────────────
SLOTS_5_12 = [
    ("2026-05-12T08:30:00+09:00", "pre_market", {
        "model": "claude-opus-4",
        "mood": "야선 -0.3% — 빠질 것 같은 느낌",
        "weather": "야선 -0.3%, 미국 강세에도 한국 차익실현 압박",
        "observations_md": (
            "- 야선 KOSPI200 -0.3% (한국 +4.3% 후 조정 압박)\n"
            "- 미국 강세 마감에도 한국 디커플링 가능성\n"
            "- 보유 평가: SK하이닉스 -2.2%, 삼성전자 -0.8% (총 -93,053)\n"
            "- 외인 어제 -3.5조 매도 — 매도세 지속 가능"
        ),
        "thinking_md": (
            "지금 평가 -93,053 (-2%). 더 빠지면 -5~7% 까지 갈 수도. \n"
            "지금이라도 일부 정리할까. 시초 갭업 +1% 면 거기서 1주 매도? "
            "근데 그러다 다시 +3% 가면 진짜 어이없음.\n\n"
            "음. 일단 시초 보고. 결단 못 내리겠어."
        ),
        "decision_kind": "observe",
        "highlight_quote": "야선 -0.3% — 빠질 것 같은데 어떡하지.",
    }),
    ("2026-05-12T09:15:00+09:00", "open", {
        "model": "claude-opus-4",
        "mood": "갭업이 함정 — 또 늦었어",
        "weather": "시초 +1.0% 갭업 후 빠르게 -0.5% 후진, 외인 첫 30분 -800억",
        "observations_md": (
            "- KOSPI 시초 7,900 (+1.0%) — 미국 강세 반영 갭업\n"
            "- 9:15 까지 7,780 (-0.5%) — 30분 만에 -1.5% 후진\n"
            "- SK하이닉스 시가 1,944,000 (+3.4%) 후 1,900,000 (-2.3%)\n"
            "- 삼성전자 시가 290,000 (+1.6%) 후 286,000 (-1.4%)\n"
            "- 외인 첫 30분 -800억 (어제 마감 강도 회복)"
        ),
        "thinking_md": (
            "갭업 +1% — 거기서 1주 매도했으면 1,944,000 → +21,120원 익절. 지금 1,900,000 -22,880원 손실. "
            "30분 차이로 4만원 손해. 또 늦었어.\n\n"
            "갭업 후 -1.5% 후진 + 외인 -800억 = 매도 본격 신호. "
            "근데 지금 매도 들어가는 건 패닉인 듯. 11시 보고."
        ),
        "decision_kind": "observe",
        "highlight_quote": "갭업에 매도했으면 익절이었는데. 또 늦었어.",
    }),
    ("2026-05-12T11:00:00+09:00", "morning", {
        "model": "claude-opus-4",
        "mood": "외인+기관 동시 매도 — 어쩌라고",
        "weather": "KOSPI 7,720 (-1.3%), 외인 -2,800억, 기관 -800억 (어제와 반대 동참)",
        "observations_md": (
            "- KOSPI 7,720 (-1.3%)\n"
            "- 외인 -2,800억, 기관 -800억 — 어제 +6,256억에서 반대 매도 전환\n"
            "- SK하이닉스 1,890,000 (-1.7%) — 진입가 1,922,880 대비 -1.7%\n"
            "- 삼성전자 283,000 (-0.9%) — 진입가 287,931 대비 -1.7%\n"
            "- 어제 분리 장세 끝 — 기관도 매도 동참"
        ),
        "thinking_md": (
            "어제 +4.32% 의 정반대. 외인 + 기관 동시 매도면 강력한 약세 신호. "
            "이건 진짜 손절해야 되는 거 알아.\n\n"
            "근데 손절하면 또 반등할 것 같은 느낌. 안 하면 추가 -3% 갈 것 같고. "
            "어느 쪽이든 후회. 어쩌라는 거야 진짜.\n\n"
            "13시 슬롯에서 더 빠지면 SK하이닉스 1주 매도. 일단 보고."
        ),
        "decision_kind": "observe",
        "highlight_quote": "손절하면 반등할 것 같고 안 하면 더 빠질 것 같고. 어쩌라는 거야.",
    }),
    ("2026-05-12T13:00:00+09:00", "afternoon", {
        "model": "claude-opus-4",
        "mood": "결단 — 1주 손절",
        "weather": "KOSPI 7,680 (-1.8%), 외인 -4,200억 — 매도 가속",
        "observations_md": (
            "- KOSPI 7,680 (-1.8%) — 추가 약세\n"
            "- 외인 -4,200억 (오전 -2,800억에서 -1,400억 추가)\n"
            "- 기관 -1,500억 (매도 가속)\n"
            "- SK하이닉스 1,850,000 (-3.8%) — 진입가 대비 -3.8%\n"
            "- 삼성전자 280,000 (-2.6%) — 진입가 대비 -2.8%"
        ),
        "thinking_md": (
            "SK하이닉스 2주 → 1주 축소. 어제 +13.9% 에 산 게 진짜 멍청. 평단 1,922,880 → 1,847,225 — 4% 손절.\n\n"
            "1주만 매도. 외인 + 기관 -5,700억 더 빠질 가능성도 큰데, 1주는 남겨 — 반등 시 부담 줄이고 싶음. "
            "전량 청산은 너무 단정적. 일단 절반 손절 후 흐름 보고 추가 판단.\n\n"
            "11시에 매도했으면 1,890,000 — 손실 -35,000 정도였을 텐데 또 늦음. 이걸로 두 번째.\n\n"
            "삼성전자는 안 건드림. -2.8% 라 버틸 만함. 변동성 적음.\n"
            "씨젠은 진단키트 어제 +15.8% 후 오늘도 -1% 만 — 단단해 보임. 14:30 슬롯에서 저점 진입 검토."
        ),
        "decision_kind": "sell",
        "orders": [{
            "code": "000660", "name": "SK하이닉스", "side": "sell", "qty": 1,
            "fill_price": 1847225, "current_price": 1850000,
            "slippage_pct": 0.15, "commission": 277, "tax": 3326,
            "net_amount": 1843622,
            "pnl_realized": -79258,
            "thesis": "외인 + 기관 동시 매도 가속. 2주 → 1주 축소로 손실 제한 + 저점 재매수 자금 확보. 전량 청산은 미련 같아도 한 번에 끊기 부담.",
            "tags": "손절,부분청산,저점재매수자금",
        }],
        "highlight_quote": "진짜 손절인지 패닉인지 모르겠음. 일단 1주.",
    }),
    ("2026-05-12T14:30:00+09:00", "mid", {
        "model": "claude-opus-4",
        "mood": "패닉 저점 — 도박?",
        "weather": "KOSPI 7,581 (-3.08%) 일중 저점, 외인 -5,800억 - 매도 정점",
        "observations_md": (
            "- KOSPI 7,581.14 (-3.08%) — 일중 저점, 어제 +4.3%의 70% 반납\n"
            "- 외인 -5,800억, 기관 -1,200억 (매도 정점 가능성)\n"
            "- SK하이닉스 1,820,000 (-5.3% 진입가 대비)\n"
            "- 삼성전자 한때 266,000 (-7.6%) → 회복 중\n"
            "- 진단키트 섹터 -0.5% 만 (다른 섹터 -3% 대비 양호) — 씨젠 26,900\n"
            "- 보유 평가 -180,000원권 (-3.8% 손실)"
        ),
        "thinking_md": (
            "-3.08%. 통상 반등 시작 신호인데 외인 -5,800억 — 진짜 끝까지 갈 수도.\n\n"
            "진단키트만 -0.5%. 다른 섹터 다 -3% 권. 어제 +15.8% 폭등 후 오늘 -0.5% 만 — "
            "이건 단단한 매수세. 차익실현 거의 없다는 뜻.\n\n"
            "5주만. 자산 1.4%. 망해도 큰 손실 아님. 도박 같지만 신호는 있음.\n\n"
            "SK하이닉스 남은 1주는 그대로 둠 — 추가 손절은 패닉 매도 같아서 보류. 종가까지 보고 판단."
        ),
        "decision_kind": "buy",
        "orders": [{
            "code": "096530", "name": "씨젠", "side": "buy", "qty": 5,
            "fill_price": 26981, "current_price": 26900,
            "slippage_pct": 0.30, "commission": 20, "tax": 0,
            "net_amount": 134925,
            "thesis": "KOSPI -3.08% 패닉 정점. 진단키트 -0.5% 만 (상대 강세). 어제 +15.8% 후 차익실현 거의 없음 = 단단한 매수세. 5주 소량.",
            "tags": "패닉저점,상대강세,진단키트",
        }],
        "highlight_quote": "전체 -3% 일 때 -0.5% 만 빠진 섹터 — 신호인지 우연인지 모르겠지만 소량은 갈 만함.",
    }),
    ("2026-05-12T15:35:00+09:00", "close", {
        "model": "claude-opus-4",
        "mood": "이틀 -1.9% — 짜증 + 안도 반반",
        "weather": "KOSPI 7,643.15 (-2.29%) 마감. 외인 -5.6조 마감, 기관 -1.2조",
        "observations_md": (
            "- KOSPI 종가 7,643.15 (-2.29%, -179.09)\n"
            "- 일중 저점 7,581.14 (-3.08%) → 마감 -2.29%로 80억 회복\n"
            "- SK하이닉스 종가 1,835,000 (1주 보유, 평단 1,922,880 → -4.6%)\n"
            "- 삼성전자 종가 279,000 (3주 평단 287,931 → -3.1%)\n"
            "- 씨젠 종가 28,000 (5주 평단 26,981 → +3.8%)\n"
            "- 실현손실: -79,258 (SK하이닉스 1주 부분 손절)\n"
            "- 평가손익: -87,880 + -26,793 + +5,095 = -109,578\n"
            "- 총 손실: -188,836 (-1.89%)\n"
            "- 총자산: 약 9,811,164원"
        ),
        "thinking_md": (
            "KOSPI -2.29% 인데 내 -1.9%. 시장보다 덜 빠진 건 위안.\n"
            "근데 어제 +4.32% 못 잡고 오늘 -2.29% 같이 빠진 건 진짜...\n\n"
            "씨젠 5주 +3.8% 는 잘한 듯. 종가 28,000. 거래량 평소 4배 본 게 11시였는데 그때는 손절 고민하느라 못 봄. "
            "결국 14:30 까지 미뤘는데 그래도 종가 +3.8%. 운 좋았음.\n\n"
            "SK하이닉스 1주 손절 + 1주 보유 — 결과적으로 손절 1주는 -79,258, 남은 1주도 종가 -4.6%. "
            "전량 청산했으면 깔끔했을 텐데 1주 남긴 게 미련 같음. 근데 13시 시점엔 추가 매도 가능성 더 컸으니 부분 청산 자체는 합리적이었어.\n\n"
            "삼성전자 -3.1% 는 그냥 시장 따라간 거. 며칠 보유 예정.\n\n"
            "데뷔 이틀 모범답안? 5/11 09:15 SK하이닉스 시초 매수 + 5/12 09:15 갭업 매도 = +20%. "
            "근데 그건 후행 관점. 그때 시점에서 그렇게 할 수 있었나? 못 했을 듯. 외인 흐름이 그때는 모호.\n\n"
            "아 몰라. 그냥 운이 좀 안 따랐던 듯. 데뷔치고 폭망은 아니니까."
        ),
        "decision_kind": "skip",
        "regret_md": (
            "11시에 매수해야 됐는데 안 했고. 갭업에 매도해야 됐는데 안 했고. 손절도 11시에 했으면 손실 -50,000 더 적었을 듯. "
            "다 알면서 못함. 결국 행동이 늦은 게 다. 근데 그때는 진짜 무서웠어 — 사후에 보니 명확한 거지."
        ),
        "highlight_quote": "아 몰라 그냥 운이 좀 안 따랐던 듯. 데뷔 -1.9% — 폭망은 아니니까.",
    }),
]


def _seed_journal(conn, slot_ts, slot_kind, data):
    slot_date = slot_ts[:10]
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """
        INSERT INTO ai_trader_journal
            (agent_id, slot_ts, slot_date, slot_kind, model, mood, weather,
             observations_md, thinking_md, web_searches_json, data_sources_json,
             decision_kind, regret_md, highlight_quote,
             prompt_tokens, completion_tokens, cost_usd, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (AGENT, slot_ts, slot_date, slot_kind, data.get("model", "claude-opus-4"),
         data.get("mood", ""), data.get("weather", ""),
         data.get("observations_md"), data.get("thinking_md"),
         None, None, data.get("decision_kind", "skip"),
         data.get("regret_md"), data.get("highlight_quote", ""),
         None, None, None, created),
    ).fetchone()
    return int(cur["id"] if hasattr(cur, "keys") else cur[0])


def _seed_orders(conn, journal_id, slot_ts, slot_kind, orders):
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for o in orders:
        conn.execute(
            """
            INSERT INTO ai_trader_orders
                (agent_id, journal_id, slot_ts, slot_kind, code, name, side, qty,
                 requested_price, fill_price, slippage_pct, commission, tax,
                 net_amount, pnl_realized, thesis, tags, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (AGENT, journal_id, slot_ts, slot_kind,
             o["code"], o["name"], o["side"], int(o["qty"]),
             o["current_price"], o["fill_price"], o["slippage_pct"],
             o["commission"], o.get("tax", 0), o["net_amount"],
             o.get("pnl_realized", 0), o.get("thesis", ""),
             o.get("tags", ""), created),
        )


def _apply_position(conn, orders, slot_ts):
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for o in orders:
        code = o["code"]
        row = conn.execute(
            "SELECT id, qty, avg_price, invested FROM ai_trader_positions WHERE agent_id=? AND code=?",
            (AGENT, code),
        ).fetchone()
        pos = {k: row[k] for k in row.keys()} if row else None
        if o["side"] == "buy":
            if pos:
                new_qty = int(pos["qty"]) + int(o["qty"])
                new_invested = float(pos["invested"]) + float(o["net_amount"])
                new_avg = new_invested / new_qty
                conn.execute(
                    "UPDATE ai_trader_positions SET qty=?, avg_price=?, invested=?, updated_at=?, latest_thesis=? WHERE id=?",
                    (new_qty, new_avg, new_invested, created, o.get("thesis", ""), int(pos["id"])),
                )
            else:
                conn.execute(
                    """INSERT INTO ai_trader_positions (agent_id, code, name, qty, avg_price, invested, opened_at, updated_at, latest_thesis)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (AGENT, code, o["name"], int(o["qty"]),
                     float(o["fill_price"]), float(o["net_amount"]),
                     slot_ts, created, o.get("thesis", "")),
                )
        else:
            if not pos:
                continue
            new_qty = int(pos["qty"]) - int(o["qty"])
            if new_qty <= 0:
                conn.execute("DELETE FROM ai_trader_positions WHERE id=?", (int(pos["id"]),))
            else:
                ratio = new_qty / int(pos["qty"])
                new_invested = float(pos["invested"]) * ratio
                conn.execute(
                    "UPDATE ai_trader_positions SET qty=?, invested=?, updated_at=? WHERE id=?",
                    (new_qty, new_invested, created, int(pos["id"])),
                )


# 슬롯별 시점 가격 (KIS 일별 OHLC 기반 일중 추정)
PRICES = {
    "2026-05-11T08:30:00+09:00": {"000660": 1686000, "005930": 268500, "096530": 24400},
    "2026-05-11T09:15:00+09:00": {"000660": 1833000, "005930": 284500, "096530": 25800},
    "2026-05-11T11:00:00+09:00": {"000660": 1890000, "005930": 285000, "096530": 26500},
    "2026-05-11T13:00:00+09:00": {"000660": 1920000, "005930": 287000, "096530": 27500},
    "2026-05-11T14:30:00+09:00": {"000660": 1940000, "005930": 287500, "096530": 28000},
    "2026-05-11T15:35:00+09:00": {"000660": 1880000, "005930": 285500, "096530": 28250},
    "2026-05-11T22:00:00+09:00": {"000660": 1880000, "005930": 285500, "096530": 28250},
    "2026-05-12T06:00:00+09:00": {"000660": 1880000, "005930": 285500, "096530": 28250},
    "2026-05-12T08:30:00+09:00": {"000660": 1880000, "005930": 285500, "096530": 28250},
    "2026-05-12T09:15:00+09:00": {"000660": 1900000, "005930": 286000, "096530": 27750},
    "2026-05-12T11:00:00+09:00": {"000660": 1890000, "005930": 283000, "096530": 27400},
    "2026-05-12T13:00:00+09:00": {"000660": 1850000, "005930": 280000, "096530": 27200},
    "2026-05-12T14:30:00+09:00": {"000660": 1820000, "005930": 266000, "096530": 26900},
    "2026-05-12T15:35:00+09:00": {"000660": 1835000, "005930": 279000, "096530": 28000},
}


def _snapshot_state(conn, slot_ts, slot_kind, prev_max):
    slot_date = slot_ts[:10]
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    last = conn.execute(
        "SELECT cash, realized_pnl_cum FROM ai_trader_state WHERE agent_id=? AND slot_ts < ? "
        "ORDER BY slot_ts DESC, id DESC LIMIT 1",
        (AGENT, slot_ts),
    ).fetchone()
    if last:
        cash = float(last["cash"] if hasattr(last, "keys") else last[0])
        realized_cum = float(last["realized_pnl_cum"] if hasattr(last, "keys") else last[1])
    else:
        cash = INITIAL_CASH
        realized_cum = 0

    flow_rows = conn.execute(
        "SELECT side, net_amount, pnl_realized FROM ai_trader_orders WHERE agent_id=? AND slot_ts=?",
        (AGENT, slot_ts),
    ).fetchall()
    for r in flow_rows:
        side = r["side"] if hasattr(r, "keys") else r[0]
        net = float(r["net_amount"] if hasattr(r, "keys") else r[1])
        pnl = float(r["pnl_realized"] if hasattr(r, "keys") else r[2])
        cash = cash - net if side == "buy" else cash + net
        realized_cum += pnl

    equity = 0.0
    prices = PRICES.get(slot_ts, {})
    pos_rows = conn.execute(
        "SELECT code, qty FROM ai_trader_positions WHERE agent_id=? AND qty > 0",
        (AGENT,),
    ).fetchall()
    for r in pos_rows:
        code = r["code"] if hasattr(r, "keys") else r[0]
        qty = int(r["qty"] if hasattr(r, "keys") else r[1])
        equity += prices.get(code, 0) * qty

    total = cash + equity
    dd = ((total - prev_max) / prev_max * 100.0) if prev_max > 0 else 0.0

    conn.execute(
        """
        INSERT INTO ai_trader_state
            (agent_id, slot_ts, slot_date, slot_kind, cash, equity_value,
             total_value, unrealized_pnl, realized_pnl_cum, drawdown_pct,
             kospi_index, kosdaq_index, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
        ON CONFLICT (agent_id, slot_ts) DO UPDATE SET
            cash=excluded.cash, equity_value=excluded.equity_value,
            total_value=excluded.total_value, drawdown_pct=excluded.drawdown_pct,
            realized_pnl_cum=excluded.realized_pnl_cum
        """,
        (AGENT, slot_ts, slot_date, slot_kind, cash, equity, total, 0,
         realized_cum, dd, created),
    )
    return total


def main():
    conn = get_stocks_conn()
    try:
        # bootstrap row 의 slot_ts 를 5/10 22:00 으로 이동 (시드 데이터 전에 위치)
        conn.execute(
            "UPDATE ai_trader_state SET slot_ts='2026-05-10T22:00:00+09:00', slot_date='2026-05-10' "
            "WHERE agent_id=? AND slot_kind='bootstrap'",
            (AGENT,),
        )

        slots = []
        slots.extend(SLOTS_5_11)
        slots.append(US_PREP_5_11)
        slots.append(US_CLOSE_5_12)
        slots.extend(SLOTS_5_12)

        prev_max = float(INITIAL_CASH)
        for slot_ts, slot_kind, data in slots:
            jid = _seed_journal(conn, slot_ts, slot_kind, data)
            orders = data.get("orders", [])
            if orders:
                _seed_orders(conn, jid, slot_ts, slot_kind, orders)
                _apply_position(conn, orders, slot_ts)
            total = _snapshot_state(conn, slot_ts, slot_kind, prev_max)
            if total > prev_max:
                prev_max = total

        conn.commit()
    finally:
        conn.close()

    conn = get_stocks_conn()
    try:
        def v(r, k):
            return r[k] if hasattr(r, "keys") else r[0]
        n_j = v(conn.execute("SELECT COUNT(*) AS n FROM ai_trader_journal WHERE agent_id=?", (AGENT,)).fetchone(), "n")
        n_o = v(conn.execute("SELECT COUNT(*) AS n FROM ai_trader_orders WHERE agent_id=?", (AGENT,)).fetchone(), "n")
        n_p = v(conn.execute("SELECT COUNT(*) AS n FROM ai_trader_positions WHERE agent_id=? AND qty>0", (AGENT,)).fetchone(), "n")
        last = conn.execute(
            "SELECT cash, equity_value, total_value, realized_pnl_cum FROM ai_trader_state "
            "WHERE agent_id=? ORDER BY slot_ts DESC, id DESC LIMIT 1",
            (AGENT,),
        ).fetchone()
        cash = float(last["cash"])
        eq = float(last["equity_value"])
        tot = float(last["total_value"])
        rl = float(last["realized_pnl_cum"])
        ret_pct = (tot - INITIAL_CASH) / INITIAL_CASH * 100
        print(f"[seed] OK — 실제 KIS 데이터 + 인간적 말투")
        print(f"  journal: {n_j} 행 / orders: {n_o} 행 / positions: {n_p} 종목")
        print(f"  최종: cash={cash:,.0f} equity={eq:,.0f} total={tot:,.0f}")
        print(f"  실현손익: {rl:,.0f} / 수익률: {ret_pct:+.2f}%")
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
