"""
'내 매매 코치' 리포트 생성기.

- 특정 연도의 거래를 분석해 Wrapped 스토리 카드용 데이터 반환.
- 페르소나 분류 + 진단(잔소리) + 처방 생성.
- 톤: 직설적 · 밈 · 코치(친구의 쓴소리).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from statistics import mean, pstdev
from typing import Any

from server.services.trade_journal.journal import compute_realizations
from server.services.trade_journal.models import Trade
from server.services.trade_journal.personas import _P, _PERSONAS


_KOR_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
_ETF_PATTERNS = ("KODEX", "TIGER", "SOL", "HANARO", "KBSTAR", "ARIRANG", "ACE", "RISE")
_SPAC_KEYWORDS = ("스팩", "SPAC")
_FOREIGN_PREFIX_LEN = 2  # US, IE, CA, etc.


def _safe_stdev(xs):
    return pstdev(xs) if len(xs) >= 2 else 0.0


# ---------------------------- 메트릭 계산 ---------------------------- #

def _compute_metrics(trades: list[Trade], year: int) -> dict[str, Any]:
    # ★ 전체 거래로 실현이벤트를 계산해서 이전 연도 매수분의 원가를 살림.
    #   그 뒤에 해당 연도에 '청산된' 이벤트만 필터링.
    all_trips, all_opens = compute_realizations(trades)
    trips = [r for r in all_trips if datetime.fromisoformat(r["closed_at"]).year == year]
    opens = [o for o in all_opens]

    year_trades = [t for t in trades if t.traded_at.year == year]
    if not year_trades or not trips:
        return {"empty": True, "year": year, "trade_count": 0}

    # 기본 합계
    total_buy = sum(t.amount for t in year_trades if t.side == "BUY")
    total_sell = sum(t.amount for t in year_trades if t.side == "SELL")
    total_fee = sum(t.fee for t in year_trades)
    total_tax = sum(t.tax for t in year_trades)
    total_pnl = sum(r["pnl"] for r in trips)

    trade_days = len({t.traded_at.date() for t in year_trades})
    tickers = {t.ticker or t.symbol for t in year_trades}
    ticker_names = {(t.symbol or "").upper() for t in year_trades}
    ticker_count = len(tickers)

    # 종목별 빈도 (최애)
    ticker_counter = Counter(t.ticker or t.symbol for t in year_trades)
    fav_key, fav_count = ticker_counter.most_common(1)[0]
    fav_symbol = next((t.symbol for t in year_trades if (t.ticker or t.symbol) == fav_key), fav_key)
    fav_pnl = sum(r["pnl"] for r in trips if (r["ticker"] or r["symbol"]) == fav_key)

    # 베스트/워스트 실현
    best = max(trips, key=lambda r: r["pnl"], default=None)
    worst = min(trips, key=lambda r: r["pnl"], default=None)

    # 승률/손익비 (실현 이벤트 기반)
    wins = [r for r in trips if r["pnl"] > 0]
    losses = [r for r in trips if r["pnl"] < 0]
    win_count = len(wins)
    loss_count = len(losses)
    win_rate = (win_count / (win_count + loss_count) * 100.0) if (win_count + loss_count) else 0.0

    avg_win_pct = mean(r["return_pct"] for r in wins) if wins else 0.0
    avg_loss_pct = mean(r["return_pct"] for r in losses) if losses else 0.0
    sum_wins = sum(r["pnl"] for r in wins)
    sum_losses = sum(r["pnl"] for r in losses)
    profit_factor = (sum_wins / abs(sum_losses)) if sum_losses else float("inf") if sum_wins else 0.0

    # 본전 필요 승률: 지금 패턴대로면 몇 % 승률이 필요한지
    required_wr = 0.0
    if avg_win_pct > 0 and avg_loss_pct < 0:
        required_wr = (abs(avg_loss_pct) / (avg_win_pct + abs(avg_loss_pct))) * 100.0

    # 평균 보유기간
    holding_days = [r["holding_days"] for r in trips if r["holding_days"] is not None]
    avg_hold = mean(holding_days) if holding_days else 0.0
    max_hold = max(holding_days) if holding_days else 0
    min_hold = min(holding_days) if holding_days else 0

    # 종목별 집계 (워스트 종목 = 합계 손실이 가장 큰 종목)
    per_ticker: dict[str, dict] = {}
    for r in trips:
        k = r["ticker"] or r["symbol"]
        b = per_ticker.setdefault(k, {
            "ticker": r["ticker"],
            "symbol": r["symbol"],
            "pnl": 0.0,
            "trip_count": 0,
            "buy_amount": 0.0,
            "sell_amount": 0.0,
        })
        b["pnl"] += r["pnl"]
        b["trip_count"] += 1
        b["buy_amount"] += r["buy_amount"]
        b["sell_amount"] += r["sell_amount"]

    ticker_rank = sorted(per_ticker.values(), key=lambda b: b["pnl"])
    worst_ticker = ticker_rank[0] if ticker_rank else None
    best_ticker = ticker_rank[-1] if ticker_rank else None

    # 월별 (트립 기준)
    by_month: dict[str, dict] = {}
    for r in trips:
        mkey = r["closed_at"][:7]
        mm = by_month.setdefault(mkey, {"key": mkey, "pnl": 0.0, "count": 0})
        mm["pnl"] += r["pnl"]
        mm["count"] += 1
    best_month = max(by_month.values(), key=lambda m: m["pnl"]) if by_month else None
    worst_month = min(by_month.values(), key=lambda m: m["pnl"]) if by_month else None

    # 일 최대 거래
    by_day = Counter(t.traded_at.date().isoformat() for t in year_trades)
    max_trades_in_day_date, max_trades_in_day = by_day.most_common(1)[0] if by_day else ("", 0)

    # 복수매매 지표: 손절(return_pct < -3) 직후 같은 종목 재진입 횟수 추정
    # ordered trips + following BUY of same ticker within 1 day
    ordered_trips = sorted(trips, key=lambda r: r["closed_at"])
    revenge_count = 0
    for i, r in enumerate(ordered_trips):
        if r["return_pct"] < -3:
            sell_date = datetime.fromisoformat(r["closed_at"]).date()
            key = r["ticker"] or r["symbol"]
            for t in year_trades:
                if t.side != "BUY":
                    continue
                if (t.ticker or t.symbol) != key:
                    continue
                diff = (t.traded_at.date() - sell_date).days
                if 0 <= diff <= 1:
                    revenge_count += 1
                    break

    # 재진입 평균 (같은 종목을 완전히 청산한 뒤 다시 들어간 횟수)
    reentry_counts: list[int] = []
    for k in tickers:
        ticker_trips = [r for r in trips if (r["ticker"] or r["symbol"]) == k]
        if ticker_trips:
            reentry_counts.append(len(ticker_trips))
    avg_reentry = mean(reentry_counts) if reentry_counts else 0.0

    # ========= MDD (Max Drawdown, 누적 실현손익 곡선 기준) =========
    # 시간순 실현 이벤트로 누적 곡선을 그리고 피크 대비 최대 낙폭을 구한다.
    sorted_trips = sorted(trips, key=lambda r: r["closed_at"])
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    mdd_peak = 0.0
    mdd_trough_date = None
    equity_curve: list[dict] = []
    for r in sorted_trips:
        cum += r["pnl"]
        if cum > peak:
            peak = cum
        dd = peak - cum  # 낙폭
        if dd > mdd:
            mdd = dd
            mdd_peak = peak
            mdd_trough_date = r["closed_at"][:10]
        equity_curve.append({"date": r["closed_at"][:10], "cum": cum, "peak": peak})

    # ========= 보유 기간 버킷별 수익률 =========
    buckets_def = [
        ("intraday", "당일", 0, 0),
        ("1to3", "1~3일", 1, 3),
        ("3to7", "3~7일", 3, 7),
        ("1to2w", "1~2주", 7, 14),
        ("2wplus", "2주+", 14, 10_000),
    ]
    buckets: list[dict] = []
    for bid, label, lo, hi in buckets_def:
        items = [r for r in trips if lo <= r["holding_days"] <= hi] if lo == 0 and hi == 0 else [
            r for r in trips if lo < r["holding_days"] <= hi
        ]
        if bid == "intraday":
            items = [r for r in trips if r["holding_days"] == 0]
        if not items:
            buckets.append({
                "id": bid, "label": label, "count": 0,
                "pnl": 0.0, "avg_return_pct": 0.0, "win_rate": 0.0,
            })
            continue
        wins_b = [r for r in items if r["pnl"] > 0]
        buckets.append({
            "id": bid,
            "label": label,
            "count": len(items),
            "pnl": sum(r["pnl"] for r in items),
            "avg_return_pct": mean(r["return_pct"] for r in items) if items else 0.0,
            "win_rate": (len(wins_b) / len(items) * 100.0) if items else 0.0,
        })

    # ========= 자동 실수 태그 =========
    tags: list[dict] = []
    # #손절미준수 — 손실 트립 중 -10% 이상
    big_loss_count = sum(1 for r in trips if r["return_pct"] <= -10)
    if big_loss_count >= 3:
        tags.append({
            "tag": "#손절미준수",
            "count": big_loss_count,
            "description": f"{big_loss_count}건이 -10% 이상 손실. 손절선을 늦췄거나 없었던 거예요.",
        })
    # #물타기실패 — 재진입 평균 3회 이상
    if avg_reentry >= 3:
        tags.append({
            "tag": "#물타기실패",
            "count": int(avg_reentry),
            "description": f"한 종목 평균 재진입 {avg_reentry:.1f}회. 물을 탔지만 대부분 손실로 끝났어요.",
        })
    # #복수매매 — revenge_count
    if revenge_count >= 10:
        tags.append({
            "tag": "#복수매매",
            "count": revenge_count,
            "description": f"손절 직후 같은 종목 재진입 {revenge_count}회.",
        })
    # #조급함 — 하루 최대 거래가 20건 이상
    if max_trades_in_day >= 20:
        tags.append({
            "tag": "#조급함",
            "count": max_trades_in_day,
            "description": f"{max_trades_in_day_date}에 하루 {max_trades_in_day}건 체결. 조급함의 흔적.",
        })
    # #익절성공 — 10% 이상 수익 트립 여러 건
    big_wins = sum(1 for r in trips if r["return_pct"] >= 10)
    if big_wins >= 3:
        tags.append({
            "tag": "#익절성공",
            "count": big_wins,
            "description": f"{big_wins}건이 +10% 이상 익절. 이건 자랑해도 돼요.",
        })
    # #단타과잉 — 평균 보유 < 1일 + 거래 수 > 200
    avg_hold_tmp = mean([r["holding_days"] for r in trips]) if trips else 0
    if avg_hold_tmp < 1 and len(year_trades) > 200:
        tags.append({
            "tag": "#단타과잉",
            "count": len(year_trades),
            "description": f"평균 보유 {avg_hold_tmp:.1f}일, 연 {len(year_trades)}건. 손은 빠르지만 수수료는 못 이겨요.",
        })

    # ========= R:R 비율 인사이트 =========
    rr_ratio = (avg_win_pct / abs(avg_loss_pct)) if avg_loss_pct < 0 and avg_win_pct > 0 else None
    # 지금 패턴으로 본전 치려면 필요한 승률
    required_wr_from_rr = (
        (abs(avg_loss_pct) / (avg_win_pct + abs(avg_loss_pct)) * 100.0)
        if (avg_win_pct > 0 and avg_loss_pct < 0) else 0.0
    )

    # ========= 요일별 패턴 =========
    weekday_pnl: dict[int, float] = defaultdict(float)
    weekday_count: dict[int, int] = defaultdict(int)
    weekday_wins: dict[int, int] = defaultdict(int)
    for r in trips:
        wd = datetime.fromisoformat(r["closed_at"]).weekday()
        weekday_pnl[wd] += r["pnl"]
        weekday_count[wd] += 1
        if r["pnl"] > 0:
            weekday_wins[wd] += 1

    weekday_stats = []
    for wd in range(7):
        cnt = weekday_count[wd]
        if cnt > 0:
            weekday_stats.append({
                "wd": wd,
                "label": _KOR_WEEKDAYS[wd],
                "pnl": weekday_pnl[wd],
                "count": cnt,
                "win_rate": weekday_wins[wd] / cnt * 100.0 if cnt else 0.0,
            })
    best_weekday = max(weekday_stats, key=lambda x: x["pnl"], default=None)
    worst_weekday = min(weekday_stats, key=lambda x: x["pnl"], default=None)

    # ========= 월별 일관성 =========
    monthly_pnl_list = [mm["pnl"] for mm in by_month.values()]
    monthly_pnl_stdev = _safe_stdev(monthly_pnl_list)
    monthly_pnl_mean = mean(monthly_pnl_list) if monthly_pnl_list else 0.0
    active_months = len(by_month)
    winning_months = sum(1 for p in monthly_pnl_list if p > 0)
    losing_months = sum(1 for p in monthly_pnl_list if p < 0)
    sorted_months = sorted(by_month.items(), key=lambda x: x[0])
    # 연속 손실월 스트릭
    max_losing_months_streak = cur_lms = 0
    max_winning_months_streak = cur_wms = 0
    for _, mm in sorted_months:
        if mm["pnl"] < 0:
            cur_lms += 1
            cur_wms = 0
            max_losing_months_streak = max(max_losing_months_streak, cur_lms)
        elif mm["pnl"] > 0:
            cur_wms += 1
            cur_lms = 0
            max_winning_months_streak = max(max_winning_months_streak, cur_wms)
        else:
            cur_wms = cur_lms = 0

    first_month = sorted_months[0] if sorted_months else None
    last_month = sorted_months[-1] if sorted_months else None

    # ========= 종목 집중도 (Top N 비중) =========
    ticker_volume = defaultdict(float)
    ticker_pnl_all = defaultdict(float)
    ticker_trip_count = defaultdict(int)
    for t in year_trades:
        if t.side == "BUY":
            ticker_volume[t.ticker or t.symbol] += t.amount
    for r in trips:
        k = r["ticker"] or r["symbol"]
        ticker_pnl_all[k] += r["pnl"]
        ticker_trip_count[k] += 1

    sorted_by_volume = sorted(ticker_volume.items(), key=lambda x: -x[1])
    top1_pct = (sorted_by_volume[0][1] / max(total_buy, 1) * 100) if sorted_by_volume else 0
    top3_pct = (sum(v for _, v in sorted_by_volume[:3]) / max(total_buy, 1) * 100) if sorted_by_volume else 0
    top5_pct = (sum(v for _, v in sorted_by_volume[:5]) / max(total_buy, 1) * 100) if sorted_by_volume else 0
    top10_pct = (sum(v for _, v in sorted_by_volume[:10]) / max(total_buy, 1) * 100) if sorted_by_volume else 0

    profitable_ticker_count = sum(1 for p in ticker_pnl_all.values() if p > 0)
    losing_ticker_count = sum(1 for p in ticker_pnl_all.values() if p < 0)

    # ========= ETF / 스팩 / 해외 비중 =========
    etf_trade_count = 0
    spac_trade_count = 0
    foreign_trade_count = 0
    for t in year_trades:
        sym = (t.symbol or "").upper()
        if any(p in sym for p in _ETF_PATTERNS):
            etf_trade_count += 1
        if any(k in (t.symbol or "") for k in _SPAC_KEYWORDS):
            spac_trade_count += 1
        if t.currency != "KRW":
            foreign_trade_count += 1

    etf_ratio = etf_trade_count / max(len(year_trades), 1)
    spac_ratio = spac_trade_count / max(len(year_trades), 1)
    foreign_ratio = foreign_trade_count / max(len(year_trades), 1)

    # ========= 당일/오버나잇 손익 =========
    intraday_pnl = sum(r["pnl"] for r in trips if r["holding_days"] == 0)
    overnight_pnl = sum(r["pnl"] for r in trips if r["holding_days"] > 0)
    intraday_count = sum(1 for r in trips if r["holding_days"] == 0)
    overnight_count = sum(1 for r in trips if r["holding_days"] > 0)

    # ========= 손익 집중도 (한 건이 차지하는 비중) =========
    top_gain_pnl = best["pnl"] if best else 0
    top_loss_pnl = worst["pnl"] if worst else 0
    single_gain_dominance = (top_gain_pnl / max(abs(total_pnl), 1)) if total_pnl != 0 else 0
    single_loss_dominance = (abs(top_loss_pnl) / max(abs(total_pnl), 1)) if total_pnl != 0 else 0

    # ========= 수익/손실 트립 합 =========
    sum_win_pnl = sum(r["pnl"] for r in trips if r["pnl"] > 0)
    sum_loss_pnl = sum(r["pnl"] for r in trips if r["pnl"] < 0)

    # ========= 평균 포지션 크기 =========
    buy_sizes_all = [t.amount for t in year_trades if t.side == "BUY"]
    avg_position_size = mean(buy_sizes_all) if buy_sizes_all else 0
    position_size_stdev = _safe_stdev(buy_sizes_all)
    position_size_cv = (position_size_stdev / avg_position_size) if avg_position_size > 0 else 0

    # ========= 첫 트립 vs 마지막 트립 =========
    sorted_trips_all = sorted(trips, key=lambda r: r["closed_at"])
    first_trip_overall = sorted_trips_all[0] if sorted_trips_all else None
    last_trip_overall = sorted_trips_all[-1] if sorted_trips_all else None

    # 최근 10건 승률
    recent_10 = sorted_trips_all[-10:] if len(sorted_trips_all) >= 10 else sorted_trips_all
    recent_10_win_rate = (sum(1 for r in recent_10 if r["pnl"] > 0) / len(recent_10) * 100) if recent_10 else 0

    # ========= 연속 손실 트립 =========
    max_consecutive_loss_trips = 0
    cur_loss_streak = 0
    for r in sorted_trips_all:
        if r["pnl"] < 0:
            cur_loss_streak += 1
            max_consecutive_loss_trips = max(max_consecutive_loss_trips, cur_loss_streak)
        else:
            cur_loss_streak = 0

    # ========= 전반기/후반기 손익 =========
    first_half_pnl = sum(r["pnl"] for r in trips if r["closed_at"][:7] <= f"{year}-06")
    second_half_pnl = sum(r["pnl"] for r in trips if r["closed_at"][:7] > f"{year}-06")

    # ========= 본전 대비 회복률 =========
    # MDD 이후 얼마나 회복했는가
    if equity_curve and mdd > 0:
        final_cum = equity_curve[-1]["cum"]
        trough = mdd_peak - mdd  # 최저점 누적 손익
        recovered = final_cum - trough  # 최저점 이후 회복한 금액
        recovery_pct = (recovered / mdd * 100) if mdd > 0 else 0  # 100% = 완전 회복
    else:
        recovery_pct = 0

    # ========= 평균 트립 크기 (금액) =========
    avg_trip_buy = mean([r["buy_amount"] for r in trips]) if trips else 0
    avg_trip_sell = mean([r["sell_amount"] for r in trips]) if trips else 0

    return {
        "empty": False,
        "year": year,
        "trade_count": len(year_trades),
        "trip_count": len(trips),
        "trade_days": trade_days,
        "ticker_count": ticker_count,

        "total_pnl": total_pnl,
        "total_buy": total_buy,
        "total_sell": total_sell,
        "total_fee": total_fee,
        "total_tax": total_tax,
        "cost_burden": total_fee + total_tax,

        "fav_ticker": {"ticker": fav_key, "symbol": fav_symbol, "count": fav_count, "pnl": fav_pnl},
        "best_trip": best,
        "worst_trip": worst,
        "best_ticker": best_ticker,
        "worst_ticker": worst_ticker,
        "best_month": best_month,
        "worst_month": worst_month,

        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": win_rate,
        "avg_win_pct": avg_win_pct,
        "avg_loss_pct": avg_loss_pct,
        "profit_factor": profit_factor,
        "required_win_rate": required_wr,

        "avg_holding_days": avg_hold,
        "max_holding_days": max_hold,
        "min_holding_days": min_hold,

        "max_trades_in_day": max_trades_in_day,
        "max_trades_in_day_date": max_trades_in_day_date,

        "revenge_count": revenge_count,
        "avg_reentry": avg_reentry,

        "open_position_count": len(opens),

        # 신규 지표
        "mdd": mdd,
        "mdd_peak": mdd_peak,
        "mdd_trough_date": mdd_trough_date,
        "equity_curve": equity_curve[-60:],
        "holding_buckets": buckets,
        "mistake_tags": tags,
        "rr_ratio": rr_ratio,
        "required_wr_from_rr": required_wr_from_rr,

        # 대폭 확장 메트릭
        "weekday_stats": weekday_stats,
        "best_weekday": best_weekday,
        "worst_weekday": worst_weekday,
        "monthly_pnl_stdev": monthly_pnl_stdev,
        "monthly_pnl_mean": monthly_pnl_mean,
        "active_months": active_months,
        "winning_months": winning_months,
        "losing_months": losing_months,
        "max_losing_months_streak": max_losing_months_streak,
        "max_winning_months_streak": max_winning_months_streak,
        "first_month": first_month[1] if first_month else None,
        "last_month": last_month[1] if last_month else None,
        "top1_volume_pct": top1_pct,
        "top3_volume_pct": top3_pct,
        "top5_volume_pct": top5_pct,
        "top10_volume_pct": top10_pct,
        "profitable_ticker_count": profitable_ticker_count,
        "losing_ticker_count": losing_ticker_count,
        "etf_trade_count": etf_trade_count,
        "etf_ratio": etf_ratio,
        "spac_trade_count": spac_trade_count,
        "spac_ratio": spac_ratio,
        "foreign_trade_count": foreign_trade_count,
        "foreign_ratio": foreign_ratio,
        "intraday_pnl": intraday_pnl,
        "overnight_pnl": overnight_pnl,
        "intraday_count": intraday_count,
        "overnight_count": overnight_count,
        "single_gain_dominance": single_gain_dominance,
        "single_loss_dominance": single_loss_dominance,
        "sum_win_pnl": sum_win_pnl,
        "sum_loss_pnl": sum_loss_pnl,
        "avg_position_size": avg_position_size,
        "position_size_stdev": position_size_stdev,
        "position_size_cv": position_size_cv,
        "first_trip_overall": first_trip_overall,
        "last_trip_overall": last_trip_overall,
        "recent_10_win_rate": recent_10_win_rate,
        "recovery_pct": recovery_pct,
        "avg_trip_buy": avg_trip_buy,
        "avg_trip_sell": avg_trip_sell,
        "max_consecutive_loss_trips": max_consecutive_loss_trips,
        "first_half_pnl": first_half_pnl,
        "second_half_pnl": second_half_pnl,
        "ticker_names": list(ticker_names),
    }


# 페르소나 데이터는 personas.py로 분리됨 (_P, _PERSONAS)




def _classify_persona(m: dict) -> dict:
    """
    51개 페르소나에 대한 점수제 매칭.
    메트릭 조합으로 가장 가까운 유형 1개 반환.
    매칭되는 규칙이 전혀 없으면 'explorer'/'balanced_trader' 폴백.
    """
    scores: dict[str, float] = {k: 0.0 for k in _PERSONAS}

    # ---- 빌드업 숏컷 ----
    tc = m["trade_count"]
    tp = m["total_pnl"]
    td = m["trade_days"]
    tkc = m["ticker_count"]
    wr = m["win_rate"]
    awp = m["avg_win_pct"]
    alp = m["avg_loss_pct"]
    ahd = m["avg_holding_days"]
    lhd = m["max_holding_days"]
    rev = m["revenge_count"]
    ar = m["avg_reentry"]
    req_wr = m["required_win_rate"]
    cb = m["cost_burden"]
    mdd = m["mdd"]
    r10 = m.get("recent_10_win_rate", 0)
    inr = m.get("intraday_pnl", 0)
    onr = m.get("overnight_pnl", 0)
    top1 = m.get("top1_volume_pct", 0)
    top3 = m.get("top3_volume_pct", 0)
    etf_r = m.get("etf_ratio", 0)
    spac_r = m.get("spac_ratio", 0)
    foreign_r = m.get("foreign_ratio", 0)
    same_day_count = m.get("intraday_count", 0)
    overnight_count = m.get("overnight_count", 0)
    first_half_pnl = m.get("first_half_pnl", 0) or 0
    second_half_pnl = m.get("second_half_pnl", 0) or 0
    best_month_pnl = m["best_month"]["pnl"] if m.get("best_month") else 0
    worst_month_pnl = m["worst_month"]["pnl"] if m.get("worst_month") else 0
    best_mkey = m["best_month"]["key"] if m.get("best_month") else ""
    worst_mkey = m["worst_month"]["key"] if m.get("worst_month") else ""
    mad = m["max_trades_in_day"]
    max_ws = m.get("max_winning_months_streak", 0)
    max_ls = m.get("max_losing_months_streak", 0)
    pos_cv = m.get("position_size_cv", 0)
    avg_pos_size = m.get("avg_position_size", 0)
    single_loss_dom = m.get("single_loss_dominance", 0)

    # ============ STYLE ============
    if tc > 500 and ahd < 3: scores["fire_moth"] += 6
    if tc > 300 and ahd < 2: scores["fire_moth"] += 3
    if cb > abs(tp) * 0.25 and ahd < 2: scores["fire_moth"] += 2

    if same_day_count > overnight_count * 3 and tc > 100: scores["scalper"] += 5
    if ahd < 1 and tc > 200: scores["scalper"] += 3

    if 3 <= ahd <= 10 and tp > 0: scores["swing_master"] += 5
    if 3 <= ahd <= 10 and wr > 50: scores["swing_master"] += 2

    if ahd > 30 and tp > -1_000_000: scores["diamond_hand"] += 6
    if ahd > 45: scores["diamond_hand"] += 2

    if ahd > 60: scores["long_term_believer"] += 6
    if ahd > 90 and tc < 50: scores["long_term_believer"] += 3

    if tc / max(td, 1) > 10: scores["gambler"] += 6
    if mad > 30: scores["gambler"] += 3
    if cb > abs(tp) * 0.5: scores["gambler"] += 2

    # ============ MISTAKE ============
    if awp > 0 and alp < 0:
        ratio = abs(alp) / max(awp, 0.1)
        if ratio > 1.5: scores["loss_holder"] += 5
        if ratio > 2.5: scores["loss_holder"] += 3
    if req_wr - wr > 15: scores["loss_holder"] += 3
    if req_wr > 65: scores["loss_holder"] += 2

    if rev >= 20: scores["revenge_trader"] += 6
    if rev >= 50: scores["revenge_trader"] += 3
    if rev >= 100: scores["revenge_trader"] += 3

    if ar >= 3: scores["average_down_master"] += 5
    if ar >= 5: scores["average_down_master"] += 3
    if tkc < 50 and ar >= 2: scores["average_down_master"] += 1

    if 0 < awp < 3: scores["panic_seller"] += 6
    if 0 < awp < 2: scores["panic_seller"] += 2

    if tkc > 80 and ahd < 5: scores["whale_chaser"] += 5
    if tkc > 120: scores["whale_chaser"] += 2

    if alp < -15 and m["loss_count"] >= 5: scores["frozen_holder"] += 5
    if m.get("avg_loss_pct", 0) < -10: scores["frozen_holder"] += 2

    # ============ TICKER ============
    if etf_r >= 0.3: scores["etf_safety"] += 6
    if etf_r >= 0.5: scores["etf_safety"] += 3

    if spac_r >= 0.05: scores["spac_gambler"] += 6

    if foreign_r >= 0.2: scores["foreign_believer"] += 6
    if foreign_r >= 0.4: scores["foreign_believer"] += 3

    # 테마주 헌터: 종목 수 많고 보유기간 짧음
    if tkc >= 50 and ahd < 5 and tp > -3_000_000: scores["theme_hunter"] += 4

    # 방산/바이오 — 종목 이름 패턴 매칭
    names = set(m.get("ticker_names", []))
    _BIO_KW = ("바이오", "제약", "셀트리온", "삼성바이오", "헬스", "메디", "진단", "임상", "HK이노엔")
    _DEF_KW = ("한화에어로", "LIG넥스원", "현대로템", "한국항공우주", "풍산", "한화시스템", "방산")
    bio_count = sum(1 for n in names if any(k in n for k in _BIO_KW))
    def_count = sum(1 for n in names if any(k in n for k in _DEF_KW))
    if bio_count >= 3: scores["bio_gambler"] += 5
    if bio_count >= 5: scores["bio_gambler"] += 3
    if def_count >= 2: scores["defense_patriot"] += 5
    if def_count >= 4: scores["defense_patriot"] += 3

    # 대형주 신봉자: 종목 수 적고 보유 길고 손실 크지 않음
    if tkc <= 15 and ahd >= 10 and tp > -2_000_000: scores["bluechip_believer"] += 3

    # 배당주 수집가: 매우 장기 + 종목 적음
    if ahd > 45 and tkc <= 20: scores["dividend_collector"] += 3

    # ============ RESULT ============
    if wr >= 70: scores["win_champion"] += 6
    if wr >= 60 and tp > 0: scores["win_champion"] += 2

    if wr < 40 and tp > 1_000_000: scores["one_shot_hunter"] += 6
    if wr < 35 and tp > 500_000: scores["one_shot_hunter"] += 2

    if wr < 35 and tp < -5_000_000 and tc > 50: scores["complete_beginner"] += 5
    if req_wr > 60 and wr < 40: scores["complete_beginner"] += 2

    # 그라인더: 수익률 꾸준, 작은 진폭, 총손익 양수
    if tp > 0 and 0 < awp < 5 and alp > -5 and tc >= 50: scores["grinder"] += 4
    if max_ws >= 3 and tp > 0: scores["grinder"] += 2

    if tp < -3_000_000 and max_ls >= 3: scores["chronic_loser"] += 5
    if m["losing_months"] > m["winning_months"] and tp < -2_000_000: scores["chronic_loser"] += 2

    # 성장형: 최근 10건 승률 > 전체 승률 OR 후반기 > 전반기
    if r10 > wr + 10 and r10 > 50: scores["growing_trader"] += 5
    if best_mkey > worst_mkey and best_mkey: scores["growing_trader"] += 2
    if second_half_pnl > first_half_pnl + 1_000_000: scores["growing_trader"] += 3

    # 퇴보형: 최근 10건 승률 < 전체 승률 OR 후반기 << 전반기
    if r10 + 10 < wr and r10 < 40: scores["declining_trader"] += 5
    if worst_mkey > best_mkey and worst_mkey and worst_month_pnl < -3_000_000:
        scores["declining_trader"] += 3
    if first_half_pnl > second_half_pnl + 2_000_000 and second_half_pnl < 0:
        scores["declining_trader"] += 3

    # ============ FREQUENCY ============
    if mad >= 50: scores["mad_day"] += 6
    if mad >= 100: scores["mad_day"] += 4

    if tc < 30: scores["quiet_trader"] += 5
    if tc < 15: scores["quiet_trader"] += 3

    if 30 <= tc <= 100 and td >= 20: scores["weekly_trader"] += 3

    # 월말 몰아치기: worst_month의 거래가 가장 많을 때 (간접)
    # (정확한 day 분포 필요하지만 간이)

    # ============ PSYCH ============
    if tc < 20 and tp < 0: scores["analysis_paralysis"] += 4

    if tc > 200 and pos_cv > 1.5: scores["impulse_trader"] += 5
    if rev >= 10 and cb > abs(tp) * 0.2: scores["impulse_trader"] += 3

    if tkc > 100 and ahd < 7: scores["trend_follower"] += 4

    if tkc < 20 and ar >= 4: scores["stubborn_holder"] += 5

    if alp < -10 and ahd > 15: scores["prayer_trader"] += 5

    if m["open_position_count"] >= 20: scores["dormant_holder"] += 4
    if m["open_position_count"] >= 50: scores["dormant_holder"] += 3

    # ============ SIZE ============
    # 한 종목 집중 + 손실 → 몰빵 마스터 (나쁜 케이스)
    if top1 >= 30 and tp < 0: scores["all_in_master"] += 6
    if top1 >= 50 and tp < 0: scores["all_in_master"] += 3
    # 한 종목 집중 + 이익 → 스윙 마스터 쪽 (좋은 케이스)
    if top1 >= 40 and tp > 0: scores["swing_master"] += 3
    if top1 >= 40 and tp > 0 and 3 <= ahd <= 10: scores["swing_master"] += 3

    if avg_pos_size < 500_000 and tc > 50: scores["micro_trader"] += 5
    if avg_pos_size < 300_000 and tc > 100: scores["micro_trader"] += 3

    # 분할매수 교과서: 재진입 평균 높지만 수익 나는 케이스
    if ar >= 3 and tp > 0 and wr > 50: scores["ladder_textbook"] += 5

    if cb > abs(tp) * 0.3: scores["fee_donator"] += 5
    if cb > abs(tp) * 0.5: scores["fee_donator"] += 3
    if cb > 5_000_000: scores["fee_donator"] += 2

    # ============ HOLDING ============
    if same_day_count > overnight_count * 4: scores["daily_runner"] += 5
    if ahd < 0.5 and tc > 100: scores["daily_runner"] += 3

    if ahd > 30 and m["open_position_count"] > 20: scores["months_ignorer"] += 4

    if tc > 50 and 5 <= tkc <= 30 and pos_cv < 1 and -2_000_000 < tp < 2_000_000:
        scores["timing_chaser"] += 3

    if ahd < 3 and tc > 200 and tkc > 50: scores["wave_surfer"] += 4

    # ============ SPECIAL ============
    if m["best_ticker"] and m["best_ticker"]["trip_count"] >= 3:
        if m["best_ticker"]["pnl"] > 3_000_000:
            scores["alpha_hunter"] += 5

    if single_loss_dom > 0.5: scores["black_hole_holder"] += 6
    if m["worst_ticker"] and m["worst_ticker"]["pnl"] < -5_000_000:
        scores["black_hole_holder"] += 3

    if mdd > abs(tp) * 1.5 and mdd > 10_000_000: scores["roller_coaster"] += 6
    if mdd > 20_000_000: scores["roller_coaster"] += 3

    if mdd < 5_000_000 and abs(tp) < 3_000_000 and tc > 20: scores["defensive_trader"] += 5
    if mdd < 2_000_000 and tc >= 50: scores["defensive_trader"] += 2

    if r10 >= 70 and r10 > wr: scores["hot_hand"] += 5

    if r10 <= 20 and tc >= 20: scores["cold_hand"] += 5

    # ============ FALLBACK ============
    # 다른 조건 없을 때만 발화되는 기본값들에 작은 점수
    if 30 <= wr <= 60 and abs(tp) < 3_000_000:
        scores["balanced_trader"] += 2
    if tc >= 20:
        scores["explorer"] += 1  # 항상 약간

    # ---- 최고 점수 페르소나 선택 ----
    best = max(scores.items(), key=lambda x: x[1])
    if best[1] < 2:
        best = ("explorer", 0)

    persona = dict(_PERSONAS[best[0]])
    persona["score"] = best[1]
    persona["traits"] = [
        f"평균 보유 {ahd:.1f}일",
        f"승률 {wr:.0f}%",
        f"평균 손절 {alp:.1f}%",
        f"평균 익절 +{awp:.1f}%",
    ]
    # 후보 상위 3개 (디버깅/대체 매칭용)
    sorted_scores = sorted(scores.items(), key=lambda x: -x[1])
    persona["runner_ups"] = [
        {"id": k, "name": _PERSONAS[k]["name"], "emoji": _PERSONAS[k]["emoji"], "score": v}
        for k, v in sorted_scores[1:4] if v > 0
    ]
    return persona


# ---------------------------- 진단 / 처방 ---------------------------- #

def _krw(n: float) -> str:
    return f"{round(n):,}원"


def _make_diagnoses(m: dict) -> list[dict]:
    diag: list[dict] = []

    # ================================================================
    # 25+개의 전체 진단 규칙. 사용자의 매매 전반을 다각도로 검진.
    # 매칭되는 것만 발화 → 유저마다 다르게 나옴.
    # ================================================================

    # 1) 손익비 역전
    if m["avg_win_pct"] > 0 and m["avg_loss_pct"] < 0:
        ratio = abs(m["avg_loss_pct"]) / max(m["avg_win_pct"], 0.1)
        if ratio > 1.2:
            diag.append({
                "severity": "critical",
                "emoji": "💀",
                "title": "손절은 두 배, 익절은 반토막",
                "body": (
                    f"평균 손절 {m['avg_loss_pct']:.1f}%, 평균 익절 +{m['avg_win_pct']:.1f}%. "
                    f"이 패턴으로 본전 치려면 승률 {m['required_win_rate']:.0f}%가 나와야 하는데, "
                    f"님 승률은 {m['win_rate']:.0f}%예요."
                ),
                "verdict": "수학적으로 망하는 공식 ㅠ",
                "fix": "손절 라인을 먼저 타이트하게 (-3~5%), 익절 목표는 손절의 최소 2배 (+6~10%)로.",
            })

    # 2) 복수매매
    if m["revenge_count"] >= 10:
        diag.append({
            "severity": "critical" if m["revenge_count"] >= 30 else "warning",
            "emoji": "⚔️",
            "title": "복수매매의 흔적",
            "body": (
                f"손절 직후 같은 종목 재진입이 {m['revenge_count']}회. "
                "시장은 복수를 허락하지 않아요. 방금 나한테 손절 맞춘 종목이 갑자기 친해질 리가 없거든요."
            ),
            "verdict": "복수는 시장이 아니라 나한테 돌아와요",
            "fix": "손절한 종목은 최소 3거래일 쳐다도 보지 말기. 차트 끄고 커피 마시기.",
        })

    # 3) 수수료 중독
    if m["cost_burden"] > 1_000_000:
        ratio_of_loss = abs(m["cost_burden"] / m["total_pnl"]) * 100 if m["total_pnl"] != 0 else 0
        diag.append({
            "severity": "warning",
            "emoji": "💸",
            "title": "토스에 낸 수수료 + 세금",
            "body": (
                f"{m['year']}년 한 해 동안 {_krw(m['cost_burden'])}. "
                f"({m['trade_count']}건 거래하면서 건당 평균 {_krw(m['cost_burden']/max(m['trade_count'],1))})."
            ),
            "verdict": "거래 빈도를 반으로 줄이면 이게 절반이에요",
            "fix": "하루에 3건 이상 거래 금지 룰 걸어보기. 도파민 참고 보유하기.",
        })

    # 4) 물타기
    if m["avg_reentry"] >= 3:
        diag.append({
            "severity": "warning",
            "emoji": "🧲",
            "title": "물타기 중독",
            "body": (
                f"한 종목 평균 재진입 {m['avg_reentry']:.1f}회. "
                "물을 타면 단가는 낮아지지만, 잘못된 종목에 물을 타면 손실도 곱하기가 돼요."
            ),
            "verdict": "물타기는 기업에 하는 게 아니라 잘못된 타이밍에 하는 거예요",
            "fix": "같은 종목 최대 2회 분할 매수로 제한. 세 번째부터는 '난 틀렸다' 신호로 받아들이기.",
        })

    # 5) 단타 과잉
    if m["trade_count"] / max(m["trade_days"], 1) > 5:
        per_day = m["trade_count"] / max(m["trade_days"], 1)
        diag.append({
            "severity": "warning",
            "emoji": "🎰",
            "title": "너무 바쁜 손",
            "body": (
                f"거래일 평균 {per_day:.1f}건. 최다는 {m['max_trades_in_day']}건/일. "
                "빈도가 많아질수록 실력보다 운의 비중이 커져요. 그리고 수수료는 확정적으로 나가요."
            ),
            "verdict": "많이 치는 건 실력이 아니라 조바심",
            "fix": "'하루 매수 1종목' 챌린지 해보기. 확신 없으면 아예 매매 스킵.",
        })

    # 6) 단일 종목 워스트
    if m["worst_ticker"] and m["worst_ticker"]["pnl"] < -2_000_000:
        t = m["worst_ticker"]
        diag.append({
            "severity": "info",
            "emoji": "🩹",
            "title": f"가장 아픈 이름: {t['symbol']}",
            "body": (
                f"이 종목 하나에서 {_krw(t['pnl'])}, {t['trip_count']}회 실현. "
                "기억해두세요. 이 이름이 다시 보이면, 한 번 더 생각하기로."
            ),
            "verdict": "같은 종목에 두 번 데이는 건 내 잘못",
            "fix": f"{t['symbol']}는 앞으로 감시 리스트에 넣지 말기. 아예 차트 즐겨찾기 삭제 추천.",
        })

    # 7) 종목 편식 (한 종목 비중이 너무 큼)
    if m["worst_ticker"] and m["total_buy"] > 0:
        # 워스트 종목 매매 금액이 전체의 20%+
        if m["worst_ticker"]["buy_amount"] / m["total_buy"] > 0.2:
            pct = m["worst_ticker"]["buy_amount"] / m["total_buy"] * 100
            diag.append({
                "severity": "warning",
                "emoji": "⚖️",
                "title": "한 종목 편식",
                "body": f"가장 많이 잃은 종목에 전체 매수액의 {pct:.0f}%를 썼어요. 한 종목 비중이 너무 커요.",
                "verdict": "한 종목이 계좌를 흔들면 포트폴리오가 아니에요.",
                "fix": "한 종목 매수 상한을 포트폴리오의 15%로 고정. 그 이상은 알람 걸기.",
            })

    # 8) 종목 남발 (너무 많은 종목 건드림)
    if m["ticker_count"] > 100:
        diag.append({
            "severity": "warning",
            "emoji": "🎯",
            "title": "너무 많은 종목",
            "body": f"한 해 동안 {m['ticker_count']}개 종목을 건드렸어요. 종목 수가 많을수록 나에게 맞는 스타일을 찾기 어려워요.",
            "verdict": "집중해야 이깁니다. 백화점 쇼핑은 돈이 새요.",
            "fix": "다음 달부터 관심 종목 20개 이내로 제한. 새 종목은 기존 종목 3개 이상 청산해야 추가 가능.",
        })

    # 9) MDD vs 총손실
    if m["mdd"] > 0 and abs(m["total_pnl"]) > 0:
        mdd_ratio = m["mdd"] / max(abs(m["total_pnl"]), 1)
        if m["total_pnl"] < 0 and mdd_ratio > 1.3:
            diag.append({
                "severity": "critical",
                "emoji": "📉",
                "title": "회복 실패",
                "body": f"최대 낙폭 {_krw(m['mdd'])}이 총손실 {_krw(m['total_pnl'])}보다 커요. 한때 훨씬 잘했는데 회복 못 한 거예요.",
                "verdict": "가장 아팠던 순간 이후의 판단이 더 나빴어요.",
                "fix": f"MDD 바닥({m['mdd_trough_date']}) 이후 무엇을 했는지 복기. 조급함이 작용했을 가능성.",
            })

    # 10) 3~7일 스윗스팟이 있는데 당일만 함
    if m["holding_buckets"]:
        buckets_with_data = [b for b in m["holding_buckets"] if b["count"] > 0]
        if buckets_with_data:
            best_bucket = max(buckets_with_data, key=lambda b: b["avg_return_pct"])
            worst_bucket = min(buckets_with_data, key=lambda b: b["avg_return_pct"])
            if best_bucket["avg_return_pct"] > 3 and worst_bucket["avg_return_pct"] < 0:
                if worst_bucket["id"] == "intraday" and best_bucket["id"] in ("3to7", "1to2w", "2wplus"):
                    diag.append({
                        "severity": "critical",
                        "emoji": "💡",
                        "title": "당신은 단타 체질이 아니에요",
                        "body": (
                            f"당일 매매는 평균 {worst_bucket['avg_return_pct']:+.1f}% (손실), "
                            f"하지만 {best_bucket['label']} 보유 시 평균 {best_bucket['avg_return_pct']:+.1f}% (승률 {best_bucket['win_rate']:.0f}%). "
                            "데이터가 명확해요."
                        ),
                        "verdict": "몸에 안 맞는 스타일을 고집하는 중.",
                        "fix": f"{best_bucket['label']} 보유를 기본 룰로. 당일 손바뀜 유혹이 오면 다음날까지 참기.",
                    })

    # 11) 단타만 해서 손실
    if m["avg_holding_days"] < 1 and m["total_pnl"] < -1_000_000:
        diag.append({
            "severity": "critical",
            "emoji": "⚡",
            "title": "초단타 집착",
            "body": f"평균 보유 {m['avg_holding_days']:.1f}일, 총 {_krw(m['total_pnl'])} 손실. 빠른 손이 돈을 벌어주진 않아요.",
            "verdict": "많이 치는 건 실력이 아니라 중독이에요.",
            "fix": "하루 1종목만 매수하고 최소 1일 이상 보유 룰. 당일 매도 금지.",
        })

    # 12) 시간 낭비 (거래는 많은데 PnL은 ~0)
    if m["trade_count"] > 200 and abs(m["total_pnl"]) < m["cost_burden"] * 0.5:
        diag.append({
            "severity": "warning",
            "emoji": "⏱️",
            "title": "시간만 쓰고 돈은 그대로",
            "body": f"{m['trade_count']}건을 쳤는데 순손익은 수수료 수준. 시간 낭비.",
            "verdict": "'거래'가 목적이면 이미 진 거예요.",
            "fix": "다음 달 목표: 거래 건수를 반으로. 대신 보유 시간을 2배로.",
        })

    # 13) 현재 연속 손절 상태
    # (recent losing streak — 마지막 5건이 모두 손실인지)
    # wrapped.py에는 trips가 없어서 간접적으로 — worst_month가 최근 월이면 알림
    if m["worst_month"] and m["best_month"]:
        if m["worst_month"]["key"] > m["best_month"]["key"]:
            diag.append({
                "severity": "warning",
                "emoji": "❄️",
                "title": "최근일수록 나빠짐",
                "body": f"{m['best_month']['key']}은 +{_krw(m['best_month']['pnl'])}, {m['worst_month']['key']}은 {_krw(m['worst_month']['pnl'])}. 흐름이 나빠지는 중.",
                "verdict": "잘하던 게 안 되면, 전략이 아니라 멘탈 문제일 가능성.",
                "fix": f"최근 가장 좋았던 {m['best_month']['key']}에 뭘 했는지 복기. 그때 지금 전략을 비교.",
            })

    # 14) 손절 대부분이 -10% 이상 큰 손실
    big_losses_expected_pct = 30  # 일반적으로 -10% 손실은 전체 손절의 10~20%가 정상
    # wrapped엔 raw trips가 없어서 mistake_tags로 추정
    if any(t["tag"] == "#손절미준수" for t in m.get("mistake_tags", [])):
        tag = next(t for t in m["mistake_tags"] if t["tag"] == "#손절미준수")
        diag.append({
            "severity": "critical",
            "emoji": "🩸",
            "title": "손절선을 지키지 않아요",
            "body": f"{tag['description']} -10% 이상 손실이 자주 나오면 손절이 늦다는 뜻.",
            "verdict": "손절을 '참는 게' 아니라 '실행'이에요.",
            "fix": "매수 시 자동 손절 주문을 걸어두기. 감정이 개입할 기회를 주지 말 것.",
        })

    # 15) 익절 성공 패턴
    if any(t["tag"] == "#익절성공" for t in m.get("mistake_tags", [])):
        tag = next(t for t in m["mistake_tags"] if t["tag"] == "#익절성공")
        diag.append({
            "severity": "info",
            "emoji": "🎯",
            "title": "10%+ 익절 경험이 있어요",
            "body": f"{tag['description']} 이 경험들이 내가 이길 수 있다는 증거예요.",
            "verdict": "10% 수익을 낸 적이 있다 = 더 낼 수 있다.",
            "fix": "10%+ 익절했던 종목들의 공통 패턴을 찾아서 매매 규칙화.",
        })

    # 16) 조급함 (하루 20건 이상)
    if m["max_trades_in_day"] >= 30:
        diag.append({
            "severity": "warning",
            "emoji": "🎰",
            "title": f"하루 {m['max_trades_in_day']}건 체결",
            "body": f"{m['max_trades_in_day_date']}에 하루 {m['max_trades_in_day']}건. 이게 정상인 사람은 없어요.",
            "verdict": "그 날의 손익 한 번 보세요. 대부분 마이너스예요.",
            "fix": "하루 거래 5건 제한 알람. 넘으면 강제로 앱 닫기.",
        })

    # 17) 승률 높은데 손실 (손익비 관리 실패)
    if m["win_rate"] >= 55 and m["total_pnl"] < 0:
        diag.append({
            "severity": "warning",
            "emoji": "🤔",
            "title": "많이 이겨도 지는 이유",
            "body": f"승률 {m['win_rate']:.0f}%인데 총 {_krw(m['total_pnl'])}. 이긴 판이 작고 진 판이 컸다는 뜻.",
            "verdict": "승률이 높아도 손익비가 나쁘면 결국 져요.",
            "fix": "'익절 먼저 짧게'가 아니라 '손절 먼저 짧게, 익절 길게'. 순서 반대.",
        })

    # 18) 승률 낮은데 수익 (운/한 방)
    if m["win_rate"] < 40 and m["total_pnl"] > 1_000_000:
        diag.append({
            "severity": "info",
            "emoji": "🍀",
            "title": "승률 낮은데 플러스",
            "body": f"승률 {m['win_rate']:.0f}%인데 {_krw(m['total_pnl'])} 플러스. 큰 수익 몇 번으로 커버된 구조.",
            "verdict": "구조는 이길 수 있게 만들어져 있어요. 근데 운에 가까울 수 있어요.",
            "fix": "큰 수익을 낸 트레이드의 공통점을 찾기. 재현 가능한 패턴이 있다면 승률도 올라와요.",
        })

    # 19) 미청산 포지션 많음
    if m["open_position_count"] >= 20:
        diag.append({
            "severity": "warning",
            "emoji": "📌",
            "title": f"미청산 {m['open_position_count']}종목",
            "body": f"아직 정리되지 않은 포지션이 {m['open_position_count']}개. 감시할 게 너무 많아요.",
            "verdict": "관심 종목 많을수록 집중력은 떨어져요.",
            "fix": "보유 중인 종목 중 -5% 이하인 건 지금 당장 손절 결정. 미련이 가장 비싸요.",
        })

    # 20) 종목 집중 성공
    if m["best_ticker"] and m["best_ticker"]["pnl"] > 3_000_000:
        t = m["best_ticker"]
        diag.append({
            "severity": "info",
            "emoji": "👑",
            "title": f"VIP 종목: {t['symbol']}",
            "body": f"이 종목 하나로 +{_krw(t['pnl'])}, {t['trip_count']}회 실현. 당신의 메인 수익원.",
            "verdict": "이 종목을 잘 안다는 증거.",
            "fix": f"{t['symbol']}에서의 패턴을 상세히 기록. 이것이 나만의 Edge예요.",
        })

    # 21) 3월 함몰 (특정 월 대형 손실)
    if m["worst_month"] and m["worst_month"]["pnl"] < -10_000_000:
        mkey = m["worst_month"]["key"]
        diag.append({
            "severity": "critical",
            "emoji": "💥",
            "title": f"{mkey} 함몰",
            "body": f"이 달에만 {_krw(m['worst_month']['pnl'])}, {m['worst_month']['count']}건. 한 달이 연간 손익을 망쳤어요.",
            "verdict": "단 한 달이 1년을 결정하면 위험 관리가 없는 거예요.",
            "fix": "월 손실 한도(예: 월 -5%)를 정하기. 달성하면 그 달은 휴장.",
        })

    # 22) 전체 실현손익 vs MDD (변동성 큰 경우)
    if m["mdd"] > abs(m["total_pnl"]) * 2 and m["total_pnl"] != 0:
        diag.append({
            "severity": "warning",
            "emoji": "🎢",
            "title": "심한 롤러코스터",
            "body": f"총 손익 {_krw(m['total_pnl'])} 대비 최대 낙폭 {_krw(m['mdd'])}. 진폭이 훨씬 커요.",
            "verdict": "총 손익이 작아보여도 그 과정이 고통스러웠어요.",
            "fix": "포지션 크기를 절반으로. 같은 손익이어도 덜 아프면 결정이 좋아져요.",
        })

    # 23) 평균 익절이 너무 작음 (조급한 익절)
    if m["avg_win_pct"] < 3 and m["win_count"] >= 10:
        diag.append({
            "severity": "warning",
            "emoji": "😱",
            "title": "공포 익절",
            "body": f"평균 익절 +{m['avg_win_pct']:.1f}%. 조금만 오르면 바로 팔아요. 확신이 부족하다는 신호.",
            "verdict": "익절이 너무 빠르면 큰 수익을 볼 수 없어요.",
            "fix": "익절 목표 +5% 이상 설정. 도달 전 매도 금지. 목표 깨면 즉시 손절.",
        })

    # 24) 평균 손절이 너무 큼 (손절 지연)
    if m["avg_loss_pct"] < -8 and m["loss_count"] >= 10:
        diag.append({
            "severity": "critical",
            "emoji": "🪨",
            "title": "손절을 참고 있음",
            "body": f"평균 손절 {m['avg_loss_pct']:.1f}%. 손실이 커질 때까지 버티고 있다는 증거.",
            "verdict": "희망은 전략이 아니에요.",
            "fix": "-3% 손절선 자동화. 감정이 개입할 수 없게 주문 자동화.",
        })

    # 25) 본전 치려면 승률이 불가능한 수준
    if m["required_win_rate"] > 65:
        diag.append({
            "severity": "critical",
            "emoji": "🧮",
            "title": "구조가 불가능한 승률 요구",
            "body": f"현재 손익비로 본전 치려면 승률 {m['required_win_rate']:.0f}% 필요. 프로도 못 내는 숫자.",
            "verdict": "이 구조로는 반복할수록 손해.",
            "fix": "손익비 자체를 바꿔야 해요. 손절 -3% / 익절 +9% 이상.",
        })

    # ═══════════════════════════════════════════════════════════
    # 확장 진단 (긍정/관찰/특이 케이스) — 모든 유저에게 말할 거리 제공
    # ═══════════════════════════════════════════════════════════

    # 26) 총손익 스펙트럼별 평가
    if m["total_pnl"] >= 10_000_000:
        diag.append({
            "severity": "positive", "emoji": "👑", "title": "올해의 대박",
            "body": f"**{_krw(m['total_pnl'])}** 실현. 대부분 주린이들이 만지지도 못하는 숫자를 냈어요.",
            "verdict": "이 성적은 자랑해도 됩니다.",
            "fix": "잘하는 이유를 **반드시 문서화**. 기록 없이는 반복 못 해요.",
        })
    elif m["total_pnl"] >= 3_000_000:
        diag.append({
            "severity": "positive", "emoji": "🏆", "title": "양호한 한 해",
            "body": f"실현손익 **+{_krw(m['total_pnl'])}**. 시장을 이겼어요.",
            "verdict": "꾸준함의 결과예요.",
            "fix": "지금 스타일을 바꾸지 마세요. 잘 되고 있을 때가 가장 바꾸기 쉬워요.",
        })
    elif m["total_pnl"] > 0:
        diag.append({
            "severity": "positive", "emoji": "✨", "title": "플러스로 마감",
            "body": f"**+{_krw(m['total_pnl'])}** 실현. 큰 수익은 아니지만 **적자는 아닌** 한 해.",
            "verdict": "본전만 쳐도 상위 30%예요.",
            "fix": "다음 해는 손익비 개선을 목표로. 수익률 +5% 돌파가 현실적인 다음 스텝.",
        })
    elif m["total_pnl"] >= -3_000_000:
        diag.append({
            "severity": "info", "emoji": "😐", "title": "작은 적자",
            "body": f"**{_krw(m['total_pnl'])}** 손실. 수업료 수준으로 보면 비싸지는 않아요.",
            "verdict": "배우면 돌려받을 수 있어요.",
            "fix": "이번 리포트에서 발견한 실수 중 **하나만** 다음 달부터 고치기.",
        })

    # 27) 승률 칭찬
    if m["win_rate"] >= 60 and m["trip_count"] >= 10:
        diag.append({
            "severity": "positive", "emoji": "🎯", "title": f"승률 {m['win_rate']:.0f}% — 명사수",
            "body": f"10건 이상 거래했는데 **{m['win_rate']:.0f}%** 승률. 엔트리 타점이 정교하다는 증거.",
            "verdict": "이게 '운'이 아니라 '실력' 구간.",
            "fix": "지금 승률을 유지하면서 **익절 폭을 늘려보세요**. 수익이 배가 됩니다.",
        })

    # 28) 승률 구조적 문제
    if m["win_rate"] < 30 and m["trip_count"] >= 10:
        diag.append({
            "severity": "critical", "emoji": "🚨", "title": f"승률 {m['win_rate']:.0f}% — 구조적 문제",
            "body": f"10건 중 **{10 - round(m['win_rate']/10)}건 이상 손절**. 종목 선정 프로세스가 작동 안 해요.",
            "verdict": "시장을 역방향으로 치고 있을 가능성.",
            "fix": "매매 **중단 후 1주일 복기**. 이긴 매매 3개, 진 매매 3개를 나란히 비교해보기.",
        })

    # 29) 손익비 우수
    if m.get("rr_ratio") and m["rr_ratio"] >= 2.0:
        diag.append({
            "severity": "positive", "emoji": "⚖️", "title": "손익비 우수",
            "body": f"평균 익절 **+{m['avg_win_pct']:.1f}%** / 평균 손절 **{m['avg_loss_pct']:.1f}%** → 손익비 1:{m['rr_ratio']:.2f}",
            "verdict": "이 구조면 승률 35%만 나와도 벌어요.",
            "fix": "손익비 유지하면서 **거래 건수를 늘리면** 총 수익이 배증.",
        })

    # 30) 보유 기간 스윗스팟 (3~7일에서 잘함)
    if m.get("holding_buckets"):
        three_to_seven = next((b for b in m["holding_buckets"] if b["id"] == "3to7"), None)
        if three_to_seven and three_to_seven["count"] >= 5 and three_to_seven["avg_return_pct"] >= 5:
            diag.append({
                "severity": "positive", "emoji": "🎯", "title": "스윗스팟: 3~7일 보유",
                "body": f"**3~7일 구간**에서 {three_to_seven['count']}건, 평균 **+{three_to_seven['avg_return_pct']:.1f}%**, 승률 **{three_to_seven['win_rate']:.0f}%**.",
                "verdict": "이 구간이 당신의 정답 구간.",
                "fix": "이 구간 비중을 더 높이고, **당일 매매는 줄이기**.",
            })

    # 31) MDD 프로급
    if m.get("mdd", 0) < 2_000_000 and m["trade_count"] >= 30:
        diag.append({
            "severity": "positive", "emoji": "🛡️", "title": "리스크 관리 프로급",
            "body": f"최대 낙폭 **{_krw(m['mdd'])}** — 거래 {m['trade_count']}건 치고 극도로 안정적.",
            "verdict": "방어의 달인.",
            "fix": "이제 **조심스런 공격** 단계. 확신 있는 종목엔 사이즈 늘려보기.",
        })

    # 32) 비용 효율 우수
    if m["cost_burden"] < 500_000 and m["trade_count"] >= 20:
        diag.append({
            "severity": "positive", "emoji": "💎", "title": "비용 구조 효율적",
            "body": f"수수료+세금 **{_krw(m['cost_burden'])}** — 거래 {m['trade_count']}건 치고 거의 안 쓴 셈.",
            "verdict": "가성비 매매.",
            "fix": "이 저비용 구조를 유지하면서 수익 기회를 늘리는 방향으로.",
        })

    # 33) 월별 연승 스트릭
    if m.get("max_winning_months_streak", 0) >= 3 and m["total_pnl"] > 0:
        diag.append({
            "severity": "positive", "emoji": "📈", "title": f"연속 {m['max_winning_months_streak']}개월 수익",
            "body": f"최대 **{m['max_winning_months_streak']}개월 연속** 플러스. 이건 운 아닙니다.",
            "verdict": "꾸준함이 만드는 복리의 증거.",
            "fix": "그 시기에 뭘 했는지 **매매 일지에 기록**. 나만의 시스템이 될 수 있어요.",
        })

    # 34) 월별 연속 손실
    if m.get("max_losing_months_streak", 0) >= 3:
        diag.append({
            "severity": "warning", "emoji": "❄️", "title": f"연속 {m['max_losing_months_streak']}개월 손실",
            "body": "3개월 이상 연속 마이너스. 일시적 슬럼프가 아닌 **구조적 신호**.",
            "verdict": "같은 패턴이 반복되면 운이 아니라 방식이에요.",
            "fix": "**일주일 휴식** 후 이 리포트 다시 읽기. 그 동안 시장 관전만.",
        })

    # 35) 종목 수 적극 탐색
    if 50 < m["ticker_count"] <= 100:
        diag.append({
            "severity": "info", "emoji": "🗺️", "title": f"{m['ticker_count']}개 종목 탐색",
            "body": "종목을 적극적으로 탐색한 한 해. 경험치는 쌓였지만 **집중력**은 분산.",
            "verdict": "넓이 vs 깊이의 트레이드오프.",
            "fix": "내년엔 **잘 맞는 20종목**만 골라서 집중해보기.",
        })

    # 36) 종목 수 적정 + 수익
    if 10 <= m["ticker_count"] <= 30 and m["total_pnl"] >= 0:
        diag.append({
            "severity": "positive", "emoji": "🎯", "title": "집중력 있는 종목 선택",
            "body": f"**{m['ticker_count']}개** 종목만으로 {_krw(m['total_pnl'])} 실현.",
            "verdict": "종목 수는 적고 수익은 나옴 — 이상적.",
            "fix": "이 숫자를 유지하고 **각 종목을 깊이** 파보기.",
        })

    # 37) 초단타 성향 경고
    if m["avg_holding_days"] < 1 and m["trade_count"] >= 100:
        diag.append({
            "severity": "warning", "emoji": "⚡", "title": "평균 보유 하루 미만",
            "body": f"평균 **{m['avg_holding_days']:.1f}일** 보유. 거의 모든 거래가 당일 청산.",
            "verdict": "스캘핑인지 조바심인지 구분 필요.",
            "fix": "잘되는 날은 그대로, **안 되는 날은 건드리지 말기**.",
        })

    # 38) 장기 보유 성향
    if m["avg_holding_days"] > 30:
        diag.append({
            "severity": "info", "emoji": "💎", "title": f"평균 {m['avg_holding_days']:.0f}일 보유",
            "body": "장기 보유 스타일. 단기 흔들림에 덜 휘둘리는 장점.",
            "verdict": "인내심이 자산.",
            "fix": "보유 중 종목의 **스토리가 깨지면** 즉시 재평가. 기다림은 근거가 있을 때만.",
        })

    # 39) 최애 종목 집착
    fav = m.get("fav_ticker")
    if fav and fav["count"] >= 20:
        diag.append({
            "severity": "info", "emoji": "❤️", "title": f"{fav['symbol']}에게 몰입",
            "body": f"이 종목에 **{fav['count']}번** 돌아왔어요. 손익 {_krw(fav['pnl'])}.",
            "verdict": "집중 vs 집착의 경계.",
            "fix": "이 종목에서 **뭘 봤는지** 매번 기록. 근거가 반복되면 실력, 충동이면 중독.",
        })

    # 40) 한 종목 몰빵
    if m.get("top1_volume_pct", 0) >= 50:
        diag.append({
            "severity": "warning", "emoji": "🦣", "title": f"한 종목이 전체 매수의 {m['top1_volume_pct']:.0f}%",
            "body": "집중이 지나치면 포트폴리오가 아니라 단일 베팅이에요.",
            "verdict": "한 종목이 계좌 전체를 결정하는 건 도박.",
            "fix": "한 종목 비중 **20% 이하**로 룰 만들기.",
        })

    # 41) 거의 원 종목 투자
    if m.get("top1_volume_pct", 0) >= 90:
        diag.append({
            "severity": "critical", "emoji": "🎯", "title": "사실상 단일 종목 투자",
            "body": "거의 모든 매수가 한 종목에 집중. 성공하면 대박, 실패하면 완전 손실.",
            "verdict": "이건 투자가 아니라 예언이에요.",
            "fix": "최소 **3~5 종목**으로 분산. 믿음이 강할수록 분산이 필요해요.",
        })

    # 42) 해외주식 비중
    if m.get("foreign_ratio", 0) >= 0.3:
        diag.append({
            "severity": "info", "emoji": "🌏", "title": f"해외주식 {m['foreign_ratio']*100:.0f}% 비중",
            "body": "국내주식만 보는 사람보다 시야가 넓어요. 단, **환율 변동**도 손익에 포함돼요.",
            "verdict": "글로벌 마인드.",
            "fix": "환율 변동폭을 **월 단위로 체크**. 수익률 계산에 꼭 포함.",
        })

    # 43) ETF 비중 높음
    if m.get("etf_ratio", 0) >= 0.3:
        diag.append({
            "severity": "info", "emoji": "📊", "title": f"ETF 비중 {m['etf_ratio']*100:.0f}%",
            "body": "개별 종목보다 지수/테마 투자 스타일. 분산이 자동.",
            "verdict": "건전한 접근.",
            "fix": "테마 ETF는 **집중 리스크**가 있으니 광범위 지수 ETF와 섞기.",
        })

    # 44) 스팩 거래
    if m.get("spac_trade_count", 0) >= 3:
        diag.append({
            "severity": "warning", "emoji": "🎲", "title": f"스팩 거래 {m['spac_trade_count']}회",
            "body": "스팩은 합병 대상 발표 전엔 방향성이 없어요. 변동성만 존재.",
            "verdict": "스팩은 '투자'가 아니라 '이벤트 베팅'.",
            "fix": "스팩 총 비중 **5% 이내**로 제한.",
        })

    # 45) 총 수익률 평가
    if m["total_buy"] > 0:
        pnl_ratio = m["total_pnl"] / m["total_buy"] * 100
        if pnl_ratio >= 10:
            diag.append({
                "severity": "positive", "emoji": "🚀", "title": f"총 수익률 +{pnl_ratio:.1f}%",
                "body": "매수 대비 **두 자릿수 수익률**. KOSPI 연간 평균을 상회.",
                "verdict": "알파 존재.",
                "fix": "이 성과를 **연속 달성**하는 게 진짜 실력. 한 해로 만족하지 말기.",
            })
        elif pnl_ratio >= 3:
            diag.append({
                "severity": "positive", "emoji": "📈", "title": f"총 수익률 +{pnl_ratio:.1f}%",
                "body": "매수 대비 양호한 수익률. 은행 이자 이상.",
                "verdict": "시장에서 살아남는 중.",
                "fix": "리스크 관리를 유지하면서 사이즈 단계적으로 늘리기.",
            })
        elif pnl_ratio <= -10:
            diag.append({
                "severity": "critical", "emoji": "📉", "title": f"총 수익률 {pnl_ratio:.1f}%",
                "body": f"매수 **{_krw(m['total_buy'])}** 대비 손실 {pnl_ratio:.1f}%. 구조적 검토가 필요.",
                "verdict": "이건 시장 탓이 아니에요.",
                "fix": "매매 일시 중단 후 **전략 완전히 재구성**.",
            })

    # 46) 하루 최대 거래 경계
    mad = m.get("max_trades_in_day", 0)
    if 50 <= mad < 100:
        diag.append({
            "severity": "warning", "emoji": "🌪️", "title": f"하루 최대 {mad}건",
            "body": f"{m.get('max_trades_in_day_date', '')}에 하루 **{mad}건** 체결. 보통은 아닌 날.",
            "verdict": "이날이 수익/손실 기록을 남긴 날일 가능성.",
            "fix": "그 날 손익 복기. 감정 매매면 향후 **하루 20건 캡**.",
        })

    # 47) 미청산 적정
    if 5 <= m["open_position_count"] <= 15:
        diag.append({
            "severity": "info", "emoji": "📌", "title": f"현재 {m['open_position_count']}종목 보유",
            "body": "관리 가능한 포지션 수. 감시가 현실적.",
            "verdict": "적정 로드.",
            "fix": "각 포지션의 **손절선**을 지금 당장 써두기.",
        })

    # 48) 미청산 과다
    if m["open_position_count"] > 30:
        diag.append({
            "severity": "warning", "emoji": "📚", "title": f"{m['open_position_count']}종목 미청산",
            "body": "감시할 게 너무 많아요. 집중도 ↓ 실수 확률 ↑",
            "verdict": "포트폴리오가 아니라 종목 수집.",
            "fix": "**−5% 이하** 포지션 지금 정리.",
        })

    # 49) MVP 종목
    bt = m.get("best_ticker")
    if bt and bt["pnl"] >= 3_000_000:
        diag.append({
            "severity": "positive", "emoji": "⭐", "title": f"MVP: {bt['symbol']}",
            "body": f"이 종목 하나로 **+{_krw(bt['pnl'])}**, {bt['trip_count']}회 실현. 포트폴리오의 주인공.",
            "verdict": "당신의 Edge가 이 종목에 있음.",
            "fix": f"{bt['symbol']}에서 **뭘 봤는지** 문서화. 이게 당신의 알파.",
        })

    # 50) 공포 익절
    if 0 < m["avg_win_pct"] < 2:
        diag.append({
            "severity": "warning", "emoji": "😱", "title": f"평균 익절 +{m['avg_win_pct']:.1f}%",
            "body": "살짝만 올라도 바로 털어버리는 패턴. 큰 수익을 경험할 수 없는 구조.",
            "verdict": "작은 수익에 만족 = 큰 수익을 놓침.",
            "fix": "**익절 목표 +5% 이상**으로 고정. 도달 전엔 매도 금지.",
        })

    # severity 우선순위로 정렬 (critical → warning → info → positive)
    severity_order = {"critical": 0, "warning": 1, "info": 2, "positive": 3}
    diag.sort(key=lambda d: severity_order.get(d.get("severity", "info"), 2))
    # 최대 15개
    return diag[:15]


# ---------------------------- 처방 (매매 원칙) ---------------------------- #

def _make_principles(m: dict, persona_id: str) -> list[dict]:
    """페르소나 + 메트릭에 맞는 매매 원칙 3~5개."""
    rules: list[dict] = []

    # 공통: 손익비
    if m["avg_win_pct"] > 0 and m["avg_loss_pct"] < 0 and abs(m["avg_loss_pct"]) > m["avg_win_pct"]:
        rules.append({
            "emoji": "✂️",
            "title": "손절 먼저, 익절 두 배",
            "detail": "매매 들어가기 전에 손절 라인 (-3~5%)을 먼저 정하고, 익절 목표는 그 두 배 이상으로.",
        })

    # 페르소나별
    if persona_id in ("fire_moth", "gambler"):
        rules.append({
            "emoji": "⏱️",
            "title": "하루 3건 이하 룰",
            "detail": "많이 치는 건 실력이 아니에요. 도파민 말고 확신에만 반응하기.",
        })
    if persona_id in ("revenge_trader",):
        rules.append({
            "emoji": "🚫",
            "title": "손절 후 3일 금지",
            "detail": "방금 손절한 종목은 3거래일 동안 쳐다보지 않기. 감정이 식어야 판단도 돌아옵니다.",
        })
    if persona_id in ("average_down_master",):
        rules.append({
            "emoji": "2️⃣",
            "title": "같은 종목 매수는 최대 2번",
            "detail": "3번째 매수는 '난 틀렸다' 신호. 물타기 대신 손절이 답.",
        })
    if persona_id in ("loss_holder", "panic_seller"):
        rules.append({
            "emoji": "📋",
            "title": "매수 전 손절·목표가 같이 쓰기",
            "detail": "매수 시점에 '얼마에 팔지'를 함께 적어두지 않으면, 감정이 움직이는 대로 팔게 돼요.",
        })

    # 공통: 기록
    rules.append({
        "emoji": "📓",
        "title": "매매 이유 한 줄",
        "detail": "매수할 때 '왜 사는지' 한 줄 메모. 한 달 뒤에 다시 읽으면 제 실수가 보여요.",
    })

    # 공통: 사이즈
    if m["trade_count"] > 200:
        rules.append({
            "emoji": "🎯",
            "title": "집중하면 덜 잃는다",
            "detail": f"2026년에 {m['ticker_count']}개 종목을 건드렸어요. 10종목 이내로 줄여보세요.",
        })

    return rules[:5]


# ---------------------------- Wrapped 빌더 ---------------------------- #

def _clamp(n: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, n))


def _compute_rpg_stats(m: dict) -> dict:
    """
    메트릭을 RPG 게임 스탯으로 변환.
    STR/DEX/INT/LUK/HP/MENT 각 0~100, LEVEL은 거래 경험치 기반.
    """
    tc = m["trade_count"]
    tp = m["total_pnl"]
    tb = max(m["total_buy"], 1)
    wr = m["win_rate"]
    awp = m.get("avg_win_pct", 0)
    alp = m.get("avg_loss_pct", 0)
    ahd = m.get("avg_holding_days", 0)
    mdd = m.get("mdd", 0)
    rev = m.get("revenge_count", 0)
    cb = m.get("cost_burden", 0)
    ar = m.get("avg_reentry", 0)
    pnl_ratio = (tp / tb * 100)  # 매수 대비 수익률 %

    # ⚡ 실행력 (STR) — 매매 빈도 (많을수록 높지만 100 cap)
    if tc < 30:
        str_val = tc * 0.8
    elif tc < 100:
        str_val = 24 + (tc - 30) * 0.5
    elif tc < 300:
        str_val = 59 + (tc - 100) * 0.12
    else:
        str_val = 83 + min(tc - 300, 1000) * 0.017
    str_val = _clamp(str_val)

    # 🏃 손절 민첩성 (DEX) — 손절이 빠를수록 높음 (avg_loss_pct가 0에 가까울수록 good)
    if alp < 0:
        dex_val = _clamp(100 + alp * 8)  # -3%면 76, -10%면 20, -15%면 -20→0
    else:
        dex_val = 100
    dex_val = _clamp(dex_val)

    # 🧠 분석력 (INT) — 승률 + 손익비 가중
    int_base = wr  # 0~100
    rr = (awp / abs(alp)) if (alp < 0 and awp > 0) else 1.0
    rr_bonus = _clamp((rr - 1) * 20, -20, 20)
    int_val = _clamp(int_base + rr_bonus)

    # 🍀 수익성 (LUK) — 총 수익률 기반
    if pnl_ratio >= 0:
        luk_val = _clamp(50 + pnl_ratio * 2.5)
    else:
        luk_val = _clamp(50 + pnl_ratio * 5)
    luk_val = _clamp(luk_val)

    # 💀 HP — 손실이 클수록 낮음
    if tp >= 0:
        hp = _clamp(80 + pnl_ratio * 2, 80, 100)
    else:
        hp = _clamp(80 + pnl_ratio * 3, 0, 80)

    # 🧘 MENT (멘탈) — MDD, 복수매매, 연속손절 등 감정 지표
    ment = 100
    if mdd > 0 and tb > 0:
        ment -= min((mdd / tb) * 200, 40)  # MDD 비율 차감
    if rev > 10:
        ment -= min(rev * 0.3, 30)  # 복수매매 차감
    if ar > 3:
        ment -= min((ar - 3) * 5, 20)  # 물타기 차감
    ment = _clamp(ment)

    # LEVEL — 거래 경험치 기반 (1~99)
    level = int(_clamp(1 + (tc ** 0.55) * 2.5, 1, 99))
    xp_current = tc - int((level - 1) ** (1 / 0.55) / 2.5) if level > 1 else 0
    xp_next = int((level) ** (1 / 0.55) / 2.5) - int((level - 1) ** (1 / 0.55) / 2.5)

    return {
        "level": level,
        "xp_current": max(0, xp_current),
        "xp_next": max(xp_next, 1),
        "hp": round(hp),
        "ment": round(ment),
        "stats": [
            {"key": "STR", "name": "실행력", "emoji": "⚡", "value": round(str_val),
             "description": f"매매 빈도 ({tc}건)"},
            {"key": "DEX", "name": "민첩성", "emoji": "🏃", "value": round(dex_val),
             "description": f"손절 속도 (평균 {alp:.1f}%)"},
            {"key": "INT", "name": "분석력", "emoji": "🧠", "value": round(int_val),
             "description": f"승률 {wr:.0f}% · 손익비 1:{rr:.2f}" if rr else f"승률 {wr:.0f}%"},
            {"key": "LUK", "name": "수익성", "emoji": "🍀", "value": round(luk_val),
             "description": f"총 수익률 {pnl_ratio:+.1f}%"},
        ],
    }


def _compute_titles(m: dict) -> list[dict]:
    """
    조건 만족 시 부여되는 타이틀(칭호). 여러 개 동시 가능.
    """
    titles: list[dict] = []
    tc = m["trade_count"]
    tp = m["total_pnl"]
    wr = m["win_rate"]
    rev = m.get("revenge_count", 0)
    cb = m.get("cost_burden", 0)
    max_mad = m.get("max_trades_in_day", 0)
    ar = m.get("avg_reentry", 0)
    mdd = m.get("mdd", 0)
    alp = m.get("avg_loss_pct", 0)
    awp = m.get("avg_win_pct", 0)
    best_t = m.get("best_ticker")
    worst_t = m.get("worst_ticker")

    if tc >= 1000:
        titles.append({"emoji": "🌪️", "name": "광란의 사도", "color": "#f59e0b", "rarity": "레전더리"})
    elif tc >= 500:
        titles.append({"emoji": "🔥", "name": "트리거 중독자", "color": "#dc2626", "rarity": "에픽"})
    elif tc >= 100:
        titles.append({"emoji": "⚡", "name": "부지런한 손", "color": "#0ea5e9", "rarity": "레어"})

    if max_mad >= 100:
        titles.append({"emoji": "🎰", "name": "도파민 원숭이", "color": "#ef4444", "rarity": "에픽"})
    elif max_mad >= 50:
        titles.append({"emoji": "🎲", "name": "속공러", "color": "#f97316", "rarity": "레어"})

    if rev >= 100:
        titles.append({"emoji": "⚔️", "name": "복수의 화신", "color": "#991b1b", "rarity": "레전더리"})
    elif rev >= 30:
        titles.append({"emoji": "🗡️", "name": "복수매매러", "color": "#dc2626", "rarity": "에픽"})
    elif rev >= 10:
        titles.append({"emoji": "🎯", "name": "재진입 전문가", "color": "#f97316", "rarity": "레어"})

    if cb >= 5_000_000:
        titles.append({"emoji": "💸", "name": "토스 VVIP", "color": "#7c3aed", "rarity": "전설"})
    elif cb >= 1_000_000:
        titles.append({"emoji": "💰", "name": "수수료 기부자", "color": "#a855f7", "rarity": "에픽"})

    if ar >= 5:
        titles.append({"emoji": "🧲", "name": "물타기 장인", "color": "#0284c7", "rarity": "에픽"})

    if mdd >= 50_000_000:
        titles.append({"emoji": "🎢", "name": "롤러코스터 정회원", "color": "#db2777", "rarity": "레전더리"})
    elif mdd >= 20_000_000:
        titles.append({"emoji": "📉", "name": "큰 파도 타기", "color": "#e11d48", "rarity": "에픽"})

    # 긍정 타이틀
    if tp > 10_000_000:
        titles.append({"emoji": "👑", "name": "수익 킹", "color": "#eab308", "rarity": "레전더리"})
    elif tp > 3_000_000:
        titles.append({"emoji": "🏆", "name": "흑자 트레이더", "color": "#ca8a04", "rarity": "에픽"})
    elif tp > 0:
        titles.append({"emoji": "✨", "name": "본전 사수자", "color": "#16a34a", "rarity": "레어"})

    if wr >= 70 and tc >= 20:
        titles.append({"emoji": "🎯", "name": "명사수", "color": "#15803d", "rarity": "에픽"})
    elif wr >= 60 and tc >= 20:
        titles.append({"emoji": "🏹", "name": "적중률 장인", "color": "#16a34a", "rarity": "레어"})

    if awp > 0 and alp < 0 and (awp / abs(alp)) >= 2:
        titles.append({"emoji": "⚖️", "name": "손익비의 달인", "color": "#059669", "rarity": "에픽"})

    if best_t and best_t["pnl"] >= 5_000_000:
        titles.append({"emoji": "⭐", "name": f"'{best_t['symbol']}' 마스터", "color": "#f59e0b", "rarity": "에픽"})

    if worst_t and worst_t["pnl"] <= -5_000_000:
        titles.append({"emoji": "☠️", "name": f"'{worst_t['symbol']}' 숙적", "color": "#1e293b", "rarity": "에픽"})

    return titles[:6]  # 최대 6개


def _compute_buffs(m: dict) -> list[dict]:
    """
    현재 적용 중인 디버프/버프 상태.
    """
    buffs: list[dict] = []
    rev = m.get("revenge_count", 0)
    max_mad = m.get("max_trades_in_day", 0)
    alp = m.get("avg_loss_pct", 0)
    ahd = m.get("avg_holding_days", 0)
    tp = m.get("total_pnl", 0)
    cb = m.get("cost_burden", 0)
    wr = m.get("win_rate", 0)
    open_pos = m.get("open_position_count", 0)

    # 디버프
    if rev >= 30:
        buffs.append({
            "type": "debuff", "emoji": "💀", "name": "복수의 저주",
            "description": f"복수매매 {rev}회. 같은 실수 반복 중.",
        })
    if alp < -8:
        buffs.append({
            "type": "debuff", "emoji": "🪨", "name": "손절 불능",
            "description": f"평균 손절 {alp:.1f}%. 손실이 불어나는 중.",
        })
    if cb > abs(tp) * 0.3 and tp != 0:
        buffs.append({
            "type": "debuff", "emoji": "💸", "name": "수수료 출혈",
            "description": f"비용 {_krw(cb)}이(가) 수익의 30%+ 잠식",
        })
    if ahd < 1 and tp < 0:
        buffs.append({
            "type": "debuff", "emoji": "⚡", "name": "단타 피로",
            "description": "평균 보유 1일 미만 + 손실",
        })
    if open_pos >= 30:
        buffs.append({
            "type": "debuff", "emoji": "📌", "name": "미청산 과적",
            "description": f"보유 중 {open_pos}종목. 관리 과부하.",
        })

    # 버프
    if wr >= 60:
        buffs.append({
            "type": "buff", "emoji": "🎯", "name": "핫 핸드",
            "description": f"승률 {wr:.0f}% — 감 잡은 상태",
        })
    if tp > 3_000_000:
        buffs.append({
            "type": "buff", "emoji": "💰", "name": "수익 모드",
            "description": f"누적 +{_krw(tp)}",
        })
    if 3 <= ahd <= 10 and tp > 0:
        buffs.append({
            "type": "buff", "emoji": "🎯", "name": "스윗스팟",
            "description": f"평균 {ahd:.1f}일 보유 + 수익",
        })

    return buffs[:5]


def _percentile_label(value: float, thresholds: list[tuple[float, str, str]]) -> tuple[str, str]:
    """
    value와 임계값 리스트로부터 (레이블, 등급) 반환.
    thresholds: [(threshold, label, grade), ...] — 오름차순.
    """
    for th, label, grade in thresholds:
        if value <= th:
            return label, grade
    return thresholds[-1][1], thresholds[-1][2]


def _compute_rank_indexes(m: dict) -> list[dict]:
    """
    '손절 불능 지수 상위 X%' 같은 랭킹 인덱스.
    """
    ranks: list[dict] = []

    rev = m.get("revenge_count", 0)
    if rev > 0:
        label, grade = _percentile_label(rev, [
            (5, "하위권", "D"),
            (20, "평균", "C"),
            (50, "주의", "B"),
            (100, "상위 5%", "A"),
            (200, "상위 1%", "S"),
            (float("inf"), "상위 0.1%", "SS"),
        ])
        ranks.append({
            "id": "revenge_index",
            "title": "복수매매 지수",
            "emoji": "⚔️",
            "value": rev,
            "value_label": f"{rev}회",
            "rank": label,
            "grade": grade,
            "description": "손절 직후 같은 종목 재진입 횟수",
            "color_start": "#991b1b",
            "color_end": "#450a0a",
        })

    alp = m.get("avg_loss_pct", 0)
    if alp < 0:
        label, grade = _percentile_label(abs(alp), [
            (3, "훌륭함", "S"),
            (5, "양호", "A"),
            (8, "주의", "B"),
            (12, "심각", "C"),
            (float("inf"), "재앙", "D"),
        ])
        ranks.append({
            "id": "loss_cut_index",
            "title": "손절 불능 지수",
            "emoji": "🪨",
            "value": abs(alp),
            "value_label": f"{alp:.1f}%",
            "rank": label,
            "grade": grade,
            "description": "평균 손절 폭",
            "color_start": "#1e293b",
            "color_end": "#0f172a",
        })

    ar = m.get("avg_reentry", 0)
    if ar > 0:
        label, grade = _percentile_label(ar, [
            (1.5, "깔끔", "S"),
            (2.5, "적정", "A"),
            (4, "반복", "B"),
            (6, "집착", "C"),
            (float("inf"), "중독", "D"),
        ])
        ranks.append({
            "id": "reentry_index",
            "title": "물타기 깊이",
            "emoji": "🧲",
            "value": ar,
            "value_label": f"{ar:.1f}회",
            "rank": label,
            "grade": grade,
            "description": "종목당 평균 재진입 횟수",
            "color_start": "#0284c7",
            "color_end": "#0c4a6e",
        })

    cb = m.get("cost_burden", 0)
    if cb > 0:
        label, grade = _percentile_label(cb, [
            (100_000, "미미", "S"),
            (500_000, "적정", "A"),
            (1_500_000, "주의", "B"),
            (5_000_000, "심각", "C"),
            (float("inf"), "VVIP", "D"),
        ])
        ranks.append({
            "id": "fee_index",
            "title": "수수료 지출 랭크",
            "emoji": "💸",
            "value": cb,
            "value_label": _krw(cb),
            "rank": label,
            "grade": grade,
            "description": "수수료+세금 총액",
            "color_start": "#7c3aed",
            "color_end": "#4c1d95",
        })

    max_mad = m.get("max_trades_in_day", 0)
    if max_mad > 0:
        label, grade = _percentile_label(max_mad, [
            (5, "차분", "S"),
            (15, "활발", "A"),
            (30, "과열", "B"),
            (60, "도파민 중독", "C"),
            (float("inf"), "극한", "D"),
        ])
        ranks.append({
            "id": "frenzy_index",
            "title": "도파민 중독도",
            "emoji": "🎰",
            "value": max_mad,
            "value_label": f"하루 {max_mad}건",
            "rank": label,
            "grade": grade,
            "description": "하루 최대 거래 수",
            "color_start": "#dc2626",
            "color_end": "#7f1d1d",
        })

    mdd = m.get("mdd", 0)
    if mdd > 0:
        label, grade = _percentile_label(mdd, [
            (1_000_000, "안정", "S"),
            (5_000_000, "양호", "A"),
            (15_000_000, "변동", "B"),
            (30_000_000, "심각", "C"),
            (float("inf"), "롤러코스터", "D"),
        ])
        ranks.append({
            "id": "mdd_index",
            "title": "리스크 진폭",
            "emoji": "🎢",
            "value": mdd,
            "value_label": _krw(mdd),
            "rank": label,
            "grade": grade,
            "description": "최대 낙폭 (MDD)",
            "color_start": "#db2777",
            "color_end": "#831843",
        })

    # 긍정 랭크
    tp = m.get("total_pnl", 0)
    tb = max(m.get("total_buy", 1), 1)
    pnl_ratio = tp / tb * 100
    label, grade = _percentile_label(-pnl_ratio, [
        (-10, "전설", "SS"),
        (-5, "우수", "S"),
        (0, "흑자", "A"),
        (5, "적자", "C"),
        (float("inf"), "대형 적자", "D"),
    ])
    ranks.append({
        "id": "profit_index",
        "title": "수익성 랭크",
        "emoji": "💰",
        "value": pnl_ratio,
        "value_label": f"{pnl_ratio:+.1f}%",
        "rank": label,
        "grade": grade,
        "description": "매수 대비 실현 수익률",
        "color_start": "#ca8a04" if tp >= 0 else "#475569",
        "color_end": "#713f12" if tp >= 0 else "#1e293b",
    })

    return ranks


def build_wrapped(trades: list[Trade], year: int = 2026) -> dict:
    m = _compute_metrics(trades, year)
    if m.get("empty"):
        return {"year": year, "empty": True}

    persona = _classify_persona(m)
    diagnoses = _make_diagnoses(m)
    principles = _make_principles(m, persona["id"])

    rpg = _compute_rpg_stats(m)
    titles = _compute_titles(m)
    buffs = _compute_buffs(m)
    ranks = _compute_rank_indexes(m)

    return {
        "year": year,
        "empty": False,
        "metrics": m,
        "persona": persona,
        "diagnoses": diagnoses,
        "principles": principles,
        "rpg": rpg,
        "titles": titles,
        "buffs": buffs,
        "ranks": ranks,
    }
