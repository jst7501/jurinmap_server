#!/usr/bin/env bash
# 미국 공매도 종합 일일 sync — 4개 데이터 소스 순차 실행.
# 권장 cron: 매일 KST 08:00 (= ET 18:00, FINRA EOD 파일 공개 직후)
#
# crontab -e 예시:
#   0 8 * * * cd /path/to/투자정보 && ./scripts/run_us_short_sync.sh >> logs/us_short_sync.log 2>&1
#
# Windows Task Scheduler: PowerShell wrapper 별도. 또는 git-bash 에서 동일하게 실행.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] $*"; }

log "=== us-short sync start ==="

# 1) FINRA Reg SHO Daily Short Volume — 가장 신선한 시그널 (T+1)
log "1/4 FINRA Daily Short Volume"
python scripts/sync_us_short_volume.py --days 1 || log "  ! finra failed"

# 2) Finviz SI% / DTC / ownership — 일 1회 fetch (실제 SI 보고서는 격주)
log "2/4 SI + ownership (Finviz)"
python scripts/sync_us_short_noapi.py --max-universe 1500 --workers 16 || log "  ! finviz failed"

# 3) iBorrowDesk 차입 데이터 — squeeze 후보 위주
log "3/5 iBorrowDesk borrow history"
python scripts/sync_us_borrow_history.py --max-universe 300 --days 14 || log "  ! borrow failed"

# 4) NYSE Reg SHO Threshold Securities — 강제 buy-in 압력 누적 종목
log "4/5 NYSE Threshold Securities"
python scripts/sync_us_threshold.py --days 1 || log "  ! threshold failed"

# 5) Squeeze Score 산출 + 로그 출력 (DB 저장은 안 함 — 매번 JOIN으로 계산)
log "5/5 Squeeze Score TOP 10"
python scripts/compute_us_squeeze_score.py --top 10 --require-si 2>&1 | tail -20 || log "  ! score failed"

log "=== us-short sync done ==="
