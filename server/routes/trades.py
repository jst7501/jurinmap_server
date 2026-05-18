"""매매일지 API — PDF 업로드 → 파싱+분석 → JSON 반환 (DB 통계 기록 추가)"""
from __future__ import annotations

import tempfile
import logging
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, UploadFile, File, HTTPException

from server.services.trade_journal.parser import parse_toss_pdf
from server.services.trade_journal.journal import build_journal_tree, compute_realizations
from server.services.trade_journal.wrapped import build_wrapped
from server.db.connections import get_stocks_conn

router = APIRouter()
logger = logging.getLogger("server.trades")

_PDF_LOGS_SCHEMA_READY = False


def _ensure_pdf_logs_table(conn):
    """PDF 분석 로그 테이블 생성 (per-process once)."""
    global _PDF_LOGS_SCHEMA_READY
    if _PDF_LOGS_SCHEMA_READY:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pdf_analysis_logs (
            id BIGSERIAL PRIMARY KEY,
            filename TEXT,
            trade_count INTEGER,
            analysis_year INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    _PDF_LOGS_SCHEMA_READY = True


def _log_pdf_analysis(filename: str, trade_count: int, year: int):
    """분석 성공 로그 기록 (상세 내역은 저장하지 않음)"""
    try:
        conn = get_stocks_conn()
        try:
            _ensure_pdf_logs_table(conn)
            # PgCompatConnection이 ? → %s 변환을 처리한다.
            conn.execute(
                "INSERT INTO pdf_analysis_logs (filename, trade_count, analysis_year) VALUES (?, ?, ?)",
                (filename, trade_count, year),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Failed to log PDF analysis: {e}")


def _by_symbol(trades):
    """종목별 집계"""
    buckets = {}
    for t in trades:
        key = t.ticker or t.symbol
        b = buckets.setdefault(key, {
            "ticker": t.ticker, "symbol": t.symbol,
            "buy_qty": 0.0, "sell_qty": 0.0,
            "buy_amount": 0.0, "sell_amount": 0.0,
            "fee": 0.0, "tax": 0.0, "trade_count": 0,
            "last_traded_at": t.traded_at,
        })
        if t.side == "BUY":
            b["buy_qty"] += t.quantity
            b["buy_amount"] += t.amount
        else:
            b["sell_qty"] += t.quantity
            b["sell_amount"] += t.amount
        b["fee"] += t.fee
        b["tax"] += t.tax
        b["trade_count"] += 1
        if t.traded_at > b["last_traded_at"]:
            b["last_traded_at"] = t.traded_at

    rows = []
    for b in buckets.values():
        open_qty = b["buy_qty"] - b["sell_qty"]
        realized = b["sell_amount"] - b["buy_amount"] - b["fee"] - b["tax"]
        rows.append({
            **b,
            "open_qty": open_qty,
            "is_closed": abs(open_qty) < 1e-9,
            "realized_pnl": realized,
            "last_traded_at": b["last_traded_at"].isoformat(),
        })
    rows.sort(key=lambda r: r["realized_pnl"])
    return rows


@router.get("/api/trades/stats")
async def get_trades_stats():
    """PDF 분석 통계 조회"""
    try:
        conn = get_stocks_conn()
        try:
            _ensure_pdf_logs_table(conn)
            row = conn.execute("SELECT COUNT(*) as count FROM pdf_analysis_logs").fetchone()
            total_count = row["count"] if row else 0
            
            # 추가 통계: 총 분석된 거래 건수 합계
            row_trades = conn.execute("SELECT SUM(trade_count) as total_trades FROM pdf_analysis_logs").fetchone()
            total_trades = row_trades["total_trades"] if row_trades and row_trades["total_trades"] else 0
            
            return {
                "ok": True,
                "total_analysis_count": total_count,
                "total_trades_analyzed": total_trades
            }
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Failed to fetch trades stats: {e}")
        return {"ok": False, "error": str(e)}


@router.post("/api/trades/analyze")
async def analyze_pdf(file: UploadFile = File(...)):
    """
    PDF 업로드 → 파싱 + 전체 분석 → JSON 한 방 반환.
    파일 상세 내용은 저장하지 않고, 분석 통계만 DB에 기록함.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="PDF 파일만 업로드할 수 있어요.")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        trades = parse_toss_pdf(tmp_path, source_file=file.filename)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if not trades:
        raise HTTPException(status_code=422, detail="매매 내역을 추출하지 못했어요. PDF 형식을 확인해주세요.")

    # 연도 자동 감지
    years = {t.traded_at.year for t in trades if t.traded_at}
    year = max(years) if years else datetime.now().year

    # 전체 분석 한 번에
    journal = build_journal_tree(trades)
    wrapped = build_wrapped(trades, year=year)
    by_symbol = _by_symbol(trades)
    trips, opens = compute_realizations(trades)

    # ── 통계 기록 (비동기 처리 대신 일단 동기로 처리, 실패해도 분석 결과는 반환) ──
    _log_pdf_analysis(file.filename, len(trades), year)

    return {
        "ok": True,
        "trade_count": len(trades),
        "year": year,
        "journal": journal,
        "wrapped": wrapped,
        "by_symbol": by_symbol,
        "roundtrips": {"closed": trips, "open": opens},
    }

