"""워치리스트 푸시 트리거 — halt / filing / spike 감지 → FCM 푸시.

사용 패턴:
  cron 또는 systemd timer 로 1분 ~ 5분 마다 실행.

체크 항목:
  halt   — NASDAQ Trader RSS 의 활성 LULD halt 중 워치리스트 종목 발견
  filing — 최근 N분 내 새 8-K / 6-K / 424B5 / S-1 / S-3 / F-1 도착
  spike  — yfinance 1분봉으로 pre/regular ±20% 감지

중복 푸시 회피:
  us_watchlist_alert_log 테이블 — (token, symbol, kind, key, sent_at)
  같은 (kind, key) 는 24시간 내 1회만 발송.

사용법:
  python scripts/run_watchlist_alerts.py --kind halt --window-min 5
  python scripts/run_watchlist_alerts.py --kind filing --window-min 30
  python scripts/run_watchlist_alerts.py --kind spike --min-change-pct 20
  python scripts/run_watchlist_alerts.py --kind all  # 세 가지 모두

dry-run:
  python scripts/run_watchlist_alerts.py --kind all --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("scripts.run_watchlist_alerts")

ALERT_DEDUPE_HOURS = 24


def _ensure_alert_log(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS us_watchlist_alert_log (
            token TEXT NOT NULL,
            symbol TEXT NOT NULL,
            kind TEXT NOT NULL,
            key TEXT NOT NULL,
            sent_at TIMESTAMP,
            payload_title TEXT,
            payload_body TEXT,
            PRIMARY KEY (token, symbol, kind, key)
        )
        """
    )
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_log_recent ON us_watchlist_alert_log(sent_at DESC)")
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def _already_sent(conn, token: str, symbol: str, kind: str, key: str) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ALERT_DEDUPE_HOURS)).replace(tzinfo=None)
    cur = conn.execute(
        """
        SELECT 1 FROM us_watchlist_alert_log
        WHERE token = %s AND symbol = %s AND kind = %s AND key = %s
          AND sent_at >= %s
        LIMIT 1
        """,
        (token, symbol, kind, key, cutoff),
    )
    return cur.fetchone() is not None


def _record_alert(conn, token: str, symbol: str, kind: str, key: str, title: str, body: str) -> None:
    conn.execute(
        """
        INSERT INTO us_watchlist_alert_log
            (token, symbol, kind, key, sent_at, payload_title, payload_body)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(token, symbol, kind, key) DO UPDATE SET
            sent_at = EXCLUDED.sent_at,
            payload_title = EXCLUDED.payload_title,
            payload_body = EXCLUDED.payload_body
        """,
        (token, symbol, kind, key, datetime.now(timezone.utc).replace(tzinfo=None), title, body),
    )


def _send_push(token: str, title: str, body: str, url: str, dry_run: bool = False) -> bool:
    if dry_run:
        logger.info("[DRY] → %s: %s | %s", token[:12], title, body)
        return True
    try:
        from server.services.push_service import _send_fcm_to_token
        _send_fcm_to_token(token, {"title": title, "body": body, "url": url})
        return True
    except Exception as exc:
        logger.error("send_fcm_to_token failed: %s", exc)
        return False


def _get_watchlist_subscribers(conn, kind: str) -> dict[str, list[str]]:
    """{symbol: [token1, token2, ...]} — alert_kinds 에 kind 포함된 사람만."""
    cur = conn.execute(
        """
        SELECT symbol, token, alert_kinds
        FROM us_user_watchlist
        WHERE alert_kinds LIKE %s OR alert_kinds IS NULL OR alert_kinds = ''
        """,
        (f"%{kind}%",),
    )
    by_symbol: dict[str, list[str]] = {}
    for r in cur.fetchall():
        sym, tok, kinds = r[0], r[1], r[2] or ""
        if not sym or not tok:
            continue
        kinds_set = set(s.strip() for s in kinds.split(",") if s.strip()) if kinds else {"halt", "filing", "spike"}
        if kind not in kinds_set:
            continue
        by_symbol.setdefault(sym.upper(), []).append(tok)
    return by_symbol


# ── halt ──
def check_halts(conn, window_min: int, dry_run: bool) -> int:
    """현재 활성 LULD halt 중 워치리스트 종목 → 푸시."""
    from collectors.us_trade_halts import get_recent_halts
    halts = get_recent_halts(active_only=True, hours=max(1, window_min // 30 + 1), max_items=200)
    subscribers = _get_watchlist_subscribers(conn, "halt")
    if not subscribers:
        logger.info("[halt] no subscribers")
        return 0

    now_utc = datetime.now(timezone.utc)
    sent = 0
    for h in halts:
        sym = (h.get("symbol") or "").upper()
        if not sym or sym not in subscribers:
            continue
        # 활성 halt 만 (재개 시각이 미래)
        resume = h.get("expected_resume_at_utc")
        if resume:
            try:
                resume_dt = datetime.fromisoformat(resume.replace("Z", "+00:00"))
                if resume_dt <= now_utc:
                    continue
            except Exception:
                pass
        halt_key = f"{sym}|{h.get('halt_at_utc') or ''}"
        title = f"{sym} 거래정지"
        reason = h.get("reason_kr") or h.get("reason_code") or "LULD"
        body = f"{reason} · 재개 {resume[11:19] if resume else '미정'} (UTC)"
        for token in subscribers[sym]:
            if _already_sent(conn, token, sym, "halt", halt_key):
                continue
            url = f"/us/stock/NAS/{sym}"
            ok = _send_push(token, title, body, url, dry_run)
            if ok:
                _record_alert(conn, token, sym, "halt", halt_key, title, body)
                sent += 1
    try:
        conn.commit()
    except Exception:
        pass
    logger.info("[halt] sent %d", sent)
    return sent


# ── filing ──
def check_filings(conn, window_min: int, dry_run: bool) -> int:
    """최근 window_min 분 내 새 push-worthy filing → 푸시."""
    subscribers = _get_watchlist_subscribers(conn, "filing")
    if not subscribers:
        logger.info("[filing] no subscribers")
        return 0
    syms = list(subscribers.keys())
    placeholders = ",".join(["%s"] * len(syms))
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_min)).replace(tzinfo=None)
    cur = conn.execute(
        f"""
        SELECT symbol, accession, form, filing_date, primary_doc_desc, items, doc_url, is_dilution
        FROM us_sec_filings
        WHERE symbol IN ({placeholders})
          AND created_at >= %s
          AND (is_summary_target = TRUE OR is_dilution = TRUE)
        ORDER BY filing_date DESC
        """,
        (*syms, cutoff),
    )
    rows = cur.fetchall()
    sent = 0
    for r in rows:
        sym, accession, form, fd, desc, items, url, is_dil = r
        sym = (sym or "").upper()
        if sym not in subscribers:
            continue
        prefix = "⚠ Dilution: " if is_dil else "공시: "
        items_str = f" ({items})" if items else ""
        title = f"{sym} {prefix}{form}"
        body = (desc or "")[:80] + items_str
        for token in subscribers[sym]:
            if _already_sent(conn, token, sym, "filing", accession):
                continue
            ok = _send_push(token, title, body, url or f"/us/stock/NAS/{sym}", dry_run)
            if ok:
                _record_alert(conn, token, sym, "filing", accession, title, body)
                sent += 1
    try:
        conn.commit()
    except Exception:
        pass
    logger.info("[filing] sent %d", sent)
    return sent


# ── spike ──
def check_spikes(conn, min_change_pct: float, dry_run: bool) -> int:
    """워치리스트 종목 중 현재 세션 ±min_change_pct 변동 → 푸시."""
    subscribers = _get_watchlist_subscribers(conn, "spike")
    if not subscribers:
        logger.info("[spike] no subscribers")
        return 0
    syms = list(subscribers.keys())
    from collectors.us_premarket import fetch_premarket_movers, detect_session
    session = detect_session()
    if session == "closed":
        logger.info("[spike] market closed")
        return 0
    try:
        res = fetch_premarket_movers(syms, min_change_pct=min_change_pct, min_volume=500, limit=200)
    except Exception as exc:
        logger.error("spike fetch fail: %s", exc)
        return 0
    movers = res.get("movers") or []
    sent = 0
    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for m in movers:
        sym = (m.get("symbol") or "").upper()
        if sym not in subscribers:
            continue
        pct = m.get("change_pct") or 0
        # 종목별 하루 1회 spike 만 (이미 +30 알림 후 +40 또 안 보냄)
        key = f"{today_key}|{session}|{'up' if pct > 0 else 'down'}"
        sign = "+" if pct > 0 else ""
        title = f"{sym} {sign}{pct:.1f}%"
        body = f"{session} 세션 · ${m.get('last') or 0:.2f} · 거래량 {m.get('volume') or 0:,}"
        for token in subscribers[sym]:
            if _already_sent(conn, token, sym, "spike", key):
                continue
            url = f"/us/stock/NAS/{sym}"
            ok = _send_push(token, title, body, url, dry_run)
            if ok:
                _record_alert(conn, token, sym, "spike", key, title, body)
                sent += 1
    try:
        conn.commit()
    except Exception:
        pass
    logger.info("[spike] sent %d", sent)
    return sent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["halt", "filing", "spike", "all"], default="all")
    ap.add_argument("--window-min", type=int, default=10, help="halt/filing 윈도우 분")
    ap.add_argument("--min-change-pct", type=float, default=20.0, help="spike 최소 |%|")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from server.db.connections import get_stocks_conn
    conn = get_stocks_conn()
    try:
        _ensure_alert_log(conn)
        total = 0
        if args.kind in ("halt", "all"):
            total += check_halts(conn, args.window_min, args.dry_run)
        if args.kind in ("filing", "all"):
            total += check_filings(conn, args.window_min, args.dry_run)
        if args.kind in ("spike", "all"):
            total += check_spikes(conn, args.min_change_pct, args.dry_run)
        print(f"[done] sent={total}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
