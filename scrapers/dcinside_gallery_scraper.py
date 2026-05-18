import re
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


class DCInsideGalleryScraper:
    """
    DCInside gallery list/detail scraper.
    Supports both major gallery and minor gallery list URLs.
    """

    BASE_URL = "https://gall.dcinside.com"
    MOBILE_BASE_URL = "https://m.dcinside.com"
    COMMENT_API_URL = "https://gall.dcinside.com/board/comment/"

    def __init__(
        self,
        timeout: int = 12,
        sleep_sec: float = 0.15,
        proxies: Optional[List[str]] = None,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.timeout = timeout
        self.sleep_sec = sleep_sec
        self.proxies = [self._clean_text(x) for x in (proxies or []) if self._clean_text(x)]
        self.proxy_index = 0
        self.current_proxy = self.proxies[0] if self.proxies else ""
        self.progress_cb = progress_cb
        self.proxy_rotate_count = 0
        self.session = self._create_session()

    def _create_session(self) -> requests.Session:
        self.session = requests.Session()
        # Do not inherit broken proxy env; crawl directly.
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
                "Referer": self.BASE_URL,
            }
        )
        if self.current_proxy:
            proxy_url = f"http://{self.current_proxy}"
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})
        return self.session

    def _rotate_proxy(self):
        if not self.proxies:
            return
        self.proxy_index = (self.proxy_index + 1) % len(self.proxies)
        self.current_proxy = self.proxies[self.proxy_index]
        self.proxy_rotate_count += 1
        self._emit_progress(
            {
                "event": "proxy_rotated",
                "proxy_rotate_count": self.proxy_rotate_count,
                "current_proxy": self.current_proxy,
            }
        )

    def _emit_progress(self, payload: Dict[str, Any]):
        if not self.progress_cb:
            return
        try:
            self.progress_cb(payload)
        except Exception:
            pass

    @staticmethod
    def _to_int(value: str) -> int:
        if value is None:
            return 0
        text = str(value).strip()
        if not text:
            return 0
        text = text.replace(",", "")
        m = re.search(r"-?\d+", text)
        return int(m.group(0)) if m else 0

    @staticmethod
    def _clean_text(value: str) -> str:
        if value is None:
            return ""
        return " ".join(str(value).replace("\xa0", " ").strip().split())

    @staticmethod
    def _extract_comment_count(title_text: str) -> int:
        # Ex) "제목 [12]"
        if not title_text:
            return 0
        m = re.search(r"\[(\d+)\]\s*$", title_text.strip())
        return int(m.group(1)) if m else 0

    def _list_url_candidates(self, gallery_id: str, page: int) -> List[str]:
        return [
            f"{self.BASE_URL}/board/lists?id={gallery_id}&page={page}",
            f"{self.BASE_URL}/mgallery/board/lists?id={gallery_id}&page={page}",
        ]

    def _mobile_list_url(self, gallery_id: str, page: int) -> str:
        return f"{self.MOBILE_BASE_URL}/board/{gallery_id}?page={page}"

    def _mobile_view_url(self, gallery_id: str, post_no: int, page: int = 1) -> str:
        return f"{self.MOBILE_BASE_URL}/board/{gallery_id}/{post_no}?page={page}"

    @staticmethod
    def _extract_script_redirect_url(html: str) -> str:
        if not html:
            return ""
        m = re.search(r'location\.replace\(["\']([^"\']+)["\']\)', html)
        if not m:
            return ""
        return m.group(1).strip()

    @staticmethod
    def _with_page_param(url: str, page: int) -> str:
        parsed = urlparse(url)
        query_pairs = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if query_pairs.get("page") == str(page):
            return url
        query_pairs["page"] = str(page)
        new_query = urlencode(query_pairs)
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                new_query,
                parsed.fragment,
            )
        )

    @staticmethod
    def _extract_gallery_and_no(url: str) -> Tuple[str, int]:
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        gallery_id = str(query.get("id", "")).strip()
        post_no = 0
        no_text = str(query.get("no", "")).strip()
        if no_text.isdigit():
            post_no = int(no_text)
        return gallery_id, post_no

    @staticmethod
    def _extract_esno(html: str) -> str:
        if not html:
            return ""
        patterns = [
            r'name=["\']e_s_n_o["\']\s+value=["\']([^"\']+)["\']',
            r'e_s_n_o["\']?\s*[:=]\s*["\']([^"\']+)["\']',
            r'_esno["\']?\s*[:=]\s*["\']([^"\']+)["\']',
        ]
        for pattern in patterns:
            m = re.search(pattern, html, flags=re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return ""

    def _request_html(self, url: str, referer: str = "", retries: int = 3) -> str:
        for attempt in range(1, retries + 1):
            try:
                headers = {"Referer": referer} if referer else None
                if "m.dcinside.com" in url:
                    mobile_headers = {
                        "User-Agent": (
                            "Mozilla/5.0 (Linux; Android 13; SM-S918N) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Mobile Safari/537.36"
                        )
                    }
                    if headers:
                        mobile_headers.update(headers)
                    headers = mobile_headers
                response = self.session.get(url, timeout=self.timeout, headers=headers)
                if response.status_code == 200 and response.text:
                    return response.text
            except Exception:
                pass
            if attempt < retries:
                # Refresh session when DC returns intermittent empty-body responses.
                self._rotate_proxy()
                self.session = self._create_session()
                if self.sleep_sec > 0:
                    time.sleep(self.sleep_sec * attempt)
        return ""

    def _parse_comments_from_html(self, html: str) -> List[Dict]:
        soup = BeautifulSoup(html, "lxml")
        candidates = (
            soup.select("#comment_box > li.comment")
            or soup.select("#comment_box li")
            or soup.select("ul.cmt_list li")
            or soup.select("div.comment_box li")
            or soup.select("div.view_comment li")
            or soup.select("div.comment_wrap li")
        )
        comments: List[Dict] = []
        seen = set()
        for li in candidates:
            classes = li.get("class") or []
            if any(name in classes for name in ("more", "reply_line", "del", "disabled")):
                continue

            no_text = (
                li.get("no")
                or li.get("data-no")
                or li.get("data-cmt-no")
                or li.get("data-comment-no")
                or ""
            )
            comment_no = self._to_int(no_text)
            author_node = (
                li.select_one("button.nick")
                or li.select_one(".nickname")
                or li.select_one(".name")
                or li.select_one(".gall_writer")
                or li.select_one(".nick")
            )
            text_node = (
                li.select_one(".usertxt")
                or li.select_one(".txt")
                or li.select_one(".comment_txt")
                or li.select_one(".cmt_txt")
                or li.select_one("p")
            )
            date_node = (
                li.select_one(".date_time")
                or li.select_one(".date")
                or li.select_one("span.fr")
                or li.select_one(".regdate")
            )
            rec_node = (
                li.select_one(".cmt_recommend")
                or li.select_one(".rec")
                or li.select_one(".num")
            )

            author = self._clean_text(author_node.get_text(" ", strip=True) if author_node else "")
            text = self._clean_text(text_node.get_text("\n", strip=True) if text_node else "")
            date_text = self._clean_text(date_node.get_text(" ", strip=True) if date_node else "")
            recommend = self._to_int(rec_node.get_text(" ", strip=True) if rec_node else "")

            if not text:
                continue
            key = (comment_no, author, text[:80], date_text)
            if key in seen:
                continue
            seen.add(key)
            comments.append(
                {
                    "comment_no": comment_no,
                    "author": author,
                    "content": text,
                    "date": date_text,
                    "recommend": recommend,
                }
            )
        return comments

    def _parse_comments_from_json(self, payload: Dict[str, Any]) -> List[Dict]:
        candidates = []
        for key in ("comments", "comment_list", "list", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
        comments: List[Dict] = []
        seen = set()
        for item in candidates:
            if not isinstance(item, dict):
                continue
            content = self._clean_text(
                item.get("memo")
                or item.get("comment_memo")
                or item.get("content")
                or item.get("comment")
                or ""
            )
            if not content:
                continue
            comment_no = self._to_int(
                item.get("no")
                or item.get("comment_no")
                or item.get("cmt_no")
                or item.get("num")
                or ""
            )
            author = self._clean_text(
                item.get("name")
                or item.get("nick")
                or item.get("nickname")
                or item.get("user_nick")
                or ""
            )
            date_text = self._clean_text(
                item.get("reg_date")
                or item.get("date")
                or item.get("write_date")
                or ""
            )
            recommend = self._to_int(item.get("recommend") or item.get("rcnt") or item.get("up") or "")
            key = (comment_no, author, content[:80], date_text)
            if key in seen:
                continue
            seen.add(key)
            comments.append(
                {
                    "comment_no": comment_no,
                    "author": author,
                    "content": content,
                    "date": date_text,
                    "recommend": recommend,
                }
            )
        return comments

    def fetch_list_html(self, gallery_id: str, page: int) -> str:
        last_error = None
        for _ in range(3):
            for url in self._list_url_candidates(gallery_id, page):
                try:
                    html = self._request_html(url=url, referer=self.BASE_URL, retries=2)
                    if not html:
                        continue

                    redirect_url = self._extract_script_redirect_url(html)
                    if redirect_url:
                        redirect_url = self._with_page_param(redirect_url, page)
                        redirected_html = self._request_html(
                            url=redirect_url,
                            referer=url,
                            retries=2,
                        )
                        if redirected_html:
                            html = redirected_html

                    # Skip tiny script-only response if parsing is impossible.
                    if len(html) < 300 and "location.replace" in html:
                        continue
                    return html
                except Exception as e:
                    last_error = e
            self.session = self._create_session()
            if self.sleep_sec > 0:
                time.sleep(self.sleep_sec * 2)
        if last_error:
            raise last_error
        raise RuntimeError(f"Failed to fetch list page: gallery_id={gallery_id}, page={page}")

    def fetch_mobile_list_html(self, gallery_id: str, page: int) -> str:
        url = self._mobile_list_url(gallery_id=gallery_id, page=page)
        html = self._request_html(url=url, referer=self.MOBILE_BASE_URL, retries=3)
        if not html:
            raise RuntimeError(f"Failed to fetch mobile list page: gallery_id={gallery_id}, page={page}")
        return html

    def parse_list_html(self, html: str, gallery_id: str) -> List[Dict]:
        soup = BeautifulSoup(html, "lxml")
        rows = soup.select("tr.ub-content")

        items: List[Dict] = []
        for row in rows:
            classes = row.get("class") or []
            if "notice" in classes:
                continue

            no_cell = row.select_one("td.gall_num")
            no_text = self._clean_text(no_cell.get_text(" ", strip=True) if no_cell else "")
            if not no_text or no_text in ("공지", "AD"):
                continue
            post_no = self._to_int(no_text)
            if post_no <= 0:
                continue

            title_cell = row.select_one("td.gall_tit")
            if not title_cell:
                continue

            anchor = title_cell.select_one("a")
            if not anchor:
                continue
            raw_title = self._clean_text(anchor.get_text(" ", strip=True))
            if not raw_title:
                continue
            comment_count = self._extract_comment_count(raw_title)
            title = re.sub(r"\s*\[\d+\]\s*$", "", raw_title).strip()

            href = anchor.get("href") or ""
            url = urljoin(self.BASE_URL, href)
            if "/board/view/" not in url and "/mgallery/board/view/" not in url:
                url = f"{self.BASE_URL}/board/view/?id={gallery_id}&no={post_no}"

            writer_cell = row.select_one("td.gall_writer")
            date_cell = row.select_one("td.gall_date")
            view_cell = row.select_one("td.gall_count")
            rec_cell = row.select_one("td.gall_recommend")

            writer = ""
            if writer_cell:
                writer = (
                    writer_cell.get("data-nick")
                    or writer_cell.get("data-name")
                    or self._clean_text(writer_cell.get_text(" ", strip=True))
                )

            date_text = ""
            if date_cell:
                date_text = date_cell.get("title") or self._clean_text(date_cell.get_text(" ", strip=True))

            item = {
                "gallery_id": gallery_id,
                "post_no": post_no,
                "title": title,
                "comment_count": comment_count,
                "url": url,
                "author": self._clean_text(writer),
                "date": self._clean_text(date_text),
                "views": self._to_int(view_cell.get_text(" ", strip=True) if view_cell else ""),
                "recommend": self._to_int(rec_cell.get_text(" ", strip=True) if rec_cell else ""),
            }
            items.append(item)

        return items

    def parse_mobile_list_html(self, html: str, gallery_id: str) -> List[Dict]:
        soup = BeautifulSoup(html, "lxml")
        rows = soup.select("ul.gall-detail-lst > li")
        items: List[Dict] = []
        for row in rows:
            link = row.select_one("a.lt")
            if not link:
                continue

            href = link.get("href") or ""
            view_url = urljoin(self.MOBILE_BASE_URL, href)
            parsed = urlparse(view_url)
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) < 3 or parts[0] != "board":
                continue

            g_id = parts[1].strip() or gallery_id
            post_no = self._to_int(parts[2])
            if post_no <= 0:
                continue

            title_node = link.select_one("span.subjectin")
            title = self._clean_text(title_node.get_text(" ", strip=True) if title_node else "")
            if not title:
                title = self._clean_text(link.get_text(" ", strip=True))

            ginfo = link.select("ul.ginfo > li")
            author = self._clean_text(ginfo[1].get_text(" ", strip=True) if len(ginfo) > 1 else "")
            date_text = self._clean_text(ginfo[2].get_text(" ", strip=True) if len(ginfo) > 2 else "")
            views = self._to_int(ginfo[3].get_text(" ", strip=True) if len(ginfo) > 3 else "")
            recommend = self._to_int(ginfo[4].get_text(" ", strip=True) if len(ginfo) > 4 else "")

            cmt_node = row.select_one("a.rt span.ct")
            comment_count = self._to_int(cmt_node.get_text(" ", strip=True) if cmt_node else "")
            desktop_url = f"{self.BASE_URL}/mgallery/board/view/?id={g_id}&no={post_no}&page=1"

            items.append(
                {
                    "gallery_id": g_id,
                    "post_no": post_no,
                    "title": title,
                    "comment_count": comment_count,
                    "url": desktop_url,
                    "mobile_url": view_url,
                    "author": author,
                    "date": date_text,
                    "views": views,
                    "recommend": recommend,
                }
            )
        return items

    def fetch_post_detail(self, url: str) -> Dict[str, Any]:
        html = self._request_html(url=url, referer=self.BASE_URL, retries=3)
        g_id, p_no = self._extract_gallery_and_no(url)
        if not html and g_id and p_no > 0:
            mobile_url = self._mobile_view_url(gallery_id=g_id, post_no=p_no, page=1)
            html = self._request_html(url=mobile_url, referer=self.MOBILE_BASE_URL, retries=3)
        if not html:
            return {"content": "", "comments": [], "raw_html": "", "e_s_n_o": ""}

        soup = BeautifulSoup(html, "lxml")
        content_node = (
            soup.select_one("div.writing_view_box div.write_div")
            or soup.select_one("div.write_div")
            or soup.select_one("div.gallview_contents")
            or soup.select_one("div.us-post")
            or soup.select_one("div.thum-txt")
            or soup.select_one("article div.user_content")
        )

        content = ""
        if content_node:
            for tag in content_node(["script", "style"]):
                tag.decompose()
            content = self._clean_text(content_node.get_text("\n", strip=True))

        comments_inline = self._parse_comments_from_html(html)
        e_s_n_o = self._extract_esno(html)

        # Desktop view can return empty/limited comment section.
        # If content/comments are missing, try mobile view once more.
        if g_id and p_no > 0 and (not content.strip() or len(comments_inline) == 0):
            mobile_url = self._mobile_view_url(gallery_id=g_id, post_no=p_no, page=1)
            mobile_html = self._request_html(url=mobile_url, referer=self.MOBILE_BASE_URL, retries=2)
            if mobile_html:
                mobile_soup = BeautifulSoup(mobile_html, "lxml")
                if not content.strip():
                    mobile_content_node = (
                        mobile_soup.select_one("div.thum-txt")
                        or mobile_soup.select_one("article div.user_content")
                        or mobile_soup.select_one("div.write_div")
                    )
                    if mobile_content_node:
                        for tag in mobile_content_node(["script", "style"]):
                            tag.decompose()
                        mobile_content = self._clean_text(mobile_content_node.get_text("\n", strip=True))
                        if mobile_content:
                            content = mobile_content

                mobile_comments = self._parse_comments_from_html(mobile_html)
                if mobile_comments:
                    seen = set(
                        (
                            c.get("comment_no", 0),
                            c.get("author", ""),
                            c.get("content", "")[:80],
                            c.get("date", ""),
                        )
                        for c in comments_inline
                    )
                    for c in mobile_comments:
                        key = (
                            c.get("comment_no", 0),
                            c.get("author", ""),
                            c.get("content", "")[:80],
                            c.get("date", ""),
                        )
                        if key in seen:
                            continue
                        seen.add(key)
                        comments_inline.append(c)

                if not e_s_n_o:
                    e_s_n_o = self._extract_esno(mobile_html)

        return {
            "content": content,
            "comments": comments_inline,
            "raw_html": html,
            "e_s_n_o": e_s_n_o,
        }

    def fetch_comments_api(
        self,
        gallery_id: str,
        post_no: int,
        view_url: str,
        e_s_n_o: str = "",
        max_comment_pages: int = 10,
        max_comments: int = 200,
    ) -> List[Dict]:
        if not gallery_id or post_no <= 0:
            return []

        comments: List[Dict] = []
        seen = set()
        for page in range(1, max_comment_pages + 1):
            page_comments: List[Dict] = []
            got_valid_response = False
            for _ in range(3):
                payload = {
                    "id": gallery_id,
                    "no": str(post_no),
                    "comment_page": str(page),
                    "sort": "D",
                }
                if e_s_n_o:
                    payload["e_s_n_o"] = e_s_n_o

                response_text = ""
                try:
                    response = self.session.post(
                        self.COMMENT_API_URL,
                        data=payload,
                        timeout=self.timeout,
                        headers={"Referer": view_url},
                    )
                    response_text = response.text or ""
                    if response.status_code == 200 and response_text:
                        got_valid_response = True
                        try:
                            json_payload = response.json()
                            if isinstance(json_payload, dict):
                                page_comments = self._parse_comments_from_json(json_payload)
                        except Exception:
                            page_comments = self._parse_comments_from_html(response_text)
                except Exception:
                    pass
                if page_comments:
                    break
                # Empty comment result is acceptable; do not rotate endlessly.
                if got_valid_response:
                    break
                # Treat empty comment payload as soft-failure: rotate immediately.
                self._rotate_proxy()
                self.session = self._create_session()
                if self.sleep_sec > 0:
                    time.sleep(self.sleep_sec)

            if not page_comments:
                break

            added = 0
            for item in page_comments:
                key = (
                    item.get("comment_no", 0),
                    item.get("author", ""),
                    item.get("content", "")[:80],
                    item.get("date", ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                comments.append(item)
                added += 1
                if max_comments > 0 and len(comments) >= max_comments:
                    return comments

            if added == 0:
                break

            time.sleep(self.sleep_sec)

        return comments

    def crawl(
        self,
        gallery_id: str,
        pages: int = 10,
        with_content: bool = False,
        with_comments: bool = False,
        max_posts: int = 0,
        max_comments_per_post: int = 200,
        max_comment_pages: int = 10,
    ) -> Dict:
        posts: List[Dict] = []
        seen = set()
        self._emit_progress(
            {
                "event": "crawl_start",
                "gallery_id": gallery_id,
                "pages": pages,
                "max_posts": max_posts,
                "with_content": with_content,
                "with_comments": with_comments,
                "proxy_pool": len(self.proxies),
            }
        )

        for page in range(1, pages + 1):
            self._emit_progress(
                {
                    "event": "list_page_start",
                    "gallery_id": gallery_id,
                    "page": page,
                    "pages": pages,
                    "posts_collected": len(posts),
                }
            )
            parsed: List[Dict] = []
            try:
                html = self.fetch_list_html(gallery_id=gallery_id, page=page)
                parsed = self.parse_list_html(html=html, gallery_id=gallery_id)
            except Exception:
                parsed = []
            if not parsed:
                try:
                    mobile_html = self.fetch_mobile_list_html(gallery_id=gallery_id, page=page)
                    parsed = self.parse_mobile_list_html(html=mobile_html, gallery_id=gallery_id)
                    self._emit_progress(
                        {
                            "event": "list_page_mobile_fallback",
                            "gallery_id": gallery_id,
                            "page": page,
                        }
                    )
                except Exception:
                    parsed = []
            if not parsed:
                self._emit_progress(
                    {
                        "event": "list_page_empty",
                        "gallery_id": gallery_id,
                        "page": page,
                        "posts_collected": len(posts),
                    }
                )
                break

            added = 0
            for item in parsed:
                key = item["post_no"]
                if key in seen:
                    continue
                seen.add(key)
                posts.append(item)
                added += 1
                if max_posts > 0 and len(posts) >= max_posts:
                    break
            self._emit_progress(
                {
                    "event": "list_page_done",
                    "gallery_id": gallery_id,
                    "page": page,
                    "added": added,
                    "posts_collected": len(posts),
                }
            )
            if added == 0:
                break
            if max_posts > 0 and len(posts) >= max_posts:
                break
            time.sleep(self.sleep_sec)

        if with_content or with_comments:
            detail_total = len(posts)
            for idx, post in enumerate(posts, 1):
                try:
                    detail = self.fetch_post_detail(post["url"])
                    if with_content:
                        post["content"] = detail.get("content", "")
                    if with_comments:
                        post["comments"] = detail.get("comments", [])
                        g_id, p_no = self._extract_gallery_and_no(post["url"])
                        if not g_id:
                            g_id = gallery_id
                        if p_no <= 0:
                            p_no = int(post.get("post_no") or 0)
                        if len(post["comments"]) < max_comments_per_post:
                            remain = max_comments_per_post - len(post["comments"])
                            api_comments = self.fetch_comments_api(
                                gallery_id=g_id,
                                post_no=p_no,
                                view_url=post["url"],
                                e_s_n_o=detail.get("e_s_n_o", ""),
                                max_comment_pages=max_comment_pages,
                                max_comments=remain,
                            )
                            if api_comments:
                                seen_comments = set(
                                    (
                                        c.get("comment_no", 0),
                                        c.get("author", ""),
                                        c.get("content", "")[:80],
                                        c.get("date", ""),
                                    )
                                    for c in post["comments"]
                                )
                                for c in api_comments:
                                    key = (
                                        c.get("comment_no", 0),
                                        c.get("author", ""),
                                        c.get("content", "")[:80],
                                        c.get("date", ""),
                                    )
                                    if key in seen_comments:
                                        continue
                                    seen_comments.add(key)
                                    post["comments"].append(c)
                                    if len(post["comments"]) >= max_comments_per_post:
                                        break
                        post["comment_count_crawled"] = len(post["comments"])
                except Exception:
                    if with_content:
                        post["content"] = ""
                    if with_comments:
                        post["comments"] = []
                        post["comment_count_crawled"] = 0
                self._emit_progress(
                    {
                        "event": "detail_progress",
                        "gallery_id": gallery_id,
                        "done": idx,
                        "total": detail_total,
                        "post_no": int(post.get("post_no") or 0),
                        "comment_count_crawled": int(post.get("comment_count_crawled", 0) or 0),
                        "current_proxy": self.current_proxy,
                    }
                )
                if idx % 20 == 0:
                    time.sleep(self.sleep_sec)

            # Retry missing content/comments with a fresh session.
            for _ in range(2):
                missing_idx = []
                for i, p in enumerate(posts):
                    need_content = with_content and not (p.get("content") or "").strip()
                    need_comments = False
                    if need_content or need_comments:
                        missing_idx.append(i)
                if not missing_idx:
                    break
                self.session = self._create_session()
                if self.sleep_sec > 0:
                    time.sleep(self.sleep_sec * 4)
                for pos, i in enumerate(missing_idx, 1):
                    try:
                        detail = self.fetch_post_detail(posts[i]["url"])
                        if with_content and detail.get("content", "").strip():
                            posts[i]["content"] = detail["content"]
                        if with_comments and not posts[i].get("comments"):
                            posts[i]["comments"] = detail.get("comments", [])
                            posts[i]["comment_count_crawled"] = len(posts[i]["comments"])
                    except Exception:
                        pass
                    need_content_after = with_content and not (posts[i].get("content") or "").strip()
                    need_comments_after = False
                    if need_content_after or need_comments_after:
                        self._rotate_proxy()
                        self.session = self._create_session()
                    self._emit_progress(
                        {
                            "event": "retry_progress",
                            "gallery_id": gallery_id,
                            "post_no": int(posts[i].get("post_no") or 0),
                            "remaining": max(len(missing_idx) - pos, 0),
                            "current_proxy": self.current_proxy,
                        }
                    )
                    time.sleep(self.sleep_sec)

        total_comments = 0
        if with_comments:
            total_comments = sum(int(p.get("comment_count_crawled", 0) or 0) for p in posts)

        return {
            "source": "dcinside",
            "gallery_id": gallery_id,
            "crawled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_posts": len(posts),
            "total_comments_crawled": total_comments,
            "proxy_rotate_count": self.proxy_rotate_count,
            "posts": posts,
        }
