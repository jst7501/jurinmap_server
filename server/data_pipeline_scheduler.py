"""
server/data_pipeline_scheduler.py
──────────────────────────────────────────────────────────────
크롤러 3종(투자경고·투자자수급·DART)을 APScheduler로 자동 실행.

기본 스케줄 (Asia/Seoul, 월~금):
  09:00          scripts/sync_investment_warnings.py   (장 전 시장경보)
  16:00          scripts/sync_investment_warnings.py   (장 후 해제/신규 반영)
  16:30          scripts/sync_investor_to_db.py        (장 마감 후 수급 종합)
  17:00          scripts/sync_dart_to_db.py            (공시 누적 반영)

환경변수(기본값 / override):
  DATA_PIPELINE_ENABLED="1"
  DATA_PIPELINE_WARN_HOURS="9,16"         # 투자경고 실행 시각(시만, 분=0)
  DATA_PIPELINE_INVESTOR_TIME="16:30"     # HH:MM
  DATA_PIPELINE_DART_TIME="17:00"         # HH:MM
  DATA_PIPELINE_TIMEZONE="Asia/Seoul"

기존 `_start_nightly_scheduler()` 와 별개로 동작하며, 각 잡은
서브프로세스(scripts/*.py)로 실행해 FastAPI 이벤트 루프를 차단하지 않음.

- `coalesce=True` + `misfire_grace_time=3600` : 서버가 꺼졌다가 켜져도
  1시간 이내 지나간 예정은 1회로 합쳐 실행.
- `max_instances=1` : 같은 잡이 동시에 두 개 돌지 않음.
- reload/재시작 시 모듈 싱글톤 `_STARTED` 가드로 중복 등록 방지.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOG_DIR = os.path.join(_ROOT_DIR, "logs")

_STARTED = False
_SCHEDULER: Optional[BackgroundScheduler] = None


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _log_path(kind: str) -> str:
    os.makedirs(_LOG_DIR, exist_ok=True)
    return os.path.join(_LOG_DIR, f"{kind}_{_timestamp()}.log")


def _run_script(kind: str, argv: list) -> None:
    """서브프로세스로 scripts/*.py 실행. stdout+stderr을 로그 파일로."""
    cmd = [sys.executable, "-u", "-X", "utf8", *argv]
    log_file = _log_path(kind)
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    logger.info("[pipeline] run %s cmd=%s log=%s", kind, cmd, log_file)
    try:
        with open(log_file, "w", encoding="utf-8") as f:
            res = subprocess.run(
                cmd, cwd=_ROOT_DIR, env=env,
                stdout=f, stderr=subprocess.STDOUT,
            )
        if res.returncode == 0:
            logger.info("[pipeline] done %s rc=0", kind)
        else:
            logger.warning(
                "[pipeline] %s non-zero exit rc=%s (see %s)",
                kind, res.returncode, log_file,
            )
    except Exception as e:
        logger.exception("[pipeline] %s crashed: %s", kind, e)


# ── 잡 정의 (서브프로세스로 실행할 스크립트) ─────────────────────────
def _job_investment_warnings():
    # 1) 네이버 시장경보 페이지 → 거래정지 사유 풍부 / caution·warning 단계는 빈 사유
    _run_script(
        "investment_warnings",
        [os.path.join(_ROOT_DIR, "scripts", "sync_investment_warnings.py")],
    )
    # 2) KIS 현재가 raw_json → caution·warning·risk 단계 정확 + 단기과열·관리·정리매매 추가
    #    네이버가 먼저 들어간 후 KIS 가 빈 사유 채움 (ON CONFLICT DO UPDATE 로 풍부 사유 보존)
    _run_script(
        "investment_warnings_kis",
        [os.path.join(_ROOT_DIR, "scripts", "sync_investment_warnings_kis.py")],
    )


def _job_stocks_meta_backfill():
    """price_today 에 시세는 들어왔는데 stocks 마스터에 메타가 없는
    신규 상장·재상장 종목 자동 backfill (네이버 → placeholder fallback).
    refresh_all_prices_today 직후 09:30 권장.
    """
    _run_script(
        "stocks_meta_backfill",
        [os.path.join(_ROOT_DIR, "scripts", "sync_stocks_meta_backfill.py")],
    )


def _job_etf_into_stocks():
    """kr_etf_master 의 ETF 를 stocks + price_today 로 전파.
    ETF 를 종목 검색·거래대금 리스트에 일반 종목과 함께 노출하기 위함.
    ETF 폴러가 kr_etf_master 를 갱신한 뒤 따라 도는 게 이상적이라 장중 30분 간격.
    """
    _run_script(
        "etf_into_stocks",
        [os.path.join(_ROOT_DIR, "scripts", "sync_etf_into_stocks.py")],
    )


def _job_investor_flow():
    _run_script(
        "investor_sync",
        [
            os.path.join(_ROOT_DIR, "scripts", "sync_investor_to_db.py"),
            "--mode", "full",
            "--workers", "2",
            "--sleep", "0.15",
            "--days", "20",
        ],
    )


def _job_investor_flow_intra():
    """장중 종목별 수급 갱신 — full 16:30 잡 외에 09:30/12:30/14:30/15:30 추가.
    KIS inquire-investor 가 장중에도 어제까지의 마감 누적 + 당일 부분 누적을 주므로
    하루 1회보다 5회가 훨씬 신선. mode=missing/days=1 로 가벼운 호출.
    """
    _run_script(
        "investor_intra",
        [
            os.path.join(_ROOT_DIR, "scripts", "sync_investor_to_db.py"),
            "--mode", "missing",
            "--workers", "2",
            "--sleep", "0.15",
            "--days", "1",
        ],
    )


def _job_market_flow_intraday():
    """KOSPI/KOSDAQ 시장 단위 시간대 수급 5분 폴링.
    KIS empty_output 시 skip — 다음 cron 재시도.
    """
    _run_script(
        "market_flow_intra",
        [os.path.join(_ROOT_DIR, "scripts", "sync_market_flow_intraday.py")],
    )


def _job_broker_top_daily():
    """종목별 거래원 매수/매도 Top5 — 16:35 (investor 16:30 끝난 직후) 1회.
    상위 거래대금 + 메가캡(~150종목)만 수집해 1-2분에 끝남.
    full 모드(전 종목)는 사용자가 수동으로 `--mode full` 호출.
    """
    _run_script(
        "broker_top_daily",
        [
            os.path.join(_ROOT_DIR, "scripts", "sync_broker_top.py"),
            "--mode", "top",
            "--workers", "2",
            "--sleep", "0.15",
        ],
    )


def _job_dart_sync():
    _run_script(
        "dart_sync",
        [
            os.path.join(_ROOT_DIR, "scripts", "sync_dart_to_db.py"),
            "--mode", "missing",
            "--workers", "4",
            "--disclosure-count", "10",
            "--days", "60",
        ],
    )


def _job_dart_financials():
    # 3개년 핵심 재무계정 (영업이익·매출액·당기순이익) — 사업보고서 기반.
    # 연간 데이터라 큰 변동 없지만 신규 상장사/누락분 보완을 위해 매일 missing 모드로.
    _run_script(
        "dart_financials",
        [
            os.path.join(_ROOT_DIR, "scripts", "sync_dart_financials_to_db.py"),
            "--mode", "missing",
            "--workers", "4",
            "--sleep", "0.05",
        ],
    )


def _job_global_indicators():
    # 홈 매크로 대시보드 (지수·선물·원자재·크립토·금리·환율·F&G). yfinance 배치.
    _run_script(
        "global_indicators",
        [os.path.join(_ROOT_DIR, "scripts", "sync_global_indicators.py")],
    )


def _job_us_quote_refresh():
    """미국 페니 현재가 캐시 갱신 — Finnhub → yfinance fallback → DB."""
    _run_script(
        "us_quote_cache",
        [os.path.join(_ROOT_DIR, "scripts", "sync_us_quote_cache.py"),
         "--penny-only", "--limit", "100"],
    )


def _job_us_price_history():
    """미국 페니 일봉 캐시 갱신 — yfinance 1y → DB (pump-dump·52주 차트 공용)."""
    _run_script(
        "us_price_history",
        [os.path.join(_ROOT_DIR, "scripts", "sync_us_price_history.py"),
         "--penny-only", "--period", "1y"],
    )


def _job_us_yfinance_refresh():
    """미국 페니 yfinance 스냅샷 캐시 갱신 — info·share_stats·holders·financials → DB.

    상세페이지 yfinance.info(1~5초) 동기 호출을 DB hit 으로 전환.
    """
    _run_script(
        "us_yfinance_cache",
        [os.path.join(_ROOT_DIR, "scripts", "sync_us_yfinance_cache.py"),
         "--penny-only", "--limit", "100"],
    )


def _parse_hm(s: str, default_h: int, default_m: int):
    try:
        h, m = s.split(":")
        return int(h), int(m)
    except Exception:
        return default_h, default_m


def start_data_pipeline_scheduler() -> None:
    """서버 startup에서 1회 호출. 중복 등록 방지."""
    global _STARTED, _SCHEDULER
    if _STARTED:
        return

    if os.getenv("DATA_PIPELINE_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        logger.info("[pipeline] scheduler disabled via DATA_PIPELINE_ENABLED env")
        return

    tz = os.getenv("DATA_PIPELINE_TIMEZONE", "Asia/Seoul").strip() or "Asia/Seoul"

    sched = BackgroundScheduler(
        timezone=tz,
        job_defaults={
            "coalesce": True,
            "misfire_grace_time": 3600,
            "max_instances": 1,
        },
    )

    warn_hours_raw = os.getenv("DATA_PIPELINE_WARN_HOURS", "9,16").strip()
    warn_hours = [
        int(x) for x in warn_hours_raw.split(",")
        if x.strip().isdigit() and 0 <= int(x) <= 23
    ] or [9, 16]

    inv_h, inv_m = _parse_hm(
        os.getenv("DATA_PIPELINE_INVESTOR_TIME", "16:30"), 16, 30
    )
    dart_h, dart_m = _parse_hm(
        os.getenv("DATA_PIPELINE_DART_TIME", "17:00"), 17, 0
    )
    fin_h, fin_m = _parse_hm(
        os.getenv("DATA_PIPELINE_FINANCIALS_TIME", "17:30"), 17, 30
    )

    # 시장경보 (월~금, 지정된 시각마다)
    for h in warn_hours:
        sched.add_job(
            _job_investment_warnings,
            CronTrigger(day_of_week="mon-fri", hour=h, minute=0, timezone=tz),
            id=f"investment_warnings_{h:02d}",
            replace_existing=True,
        )

    # stocks 마스터 backfill — 신규 상장 종목 메타 자동 채우기
    # 09:30 (장 시작 직후 첫 시세 들어온 후), 16:30 (장 마감 후) 2회.
    # 네이버 finance.naver.com 매칭 + price_today.raw_json fallback.
    sched.add_job(
        _job_stocks_meta_backfill,
        CronTrigger(day_of_week="mon-fri", hour="9,16", minute=30, timezone=tz),
        id="stocks_meta_backfill",
        replace_existing=True,
    )

    # ETF → stocks/price_today 전파 — 종목 검색·거래대금 리스트에 ETF 함께 노출.
    # 장중(09-15) 30분 간격 + 16:40 마감 정산 1회.
    # 끄려면: PIPELINE_ETF_INTO_STOCKS=0
    if _env_truthy_local("PIPELINE_ETF_INTO_STOCKS", default="1"):
        sched.add_job(
            _job_etf_into_stocks,
            CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/30", timezone=tz),
            id="etf_into_stocks_intra",
            replace_existing=True,
        )
        sched.add_job(
            _job_etf_into_stocks,
            CronTrigger(day_of_week="mon-fri", hour=16, minute=40, timezone=tz),
            id="etf_into_stocks_close",
            replace_existing=True,
        )

    # 투자자 수급 (장 마감 후 종합 — 20일치 full)
    sched.add_job(
        _job_investor_flow,
        CronTrigger(day_of_week="mon-fri", hour=inv_h, minute=inv_m, timezone=tz),
        id="investor_flow",
        replace_existing=True,
    )

    # 투자자 수급 (장중 — 09:30/12:30/14:30/15:30 missing 모드).
    # 가벼운 호출이라 정규장 시간 내 4회 추가 → 사용자 화면 수급이 16:30 까지 박제되던 문제 해소.
    # 끄려면: PIPELINE_INVESTOR_INTRA=0
    if _env_truthy_local("PIPELINE_INVESTOR_INTRA", default="1"):
        sched.add_job(
            _job_investor_flow_intra,
            CronTrigger(day_of_week="mon-fri", hour="9,12,14,15", minute="30", timezone=tz),
            id="investor_flow_intra",
            replace_existing=True,
        )

    # 시장 단위 시간대 수급 (5분 폴링) — KIS empty_output 회피용 캐시.
    # 정규장 시간(09:00-15:30)에만 호출. 그 외엔 무용.
    # 끄려면: PIPELINE_MARKET_FLOW_INTRA=0
    if _env_truthy_local("PIPELINE_MARKET_FLOW_INTRA", default="1"):
        sched.add_job(
            _job_market_flow_intraday,
            CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/5", timezone=tz),
            id="market_flow_intra",
            replace_existing=True,
        )

    # 거래원 Top — 16:35 (investor full 16:30 끝난 직후) 1회/일.
    # 끄려면: PIPELINE_BROKER_TOP=0
    broker_h, broker_m = _parse_hm(
        os.getenv("DATA_PIPELINE_BROKER_TIME", "16:35"), 16, 35
    )
    if _env_truthy_local("PIPELINE_BROKER_TOP", default="1"):
        sched.add_job(
            _job_broker_top_daily,
            CronTrigger(day_of_week="mon-fri", hour=broker_h, minute=broker_m, timezone=tz),
            id="broker_top_daily",
            replace_existing=True,
        )

    # DART 공시·주요주주
    sched.add_job(
        _job_dart_sync,
        CronTrigger(day_of_week="mon-fri", hour=dart_h, minute=dart_m, timezone=tz),
        id="dart_sync",
        replace_existing=True,
    )

    # DART 재무제표 3개년 (매출·영업이익·당기순이익)
    sched.add_job(
        _job_dart_financials,
        CronTrigger(day_of_week="mon-fri", hour=fin_h, minute=fin_m, timezone=tz),
        id="dart_financials",
        replace_existing=True,
    )

    # 글로벌 매크로 대시보드 — 5분마다 상시 (crypto/선물 변동 추적)
    # 2026-05-15: 기본 ON 으로 변경 — AI 트레이더가 stale 데이터로 매매하던 문제 해결.
    # 끄려면 PIPELINE_GLOBAL_INDICATORS=0 (또는 OFF/false/no).
    _gi_env = os.getenv("PIPELINE_GLOBAL_INDICATORS", "1").strip().lower()
    _gi_enabled = _gi_env not in ("0", "false", "no", "off")
    if _gi_enabled:
        sched.add_job(
            _job_global_indicators,
            CronTrigger(minute="*/5", timezone=tz),
            id="global_indicators",
            replace_existing=True,
        )

    # ── 미국 주식 현재가 캐시 갱신 ──────────────────────────────────────
    # 끄려면: PIPELINE_US_QUOTE=0
    if _env_truthy_local("PIPELINE_US_QUOTE", default="1"):
        # 정규장 KST 22:00~06:00 (EDT 기준 미국 장 시간) — 5분마다
        sched.add_job(
            _job_us_quote_refresh,
            CronTrigger(hour="22,23,0,1,2,3,4,5", minute="*/5", timezone=tz),
            id="us_quote_market",
            replace_existing=True,
        )
        # 프리/애프터마켓 KST 06:00~10:00, 18:00~21:00 — 15분마다
        sched.add_job(
            _job_us_quote_refresh,
            CronTrigger(hour="6,7,8,9,10,18,19,20,21", minute="*/15", timezone=tz),
            id="us_quote_extended",
            replace_existing=True,
        )
        # 장 마감 KST 11:00~17:00 — 1시간마다 (stale 유지용)
        sched.add_job(
            _job_us_quote_refresh,
            CronTrigger(hour="11,12,13,14,15,16,17", minute=0, timezone=tz),
            id="us_quote_idle",
            replace_existing=True,
        )

    # ── 미국 일봉 캐시 갱신 (pump-dump·52주 차트 공용) ─────────────────
    # 매일 KST 07:30 1회 — 미국 장 마감(06:00) 후 안정화된 뒤 실행.
    # 끄려면: PIPELINE_US_HISTORY=0
    if _env_truthy_local("PIPELINE_US_HISTORY", default="1"):
        sched.add_job(
            _job_us_price_history,
            CronTrigger(hour=7, minute=30, timezone=tz),
            id="us_price_history_daily",
            replace_existing=True,
        )

    # ── 미국 yfinance 스냅샷 캐시 (상세페이지 펀더멘털·공매도·기관 비율) ──
    # 펀더멘털은 거의 안 변함 → 3시간마다. 끄려면: PIPELINE_US_YFINANCE=0
    if _env_truthy_local("PIPELINE_US_YFINANCE", default="1"):
        sched.add_job(
            _job_us_yfinance_refresh,
            CronTrigger(hour="*/3", minute=20, timezone=tz),
            id="us_yfinance_snapshot",
            replace_existing=True,
        )

    sched.start()


def _env_truthy_local(name: str, default: str = "") -> bool:
    raw = os.getenv(name, default).strip().lower()
    return raw in ("1", "true", "yes", "on")


def remove_job(job_id: str) -> bool:
    """실시간으로 등록된 잡을 제거. 서버 재시작 없이 비활성화."""
    if not _SCHEDULER:
        return False
    try:
        _SCHEDULER.remove_job(job_id)
        return True
    except Exception:
        return False
    _SCHEDULER = sched
    _STARTED = True

    for job in sched.get_jobs():
        nxt = job.next_run_time.isoformat() if job.next_run_time else "n/a"
        logger.info("[pipeline] scheduled %s next=%s", job.id, nxt)


def scheduler_status() -> dict:
    """현재 등록된 잡 상태 (디버그/API 노출용)."""
    if not _SCHEDULER:
        return {"enabled": False, "jobs": []}
    return {
        "enabled": True,
        "timezone": str(_SCHEDULER.timezone),
        "jobs": [
            {
                "id": j.id,
                "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
                "trigger": str(j.trigger),
            }
            for j in _SCHEDULER.get_jobs()
        ],
    }


def trigger_job_now(job_id: str) -> bool:
    """등록된 잡을 즉시 1회 실행 (테스트용).  True=트리거됨."""
    if not _SCHEDULER:
        return False
    job = _SCHEDULER.get_job(job_id)
    if not job:
        return False
    job.modify(next_run_time=datetime.now(_SCHEDULER.timezone))
    return True
