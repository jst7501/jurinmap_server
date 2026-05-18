"""
종목 발굴(스크리너) 라우터.

POST /api/screener/run
  body: { filters: { change_today_min, change_today_max, change_5d_min, ...,
                     market_cap_min_eok, per_max, foreign_5d_min_eok,
                     foreign_5d_consecutive_buy, near_52w_high_pct,
                     trading_value_min_eok, target_gap_pct_min, ...,
                     preset?: string },
          sort: "trading_value" | "change_pct" | "market_cap" | "per",
          limit: 50 }
  resp: { count: N, results: [{ code, name, current_price, change_pct,
                                 market_cap, per, ..., signals: [...] }],
          total_universe: 2795,
          fetched_at, cache_hit }

단일 PG SQL CTE 1회로 끝남 — price_today + price_daily 윈도우 + investor_flow 5일 누적
+ naver_extended (52주 고점·목표가). 90초 in-memory 캐시.

매칭 시그널 칩 라벨 (응답 signals 배열):
  "외인5일+", "외인5연속매수", "신고가-2%", "PER12", "거래량3배",
  "저평가-25%", "급등주", "5일강세", "오늘+5%", "20일하락" ...
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

from server.db.connections import get_stocks_conn

router = APIRouter(prefix="/api/screener", tags=["screener"])
logger = logging.getLogger("server.routes.screener")

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_SEC = 90.0
_CACHE_LOCK = threading.Lock()


# 2026-05-18: server/core/numeric 로 통합
from server.core.numeric import to_float as _to_float


def _to_int(v) -> Optional[int]:
    f = _to_float(v)
    return int(f) if f is not None else None


# ─── 프리셋 정의 ────────────────────────────────────────────────
# 한 번 클릭 = 추천 조건 자동 적용. 사용자가 그 위에서 추가 조정 가능.
PRESETS: dict[str, dict] = {
    "foreign_accumulation": {
        "label": "외국인 매집주",
        "filters": {
            "foreign_5d_min_eok": 500,
            "trading_value_min_eok": 100,
        },
    },
    "foreign_consecutive": {
        "label": "외인 5일 연속매수",
        "filters": {
            "foreign_5d_consecutive_buy": True,
            "trading_value_min_eok": 30,
        },
    },
    # ─── 주린맵 only — 다른 앱에서 못 만드는 시그널 ─────────────────
    "stealth_foreign_buy": {
        "label": "몰래 사는 외국인",
        "only": True,
        "filters": {
            "sentiment_score_max": 40,
            "foreign_5d_min_eok": 200,
            "trading_value_min_eok": 50,
            "trading_value_max_eok": 500,
        },
    },
    "averaging_down_trap": {
        "label": "물타기 함정 (관찰)",
        "only": True,
        "filters": {
            "moods": ["현실부정/물타기"],
            "foreign_5d_max_eok": 0,
            "change_5d_max": -10,
        },
    },
    "bubble_warning": {
        "label": "버블 신호 (3중 과열)",
        "only": True,
        "filters": {
            "sentiment_score_min": 80,
            "near_52w_high_pct": 3,
            "change_5d_min": 10,
        },
    },
    "ai_pick_up": {
        "label": "AI 호재 종목",
        "only": True,
        "filters": {
            "ai_tone": "up",
            "trading_value_min_eok": 30,
        },
    },
    "ai_pick_risk": {
        "label": "AI 위험 라벨",
        "only": True,
        "filters": {
            "ai_tone": "risk",
        },
    },
    # ───────────────────────────────────────────────────────────────
    "near_52w_high": {
        "label": "신고가 임박",
        "filters": {
            "near_52w_high_pct": 3,
            "change_5d_min": 3,
            "trading_value_min_eok": 50,
        },
    },
    "low_per_value": {
        "label": "저PER 가치주",
        "filters": {
            "per_max": 8,
            "market_cap_min_eok": 1000,
            "trading_value_min_eok": 30,
        },
    },
    "high_dividend": {
        "label": "고배당주",
        "filters": {
            "dividend_yield_min": 4,
            "market_cap_min_eok": 1000,
            "trading_value_min_eok": 10,
        },
    },
    "large_cap_value": {
        "label": "대형 가치주",
        "filters": {
            "market_cap_min_eok": 10000,
            "per_max": 15,
            "dividend_yield_min": 2,
        },
    },
    "today_surge": {
        "label": "오늘 급등주",
        "filters": {
            "change_today_min": 5,
            "trading_value_min_eok": 50,
        },
    },
    "undervalued_recovery": {
        "label": "저평가 회복",
        "filters": {
            "target_gap_pct_min": 20,
            "change_5d_min": 0,
            "foreign_5d_min_eok": 0,
        },
    },
    "oversold_rebound": {
        "label": "과매도 반등 후보",
        "filters": {
            "change_5d_max": -10,
            "trading_value_min_eok": 50,
            "foreign_5d_min_eok": 0,
        },
    },
    "near_52w_low_warning": {
        "label": "신저가 위험 (관찰)",
        "filters": {
            "near_52w_low_pct": 3,
            "change_5d_max": -5,
        },
    },
    "fear_with_foreign_buy": {
        "label": "공포 + 외인 매수 (역발상)",
        "filters": {
            "sentiment_score_max": 30,
            "foreign_5d_min_eok": 0,
            "trading_value_min_eok": 30,
        },
    },
    "euphoria_with_foreign_sell": {
        "label": "환희 + 외인 매도 (과열)",
        "filters": {
            "sentiment_score_min": 70,
            "trading_value_min_eok": 50,
            # foreign_5d_max 가 없어 SQL 에서 직접 구현 — 일단 sentiment 70+ + 외인 0 미만은
            # 사용자가 직접 입력하도록 안내. 차후 foreign_5d_max_eok 필터 추가 예정.
        },
    },
}


def _normalize_filters(raw: dict) -> dict:
    """입력 필터를 안전 형식으로 정규화. 오프 상태(None/공백) 는 dict 에서 제거."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}

    # 프리셋 처리: 입력 필터에 preset 만 있으면 자동 적용. 추가 필터 함께 오면 병합 (입력 우선).
    preset_key = str(raw.get("preset") or "").strip()
    if preset_key in PRESETS:
        for k, v in PRESETS[preset_key]["filters"].items():
            if k not in raw or raw.get(k) in (None, ""):
                out[k] = v

    keys_float = (
        "change_today_min", "change_today_max",
        "change_5d_min", "change_5d_max",
        "change_20d_min", "change_20d_max",
        "per_max", "pbr_max", "dividend_yield_min",
        "near_52w_high_pct", "near_52w_low_pct",
        "target_gap_pct_min",
        "foreign_hold_pct_max",
    )
    for k in keys_float:
        v = _to_float(raw.get(k))
        if v is not None:
            out[k] = v

    keys_int_eok = (
        "trading_value_min_eok", "trading_value_max_eok",
        "market_cap_min_eok", "market_cap_max_eok",
        "foreign_5d_min_eok", "foreign_5d_max_eok",
    )
    for k in keys_int_eok:
        v = _to_int(raw.get(k))
        if v is not None:
            out[k] = v

    # AI 한 줄 요약 tone 필터 (up/down/neutral/risk)
    ai_tone = str(raw.get("ai_tone") or "").strip().lower()
    if ai_tone in ("up", "down", "neutral", "risk"):
        out["ai_tone"] = ai_tone

    # 민심 score 구간
    for k in ("sentiment_score_min", "sentiment_score_max"):
        v = _to_int(raw.get(k))
        if v is not None:
            out[k] = max(0, min(100, v))

    # boolean 필터
    for k in ("foreign_5d_consecutive_buy",):
        if raw.get(k) is True:
            out[k] = True

    # 테마 list (콤마 분리 또는 배열)
    themes_raw = raw.get("themes")
    if themes_raw:
        if isinstance(themes_raw, str):
            themes = [t.strip() for t in themes_raw.split(",") if t.strip()]
        elif isinstance(themes_raw, list):
            themes = [str(t).strip() for t in themes_raw if str(t).strip()]
        else:
            themes = []
        if themes:
            out["themes"] = themes[:20]  # 최대 20개

    # 민심 phase 다중선택
    moods_raw = raw.get("moods")
    if moods_raw:
        if isinstance(moods_raw, str):
            moods = [m.strip() for m in moods_raw.split(",") if m.strip()]
        elif isinstance(moods_raw, list):
            moods = [str(m).strip() for m in moods_raw if str(m).strip()]
        else:
            moods = []
        if moods:
            out["moods"] = moods[:10]

    return out


def _cache_key(filters: dict, sort: str, limit: int) -> str:
    payload = json.dumps(
        {"filters": filters, "sort": sort, "limit": limit},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


# ─── SQL 빌더 ────────────────────────────────────────────────
# WHERE 절은 동적으로 조립 (None 인 필터는 추가 안 함). psycopg 파라미터 바인딩.
_SORT_MAP = {
    "trading_value": "pt.trading_value DESC NULLS LAST",
    "change_pct": "pt.change_pct DESC NULLS LAST",
    "market_cap": "pt.market_cap DESC NULLS LAST",
    "per": "(CASE WHEN pt.per IS NULL OR pt.per::text = '' THEN NULL ELSE NULLIF(REPLACE(pt.per::text, ',', ''), '')::numeric END) ASC NULLS LAST",
    "foreign_5d": "flow.foreign_5d DESC NULLS LAST",
    "near_52w_high": "((pt.current_price::numeric / NULLIF(ne.high_52w, 0)) * 100) DESC NULLS LAST",
}


def _build_sql_and_params(filters: dict, sort: str, limit: int) -> tuple[str, list]:
    where_parts: list[str] = ["pt.current_price > 0"]
    params: list = []

    def add(cond: str, *args):
        where_parts.append(cond)
        params.extend(args)

    f = filters

    # 가격 등락
    if "change_today_min" in f:
        add("pt.change_pct >= %s", f["change_today_min"])
    if "change_today_max" in f:
        add("pt.change_pct <= %s", f["change_today_max"])

    if "change_5d_min" in f:
        add("chg_pct.chg_5d >= %s", f["change_5d_min"])
    if "change_5d_max" in f:
        add("chg_pct.chg_5d <= %s", f["change_5d_max"])
    if "change_20d_min" in f:
        add("chg_pct.chg_20d >= %s", f["change_20d_min"])
    if "change_20d_max" in f:
        add("chg_pct.chg_20d <= %s", f["change_20d_max"])

    # 시총 (입력 단위: 억원, DB 단위: 백만원)
    # 1억원 = 100 백만원. 삼전 검증: market_cap=15,609,564 백만원 = 15.6조원 ✅
    if "market_cap_min_eok" in f:
        add("pt.market_cap >= %s", int(f["market_cap_min_eok"]) * 100)
    if "market_cap_max_eok" in f:
        add("pt.market_cap <= %s", int(f["market_cap_max_eok"]) * 100)

    # 거래대금 (입력 억원, DB 원)
    if "trading_value_min_eok" in f:
        add("pt.trading_value >= %s", int(f["trading_value_min_eok"]) * 100_000_000)
    if "trading_value_max_eok" in f:
        add("pt.trading_value <= %s", int(f["trading_value_max_eok"]) * 100_000_000)

    # PER/PBR — DB 에 text 로 저장된 케이스 대응
    # PER=0 (적자 또는 데이터 없음) 은 의미 없는 매칭이라 제외 (0 < per ≤ N).
    if "per_max" in f:
        add(
            "(pt.per IS NOT NULL AND pt.per::text NOT IN ('', '-') AND "
            "NULLIF(REPLACE(pt.per::text, ',', ''), '')::numeric > 0 AND "
            "NULLIF(REPLACE(pt.per::text, ',', ''), '')::numeric <= %s)",
            f["per_max"],
        )
    if "pbr_max" in f:
        add(
            "(pt.pbr IS NOT NULL AND pt.pbr::text NOT IN ('', '-') AND "
            "NULLIF(REPLACE(pt.pbr::text, ',', ''), '')::numeric > 0 AND "
            "NULLIF(REPLACE(pt.pbr::text, ',', ''), '')::numeric <= %s)",
            f["pbr_max"],
        )

    # 배당수익률 (naver_extended)
    # 일부 종목 dividend_yield 가 7750·3000 같은 노이즈 — 정상은 0~30% 범위.
    # 30% 초과는 데이터 오류로 간주하여 필터에서 제외.
    if "dividend_yield_min" in f:
        add(
            "(ne.dividend_yield IS NOT NULL AND ne.dividend_yield >= %s AND ne.dividend_yield <= 30)",
            f["dividend_yield_min"],
        )

    # 52주 고점·저점 근접
    if "near_52w_high_pct" in f:
        add(
            "(ne.high_52w > 0 AND (pt.current_price::numeric / ne.high_52w) >= %s)",
            1 - float(f["near_52w_high_pct"]) / 100.0,
        )
    if "near_52w_low_pct" in f:
        add(
            "(ne.low_52w > 0 AND (pt.current_price::numeric / ne.low_52w) <= %s)",
            1 + float(f["near_52w_low_pct"]) / 100.0,
        )

    # 목표가 갭 (저평가, 입력값은 % - 양수면 그만큼 저평가)
    if "target_gap_pct_min" in f:
        add(
            "(ne.target_price > 0 AND ((ne.target_price - pt.current_price)::numeric / ne.target_price) * 100 >= %s)",
            f["target_gap_pct_min"],
        )

    # 외국인 보유율
    if "foreign_hold_pct_max" in f:
        add("(pt.foreign_hold_pct IS NOT NULL AND pt.foreign_hold_pct <= %s)", f["foreign_hold_pct_max"])

    # 수급 5일 누적 (단위: 백만원이 investor_flow.*_net_amt)
    # foreign_net (수량) 누적이 아니라 거래대금 _net_amt 사용. 입력 억원 = *_net_amt 백만원 / 100
    if "foreign_5d_min_eok" in f:
        add("flow.foreign_5d_eok >= %s", f["foreign_5d_min_eok"])
    if "foreign_5d_max_eok" in f:
        add("flow.foreign_5d_eok <= %s", f["foreign_5d_max_eok"])

    if f.get("foreign_5d_consecutive_buy"):
        add("flow.foreign_5d_consecutive_buy = TRUE")

    # AI tone (stock_daily_summary)
    if "ai_tone" in f:
        add("(sds.tone IS NOT NULL AND sds.tone = %s)", f["ai_tone"])

    # 테마 (stock_themes 매핑) — 다중선택, OR 매칭
    if "themes" in f and f["themes"]:
        add(
            "EXISTS (SELECT 1 FROM stock_themes st WHERE st.code = pt.code AND st.theme = ANY(%s))",
            list(f["themes"]),
        )

    # 민심 — score 구간
    if "sentiment_score_min" in f:
        add("(bs.score IS NOT NULL AND bs.score >= %s)", f["sentiment_score_min"])
    if "sentiment_score_max" in f:
        add("(bs.score IS NOT NULL AND bs.score <= %s)", f["sentiment_score_max"])

    # 민심 phase (mood 한국어) 다중선택
    if "moods" in f and f["moods"]:
        add("(bs.mood IS NOT NULL AND bs.mood = ANY(%s))", list(f["moods"]))

    sort_clause = _SORT_MAP.get(str(sort or "trading_value"), _SORT_MAP["trading_value"])
    where_sql = " AND ".join(where_parts)
    limit = max(10, min(int(limit or 50), 200))

    sql = f"""
WITH chg AS (
    SELECT code,
        MAX(close) FILTER (WHERE rn = 1) AS today_close,
        MAX(close) FILTER (WHERE rn = 6) AS d5_close,
        MAX(close) FILTER (WHERE rn = 21) AS d20_close
    FROM (
        SELECT code, close, ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
        FROM price_daily
    ) t
    WHERE rn IN (1, 6, 21)
    GROUP BY code
), chg_pct AS (
    SELECT code,
        CASE WHEN d5_close > 0 THEN (today_close - d5_close)::numeric / d5_close * 100 END AS chg_5d,
        CASE WHEN d20_close > 0 THEN (today_close - d20_close)::numeric / d20_close * 100 END AS chg_20d
    FROM chg
), flow AS (
    SELECT code,
        ROUND(SUM(CASE WHEN rn <= 5 THEN COALESCE(foreign_net_amt, 0) ELSE 0 END)::numeric / 100, 0)::bigint AS foreign_5d_eok,
        ROUND(SUM(CASE WHEN rn <= 5 THEN COALESCE(institution_net_amt, 0) ELSE 0 END)::numeric / 100, 0)::bigint AS inst_5d_eok,
        BOOL_AND(CASE WHEN rn <= 5 THEN COALESCE(foreign_net_amt, 0) > 0 ELSE TRUE END)
            FILTER (WHERE rn <= 5) AS foreign_5d_consecutive_buy,
        SUM(CASE WHEN rn = 1 THEN COALESCE(foreign_net_amt, 0) ELSE 0 END) AS foreign_today_amt
    FROM (
        SELECT code, foreign_net_amt, institution_net_amt,
            ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
        FROM investor_flow
    ) t
    WHERE rn <= 5
    GROUP BY code
)
SELECT
    s.code, s.name,
    pt.current_price, pt.change_pct, pt.trading_value, pt.market_cap,
    NULLIF(REPLACE(pt.per::text, ',', ''), '')::numeric AS per,
    NULLIF(REPLACE(pt.pbr::text, ',', ''), '')::numeric AS pbr,
    pt.foreign_hold_pct,
    ne.high_52w, ne.low_52w, ne.target_price, ne.dividend_yield,
    chg_pct.chg_5d, chg_pct.chg_20d,
    flow.foreign_5d_eok, flow.inst_5d_eok, flow.foreign_5d_consecutive_buy,
    flow.foreign_today_amt,
    bs.score AS sentiment_score, bs.mood AS sentiment_mood,
    sds.one_liner AS ai_one_liner, sds.tone AS ai_tone,
    sds.drivers_json AS ai_drivers_json, sds.summary_date AS ai_summary_date,
    (SELECT array_agg(theme ORDER BY theme) FROM stock_themes st WHERE st.code = pt.code) AS themes
FROM stocks s
JOIN price_today pt USING (code)
LEFT JOIN naver_extended ne USING (code)
LEFT JOIN chg_pct USING (code)
LEFT JOIN flow USING (code)
LEFT JOIN board_sentiment bs USING (code)
-- 종목별 최근 3일 중 가장 최신 ok 요약 1개 (LATERAL).
-- 윈도우를 7일 → 3일로 좁힘 (2026-05-08): Top 100 밖으로 밀린 종목의 stale 평가를
-- 사용자에게 보여 잘못 판단하는 것을 차단. 4일 이상 stale 이면 자동 NULL.
LEFT JOIN LATERAL (
    SELECT one_liner, tone, drivers_json, summary_date
    FROM stock_daily_summary sds_inner
    WHERE sds_inner.code = pt.code
      AND COALESCE(sds_inner.status, 'ok') = 'ok'
      AND sds_inner.summary_date >= TO_CHAR(CURRENT_DATE - INTERVAL '3 days', 'YYYY-MM-DD')
    ORDER BY sds_inner.summary_date DESC
    LIMIT 1
) sds ON TRUE
WHERE {where_sql}
ORDER BY {sort_clause}
LIMIT {limit}
"""
    return sql, params


# ─── 매칭 시그널 라벨 자동 부여 ─────────────────────────────────
def _build_signals(row: dict, filters: dict) -> list[str]:
    """결과 행에 적용된 필터 + 객관 임계 자동 라벨."""
    signals: list[str] = []

    # 가격
    if "change_today_min" in filters and (row.get("change_pct") or 0) >= filters["change_today_min"]:
        signals.append(f"오늘+{int(filters['change_today_min'])}%↑")
    chg5 = row.get("chg_5d")
    if chg5 is not None:
        if chg5 >= 5:
            signals.append("5일+5%↑")
        elif chg5 <= -5:
            signals.append("5일-5%↓")

    # 52주 신고가 근접
    high = row.get("high_52w") or 0
    cur = row.get("current_price") or 0
    if high > 0 and cur > 0:
        gap_pct = (1 - cur / high) * 100
        if gap_pct <= 1:
            signals.append("신고가1%↑")
        elif gap_pct <= 3:
            signals.append("신고가3%↑")
        elif gap_pct <= 5:
            signals.append("신고가5%↑")

    # 목표가 갭
    tgt = row.get("target_price") or 0
    if tgt > 0 and cur > 0:
        upside = (tgt - cur) / tgt * 100
        if upside >= 30:
            signals.append("저평가30%↑")
        elif upside >= 20:
            signals.append("저평가20%↑")

    # 수급
    f5 = row.get("foreign_5d_eok")
    if f5 is not None:
        if f5 >= 1000:
            signals.append("외인5일+1000억")
        elif f5 >= 500:
            signals.append("외인5일+500억")
        elif f5 <= -500:
            signals.append("외인5일-500억")
    if row.get("foreign_5d_consecutive_buy"):
        signals.append("외인5연속매수")

    # 펀더
    per = row.get("per")
    if per is not None and 0 < per <= 10:
        signals.append(f"PER{int(per)}")
    dy = row.get("dividend_yield")
    if dy is not None and dy >= 4:
        signals.append(f"배당{round(dy,1)}%")

    # 민심 (mood + score) — 우리만의 정성 라벨
    mood = row.get("sentiment_mood")
    s_score = row.get("sentiment_score")
    if mood and s_score is not None:
        # 짧은 라벨로 압축 — "환희/가즈아" → "환희", "현실부정/물타기" → "물타기"
        short_mood = str(mood).split("/")[-1] if "/" in str(mood) else str(mood)
        signals.append(f"민심 {short_mood} {int(s_score)}")
    elif s_score is not None:
        if s_score >= 80:
            signals.append("민심환희")
        elif s_score <= 20:
            signals.append("민심공포")

    # AI 한 줄 tone — 우리 요약 루틴 결과
    ai_tone = row.get("ai_tone")
    if ai_tone:
        tone_label = {"up": "AI: 호재", "down": "AI: 약세", "risk": "AI: 위험", "neutral": "AI: 중립"}.get(ai_tone)
        if tone_label:
            signals.append(tone_label)

    # 테마 — 매칭 필터에 등장한 테마 1개만 (배지 길이 절약)
    themes = row.get("themes") or []
    asked_themes = set(filters.get("themes") or [])
    if themes and asked_themes:
        for t in themes:
            if t in asked_themes:
                signals.append(f"#{t}")
                break

    return signals


# ─── Featured (홈 미리보기용 자동 추천) ────────────────────────
# 시간대 기반 후보 chain. 결과가 3개 미만이면 다음 후보로 fallback.
# 메인 후보는 ★ ONLY 우선 — 차별성 강조.
_FEATURED_SLOTS: dict[str, list[str]] = {
    "morning":     ["ai_pick_up", "stealth_foreign_buy", "foreign_accumulation"],
    "intraday":    ["stealth_foreign_buy", "today_surge", "foreign_accumulation"],
    "afternoon":   ["foreign_accumulation", "foreign_consecutive", "near_52w_high"],
    "post_market": ["near_52w_high", "ai_pick_up", "foreign_accumulation"],
    "night":       ["undervalued_recovery", "high_dividend", "low_per_value"],
}

# 시간대별 카피 — 프리셋의 desc 보다 더 톤 있는 한 줄 (선택적)
_FEATURED_TAGLINES: dict[str, dict[str, str]] = {
    "morning": {
        "ai_pick_up": "AI가 호재로 본 종목, 장 시작 전 미리 보세요",
        "stealth_foreign_buy": "여론은 시큰둥, 외인은 조용히 매집",
        "foreign_accumulation": "외국인이 5일 동안 가장 많이 산 종목",
    },
    "intraday": {
        "stealth_foreign_buy": "장중 외국인이 조용히 사들이는 종목",
        "today_surge": "지금 가장 뜨거운 급등주",
        "foreign_accumulation": "외인이 큰돈 넣고 있는 종목",
    },
    "afternoon": {
        "foreign_accumulation": "오후 들어 외인이 매집 중인 종목",
        "foreign_consecutive": "5일 연속 외인 매수가 이어진 종목",
        "near_52w_high": "마감 전 신고가 임박 종목",
    },
    "post_market": {
        "near_52w_high": "오늘 신고가에 가까이 간 종목",
        "ai_pick_up": "AI가 호재로 정리한 오늘의 종목",
        "foreign_accumulation": "마감 후 다시 보는 외인 매집 종목",
    },
    "night": {
        "undervalued_recovery": "목표가 대비 저평가, 회복 신호 있는 종목",
        "high_dividend": "안정적 배당 4% 이상",
        "low_per_value": "PER 8 이하 가치주",
    },
}


def _detect_slot_kst() -> str:
    from datetime import datetime, timezone, timedelta
    h = datetime.now(timezone(timedelta(hours=9))).hour
    if 6 <= h < 9:    return "morning"
    if 9 <= h < 13:   return "intraday"
    if 13 <= h < 16:  return "afternoon"
    if 16 <= h < 20:  return "post_market"
    return "night"


_FEATURED_CACHE: dict[str, tuple[float, dict]] = {}
_FEATURED_TTL_SEC = 300.0  # 5분 — 시간대 안에서는 결과 동일
_FEATURED_REFRESHING: set[str] = set()
_FEATURED_REFRESH_LOCK = threading.Lock()


def _featured_compute(slot: str, min_count: int, top: int, cache_key: str) -> dict:
    """무거운 계산 본체 — chain 순회 + SQL. 결과 캐시에 저장 후 반환."""
    candidates = _FEATURED_SLOTS.get(slot, ["foreign_accumulation"])
    now = time.time()

    for preset_key in candidates:
        if preset_key not in PRESETS:
            continue
        norm = _normalize_filters({"preset": preset_key})
        sql, params = _build_sql_and_params(norm, "trading_value", max(top, 5))
        conn = get_stocks_conn()
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
        except Exception:
            logger.exception("featured SQL failed for preset=%s", preset_key)
            rows = []
        finally:
            try: conn.close()
            except Exception: pass
        if len(rows) >= min_count:
            results = []
            for r in rows[:top]:
                d = dict(r) if hasattr(r, "keys") else {}
                for k, v in list(d.items()):
                    if v is not None and not isinstance(v, (str, int, float, bool, list)):
                        try: d[k] = float(v)
                        except Exception: d[k] = str(v)
                d["signals"] = _build_signals(d, norm)
                results.append(d)
            tagline = (_FEATURED_TAGLINES.get(slot) or {}).get(
                preset_key,
                PRESETS[preset_key].get("filters") and "조건에 맞는 종목" or "추천 종목",
            )
            response = {
                "preset_key": preset_key,
                "preset_label": PRESETS[preset_key]["label"],
                "preset_only": bool(PRESETS[preset_key].get("only", False)),
                "slot": slot,
                "tagline": tagline,
                "total_count": len(rows),
                "results": results,
                "cache_hit": False,
            }
            _FEATURED_CACHE[cache_key] = (now, response)
            return response

    # 모든 후보 빈 결과 → 거래대금 Top
    fallback_sql, fallback_params = _build_sql_and_params({}, "trading_value", top)
    conn = get_stocks_conn()
    try:
        rows = conn.execute(fallback_sql, tuple(fallback_params)).fetchall()
    except Exception:
        rows = []
    finally:
        try: conn.close()
        except Exception: pass
    results = []
    for r in rows[:top]:
        d = dict(r) if hasattr(r, "keys") else {}
        for k, v in list(d.items()):
            if v is not None and not isinstance(v, (str, int, float, bool, list)):
                try: d[k] = float(v)
                except Exception: d[k] = str(v)
        d["signals"] = _build_signals(d, {})
        results.append(d)
    response = {
        "preset_key": None,
        "preset_label": "거래대금 상위",
        "preset_only": False,
        "slot": slot,
        "tagline": "오늘 거래가 가장 활발한 종목",
        "total_count": len(rows),
        "results": results,
        "cache_hit": False,
    }
    _FEATURED_CACHE[cache_key] = (now, response)
    return response


def _featured_refresh_async(slot: str, min_count: int, top: int, cache_key: str):
    """stale 응답 보내고 background 에서 새 결과 계산 (다음 사용자에게 빠른 응답)."""
    with _FEATURED_REFRESH_LOCK:
        if cache_key in _FEATURED_REFRESHING:
            return
        _FEATURED_REFRESHING.add(cache_key)
    try:
        _featured_compute(slot, min_count, top, cache_key)
    except Exception:
        logger.exception("featured async refresh failed: %s", cache_key)
    finally:
        with _FEATURED_REFRESH_LOCK:
            _FEATURED_REFRESHING.discard(cache_key)


@router.get("/featured")
def get_featured(min_count: int = 3, top: int = 3):
    """홈 미리보기용 — 현재 시간대에 맞는 프리셋 + Top N 결과 자동 선정.

    응답 정책 (stale-while-revalidate):
      · cache fresh (< 5분): 즉시 반환
      · cache stale (≥ 5분): 즉시 stale 반환 + background 에서 새로 계산
      · cache 없음 (cold): 동기 계산 (첫 한 번만 느림)
    """
    slot = _detect_slot_kst()
    cache_key = f"{slot}|{min_count}|{top}"
    now = time.time()
    cached = _FEATURED_CACHE.get(cache_key)

    # 1. fresh hit — 즉시 반환
    if cached and (now - cached[0]) < _FEATURED_TTL_SEC:
        out = dict(cached[1])
        out["cache_hit"] = True
        return out

    # 2. stale hit — 즉시 반환 + background refresh (사용자는 항상 빠른 응답)
    if cached:
        threading.Thread(
            target=_featured_refresh_async,
            args=(slot, min_count, top, cache_key),
            daemon=True,
        ).start()
        out = dict(cached[1])
        out["cache_hit"] = True
        out["stale"] = True
        return out

    # 3. cold start — 동기 계산
    return _featured_compute(slot, min_count, top, cache_key)


@router.get("/presets")
def get_presets():
    return {
        "presets": [
            {
                "key": k,
                "label": v["label"],
                "filters": v["filters"],
                "only": bool(v.get("only", False)),
            }
            for k, v in PRESETS.items()
        ]
    }


@router.post("/run")
def run_screener(payload: dict):
    raw_filters = payload.get("filters") or {}
    sort = str(payload.get("sort") or "trading_value")
    limit = int(payload.get("limit") or 50)

    filters = _normalize_filters(raw_filters)

    # 캐시 조회
    ckey = _cache_key(filters, sort, limit)
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(ckey)
        if cached and (now - cached[0]) < _CACHE_TTL_SEC:
            data = dict(cached[1])
            data["cache_hit"] = True
            return data

    sql, params = _build_sql_and_params(filters, sort, limit)
    conn = get_stocks_conn()
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
        # 전체 universe (필터 무시) — 한 번만
        try:
            universe = conn.execute("SELECT COUNT(*) AS n FROM price_today WHERE current_price > 0").fetchone()
            total_universe = int((universe[0] if universe else 0) or 0) if not hasattr(universe, "keys") else int(universe["n"] or 0)
        except Exception:
            total_universe = None
    except Exception as exc:
        logger.exception("screener SQL failed")
        raise HTTPException(status_code=500, detail=f"sql_failed: {exc}")
    finally:
        try: conn.close()
        except Exception: pass

    results = []
    for r in rows:
        d = dict(r) if hasattr(r, "keys") else {}
        # numeric → float 변환 (Decimal 직렬화 대비)
        for k, v in list(d.items()):
            if v is not None and not isinstance(v, (str, int, float, bool)):
                try:
                    d[k] = float(v)
                except Exception:
                    d[k] = str(v)
        d["signals"] = _build_signals(d, filters)
        results.append(d)

    response = {
        "count": len(results),
        "total_universe": total_universe,
        "filters_applied": filters,
        "sort": sort,
        "limit": limit,
        "results": results,
        "fetched_at": now,
        "cache_hit": False,
    }

    with _CACHE_LOCK:
        _CACHE[ckey] = (now, response)
        if len(_CACHE) > 500:
            # 가장 오래된 100개 제거
            oldest = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:100]
            for k, _ in oldest:
                _CACHE.pop(k, None)

    return response
