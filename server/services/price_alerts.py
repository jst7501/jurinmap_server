"""가격 알림(price_alerts) DB 헬퍼 + 백그라운드 체크.

원래 server/state.py 안에 있던 price_alerts 관련 함수 묶음을 자기완결
모듈로 분리. state.py 가 아래 심볼들을 facade 로 re-export 한다.
"""

import logging
import threading
from datetime import datetime

from ..db.connections import get_stocks_conn

logger = logging.getLogger("server.services.price_alerts")

_BG_ALERT_LOCK = threading.Lock()
_PRICE_ALERTS_SCHEMA_READY = False


def _ensure_price_alerts_table(conn) -> None:
    """price_alerts 테이블·시퀀스 보장. 프로세스당 1회만 실행."""
    global _PRICE_ALERTS_SCHEMA_READY
    if _PRICE_ALERTS_SCHEMA_READY:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_alerts (
            id BIGSERIAL PRIMARY KEY,
            token TEXT NOT NULL,
            code TEXT NOT NULL,
            target_price DOUBLE PRECISION NOT NULL,
            direction TEXT NOT NULL DEFAULT 'above',
            triggered BIGINT NOT NULL DEFAULT 0,
            created_at TEXT,
            UNIQUE(token, code)
        )
        """
    )
    try:
        conn.execute(
            """
            ALTER TABLE price_alerts
            ADD CONSTRAINT price_alerts_token_code_key UNIQUE (token, code)
            """
        )
    except Exception:
        pass
    try:
        conn.execute("CREATE SEQUENCE IF NOT EXISTS price_alerts_id_seq")
        conn.execute(
            """
            ALTER TABLE price_alerts
            ALTER COLUMN id SET DEFAULT nextval('price_alerts_id_seq')
            """
        )
        conn.execute(
            """
            SELECT setval(
                'price_alerts_id_seq',
                COALESCE((SELECT MAX(id) FROM price_alerts), 0) + 1,
                false
            )
            """
        )
    except Exception:
        pass
    conn.commit()
    _PRICE_ALERTS_SCHEMA_READY = True


def set_price_alert(token: str, code: str, target_price: float, direction: str) -> None:
    conn = get_stocks_conn()
    try:
        _ensure_price_alerts_table(conn)
        conn.execute(
            """
            INSERT INTO price_alerts(token, code, target_price, direction, triggered, created_at)
            VALUES(?, ?, ?, ?, 0, ?)
            ON CONFLICT(token, code) DO UPDATE SET
                target_price=excluded.target_price,
                direction=excluded.direction,
                triggered=0,
                created_at=excluded.created_at
            """,
            (token, code, target_price, direction, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    finally:
        conn.close()


def cancel_price_alert(token: str, code: str) -> None:
    conn = get_stocks_conn()
    try:
        _ensure_price_alerts_table(conn)
        conn.execute("DELETE FROM price_alerts WHERE token=? AND code=?", (token, code))
        conn.commit()
    finally:
        conn.close()


def _bg_check_price_alerts() -> None:
    """미트리거 가격 알림 확인 후 조건 충족 시 FCM 발송.

    동시에 한 워커에서만 실행되도록 best-effort 락. push 발송은
    push_service 의 _send_fcm_to_token 을 lazy import 해서 사용 (순환 회피).
    """
    if not _BG_ALERT_LOCK.acquire(blocking=False):
        return
    try:
        conn = get_stocks_conn()
        try:
            _ensure_price_alerts_table(conn)
            alerts = conn.execute(
                "SELECT id, token, code, target_price, direction FROM price_alerts WHERE triggered=0"
            ).fetchall()
        finally:
            conn.close()

        if not alerts:
            return

        # 알림 종목 현재가 일괄 조회 (price_today 테이블)
        codes = list({r[2] for r in alerts})
        conn = get_stocks_conn()
        try:
            placeholders = ",".join("?" * len(codes))
            prices = dict(
                conn.execute(
                    f"SELECT code, current_price FROM price_today WHERE code IN ({placeholders})",
                    codes,
                ).fetchall()
            )
        finally:
            conn.close()

        # push_service 는 state 와 양방향 import 가 가능하므로 lazy import
        try:
            from .push_service import _send_fcm_to_token
        except Exception:
            _send_fcm_to_token = None

        # 사용자별 알림 종류×시간 환경설정 — kind="stock"
        try:
            from .notification_pref import is_token_allowed
        except Exception:
            is_token_allowed = None

        triggered_ids = []
        for alert_id, token, code, target_price, direction in alerts:
            current = prices.get(code)
            if not current:
                continue
            hit = (
                (direction == "above" and current >= target_price)
                or (direction == "below" and current <= target_price)
            )
            if not hit:
                continue

            arrow = "🔺" if direction == "above" else "🔻"
            payload = {
                "title": f"{arrow} 가격 알림 도달",
                "body": f"{code} 현재가 {int(current):,}원 — 목표가 {int(target_price):,}원 {direction}",
                # 2026-04-24 라우터 변경: HashRouter(/#/stock/...) → BrowserRouter(/stock/...)
                "url": f"/stock/{code}",
                "tag": f"price-alert-{code}",
                "kind": "stock",
            }
            # 사용자가 stock 알림을 현재 시간대에 끄면 발송 skip — 단, 조건 충족은 한 번뿐이므로
            # alert 자체는 triggered 처리해 다시 발송 시도하지 않음 (반복 알림 방지).
            if is_token_allowed is not None:
                try:
                    if not is_token_allowed(token, "stock"):
                        triggered_ids.append(alert_id)
                        continue
                except Exception:
                    pass

            if _send_fcm_to_token is not None:
                try:
                    _send_fcm_to_token(token, payload)
                except Exception:
                    pass
            triggered_ids.append(alert_id)

        if triggered_ids:
            conn = get_stocks_conn()
            try:
                conn.executemany(
                    "UPDATE price_alerts SET triggered=1 WHERE id=?",
                    [(i,) for i in triggered_ids],
                )
                conn.commit()
            finally:
                conn.close()
            logger.info("[price_alert] %d건 알림 발송 완료", len(triggered_ids))

    except Exception as e:
        logger.exception("[price_alert] 오류: %s", e)
    finally:
        _BG_ALERT_LOCK.release()
