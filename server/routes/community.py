"""
커뮤니티 게시판 + 마켓 투표 API
"""
import random
from typing import Optional
from datetime import datetime, date

from datetime import timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db.connections import get_stocks_conn

router = APIRouter()

# Schema is created on the first request per process; subsequent calls skip the
# CREATE TABLE IF NOT EXISTS round-trip.
_COMMUNITY_SCHEMA_READY = False

# ─── 닉네임 생성 ────────────────────────────────────────────────
_ADJS = ["노련한","날카로운","용감한","신중한","빠른","조용한","영리한","대담한","침착한","활발한",
         "차분한","집중한","유연한","단단한","열정적인","냉철한","예리한","묵직한"]
_ANIMALS = ["토끼","여우","곰","독수리","늑대","사슴","호랑이","판다","수달","고양이",
            "부엉이","하마","재규어","코알라","펭귄","비버","두루미","라쿤"]

def _random_nickname() -> str:
    return f"{random.choice(_ADJS)} {random.choice(_ANIMALS)}{random.randint(10, 99)}"


# ─── DB 스키마 보장 ──────────────────────────────────────────────
def _ensure_community_tables(conn) -> None:
    global _COMMUNITY_SCHEMA_READY
    if _COMMUNITY_SCHEMA_READY:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_polls (
            date TEXT PRIMARY KEY,
            up_count INTEGER NOT NULL DEFAULT 0,
            down_count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS community_posts (
            id BIGSERIAL PRIMARY KEY,
            content TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT '익명',
            created_at TEXT NOT NULL,
            likes INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS community_comments (
            id BIGSERIAL PRIMARY KEY,
            post_id BIGINT NOT NULL REFERENCES community_posts(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT '익명',
            created_at TEXT NOT NULL,
            likes INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    _COMMUNITY_SCHEMA_READY = True


def _get_conn():
    conn = get_stocks_conn()
    _ensure_community_tables(conn)
    return conn


# ─── 투표 ────────────────────────────────────────────────────────
_POLL_QUESTION = "이란이 '전략적 승리'를 선언하며 2주간의 휴전을 수용하고 호르무즈 해협 통행을 허용했습니다.\n\n이것이 실제 '종전'으로 이어질까요, 아니면 '전쟁 재발'의 전조일까요?"
_POLL_UP_LABEL = "다시 전쟁이 일어날 것이다"
_POLL_UP_SUB   = "일시적 재정비"
_POLL_DOWN_LABEL = "종전으로 이어질 것이다"
_POLL_DOWN_SUB   = "평화 정착"


class VoteBody(BaseModel):
    direction: str  # 'up' | 'down'


@router.get("/api/community/poll/today")
def get_today_poll():
    today = date.today().isoformat()
    conn = _get_conn()
    try:
        # 전체 날짜 합산 (일별 초기화 없음)
        row = conn.execute(
            "SELECT COALESCE(SUM(up_count),0) AS up_count, COALESCE(SUM(down_count),0) AS down_count FROM market_polls"
        ).fetchone()
        up = int(row["up_count"]) if row else 0
        down = int(row["down_count"]) if row else 0
        total = up + down
        return {
            "date": today,
            "question": _POLL_QUESTION,
            "up_label": _POLL_UP_LABEL,
            "up_sub": _POLL_UP_SUB,
            "down_label": _POLL_DOWN_LABEL,
            "down_sub": _POLL_DOWN_SUB,
            "up_count": up,
            "down_count": down,
            "total": total,
        }
    finally:
        conn.close()


@router.post("/api/community/poll/vote")
def vote_poll(body: VoteBody):
    direction = (body.direction or "").strip().lower()
    if direction not in ("up", "down"):
        raise HTTPException(400, "direction은 'up' 또는 'down'이어야 합니다")

    today = date.today().isoformat()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO market_polls (date) VALUES (?) ON CONFLICT(date) DO NOTHING",
            (today,),
        )

        col = "up_count" if direction == "up" else "down_count"
        conn.execute(
            f"UPDATE market_polls SET {col} = {col} + 1 WHERE date = ?", (today,)
        )
        conn.commit()

        row = conn.execute(
            "SELECT up_count, down_count FROM market_polls WHERE date = ?", (today,)
        ).fetchone()
        up = int(row["up_count"])
        down = int(row["down_count"])
        return {
            "date": today,
            "question": _POLL_QUESTION,
            "up_label": _POLL_UP_LABEL,
            "up_sub": _POLL_UP_SUB,
            "down_label": _POLL_DOWN_LABEL,
            "down_sub": _POLL_DOWN_SUB,
            "up_count": up,
            "down_count": down,
            "total": up + down,
        }
    finally:
        conn.close()


# ─── 커뮤니티 게시글 ─────────────────────────────────────────────
class PostBody(BaseModel):
    content: str
    author: Optional[str] = None


class CommentBody(BaseModel):
    content: str
    author: Optional[str] = None
    created_at: Optional[str] = None  # 봇 전용: 임의 타임스탬프 지정


@router.get("/api/community/posts")
def list_posts(limit: int = 20, offset: int = 0, sort: str = "latest"):
    conn = _get_conn()
    try:
        order = "p.likes DESC, p.id DESC" if sort == "hot" else "p.id DESC"
        rows = conn.execute(
            f"""
            SELECT p.id, p.content, p.author, p.created_at, p.likes,
                   (SELECT COUNT(*) FROM community_comments c WHERE c.post_id = p.id) AS comment_count
            FROM community_posts p
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            (min(limit, 50), max(offset, 0)),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM community_posts").fetchone()[0]
        return {
            "posts": [dict(r) for r in rows],
            "total": int(total),
            "has_more": offset + limit < int(total),
        }
    finally:
        conn.close()


@router.post("/api/community/posts")
def create_post(body: PostBody):
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(400, "내용을 입력해주세요")
    if len(content) > 500:
        raise HTTPException(400, "500자 이내로 작성해주세요")

    author = (body.author or "").strip() or _random_nickname()
    now = datetime.now().isoformat(timespec="seconds")

    conn = _get_conn()
    try:
        # 30초 내 동일 작성자 + 동일 내용 중복 방지
        cutoff = (datetime.now() - timedelta(seconds=30)).isoformat(timespec="seconds")
        dup = conn.execute(
            "SELECT id FROM community_posts WHERE author = ? AND content = ? AND created_at > ?",
            (author, content, cutoff),
        ).fetchone()
        if dup:
            raise HTTPException(409, "이미 등록된 글이에요")

        id_row = conn.execute(
            "INSERT INTO community_posts (content, author, created_at) VALUES (?, ?, ?) RETURNING id",
            (content, author, now),
        ).fetchone()
        conn.commit()
        if not id_row:
            raise HTTPException(500, "글 등록에 실패했어요")
        row = conn.execute(
            "SELECT *, 0 AS comment_count FROM community_posts WHERE id = ?",
            (id_row[0],),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


@router.post("/api/community/posts/{post_id}/delete")
def delete_post(post_id: int, body: PostBody):
    """닉네임 일치 확인 후 삭제 (서버 인증 없는 구조상 닉네임으로 본인 확인)"""
    author = (body.author or "").strip()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, author FROM community_posts WHERE id = ?", (post_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "게시글을 찾을 수 없어요")
        if author and row["author"] != author:
            raise HTTPException(403, "본인 글만 삭제할 수 있어요")
        conn.execute("DELETE FROM community_posts WHERE id = ?", (post_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.post("/api/community/posts/{post_id}/like")
def like_post(post_id: int):
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE community_posts SET likes = likes + 1 WHERE id = ?", (post_id,)
        )
        conn.commit()
        row = conn.execute(
            "SELECT likes FROM community_posts WHERE id = ?", (post_id,)
        ).fetchone()
        return {"likes": int(row["likes"]) if row else 0}
    finally:
        conn.close()


# ─── 댓글 ───────────────────────────────────────────────────────
@router.get("/api/community/posts/{post_id}/comments")
def list_comments(post_id: int):
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM community_comments WHERE post_id = ? ORDER BY id ASC",
            (post_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/api/community/posts/{post_id}/comments")
def create_comment(post_id: int, body: CommentBody):
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(400, "댓글 내용을 입력해주세요")
    if len(content) > 200:
        raise HTTPException(400, "200자 이내로 작성해주세요")

    author = (body.author or "").strip() or _random_nickname()
    now = (body.created_at or datetime.now().isoformat(timespec="seconds"))

    conn = _get_conn()
    try:
        if not conn.execute(
            "SELECT id FROM community_posts WHERE id = ?", (post_id,)
        ).fetchone():
            raise HTTPException(404, "게시글을 찾을 수 없어요")

        # 30초 내 동일 댓글 중복 방지 (봇 타임스탬프 사용 시 스킵)
        if not body.created_at:
            cutoff_c = (datetime.now() - timedelta(seconds=30)).isoformat(timespec="seconds")
            dup = conn.execute(
                "SELECT id FROM community_comments WHERE post_id = ? AND author = ? AND content = ? AND created_at > ?",
                (post_id, author, content, cutoff_c),
            ).fetchone()
            if dup:
                raise HTTPException(409, "이미 등록된 댓글이에요")

        id_row = conn.execute(
            "INSERT INTO community_comments (post_id, content, author, created_at) VALUES (?, ?, ?, ?) RETURNING id",
            (post_id, content, author, now),
        ).fetchone()
        conn.commit()
        if not id_row:
            raise HTTPException(500, "댓글 등록에 실패했어요")
        row = conn.execute(
            "SELECT * FROM community_comments WHERE id = ?", (id_row[0],)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


@router.post("/api/community/comments/{comment_id}/like")
def like_comment(comment_id: int):
    conn = _get_conn()
    try:
        # likes 컬럼 없으면 무시 (스키마 마이그레이션 전)
        try:
            conn.execute(
                "UPDATE community_comments SET likes = likes + 1 WHERE id = ?", (comment_id,)
            )
            conn.commit()
        except Exception:
            pass
        row = conn.execute(
            "SELECT * FROM community_comments WHERE id = ?", (comment_id,)
        ).fetchone()
        likes = int(row["likes"]) if row and "likes" in row.keys() else 0
        return {"likes": likes}
    finally:
        conn.close()
