"""사용자 건의사항(피드백) DB 헬퍼.

원래 server/state.py 안에 있던 user_suggestions 테이블 헬퍼를 자기완결
모듈로 분리. state.py는 아래 함수를 facade로 re-export하므로
`from server.state import save_suggestion` 같은 기존 import는 그대로 동작.
"""

from datetime import datetime

from ..db.connections import get_stocks_conn

_SUGGESTIONS_SCHEMA_READY = False


def ensure_suggestions_table(conn) -> None:
    """user_suggestions 테이블·시퀀스 보장. 프로세스당 1회만 실행."""
    global _SUGGESTIONS_SCHEMA_READY
    if _SUGGESTIONS_SCHEMA_READY:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_suggestions (
            id BIGSERIAL PRIMARY KEY,
            user_email TEXT,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            notified INTEGER DEFAULT 0
        )
        """
    )
    try:
        conn.execute("CREATE SEQUENCE IF NOT EXISTS user_suggestions_id_seq")
        conn.execute(
            """
            ALTER TABLE user_suggestions
            ALTER COLUMN id SET DEFAULT nextval('user_suggestions_id_seq')
            """
        )
    except Exception:
        pass
    conn.commit()
    _SUGGESTIONS_SCHEMA_READY = True


def save_suggestion(user_email: str, content: str) -> None:
    conn = get_stocks_conn()
    try:
        ensure_suggestions_table(conn)
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO user_suggestions (user_email, content, created_at) VALUES (?, ?, ?)",
            (user_email, content, now_ts),
        )
        conn.commit()
    finally:
        conn.close()
