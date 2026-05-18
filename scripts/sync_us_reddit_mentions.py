"""Reddit ticker mention snapshot DB sync.

가변 주기 (peak: KST 22:30~05:00 → 15분, off: 1시간) cron 에서 호출.
30일 retention 자동 cleanup.

저장 모델:
  us_reddit_mentions_snapshot: snapshot 단위 (snapshot_at, subreddit, symbol) PK
  서브별 + 통합("__all__") row 둘 다 기록

Usage:
  python scripts/sync_us_reddit_mentions.py            # 시간대 검사 후 진짜 실행
  python scripts/sync_us_reddit_mentions.py --force    # 시간대 무시 강제 실행
  python scripts/sync_us_reddit_mentions.py --retention 30  # 30일 이전 삭제
  python scripts/sync_us_reddit_mentions.py --dry-run  # fetch 만 + DB 미기록
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Windows console UTF-8
try:
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from collectors.us_reddit_mentions import (  # noqa: E402
    DEFAULT_SUBS,
    get_mentions_by_sub,
    get_mentions_aggregated,
)
from server.db.connections import get_stocks_conn  # noqa: E402

logger = logging.getLogger("scripts.sync_us_reddit_mentions")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

KST_OFFSET = timedelta(hours=9)
AGG_SUB_TOKEN = "__all__"  # 통합 row 의 subreddit 컬럼 값


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS us_reddit_mentions_snapshot (
            snapshot_at TIMESTAMP NOT NULL,
            subreddit TEXT NOT NULL,
            symbol TEXT NOT NULL,
            rank INT,
            mention_count INT,
            explicit_count INT,
            score_sum BIGINT,
            comment_sum INT,
            avg_upvote_ratio NUMERIC(5,3),
            sentiment_score INT,
            bull_n INT,
            bear_n INT,
            kw_matched_posts INT,
            comment_bull_n INT,
            comment_bear_n INT,
            comments_analyzed INT,
            pool_size INT,
            by_sub_json TEXT,
            top_post_id TEXT,
            top_post_title TEXT,
            top_post_url TEXT,
            top_post_score INT,
            fetched_at TIMESTAMP,
            PRIMARY KEY (snapshot_at, subreddit, symbol)
        )
        """
    )
    # 기존 테이블에 신규 컬럼 추가 (이미 있으면 silent skip)
    for col in (
        "comment_bull_n INT",
        "comment_bear_n INT",
        "comments_analyzed INT",
    ):
        try:
            conn.execute(f"ALTER TABLE us_reddit_mentions_snapshot ADD COLUMN IF NOT EXISTS {col}")
        except Exception:
            pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reddit_snap_symbol_time "
            "ON us_reddit_mentions_snapshot(symbol, snapshot_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reddit_snap_sub_time "
            "ON us_reddit_mentions_snapshot(subreddit, snapshot_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reddit_snap_time "
            "ON us_reddit_mentions_snapshot(snapshot_at DESC)"
        )
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def _should_run_now() -> tuple[bool, str]:
    """KST 시간 기반 가변 주기 게이트.

    peak: KST 22:30 ~ 05:00 → 15분마다 (스크립트는 항상 통과)
    off : 그 외 → 매 정시 ±5분만 실행

    cron 자체는 15분 간격으로 무조건 부르고, 이 함수가 off-peak 시 정시가 아니면 skip.
    """
    now_kst = datetime.now(timezone.utc) + KST_OFFSET
    h, m = now_kst.hour, now_kst.minute
    # peak: 22:30 ~ 24:00 또는 00:00 ~ 05:00
    is_peak = ((h == 22 and m >= 30) or (h >= 23) or (h < 5) or (h == 5 and m == 0))
    if is_peak:
        return True, f"peak {h:02d}:{m:02d} KST"
    # off-peak: 매 정시만 (cron이 :00, :15, :30, :45 호출이라고 가정 → :00 만 통과)
    if m < 5:
        return True, f"off-peak hourly {h:02d}:{m:02d} KST"
    return False, f"off-peak skip {h:02d}:{m:02d} KST"


def _upsert_row(conn, snapshot_at: datetime, sub: str, row: dict, pool_size: int) -> None:
    import json
    top = row.get("top_post") or {}
    by_sub = row.get("by_sub") or {}
    components = row.get("sentiment_components") or {}
    conn.execute(
        """
        INSERT INTO us_reddit_mentions_snapshot
            (snapshot_at, subreddit, symbol, rank, mention_count, explicit_count,
             score_sum, comment_sum, avg_upvote_ratio, sentiment_score,
             bull_n, bear_n, kw_matched_posts,
             comment_bull_n, comment_bear_n, comments_analyzed,
             pool_size,
             by_sub_json, top_post_id, top_post_title, top_post_url, top_post_score,
             fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (snapshot_at, subreddit, symbol) DO UPDATE SET
            rank = excluded.rank,
            mention_count = excluded.mention_count,
            explicit_count = excluded.explicit_count,
            score_sum = excluded.score_sum,
            comment_sum = excluded.comment_sum,
            avg_upvote_ratio = excluded.avg_upvote_ratio,
            sentiment_score = excluded.sentiment_score,
            bull_n = excluded.bull_n,
            bear_n = excluded.bear_n,
            kw_matched_posts = excluded.kw_matched_posts,
            comment_bull_n = excluded.comment_bull_n,
            comment_bear_n = excluded.comment_bear_n,
            comments_analyzed = excluded.comments_analyzed,
            pool_size = excluded.pool_size,
            by_sub_json = excluded.by_sub_json,
            top_post_id = excluded.top_post_id,
            top_post_title = excluded.top_post_title,
            top_post_url = excluded.top_post_url,
            top_post_score = excluded.top_post_score,
            fetched_at = excluded.fetched_at
        """,
        (
            snapshot_at,
            sub,
            row["symbol"],
            row.get("rank"),
            row.get("mention_count"),
            row.get("explicit_count"),
            row.get("score_sum"),
            row.get("comment_sum"),
            float(row.get("avg_upvote_ratio") or 0.5),
            row.get("sentiment_score"),
            int(components.get("bull_n") or 0),
            int(components.get("bear_n") or 0),
            int(components.get("kw_matched_posts") or 0),
            int(components.get("comment_bull_n") or 0),
            int(components.get("comment_bear_n") or 0),
            int(components.get("comments_analyzed") or 0),
            pool_size,
            json.dumps(by_sub) if by_sub else None,
            top.get("id"),
            (top.get("title") or "")[:200] if top else None,
            top.get("url"),
            int(top.get("score") or 0) if top else None,
            datetime.now(timezone.utc).replace(tzinfo=None),
        ),
    )


def sync_snapshot(
    now: datetime | None = None,
    dry_run: bool = False,
    comments_top_n: int = 1,   # sub 당 score 상위 N글의 댓글까지 sentiment 분석
) -> tuple[int, dict]:
    """현재 시점 snapshot — 모든 sub 각각 + 통합 row upsert.

    comments_top_n=1 일 때 sub 당 1 글 댓글 fetch → 10 sub × 1 = 10 추가 req/cycle.
    feed: hot + rising (2개), 10 sub × 2 = 20 req. 총 ~30 req, sleep 분산하면 3-5분.

    Returns: (upserted_count, summary_dict)
    """
    now = (now or datetime.now(timezone.utc)).replace(tzinfo=None, microsecond=0)
    summary: dict = {}

    conn = get_stocks_conn() if not dry_run else None
    if conn is not None:
        _ensure_table(conn)

    upserted = 0

    # 1) 서브별 snapshot
    for sub in DEFAULT_SUBS:
        try:
            res = get_mentions_by_sub(
                sub, top_n=50, min_mentions=1,
                fetch_comments_top_n=comments_top_n,
            )
        except Exception as exc:
            logger.warning("sub %s fetch failed: %s", sub, exc)
            summary[sub] = {"status": "error", "error": str(exc)}
            continue
        rows = res.get("data", [])
        pool_size = res.get("post_pool_size", 0)
        summary[sub] = {"rows": len(rows), "pool": pool_size}
        if dry_run or conn is None:
            continue
        for row in rows:
            try:
                _upsert_row(conn, now, sub, row, pool_size)
                upserted += 1
            except Exception as exc:
                logger.warning("upsert %s/%s failed: %s", sub, row.get("symbol"), exc)

    # 2) 통합 snapshot (sub = "__all__") — 댓글 재분석 비용 절감 위해 분석 X
    # (서브별 row 의 sentiment 가 이미 댓글 반영함. 통합은 mention 만 합산)
    try:
        agg = get_mentions_aggregated(top_n=50, min_mentions=2, fetch_comments_top_n=0)
    except Exception as exc:
        logger.warning("aggregated fetch failed: %s", exc)
        summary[AGG_SUB_TOKEN] = {"status": "error", "error": str(exc)}
    else:
        agg_rows = agg.get("data", [])
        pool = agg.get("post_pool_size", 0)
        summary[AGG_SUB_TOKEN] = {"rows": len(agg_rows), "pool": pool}
        if not dry_run and conn is not None:
            for row in agg_rows:
                try:
                    _upsert_row(conn, now, AGG_SUB_TOKEN, row, pool)
                    upserted += 1
                except Exception as exc:
                    logger.warning("agg upsert %s failed: %s", row.get("symbol"), exc)

    if conn is not None:
        try:
            conn.commit()
        finally:
            conn.close()

    return upserted, summary


def cleanup_old(retention_days: int = 30) -> int:
    """retention_days 이전 snapshot 삭제."""
    conn = get_stocks_conn()
    try:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=retention_days)
        cur = conn.execute(
            "DELETE FROM us_reddit_mentions_snapshot WHERE snapshot_at < %s",
            (cutoff,),
        )
        try:
            removed = cur.rowcount if cur and hasattr(cur, "rowcount") else 0
        except Exception:
            removed = 0
        conn.commit()
        return removed
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="시간대 게이트 무시 강제 실행")
    ap.add_argument("--dry-run", action="store_true", help="fetch 만, DB 미기록")
    ap.add_argument("--retention", type=int, default=30, help="N일 이전 삭제 (default 30)")
    ap.add_argument("--cleanup-only", action="store_true", help="cleanup 만 실행")
    args = ap.parse_args()

    if args.cleanup_only:
        removed = cleanup_old(args.retention)
        print(f"[cleanup] removed {removed} rows older than {args.retention}d")
        return 0

    if not args.force:
        ok, reason = _should_run_now()
        if not ok:
            print(f"[skip] {reason}")
            return 0
        print(f"[run] {reason}")

    started = datetime.now(timezone.utc)
    try:
        upserted, summary = sync_snapshot(dry_run=args.dry_run)
    except Exception as exc:
        logger.error("snapshot failed: %s", exc)
        return 1

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    summary_str = " | ".join(
        f"{k}:{v['rows']}@{v.get('pool', 0)}" if "rows" in v else f"{k}:ERR"
        for k, v in summary.items()
    )
    print(f"[snapshot] upserted={upserted} elapsed={elapsed:.1f}s  {summary_str}")

    # cleanup
    if not args.dry_run:
        try:
            removed = cleanup_old(args.retention)
            if removed > 0:
                print(f"[cleanup] removed {removed} rows older than {args.retention}d")
        except Exception as exc:
            logger.warning("cleanup failed: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
