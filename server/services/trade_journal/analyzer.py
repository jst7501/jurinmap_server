"""
종목별 패턴 탐지기 라이브러리.

80+개의 검사기가 각 종목 통계를 보고 매칭되는 피드백을 자동 생성.
각 피드백은 다음을 포함한다:
    - category: 'strength' | 'mistake' | 'improvement' | 'insight'
    - emoji
    - title
    - body: 구체적 수치를 포함한 한두 문장
    - verdict: 한 줄 밈/슬로건
    - fix: 실행 가능한 처방
    - severity: 'critical' | 'warning' | 'info' | 'positive'
    - id: 디버그용

사용자별로 다르게 발화하며, 특정 유저에 종속되지 않는 일반 규칙이다.
"""
from __future__ import annotations

from datetime import datetime
from statistics import mean, pstdev
from typing import Any, Callable, Optional

from server.services.trade_journal.journal import compute_realizations
from server.services.trade_journal.models import Trade


# ============================================================
# HELPERS
# ============================================================

def _safe_mean(xs: list[float]) -> float:
    return mean(xs) if xs else 0.0


def _safe_stdev(xs: list[float]) -> float:
    return pstdev(xs) if len(xs) >= 2 else 0.0


def _won(n: float) -> str:
    return f"{round(n):,}원"


def _pct(n: float, signed: bool = True) -> str:
    if signed:
        return f"{n:+.1f}%"
    return f"{n:.1f}%"


def _key_of(t: Trade | dict) -> str:
    if isinstance(t, dict):
        return t.get("ticker") or t.get("symbol") or ""
    return t.ticker or t.symbol


# ============================================================
# STATS BUILDER — 탐지기에 쓰는 모든 지표를 한 번에 계산
# ============================================================

def _build_stats(key: str, all_trades: list[Trade], all_trips: list[dict], year: int | None) -> dict | None:
    trades = [t for t in all_trades if _key_of(t) == key]
    trips = [r for r in all_trips if _key_of(r) == key]

    if year is not None:
        trades = [t for t in trades if t.traded_at.year == year]
        trips = [r for r in trips if datetime.fromisoformat(r["closed_at"]).year == year]

    if not trips:
        return None

    symbol = next((t.symbol for t in trades), key)

    wins = [r for r in trips if r["pnl"] > 0]
    losses = [r for r in trips if r["pnl"] < 0]
    flats = [r for r in trips if r["pnl"] == 0]
    sorted_trips = sorted(trips, key=lambda r: r["closed_at"])

    buy_trades = sorted([t for t in trades if t.side == "BUY"], key=lambda t: t.traded_at)
    sell_trades = sorted([t for t in trades if t.side == "SELL"], key=lambda t: t.traded_at)
    entry_prices = [t.price for t in buy_trades]
    exit_prices = [t.price for t in sell_trades]

    buy_volume = sum(t.amount for t in buy_trades)
    sell_volume = sum(t.amount for t in sell_trades)
    fees = sum(t.fee for t in trades)
    taxes = sum(t.tax for t in trades)

    # 스트릭
    max_ls = cur_ls = max_ws = cur_ws = 0
    for r in sorted_trips:
        if r["pnl"] < 0:
            cur_ls += 1
            cur_ws = 0
            max_ls = max(max_ls, cur_ls)
        elif r["pnl"] > 0:
            cur_ws += 1
            cur_ls = 0
            max_ws = max(max_ws, cur_ws)
        else:
            cur_ws = cur_ls = 0

    # 평균
    avg_hold = _safe_mean([float(r["holding_days"]) for r in trips])
    avg_win_hold = _safe_mean([float(r["holding_days"]) for r in wins])
    avg_loss_hold = _safe_mean([float(r["holding_days"]) for r in losses])
    avg_win_pct = _safe_mean([r["return_pct"] for r in wins])
    avg_loss_pct = _safe_mean([r["return_pct"] for r in losses])
    avg_win_pnl = _safe_mean([r["pnl"] for r in wins])
    avg_loss_pnl = _safe_mean([r["pnl"] for r in losses])
    avg_return_pct = _safe_mean([r["return_pct"] for r in trips])

    # R:R
    rr_ratio = None
    required_wr = 0.0
    if avg_win_pct > 0 and avg_loss_pct < 0:
        rr_ratio = avg_win_pct / abs(avg_loss_pct)
        required_wr = abs(avg_loss_pct) / (avg_win_pct + abs(avg_loss_pct)) * 100.0

    sum_wins_pnl = sum(r["pnl"] for r in wins)
    sum_losses_pnl = sum(r["pnl"] for r in losses)
    profit_factor = (sum_wins_pnl / abs(sum_losses_pnl)) if sum_losses_pnl else None

    # 분포
    hold_stdev = _safe_stdev([float(r["holding_days"]) for r in trips])
    return_stdev = _safe_stdev([r["return_pct"] for r in trips])

    # 당일 vs 오버나잇
    same_day = sum(1 for r in trips if r["holding_days"] == 0)
    same_day_ratio = same_day / len(trips) if trips else 0.0
    overnight_ratio = 1 - same_day_ratio

    # 진입가 흐름
    inc = dec = 0
    if len(entry_prices) >= 2:
        for i in range(1, len(entry_prices)):
            if entry_prices[i] > entry_prices[i - 1]:
                inc += 1
            elif entry_prices[i] < entry_prices[i - 1]:
                dec += 1
    entry_range = (max(entry_prices) - min(entry_prices)) if entry_prices else 0
    entry_spread_pct = (entry_range / mean(entry_prices) * 100) if entry_prices and mean(entry_prices) > 0 else 0

    # 전반/후반 (시간순 반으로 나눔)
    mid = len(sorted_trips) // 2
    first_half = sorted_trips[:mid] if mid > 0 else []
    second_half = sorted_trips[mid:] if mid > 0 else sorted_trips
    fh_pnl = sum(r["pnl"] for r in first_half)
    sh_pnl = sum(r["pnl"] for r in second_half)
    fh_wr = sum(1 for r in first_half if r["pnl"] > 0) / max(len(first_half), 1) * 100
    sh_wr = sum(1 for r in second_half if r["pnl"] > 0) / max(len(second_half), 1) * 100

    # 복수매매 (손절 후 1일 내 같은 종목 재매수)
    revenge = 0
    for r in sorted_trips:
        if r["pnl"] < 0 and r["return_pct"] < -3:
            sd = datetime.fromisoformat(r["closed_at"]).date()
            for t in buy_trades:
                diff = (t.traded_at.date() - sd).days
                if 0 <= diff <= 1:
                    revenge += 1
                    break

    # 최근 3건
    recent = sorted_trips[-3:] if len(sorted_trips) >= 3 else sorted_trips
    recent_wr = sum(1 for r in recent if r["pnl"] > 0) / max(len(recent), 1) * 100
    last = sorted_trips[-1] if sorted_trips else None

    # 첫 트레이드 / 마지막 트레이드
    first_trip = sorted_trips[0] if sorted_trips else None
    last_trip = sorted_trips[-1] if sorted_trips else None

    # 포지션 사이즈
    buy_sizes = [t.amount for t in buy_trades]
    max_buy = max(buy_sizes) if buy_sizes else 0
    mean_buy = mean(buy_sizes) if buy_sizes else 0
    buy_size_stdev = _safe_stdev(buy_sizes)
    buy_size_cv = (buy_size_stdev / mean_buy) if mean_buy > 0 else 0  # 변동계수
    big_bet_ratio = (max_buy / buy_volume) if buy_volume > 0 else 0

    # 거래 활동 기간
    first_date = sorted_trips[0]["opened_at"][:10] if sorted_trips else None
    last_date = sorted_trips[-1]["closed_at"][:10] if sorted_trips else None
    days_active = 0
    if first_date and last_date:
        days_active = (datetime.fromisoformat(last_date).date() - datetime.fromisoformat(first_date).date()).days + 1

    # 현재 포지션 보유 여부 (매수 수량 > 매도 수량)
    net_buy_qty = sum(t.quantity for t in trades if t.side == "BUY") - sum(t.quantity for t in trades if t.side == "SELL")
    has_open_position = net_buy_qty > 1e-9

    return {
        "key": key,
        "symbol": symbol,
        "trades": trades,
        "trips": trips,
        "sorted_trips": sorted_trips,
        "year": year,
        # basic
        "trip_count": len(trips),
        "trade_count": len(trades),
        "buy_count": len(buy_trades),
        "sell_count": len(sell_trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "flat_count": len(flats),
        "win_rate": (len(wins) / len(trips) * 100.0) if trips else 0.0,
        "total_pnl": sum(r["pnl"] for r in trips),
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "fees": fees,
        "taxes": taxes,
        "cost_burden": fees + taxes,
        "avg_buy_size": mean_buy,
        # averages
        "avg_hold": avg_hold,
        "avg_win_hold": avg_win_hold,
        "avg_loss_hold": avg_loss_hold,
        "avg_win_pct": avg_win_pct,
        "avg_loss_pct": avg_loss_pct,
        "avg_win_pnl": avg_win_pnl,
        "avg_loss_pnl": avg_loss_pnl,
        "avg_return_pct": avg_return_pct,
        # extremes
        "biggest_win": max(trips, key=lambda r: r["pnl"], default=None),
        "biggest_loss": min(trips, key=lambda r: r["pnl"], default=None),
        "biggest_win_pct": max((r["return_pct"] for r in trips), default=0),
        "biggest_loss_pct": min((r["return_pct"] for r in trips), default=0),
        "longest_hold": max((r["holding_days"] for r in trips), default=0),
        "shortest_hold": min((r["holding_days"] for r in trips), default=0),
        # streaks
        "max_loss_streak": max_ls,
        "max_win_streak": max_ws,
        # R:R
        "rr_ratio": rr_ratio,
        "required_wr": required_wr,
        "profit_factor": profit_factor,
        "sum_wins_pnl": sum_wins_pnl,
        "sum_losses_pnl": sum_losses_pnl,
        # distribution
        "hold_stdev": hold_stdev,
        "return_stdev": return_stdev,
        # same/overnight
        "same_day_ratio": same_day_ratio,
        "overnight_ratio": overnight_ratio,
        "same_day_count": same_day,
        "overnight_count": len(trips) - same_day,
        # entry patterns
        "entry_prices": entry_prices,
        "first_entry_price": entry_prices[0] if entry_prices else 0,
        "last_entry_price": entry_prices[-1] if entry_prices else 0,
        "max_entry_price": max(entry_prices) if entry_prices else 0,
        "min_entry_price": min(entry_prices) if entry_prices else 0,
        "buy_inc_count": inc,
        "buy_dec_count": dec,
        "chasing_ratio": inc / max(len(entry_prices) - 1, 1),
        "averaging_down_ratio": dec / max(len(entry_prices) - 1, 1),
        "entry_range": entry_range,
        "entry_spread_pct": entry_spread_pct,
        # halves
        "first_half_pnl": fh_pnl,
        "second_half_pnl": sh_pnl,
        "first_half_win_rate": fh_wr,
        "second_half_win_rate": sh_wr,
        "first_half_count": len(first_half),
        "second_half_count": len(second_half),
        # re-entry / revenge
        "revenge_count": revenge,
        # recent
        "recent_win_rate": recent_wr,
        "last_trip": last,
        "first_trip": first_trip,
        # sizes
        "max_buy_amount": max_buy,
        "buy_size_cv": buy_size_cv,
        "big_bet_ratio": big_bet_ratio,
        # dates
        "first_date": first_date,
        "last_date": last_date,
        "days_active": days_active,
        # position
        "net_buy_qty": net_buy_qty,
        "has_open_position": has_open_position,
    }


# ============================================================
# DETECTOR REGISTRY
# ============================================================

Detector = Callable[[dict], Optional[dict]]
_DETECTORS: list[tuple[str, Detector]] = []


def _register(name: str):
    def wrap(fn: Detector):
        _DETECTORS.append((name, fn))
        return fn
    return wrap


def _fb(
    category: str,
    severity: str,
    emoji: str,
    title: str,
    body: str,
    verdict: str,
    fix: str,
    id: str,
) -> dict:
    return {
        "id": id,
        "category": category,
        "severity": severity,
        "emoji": emoji,
        "title": title,
        "body": body,
        "verdict": verdict,
        "fix": fix,
    }


# ============================================================
# [A] 승/패 균형
# ============================================================

@_register("A001")
def _d(s):
    if s["trip_count"] >= 3 and s["win_rate"] >= 70 and s["total_pnl"] > 0:
        return _fb(
            "strength", "positive", "🎯",
            "이 종목은 내 종목",
            f"{s['trip_count']}번 중 {s['win_count']}번 이겨서 승률 {s['win_rate']:.0f}%, 누적 {_won(s['total_pnl'])} 플러스.",
            "내가 뭘 본 거지? 그걸 기억해.",
            "매수 직전에 뭘 봤는지 한 줄 메모하기. 이 종목의 패턴을 구조화해서 나만의 체크리스트 만들기.",
            "A001",
        )


@_register("A002")
def _d(s):
    if s["trip_count"] >= 3 and s["win_rate"] < 40 and s["total_pnl"] < -500_000:
        return _fb(
            "mistake", "critical", "❌",
            "이 종목, 객관적으로 안 맞아요",
            f"{s['trip_count']}번 들어가서 {s['loss_count']}번 손절. 승률 {s['win_rate']:.0f}%, 누적 {_won(s['total_pnl'])}.",
            "감정은 있지만 데이터는 정직해요.",
            f"{s['symbol']}을(를) 관심 종목에서 삭제하기. 차트 즐겨찾기 해제, 알림 끄기.",
            "A002",
        )


@_register("A003")
def _d(s):
    if s["trip_count"] >= 5 and all(r["pnl"] > 0 for r in s["trips"]):
        return _fb(
            "strength", "positive", "💎",
            "완벽 승률 종목",
            f"{s['trip_count']}번 전부 이겼어요. 단 한 번도 안 졌어요.",
            "이건 진짜 자신 있는 종목이에요.",
            "이 정도면 '실수로 잘한 것'이 아니라 내가 아는 패턴이에요. 성공 공식을 문서화할 것.",
            "A003",
        )


@_register("A004")
def _d(s):
    if s["trip_count"] >= 5 and all(r["pnl"] <= 0 for r in s["trips"]):
        return _fb(
            "mistake", "critical", "🚨",
            "한 번도 못 이긴 종목",
            f"{s['trip_count']}번 전부 손절/본전. 이건 우연이 아니에요.",
            "궁합이 최악이라는 증거.",
            "평생 손대지 않기 리스트에 등록. 차트를 쳐다보는 것도 금지.",
            "A004",
        )


@_register("A005")
def _d(s):
    if s["trip_count"] >= 2 and 45 <= s["win_rate"] <= 55 and -500_000 < s["total_pnl"] < 500_000:
        return _fb(
            "insight", "info", "⚖️",
            "완전 50:50 종목",
            f"승률 {s['win_rate']:.0f}%, 누적 {_won(s['total_pnl'])}. 이기는지 지는지 모호한 종목.",
            "동전 던지기에 수수료 낼 거면 차라리 안 하는 게.",
            "수수료/세금 생각하면 기댓값 마이너스. 이 종목은 건너뛰기.",
            "A005",
        )


@_register("A006")
def _d(s):
    if s["trip_count"] >= 4 and 40 <= s["win_rate"] <= 55 and s["total_pnl"] > 1_000_000:
        return _fb(
            "strength", "positive", "🎰",
            "승률은 평범한데 수익은 크네",
            f"승률 {s['win_rate']:.0f}%인데도 {_won(s['total_pnl'])} 플러스. 손익비가 좋게 관리됐어요.",
            "이기는 판을 크게, 지는 판을 작게 = 정답이에요.",
            "이 패턴을 다른 종목에도 적용하기. 평균 익절 > 평균 손절 구조를 유지하는 게 핵심.",
            "A006",
        )


@_register("A007")
def _d(s):
    if s["trip_count"] >= 4 and 40 <= s["win_rate"] <= 60 and s["total_pnl"] < -1_000_000:
        return _fb(
            "mistake", "warning", "😤",
            "이겨도 지는 이상한 종목",
            f"승률 {s['win_rate']:.0f}%면 못한 건 아닌데 누적 {_won(s['total_pnl'])}. 손절이 익절보다 컸어요.",
            "이기는 판이 지는 판보다 작으면, 결국 진 거예요.",
            "이 종목에서는 익절 목표를 손절의 2배 이상으로 두거나, 아예 건드리지 않기.",
            "A007",
        )


# ============================================================
# [B] 보유 기간 패턴
# ============================================================

@_register("B001")
def _d(s):
    if s["win_count"] >= 2 and s["loss_count"] >= 2 and s["avg_loss_hold"] > max(s["avg_win_hold"], 0.5) * 2:
        return _fb(
            "mistake", "critical", "⏳",
            "익절은 짧게 손절은 길게 — 진짜 이러면 안 돼요",
            f"익절 평균 {s['avg_win_hold']:.1f}일 / 손절 평균 {s['avg_loss_hold']:.1f}일. 손실을 {s['avg_loss_hold']/max(s['avg_win_hold'],0.5):.1f}배 더 오래 들고 있었어요.",
            "기다리면 오르겠지 = 이 종목에서 가장 비싼 생각.",
            "매수 전에 손절가를 먼저 정하고, 그 선 깨지면 1초 안에 실행. 익절은 반대로 천천히.",
            "B001",
        )


@_register("B002")
def _d(s):
    if s["win_count"] >= 2 and s["loss_count"] >= 2 and s["avg_win_hold"] > s["avg_loss_hold"] * 2:
        return _fb(
            "strength", "positive", "✂️",
            "손절은 짧게 익절은 길게 — 이상적이에요",
            f"익절 평균 {s['avg_win_hold']:.1f}일 / 손절 평균 {s['avg_loss_hold']:.1f}일. 손절을 빨리 한 뒤 이기는 판을 오래 들고 가는 패턴.",
            "교과서적인 거래예요.",
            "이 패턴을 모든 종목에 복사하기. 손절 룰 고정, 익절 목표 상향.",
            "B002",
        )


@_register("B003")
def _d(s):
    if s["trip_count"] >= 3 and s["same_day_ratio"] >= 0.9 and s["total_pnl"] < 0:
        return _fb(
            "mistake", "critical", "🏃",
            "순수 단타 중독인데 손실",
            f"이 종목 {s['trip_count']}번 중 {s['same_day_count']}번이 당일 매매. 합계 {_won(s['total_pnl'])}.",
            "빠른 손은 수수료 내는 속도예요.",
            "이 종목은 최소 하루 이상 보유 룰로 재도전. 당일 매매 금지.",
            "B003",
        )


@_register("B004")
def _d(s):
    if s["trip_count"] >= 3 and s["same_day_ratio"] >= 0.9 and s["total_pnl"] > 500_000:
        return _fb(
            "strength", "positive", "⚡",
            "단타 체질 증명",
            f"{s['trip_count']}번 중 {s['same_day_count']}번이 당일 매매로 누적 {_won(s['total_pnl'])}.",
            "스캘핑이 진짜 맞는 스타일.",
            "이 종목에서의 단타 패턴을 다른 변동성 큰 종목에도 시도. 단, 룰은 엄격하게.",
            "B004",
        )


@_register("B005")
def _d(s):
    if s["trip_count"] >= 4 and s["overnight_ratio"] >= 0.8 and s["total_pnl"] > 500_000:
        return _fb(
            "strength", "positive", "🌙",
            "오버나잇 승률러",
            f"{s['trip_count']}번 중 {s['overnight_count']}번이 오버나잇(1일+ 보유). 누적 {_won(s['total_pnl'])}.",
            "당일 매매보다 덜 떨고 덜 수수료 낸 스마트 선택.",
            "이 종목에서의 '참고 기다리는' 패턴을 유지하기. 짧은 스윙이 내 스타일.",
            "B005",
        )


@_register("B006")
def _d(s):
    if s["trip_count"] >= 3 and s["hold_stdev"] > 7 and s["total_pnl"] < 0:
        return _fb(
            "mistake", "warning", "🎲",
            "보유 기간이 들쭉날쭉",
            f"보유 기간 표준편차 {s['hold_stdev']:.1f}일. 어떤 건 당일, 어떤 건 2주+. 일관성이 없어요.",
            "일관성 없는 매매 = 무전략.",
            "이 종목에서 목표 보유 기간을 고정 (예: 3~7일)하고 기계적으로 실행.",
            "B006",
        )


@_register("B007")
def _d(s):
    if s["avg_hold"] > 30 and s["total_pnl"] > 0:
        return _fb(
            "strength", "positive", "💎",
            "장기 존버 성공",
            f"평균 {s['avg_hold']:.0f}일 보유, 누적 {_won(s['total_pnl'])}. 오래 기다린 게 답이었어요.",
            "긴 호흡에서 이기는 스타일.",
            "이 패턴은 다른 우량주/테마주에도 반복 가능. 단타 유혹을 참는 법을 훈련하기.",
            "B007",
        )


@_register("B008")
def _d(s):
    if s["avg_hold"] > 30 and s["total_pnl"] < -1_000_000:
        return _fb(
            "mistake", "warning", "🪨",
            "존버 실패",
            f"평균 {s['avg_hold']:.0f}일 보유했는데 {_won(s['total_pnl'])}. '기다리면 오르겠지'가 안 통한 케이스.",
            "존버는 종목을 가려서.",
            "이 종목은 단기 트레이딩 대상이지 장기 보유 대상이 아니에요. 보유 기간을 확 줄여보기.",
            "B008",
        )


@_register("B009")
def _d(s):
    if s["shortest_hold"] == 0 and s["longest_hold"] >= 30:
        return _fb(
            "insight", "info", "🎯",
            "이 종목만은 왔다갔다",
            f"가장 짧은 보유 0일, 가장 긴 보유 {s['longest_hold']}일. 한 종목에서도 스타일이 여러 개.",
            "한 종목 = 한 전략이 원칙.",
            "이 종목에 대해 '어떨 때 단타, 어떨 때 스윙' 기준을 문서화해두기.",
            "B009",
        )


# ============================================================
# [C] 진입 패턴
# ============================================================

@_register("C001")
def _d(s):
    if s["buy_count"] >= 3 and s["chasing_ratio"] >= 0.7 and s["total_pnl"] < 0:
        return _fb(
            "mistake", "critical", "🎯",
            "추격매수 흔적",
            f"매수 {s['buy_count']}번 중 {s['buy_inc_count']}번이 직전보다 비싼 가격에 진입. 결과는 {_won(s['total_pnl'])}.",
            "오르는 걸 보고 들어가면 꼭지를 잡아요.",
            "이 종목은 조정 뒤 매수 룰로 바꾸기. 신고가 돌파 매수 금지.",
            "C001",
        )


@_register("C002")
def _d(s):
    if s["buy_count"] >= 3 and s["averaging_down_ratio"] >= 0.7 and s["total_pnl"] < 0:
        return _fb(
            "mistake", "critical", "🧲",
            "물타기 지옥",
            f"매수 {s['buy_count']}번 중 {s['buy_dec_count']}번이 직전보다 싼 가격에 진입. 떨어지는 칼을 잡은 패턴.",
            "물타기는 기업이 아니라 타이밍에 하는 거예요.",
            "이 종목에서 같은 종목 2회 이상 매수 금지. 세 번째 매수는 '난 틀렸다' 신호로 받아들이기.",
            "C002",
        )


@_register("C003")
def _d(s):
    if s["buy_count"] >= 3 and s["averaging_down_ratio"] >= 0.7 and s["total_pnl"] > 500_000:
        return _fb(
            "strength", "positive", "🏗️",
            "분할매수 성공",
            f"매수 {s['buy_count']}번을 단계적으로 낮은 가격에 쌓아서 {_won(s['total_pnl'])} 수익.",
            "계획된 분할매수는 다른 얘기예요.",
            "이 패턴을 다른 우량 종목에도 시도. 단, 총 매수 금액 한도를 미리 정해두기.",
            "C003",
        )


@_register("C004")
def _d(s):
    if s["buy_count"] >= 4 and s["entry_spread_pct"] > 50:
        return _fb(
            "insight", "warning", "📐",
            "진입가 범위가 너무 넓어요",
            f"매수가가 {_won(s['min_entry_price'])} ~ {_won(s['max_entry_price'])}. 최고/최저 차이가 {s['entry_spread_pct']:.0f}%.",
            "한 종목에 이렇게 넓게 걸쳐서 사면 '뇌동 확정'.",
            "매수는 명확한 가격대/신호에서만. 아무데서나 매수하면 평균단가의 의미가 없어요.",
            "C004",
        )


@_register("C005")
def _d(s):
    if s["buy_count"] == 1 and s["total_pnl"] > 500_000:
        return _fb(
            "strength", "positive", "🎯",
            "원샷 성공",
            f"딱 한 번만 매수해서 {_won(s['total_pnl'])} 수익. 망설임 없는 단호한 진입.",
            "확신 있는 한 방.",
            "이 종목에서의 '들어갈 때 확신'을 기억하기. 이런 세팅이 올 때만 매수하는 게 이상적.",
            "C005",
        )


@_register("C006")
def _d(s):
    if s["buy_count"] == 1 and s["total_pnl"] < -500_000:
        return _fb(
            "mistake", "warning", "🎯",
            "원샷 실패",
            f"딱 한 번만 매수해서 {_won(s['total_pnl'])}. 분할매수 없이 전액 들어갔다 한방에 맞음.",
            "큰 금액을 한 번에 넣는 건 도박 구조.",
            "매수는 2~3번 나눠서 들어가고, 마지막 분할은 확신 있을 때만 추가.",
            "C006",
        )


@_register("C007")
def _d(s):
    if s["buy_count"] >= 2 and s["max_entry_price"] > s["min_entry_price"] * 1.5:
        return _fb(
            "insight", "warning", "📏",
            "최고가가 최저가의 1.5배 이상",
            f"매수가 범위 {_won(s['min_entry_price'])} ~ {_won(s['max_entry_price'])}. 넓이 {s['entry_spread_pct']:.0f}%.",
            "같은 종목인데 전혀 다른 결정.",
            "매수 근거가 뭔지 단계별로 메모. 근거가 다르면 종목 리서치부터 다시.",
            "C007",
        )


@_register("C008")
def _d(s):
    if s["buy_count"] >= 5 and s["trip_count"] >= 3 and s["total_pnl"] > 0 and s["chasing_ratio"] <= 0.4:
        return _fb(
            "strength", "positive", "🪜",
            "계단식 분할 매수",
            f"{s['buy_count']}번 나눠서 매수했고 수익 {_won(s['total_pnl'])}. 피라미딩이 아니라 하방 분할.",
            "정석적인 분할매수.",
            "리스크가 분산됐어요. 이 종목에서의 분할 간격/비중을 기록해서 다른 종목에도 적용.",
            "C008",
        )


# ============================================================
# [D] 재진입/중독 패턴
# ============================================================

@_register("D001")
def _d(s):
    if s["revenge_count"] >= 1 and s["trip_count"] >= 2 and s["total_pnl"] < 0:
        return _fb(
            "mistake", "critical", "⚔️",
            "복수매매 적발",
            f"이 종목에서 손절 직후 1일 이내 재진입이 {s['revenge_count']}회.",
            "방금 나를 손절시킨 종목이 갑자기 친해질 리가 없어요.",
            "손절 후 최소 3거래일은 같은 종목 거래 금지. 차트 보기도 금지.",
            "D001",
        )


@_register("D002")
def _d(s):
    if s["trip_count"] >= 5 and s["win_rate"] < 40:
        return _fb(
            "mistake", "critical", "🎣",
            "끈질긴 재도전 실패",
            f"이 종목에 {s['trip_count']}번 들어갔는데 승률 {s['win_rate']:.0f}%. 계속 들어가는데 계속 패.",
            "미련이 많은 건 장점이 아니에요.",
            "이 종목을 30일 블랙리스트에 올리기. 쳐다보지도 말기.",
            "D002",
        )


@_register("D003")
def _d(s):
    if s["trip_count"] >= 10:
        return _fb(
            "insight", "warning", "🎰",
            "중독 수준 매매",
            f"한 종목에 {s['trip_count']}번. 에너지와 주의력의 과투자.",
            "집중이 아니라 집착.",
            "이 종목에서의 매매를 월 3회 이하로 제한하는 룰 걸기.",
            "D003",
        )


@_register("D004")
def _d(s):
    if s["trip_count"] >= 6 and s["improving"]:
        return _fb(
            "strength", "positive", "📈",
            "후반으로 갈수록 좋아짐",
            f"전반 {s['first_half_count']}건 {_won(s['first_half_pnl'])} → 후반 {s['second_half_count']}건 {_won(s['second_half_pnl'])}. 승률도 {s['first_half_win_rate']:.0f}% → {s['second_half_win_rate']:.0f}%.",
            "배운 게 있어요. 이 흐름 유지.",
            "이 종목을 어떻게 다루는지 핵심 룰을 메모. 그게 내 진짜 알파예요.",
            "D004",
        )


@_register("D005")
def _d(s):
    if s["trip_count"] >= 6 and s["deteriorating"]:
        return _fb(
            "mistake", "warning", "📉",
            "후반으로 갈수록 나빠짐",
            f"전반 {_won(s['first_half_pnl'])} → 후반 {_won(s['second_half_pnl'])}. 승률 {s['first_half_win_rate']:.0f}% → {s['second_half_win_rate']:.0f}%.",
            "전략이 통하지 않게 된 거예요. 변곡점.",
            "이 종목에 대한 기존 가정을 재검토. 시장이 바뀐 건지 내가 느슨해진 건지.",
            "D005",
        )


@_register("D006")
def _d(s):
    if s["trip_count"] >= 4 and s["max_loss_streak"] >= 3:
        return _fb(
            "mistake", "warning", "🔁",
            f"연속 {s['max_loss_streak']}번 손절",
            f"이 종목에서 연속으로 {s['max_loss_streak']}번 손절났어요. 시장이 분명 신호를 줬을 텐데 무시한 흔적.",
            "세 번 연속 지면 시장이 '이 종목 건들지 마'라고 말하는 거예요.",
            "2연속 손절 = 1주일 쉬기 룰. 3연속 = 종목 삭제.",
            "D006",
        )


@_register("D007")
def _d(s):
    if s["max_win_streak"] >= 3:
        return _fb(
            "strength", "positive", "🔥",
            f"연속 {s['max_win_streak']}번 익절",
            f"이 종목에서 연속으로 {s['max_win_streak']}번 익절. 플레이북이 통했어요.",
            "이런 구간을 기억해두면 훨씬 잘해요.",
            "이 연속 익절 구간에서 뭘 봤는지 복기하기. 재현 가능하면 반복.",
            "D007",
        )


@_register("D008")
def _d(s):
    if s["trip_count"] == 1:
        return _fb(
            "insight", "info", "👋",
            "한 번만 거래한 종목",
            f"단 1번. 결과 {_won(s['total_pnl'])} ({_pct(s['biggest_win_pct'] or s['biggest_loss_pct'])}).",
            "한 번만으로는 아무것도 판단할 수 없어요.",
            "재현 가능한 거래였는지, 우연이었는지 스스로 평가해보기. 원샷 성공은 요행일 가능성도 있어요.",
            "D008",
        )


# ============================================================
# [E] 손익비 / 리스크 패턴
# ============================================================

@_register("E001")
def _d(s):
    if s["rr_ratio"] is not None and s["rr_ratio"] < 0.5:
        return _fb(
            "mistake", "critical", "⚖️",
            "손익비 심각하게 나쁨",
            f"평균 익절 +{s['avg_win_pct']:.1f}% / 평균 손절 {s['avg_loss_pct']:.1f}%. 손익비 1:{s['rr_ratio']:.2f}.",
            f"본전 치려면 승률 {s['required_wr']:.0f}% 나와야 하는데 실제 {s['win_rate']:.0f}%.",
            "이 종목에서 -3% 손절 / +10% 익절 같은 강제 룰 적용. 중간 청산 금지.",
            "E001",
        )


@_register("E002")
def _d(s):
    if s["rr_ratio"] is not None and 0.5 <= s["rr_ratio"] < 1.0:
        return _fb(
            "mistake", "warning", "⚖️",
            "손익비 살짝 불리",
            f"손익비 1:{s['rr_ratio']:.2f}. 본전 치려면 승률 {s['required_wr']:.0f}% 필요.",
            "이기는 판이 지는 판보다 작으면 결국 진 거예요.",
            "익절 목표를 조금만 더 참아보기. 손절은 건드리지 말고.",
            "E002",
        )


@_register("E003")
def _d(s):
    if s["rr_ratio"] is not None and s["rr_ratio"] >= 2.0:
        return _fb(
            "strength", "positive", "🎯",
            "손익비 훌륭",
            f"평균 익절 +{s['avg_win_pct']:.1f}% / 평균 손절 {s['avg_loss_pct']:.1f}%. 손익비 1:{s['rr_ratio']:.2f}.",
            "이 구조는 승률 30%만 나와도 벌어요.",
            "이 종목 스타일을 유지. 이 구조 자체가 안전망이에요.",
            "E003",
        )


@_register("E004")
def _d(s):
    if s["biggest_loss"] and s["biggest_loss_pct"] <= -15:
        big = s["biggest_loss"]
        return _fb(
            "mistake", "critical", "🩸",
            "한 방 크게 맞음",
            f"{big['qty']:.0f}주 · {_won(big['buy_price'])} → {_won(big['sell_price'])} → {_won(big['pnl'])} ({_pct(big['return_pct'])})",
            "한 종목의 단일 손실이 -15% 넘으면 관리 실패.",
            "한 트레이드 최대 손실 허용선을 -5~7%로 고정. 그 이상 가기 전에 무조건 손절.",
            "E004",
        )


@_register("E005")
def _d(s):
    if s["biggest_win"] and s["biggest_win_pct"] >= 15:
        big = s["biggest_win"]
        return _fb(
            "strength", "positive", "🚀",
            "한 번 제대로 먹었음",
            f"{big['qty']:.0f}주 · {_won(big['buy_price'])} → {_won(big['sell_price'])} → +{_won(big['pnl'])} ({_pct(big['return_pct'])})",
            "이런 게 있어야 수익이 쌓여요.",
            "이 트레이드 스크린샷으로 저장. 진입 시점에 뭘 봤는지 복기해서 체크리스트 만들기.",
            "E005",
        )


@_register("E006")
def _d(s):
    if s["profit_factor"] is not None and s["profit_factor"] < 0.5 and s["loss_count"] >= 2:
        return _fb(
            "mistake", "critical", "💧",
            "Profit Factor 심각",
            f"이긴 판 합 {_won(s['sum_wins_pnl'])}, 진 판 합 {_won(s['sum_losses_pnl'])}. PF {s['profit_factor']:.2f}.",
            "현재 구조로는 반복할수록 손해.",
            "이 종목에서는 완전히 룰을 다시 짜기. 리스크 사이즈를 줄이거나 아예 빼기.",
            "E006",
        )


@_register("E007")
def _d(s):
    if s["profit_factor"] is not None and s["profit_factor"] >= 2.0:
        return _fb(
            "strength", "positive", "💰",
            "Profit Factor 우수",
            f"이긴 판 {_won(s['sum_wins_pnl'])}, 진 판 {_won(s['sum_losses_pnl'])}. PF {s['profit_factor']:.2f}.",
            "구조 자체가 돈 버는 중.",
            "이 구조를 다른 종목에도 그대로 이식. 포지션 크기 키워도 안전.",
            "E007",
        )


@_register("E008")
def _d(s):
    if s["required_wr"] > 60 and s["trip_count"] >= 3:
        return _fb(
            "mistake", "critical", "📐",
            f"본전 치려면 승률 {s['required_wr']:.0f}% 필요",
            f"이 종목의 손익비 구조상 승률 {s['required_wr']:.0f}% 이상 안 나오면 결국 마이너스. 실제 {s['win_rate']:.0f}%.",
            "구조가 불가능한 숫자를 요구해요.",
            "손절 기준을 타이트하게 다시 설정. 예: -3%로 고정.",
            "E008",
        )


# ============================================================
# [F] 사이즈 / 포지션 패턴
# ============================================================

@_register("F001")
def _d(s):
    if s["buy_count"] >= 3 and s["big_bet_ratio"] > 0.5:
        return _fb(
            "mistake", "warning", "🎰",
            "한 번에 몰빵한 이력",
            f"최대 매수 {_won(s['max_buy_amount'])}. 이 종목 전체 매수의 {s['big_bet_ratio']*100:.0f}%가 한 번에 들어감.",
            "한 방에 계좌를 좌우하는 매매.",
            "한 번에 들어가는 금액을 총 자금의 20% 이하로 제한. 나머지는 분할 진입.",
            "F001",
        )


@_register("F002")
def _d(s):
    if s["buy_count"] >= 4 and s["buy_size_cv"] > 1.5:
        return _fb(
            "insight", "warning", "📊",
            "포지션 크기가 들쭉날쭉",
            f"매수 금액 변동계수 {s['buy_size_cv']:.1f}. 어떤 건 조금, 어떤 건 많이. 일관성 부족.",
            "사이징이 없으면 리스크 관리도 없어요.",
            "매수 1회당 금액을 고정하기 (예: 항상 100만원). 사이즈 변동은 명확한 근거 있을 때만.",
            "F002",
        )


@_register("F003")
def _d(s):
    if s["buy_count"] >= 10 and s["avg_buy_size"] < 500_000:
        return _fb(
            "insight", "info", "🔬",
            "소액 다회전",
            f"{s['buy_count']}번 매수, 평균 {_won(s['avg_buy_size'])}. 촘촘한 스캘핑.",
            "수수료 구조가 중요해요.",
            "수수료+세금 대비 수익이 충분한지 확인. 아니면 매매 빈도를 줄이거나 사이즈를 키우기.",
            "F003",
        )


@_register("F004")
def _d(s):
    if s["avg_buy_size"] > 10_000_000 and s["total_pnl"] < 0:
        return _fb(
            "mistake", "warning", "💼",
            "큰 사이즈 손실",
            f"평균 매수 {_won(s['avg_buy_size'])}. 손실 {_won(s['total_pnl'])}. 큰 돈이 한 번씩 맞으면 회복 어려움.",
            "사이즈와 확신은 비례해야 해요.",
            "이 종목에서는 사이즈를 절반으로 줄이기. 확신이 더 생기면 그때 늘리기.",
            "F004",
        )


# ============================================================
# [G] 비용 패턴
# ============================================================

@_register("G001")
def _d(s):
    if s["cost_burden"] > 0 and s["total_pnl"] > 0 and s["cost_burden"] > s["total_pnl"] * 0.3:
        return _fb(
            "insight", "warning", "💸",
            "수수료가 수익을 갉아먹음",
            f"수수료+세금 {_won(s['cost_burden'])} / 수익 {_won(s['total_pnl'])}. 비용이 수익의 {s['cost_burden']/s['total_pnl']*100:.0f}%.",
            "수익의 1/3 이상이 비용이면 곧 마이너스 됩니다.",
            "거래 횟수를 절반으로 줄여보기. 같은 수익인데 비용은 1/4로.",
            "G001",
        )


@_register("G002")
def _d(s):
    if s["cost_burden"] > abs(s["total_pnl"]) and s["total_pnl"] < 0:
        return _fb(
            "mistake", "warning", "💸",
            "수수료+세금 > 손실",
            f"비용 {_won(s['cost_burden'])}, 손실 {_won(s['total_pnl'])}. 수익이 안 나도 비용은 확정.",
            "매매만 안 해도 손실이 줄어요.",
            "이 종목 매매 일시 정지. 수수료가 손실보다 큰 건 건강하지 않아요.",
            "G002",
        )


@_register("G003")
def _d(s):
    if s["trip_count"] >= 3 and s["cost_burden"] < s["total_pnl"] * 0.05 and s["total_pnl"] > 0:
        return _fb(
            "strength", "positive", "🛡️",
            "비용 효율 우수",
            f"수익 {_won(s['total_pnl'])}, 비용 {_won(s['cost_burden'])} — 5% 미만.",
            "효율적인 구조.",
            "이 스타일 그대로 유지. 비용이 작은 매매는 복리 효과가 더 커요.",
            "G003",
        )


# ============================================================
# [H] 분포 / 변동성 패턴
# ============================================================

@_register("H001")
def _d(s):
    if s["trip_count"] >= 5 and s["return_stdev"] > 15:
        return _fb(
            "insight", "warning", "🎢",
            "수익률 변동이 매우 큼",
            f"수익률 표준편차 {s['return_stdev']:.1f}%p. 크게 벌거나 크게 잃거나의 반복.",
            "롤러코스터 = 무계획.",
            "이 종목에 대한 진입/청산 기준을 더 좁혀보기. 일관성이 핵심.",
            "H001",
        )


@_register("H002")
def _d(s):
    if s["trip_count"] >= 5 and s["return_stdev"] < 5 and s["total_pnl"] > 0:
        return _fb(
            "strength", "positive", "📊",
            "꾸준히 이김",
            f"수익률 표준편차 {s['return_stdev']:.1f}%p. 안정적으로 작은 수익을 쌓고 있어요.",
            "그라인더 스타일.",
            "이 패턴은 자본이 커질수록 유리해요. 사이즈를 단계적으로 늘려가는 것도 고려.",
            "H002",
        )


# ============================================================
# [I] 재무 결과 직접 평가
# ============================================================

@_register("I001")
def _d(s):
    if s["total_pnl"] > 5_000_000:
        return _fb(
            "strength", "positive", "🏆",
            "메인 수익원",
            f"이 종목 하나로 {_won(s['total_pnl'])}. 포트폴리오의 기둥.",
            "VIP 대우해야 할 종목.",
            "이 종목에서의 패턴을 문서화. 비슷한 종목군에 같은 전략 적용 가능한지 탐색.",
            "I001",
        )


@_register("I002")
def _d(s):
    if s["total_pnl"] < -5_000_000:
        return _fb(
            "mistake", "critical", "🩸",
            "대형 손실 종목",
            f"이 종목 하나에서 {_won(s['total_pnl'])} 손실. 연간 포트폴리오를 망가뜨린 주범.",
            "한 종목이 계좌를 흔들면 리스크 관리 실패.",
            "한 종목 최대 손실 한도를 계좌의 3~5%로 정해두기. 이 종목은 당분간 금지.",
            "I002",
        )


@_register("I003")
def _d(s):
    if s["loss_count"] >= 5 and s["avg_loss_pnl"] < 0 and abs(s["avg_loss_pnl"]) < 200_000:
        return _fb(
            "insight", "info", "🩹",
            "작은 손실이 많이 쌓임",
            f"{s['loss_count']}번 손절, 평균 {_won(s['avg_loss_pnl'])}. 한 번은 작지만 쌓여서 {_won(s['sum_losses_pnl'])}.",
            "소액 손절도 100번 쌓이면 대형 손실.",
            "매매 빈도를 줄이거나 확신 있는 때만 진입. 손절 횟수를 줄이는 게 핵심.",
            "I003",
        )


@_register("I004")
def _d(s):
    if s["win_count"] == 1 and s["loss_count"] >= 3 and s["total_pnl"] > 0:
        return _fb(
            "insight", "warning", "🎰",
            "한 번의 홈런으로 버팀",
            f"승 {s['win_count']}회, 패 {s['loss_count']}회인데 총 {_won(s['total_pnl'])} 플러스. 한 번 크게 벌어서 다 회복.",
            "로또 당첨은 재현 안 돼요.",
            "운에 의존하지 말고 반복 가능한 패턴을 찾기. 승률 자체를 올려야 해요.",
            "I004",
        )


# ============================================================
# [J] 최근 동향 / 마지막 트레이드
# ============================================================

@_register("J001")
def _d(s):
    if s["trip_count"] >= 3 and s["recent_win_rate"] == 0:
        return _fb(
            "mistake", "warning", "❄️",
            "최근 흐름 차가움",
            f"마지막 3건 전부 손절. 종목이 바뀌었거나 내 판단이 흔들린 신호.",
            "흐름 나쁠 때는 사이즈 줄이거나 쉬는 게 정답.",
            "이 종목 1주일 쉬기. 쉬는 동안 성공 사례만 복기.",
            "J001",
        )


@_register("J002")
def _d(s):
    if s["trip_count"] >= 3 and s["recent_win_rate"] == 100:
        return _fb(
            "strength", "positive", "🔥",
            "최근 흐름 뜨거움",
            "마지막 3건 전부 익절. 타점이 맞아 들어가는 중.",
            "감 잡은 구간. 사이즈 조금 늘려도 됩니다.",
            "단, 오버트레이딩 주의. 연승 뒤에는 자만이 따라오기 쉬워요.",
            "J002",
        )


@_register("J003")
def _d(s):
    if s["last_trip"] and s["last_trip"]["pnl"] < -1_000_000:
        lt = s["last_trip"]
        return _fb(
            "insight", "warning", "💥",
            "마지막이 큰 손실",
            f"가장 최근 거래가 {_won(lt['pnl'])} ({_pct(lt['return_pct'])}). 감정이 흔들렸을 타이밍.",
            "이럴 때 복수매매가 나와요. 조심.",
            "최소 1주일 쉬고 다시. 감정 가라앉은 뒤에만 재진입.",
            "J003",
        )


@_register("J004")
def _d(s):
    if s["last_trip"] and s["last_trip"]["pnl"] > 1_000_000:
        lt = s["last_trip"]
        return _fb(
            "strength", "positive", "🎉",
            "마지막이 큰 수익",
            f"가장 최근 거래가 +{_won(lt['pnl'])} ({_pct(lt['return_pct'])}). 흐름 좋음.",
            "기분은 좋은데 방심이 가장 위험.",
            "이 트레이드에서 뭐가 통했는지 즉시 메모. 다음 매매에 적용.",
            "J004",
        )


@_register("J005")
def _d(s):
    if s["has_open_position"]:
        return _fb(
            "insight", "info", "📌",
            "현재 보유 중",
            f"{s['net_buy_qty']:.0f}주 들고 있어요. 이 종목에 대한 생각을 정리해둘 타이밍.",
            "실시간으로 감정이 흔들릴 수 있어요.",
            "현재 보유 포지션의 목표 매도가 / 손절선을 지금 종이에 써두기.",
            "J005",
        )


@_register("J006")
def _d(s):
    if s["trip_count"] >= 3 and s["max_loss_streak"] >= 4:
        return _fb(
            "mistake", "critical", "❄️❄️",
            f"연속 {s['max_loss_streak']}연패",
            f"이 종목에서 연속 {s['max_loss_streak']}번 손절. 극심한 불운 또는 극심한 오판.",
            "4연패면 시장이 확성기로 말하고 있는 거예요.",
            "이 종목 최소 2주 강제 휴식. 그 기간 동안 손대면 벌점.",
            "J006",
        )


# ============================================================
# [K] 단일 트립 특수 케이스
# ============================================================

@_register("K001")
def _d(s):
    if s["biggest_loss"] and s["biggest_loss"]["pnl"] < 0 and abs(s["biggest_loss"]["pnl"]) > abs(s["total_pnl"]) * 0.8 and s["total_pnl"] < 0:
        big = s["biggest_loss"]
        return _fb(
            "mistake", "critical", "☠️",
            "단 한 번의 손실이 전체 손실",
            f"이 종목 전체 손실 {_won(s['total_pnl'])} 중 {_won(big['pnl'])}가 단 한 트레이드. {abs(big['pnl']/s['total_pnl']*100):.0f}% 이상.",
            "나머지 거래는 괜찮은데 그 한 번이 다 망쳐요.",
            "손절선을 반드시 체계화. 한 번의 실수가 1년치 수익을 지우면 시스템이 망가진 거예요.",
            "K001",
        )


@_register("K002")
def _d(s):
    if s["biggest_win"] and s["biggest_win"]["pnl"] > 0 and s["biggest_win"]["pnl"] > abs(s["sum_losses_pnl"]) and s["total_pnl"] > 0:
        big = s["biggest_win"]
        return _fb(
            "insight", "info", "🎁",
            "한 번의 대박으로 흑자 유지",
            f"최대 이익 {_won(big['pnl'])}이 모든 손실 {_won(s['sum_losses_pnl'])}을 덮어서 플러스 {_won(s['total_pnl'])}.",
            "대박이 없으면 마이너스라는 뜻.",
            "대박에 의존하지 말고 승률과 손익비를 올리기. 운에 기대는 구조 탈출.",
            "K002",
        )


@_register("K003")
def _d(s):
    if s["longest_hold"] >= 60:
        return _fb(
            "insight", "info", "🛌",
            f"최장 보유 {s['longest_hold']}일",
            "가장 오래 들고 있었던 트레이드가 2달 이상.",
            "장기 보유는 이유가 분명해야 해요.",
            "그 트레이드의 장기 보유 근거가 뭐였는지 기록. 단순히 '손절 못해서' 들고 있었다면 실수.",
            "K003",
        )


# ============================================================
# [L] 현금흐름 / 가격 움직임 패턴
# ============================================================

@_register("L001")
def _d(s):
    if s["sell_volume"] < s["buy_volume"] and s["trip_count"] >= 2 and not s["has_open_position"]:
        return _fb(
            "insight", "info", "💭",
            "사온 것보다 적게 팔았네",
            f"매수 {_won(s['buy_volume'])}, 매도 {_won(s['sell_volume'])}. 포지션은 청산됐지만 수량 대비 회수가 적음.",
            "가격 하락 구간을 지났을 가능성.",
            "다음엔 초기 진입가가 중요. 고점 대비 얼마나 떨어졌는지 체크하고 매수.",
            "L001",
        )


# ============================================================
# [M] 메타 패턴 (시간적 흐름)
# ============================================================

@_register("M001")
def _d(s):
    if s["trip_count"] >= 3 and s["days_active"] < 3:
        return _fb(
            "insight", "info", "⚡",
            "짧은 기간에 몰아친 매매",
            f"{s['trip_count']}번 거래가 {s['days_active']}일 안에 몰림.",
            "번뜩 아이디어가 왔는지, 뇌동이었는지.",
            "짧은 기간 집중 매매는 회고가 필수. 왜 그 기간에 집중됐는지 이유 적어두기.",
            "M001",
        )


@_register("M002")
def _d(s):
    if s["trip_count"] >= 4 and s["days_active"] >= 90:
        return _fb(
            "insight", "info", "📅",
            "장기 모니터링 종목",
            f"{s['trip_count']}번을 {s['days_active']}일 동안. 꾸준히 관심 가졌던 종목.",
            "장기 관심 = 감정 이입 가능성.",
            "이 종목에 대한 객관적 평가가 남아 있는지 점검. 관심이 집착으로 변하면 독.",
            "M002",
        )


@_register("M003")
def _d(s):
    if s["first_trip"] and s["first_trip"]["pnl"] > 500_000 and s["total_pnl"] < 0:
        return _fb(
            "mistake", "warning", "🪤",
            "첫 거래에 성공한 게 덫",
            f"첫 트레이드가 +{_won(s['first_trip']['pnl'])}. 이후 계속 들어가다 결국 {_won(s['total_pnl'])}.",
            "첫 성공 때문에 '쉬운 종목'이라는 착각.",
            "첫 성공은 우연일 수 있어요. 재현 가능한 패턴인지 엄격하게 검증.",
            "M003",
        )


@_register("M004")
def _d(s):
    if s["first_trip"] and s["first_trip"]["pnl"] < -500_000 and s["total_pnl"] > 1_000_000:
        return _fb(
            "strength", "positive", "🔄",
            "첫 실패를 극복",
            f"첫 트레이드가 {_won(s['first_trip']['pnl'])}였는데 결국 누적 {_won(s['total_pnl'])}.",
            "배워서 이긴 케이스.",
            "어떻게 극복했는지 기억해두기. 실패 → 분석 → 재도전의 모범 사례.",
            "M004",
        )


# ============================================================
# [N] 기타 상황 (플랫/중립)
# ============================================================

@_register("N001")
def _d(s):
    if s["trip_count"] >= 2 and abs(s["total_pnl"]) < 100_000:
        return _fb(
            "insight", "info", "😐",
            "이래도 저래도 본전",
            f"{s['trip_count']}번 거래에 순손익 {_won(s['total_pnl'])}. 사실상 제로섬.",
            "시간만 쓰고 결과 없음.",
            "이 종목 매매에 쓴 시간을 더 확신 있는 종목으로 옮기기.",
            "N001",
        )


# ============================================================
# analyze_ticker — 모든 탐지기 적용
# ============================================================

def analyze_ticker(
    key: str,
    all_trades: list[Trade],
    all_trips: list[dict],
    year: int | None = None,
) -> dict | None:
    s = _build_stats(key, all_trades, all_trips, year)
    if not s:
        return None

    fires: list[dict] = []
    for name, detector in _DETECTORS:
        try:
            res = detector(s)
            if res:
                fires.append(res)
        except Exception:
            pass

    strengths = [f for f in fires if f["category"] == "strength"]
    mistakes = [f for f in fires if f["category"] == "mistake"]
    improvements = [f for f in fires if f["category"] == "improvement"]
    insights = [f for f in fires if f["category"] == "insight"]

    return {
        "ticker": s["key"],
        "symbol": s["symbol"],
        "trip_count": s["trip_count"],
        "trade_count": s["trade_count"],
        "win_count": s["win_count"],
        "loss_count": s["loss_count"],
        "win_rate": s["win_rate"],
        "total_pnl": s["total_pnl"],
        "buy_volume": s["buy_volume"],
        "sell_volume": s["sell_volume"],
        "fees": s["fees"],
        "taxes": s["taxes"],
        "avg_hold": s["avg_hold"],
        "avg_win_hold": s["avg_win_hold"],
        "avg_loss_hold": s["avg_loss_hold"],
        "avg_win_pct": s["avg_win_pct"],
        "avg_loss_pct": s["avg_loss_pct"],
        "max_loss_streak": s["max_loss_streak"],
        "max_win_streak": s["max_win_streak"],
        "rr_ratio": s["rr_ratio"],
        "required_wr": s["required_wr"],
        "profit_factor": s["profit_factor"],
        "has_open_position": s["has_open_position"],
        "net_buy_qty": s["net_buy_qty"],
        "strengths": strengths,
        "mistakes": mistakes,
        "improvements": improvements,
        "insights": insights,
        "total_patterns": len(fires),
    }


def analyze_all_tickers(
    trades: list[Trade],
    year: int | None = None,
    limit: int = 30,
    min_trips: int = 1,
) -> list[dict]:
    all_trips, _ = compute_realizations(trades)
    keys: set[str] = set()
    for t in trades:
        keys.add(_key_of(t))

    out: list[dict] = []
    for k in keys:
        a = analyze_ticker(k, trades, all_trips, year=year)
        if a and a["trip_count"] >= min_trips:
            out.append(a)

    out.sort(key=lambda r: -abs(r["total_pnl"]))
    return out[:limit]
