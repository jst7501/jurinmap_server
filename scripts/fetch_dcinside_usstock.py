import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scrapers.dcinside_gallery_scraper import DCInsideGalleryScraper


def save_json(payload: dict, output_dir: Path, gallery_id: str) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    latest_path = output_dir / f"dcinside_{gallery_id}_latest.json"
    stamped_path = output_dir / f"dcinside_{gallery_id}_{stamp}.json"

    for path in (latest_path, stamped_path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    return {"latest": latest_path, "stamped": stamped_path}


def load_proxies(proxy_file: str) -> list[str]:
    if not proxy_file:
        return []
    path = Path(proxy_file)
    if not path.exists():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        lines.append(text)
    return lines


def build_progress_cb():
    state = {"last_done": -1}

    def _cb(payload: dict):
        event = payload.get("event", "")
        if event == "crawl_start":
            print(
                f"[START] gallery={payload.get('gallery_id')} pages={payload.get('pages')} "
                f"max_posts={payload.get('max_posts')} content={payload.get('with_content')} "
                f"comments={payload.get('with_comments')} proxy_pool={payload.get('proxy_pool')}",
                flush=True,
            )
            return

        if event == "list_page_start":
            print(
                f"[LIST] {payload.get('page')}/{payload.get('pages')} start "
                f"(collected={payload.get('posts_collected')})",
                flush=True,
            )
            return

        if event == "list_page_done":
            print(
                f"[LIST] page={payload.get('page')} added={payload.get('added')} "
                f"total={payload.get('posts_collected')}",
                flush=True,
            )
            return

        if event == "list_page_mobile_fallback":
            print(f"[FALLBACK] mobile list page={payload.get('page')}", flush=True)
            return

        if event == "proxy_rotated":
            print(
                f"[PROXY] rotate={payload.get('proxy_rotate_count')} current={payload.get('current_proxy')}",
                flush=True,
            )
            return

        if event == "detail_progress":
            done = int(payload.get("done") or 0)
            total = int(payload.get("total") or 0)
            if done == state["last_done"]:
                return
            state["last_done"] = done
            ratio = (done / total * 100.0) if total > 0 else 0.0
            sys.stdout.write(
                "\r"
                + f"[DETAIL] {done}/{total} ({ratio:.1f}%) "
                + f"post={payload.get('post_no')} "
                + f"comments={payload.get('comment_count_crawled', 0)}"
                + " " * 12
            )
            sys.stdout.flush()
            if done >= total:
                sys.stdout.write("\n")
                sys.stdout.flush()
            return

        if event == "retry_progress":
            print(
                f"[RETRY] post={payload.get('post_no')} remaining={payload.get('remaining')} "
                f"proxy={payload.get('current_proxy', '')}",
                flush=True,
            )

    return _cb


def main():
    parser = argparse.ArgumentParser(description="Fetch DCInside gallery posts and save as JSON")
    parser.add_argument("--gallery-id", default="usstock", help="DCInside gallery id (default: usstock)")
    parser.add_argument("--pages", type=int, default=20, help="list pages to crawl")
    parser.add_argument("--max-posts", type=int, default=0, help="max posts limit (0 = no limit)")
    parser.add_argument("--with-content", action="store_true", help="fetch each post detail content")
    parser.add_argument("--with-comments", action="store_true", help="fetch comments as much as possible")
    parser.add_argument(
        "--max-comments-per-post",
        type=int,
        default=200,
        help="max comments per post (default: 200)",
    )
    parser.add_argument(
        "--max-comment-pages",
        type=int,
        default=10,
        help="max comment pages to traverse per post (default: 10)",
    )
    parser.add_argument("--sleep", type=float, default=0.15, help="sleep seconds between requests")
    parser.add_argument("--no-delay", action="store_true", help="set sleep to 0 (no delay)")
    parser.add_argument("--no-progress", action="store_true", help="disable realtime progress output")
    parser.add_argument(
        "--output-dir",
        default=str(ROOT_DIR / "data"),
        help="output directory path",
    )
    parser.add_argument(
        "--proxy-file",
        default="",
        help="proxy list file (host:port per line)",
    )
    args = parser.parse_args()
    if args.no_delay:
        args.sleep = 0.0

    proxies = load_proxies(args.proxy_file)
    progress_cb = None if args.no_progress else build_progress_cb()
    scraper = DCInsideGalleryScraper(sleep_sec=args.sleep, proxies=proxies, progress_cb=progress_cb)
    payload = scraper.crawl(
        gallery_id=args.gallery_id,
        pages=args.pages,
        with_content=args.with_content,
        with_comments=args.with_comments,
        max_posts=args.max_posts,
        max_comments_per_post=args.max_comments_per_post,
        max_comment_pages=args.max_comment_pages,
    )

    saved = save_json(payload, Path(args.output_dir), args.gallery_id)
    if proxies:
        print(f"[INFO] proxy_pool={len(proxies)}")
    print(f"[DONE] gallery={args.gallery_id}, posts={payload.get('total_posts', 0)}")
    if args.with_comments:
        print(f"[INFO] comments={payload.get('total_comments_crawled', 0)}")
    print(f"[SAVE] latest : {saved['latest']}")
    print(f"[SAVE] stamped: {saved['stamped']}")


if __name__ == "__main__":
    main()
