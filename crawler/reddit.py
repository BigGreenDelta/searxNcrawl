"""Read Reddit without the official API: subreddit listings and full comment threads.

Reddit's own JSON endpoints answer 403 to logged-out clients and old.reddit
asks for a login, so this module reads:

- **Arctic Shift** (https://arctic-shift.photon-reddit.com), a public archive
  that captures posts and comments as they are written. Used for comment
  trees, post lookup and search. Scores and comment counts settle about 36 h
  after posting; before that they read 0 or 1.
- **Reddit RSS** (``/r/<sub>/<sort>/.rss``) for live hot/new/top listings,
  which the archive cannot rank.

Environment:
    ARCTIC_SHIFT_URL: archive base URL (default https://arctic-shift.photon-reddit.com)
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

LOGGER = logging.getLogger(__name__)

DEFAULT_ARCTIC = "https://arctic-shift.photon-reddit.com"
USER_AGENT = "searxNcrawl-reddit-reader/1.0 (personal, read-only)"
_ATOM = "{http://www.w3.org/2005/Atom}"
_ID_RE = re.compile(r"/comments/([a-z0-9]+)", re.IGNORECASE)
_BARE_ID_RE = re.compile(r"^(?:t3_)?([a-z0-9]{4,12})$", re.IGNORECASE)
_POST_FIELDS = "id,title,author,score,num_comments,created_utc,selftext,url,subreddit,link_flair_text"
SORTS = ("hot", "new", "top", "rising", "controversial")
TIMES = ("hour", "day", "week", "month", "year", "all")


class RedditError(Exception):
    """A Reddit source could not answer."""


def _arctic() -> str:
    return (os.getenv("ARCTIC_SHIFT_URL", "").strip() or DEFAULT_ARCTIC).rstrip("/")


def post_id(ref: str) -> str:
    """Post ID from a Reddit URL, a ``t3_`` fullname or a bare ID."""
    ref = ref.strip()
    m = _ID_RE.search(ref)
    if m:
        return m.group(1).lower()
    m = _BARE_ID_RE.match(ref)
    if m:
        return m.group(1).lower()
    raise ValueError(f"not a Reddit post URL or ID: {ref!r}")


def _when(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return str(ts or "")


def html_to_text(fragment: str) -> str:
    """Readable text from the HTML inside an RSS entry."""
    if not fragment or not fragment.strip():
        return ""
    import lxml.html

    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"(?i)</(p|div|li|blockquote|pre|h\d)>", "\n\n", fragment)
    text = lxml.html.fromstring(f"<div>{fragment}</div>").text_content()
    text = re.sub(r"\[link\]\s*\[comments\]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


RETRY_DELAYS = (2.0, 5.0)


async def _get_json(client: httpx.AsyncClient, path: str, params: dict) -> Any:
    """GET an Arctic Shift endpoint. Query timeouts (HTTP 422, "Timeout") are
    retried: the archive's database is slow on a cold query and fine on the next."""
    import asyncio

    for attempt in range(len(RETRY_DELAYS) + 1):
        resp = await client.get(f"{_arctic()}{path}", params=params, timeout=60)
        if resp.status_code == 429:
            raise RedditError(
                f"Arctic Shift rate limit, resets in {resp.headers.get('X-RateLimit-Reset', '?')} s"
            )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        error = str(body.get("error") or "")
        timed_out = resp.status_code == 422 or "timeout" in error.lower() or "timed out" in error.lower()
        if timed_out and attempt < len(RETRY_DELAYS):
            await asyncio.sleep(RETRY_DELAYS[attempt])
            continue
        if resp.status_code != 200:
            raise RedditError(f"Arctic Shift HTTP {resp.status_code}" + (f": {error}" if error else ""))
        if error:
            raise RedditError(f"Arctic Shift: {error}")
        return body.get("data")
    raise RedditError("Arctic Shift: query timed out")


_SLUG_RE = re.compile(r"/comments/[a-z0-9]+/([^/?#]+)", re.IGNORECASE)


def _title_hidden(post: dict) -> bool:
    return str(post.get("title") or "").startswith("[ Removed")


def _title(post: dict) -> str:
    """Archived title. When the archive caught the post while a moderator bot had
    it hidden ("[ Removed by moderator ]"), fall back to the URL slug if it still
    carries the original words, else to the opening of the post text."""
    title = post.get("title") or ""
    if not _title_hidden(post):
        return title
    m = _SLUG_RE.search(str(post.get("url") or ""))
    if m and "removed" not in m.group(1).lower():
        slug = m.group(1).replace("_", " ").strip()
        return f"{slug[:1].upper()}{slug[1:]} ... (title hidden in archive; from URL)"
    text = " ".join((post.get("selftext") or "").split())
    if text:
        return f"\"{text[:80]}{'...' if len(text) > 80 else ''}\" (title hidden in archive)"
    return title


async def _rss_title(pid: str) -> Optional[str]:
    """The live title from the thread's RSS feed, or None."""
    try:
        from scrapling.fetchers import AsyncFetcher

        resp = await AsyncFetcher.get(
            f"https://www.reddit.com/comments/{pid}/.rss",
            impersonate="chrome",
            stealthy_headers=True,
            timeout=15,
            retries=1,
        )
        if resp.status != 200:
            return None
        raw = resp.body if isinstance(resp.body, (bytes, bytearray)) else str(resp.body).encode()
        entry = ET.fromstring(raw).find(f"{_ATOM}entry")
        title = entry.findtext(f"{_ATOM}title") if entry is not None else None
        return title.strip() if title else None
    except Exception as exc:
        LOGGER.info("Thread RSS title lookup failed for %s: %s", pid, exc)
        return None


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


@dataclass
class Comment:
    author: str
    score: Any
    body: str
    created: Any
    depth: int
    distinguished: Optional[str] = None
    is_submitter: bool = False
    replies: list["Comment"] = field(default_factory=list)
    more: int = 0  # collapsed replies not loaded


def _parse_tree(nodes: list, depth: int = 0) -> tuple[list[Comment], int]:
    comments: list[Comment] = []
    more = 0
    for node in nodes or []:
        kind = node.get("kind")
        data = node.get("data") or {}
        if kind == "more":
            more += int(data.get("count") or len(data.get("children") or []) or 0)
            continue
        if kind != "t1":
            continue
        replies_raw = data.get("replies")
        children = (
            (replies_raw.get("data") or {}).get("children", [])
            if isinstance(replies_raw, dict)
            else []
        )
        kids, kid_more = _parse_tree(children, depth + 1)
        comments.append(
            Comment(
                author=data.get("author") or "[deleted]",
                score=data.get("score"),
                body=(data.get("body") or "").strip(),
                created=data.get("created_utc"),
                depth=depth,
                distinguished=data.get("distinguished"),
                is_submitter=bool(data.get("is_submitter")),
                replies=kids,
                more=kid_more,
            )
        )

    def rank(c: Comment):
        score = c.score if isinstance(c.score, (int, float)) else 0
        return (0 if c.distinguished == "moderator" else 1, -score)

    comments.sort(key=rank)
    return comments, more


def _format_comments(comments: list[Comment], max_chars: int) -> list[str]:
    lines: list[str] = []
    for c in comments:
        pad = "  " * c.depth
        tag = " (OP)" if c.is_submitter else (" (mod)" if c.distinguished == "moderator" else "")
        body = c.body if len(c.body) <= max_chars else c.body[:max_chars].rstrip() + " [...]"
        body = body.replace("\n", f"\n{pad}  ")
        lines.append(f"{pad}- **u/{c.author}**{tag} [{c.score}] {body}")
        lines.extend(_format_comments(c.replies, max_chars))
        if c.more:
            lines.append(f"{pad}  - _[{c.more} more replies not loaded]_")
    return lines


async def thread(
    ref: str,
    *,
    comment_limit: int = 200,
    max_depth: int = 6,
    max_comment_chars: int = 1500,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """A post and its comment tree as Markdown."""
    pid = post_id(ref)
    own = client is None
    client = client or httpx.AsyncClient(headers={"User-Agent": USER_AGENT})
    try:
        posts = await _get_json(client, "/api/posts/ids", {"ids": pid, "fields": _POST_FIELDS})
        if not posts:
            raise RedditError(f"post {pid} not in the Arctic Shift archive")
        post = posts[0]
        tree = await _get_json(
            client,
            "/api/comments/tree",
            {
                "link_id": f"t3_{pid}",
                "limit": max(1, min(comment_limit, 25000)),
                "start_depth": max(1, max_depth),
                "start_breadth": max(1, comment_limit),
            },
        )
    finally:
        if own:
            await client.aclose()

    if _title_hidden(post):
        live = await _rss_title(pid)
        if live:
            post = {**post, "title": live}

    comments, top_more = _parse_tree(tree or [])
    age_h = 0.0
    try:
        age_h = (datetime.now(timezone.utc).timestamp() - float(post.get("created_utc"))) / 3600
    except (TypeError, ValueError):
        pass
    out = [
        f"# {_title(post)}",
        f"r/{post.get('subreddit')} · u/{post.get('author')} · {_when(post.get('created_utc'))} · "
        f"score {post.get('score')} · {post.get('num_comments')} comments"
        + (f" · flair: {post['link_flair_text']}" if post.get("link_flair_text") else ""),
        f"https://www.reddit.com/comments/{pid}/",
    ]
    if post.get("url") and "reddit.com" not in str(post.get("url")):
        out.append(f"Link: {post['url']}")
    if age_h and age_h < 36:
        out.append(
            "_Archive note: post is under 36 h old, so scores and counts are not final "
            "and the newest comments may be missing._"
        )
    selftext = (post.get("selftext") or "").strip()
    if selftext:
        out += ["", selftext]
    out += ["", "## Comments (best first, source: Arctic Shift archive)", ""]
    out += _format_comments(comments, max_comment_chars) or ["_No comments archived._"]
    if top_more:
        out.append(f"- _[{top_more} more comments not loaded; raise comment_limit]_")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------


async def _rss_entries(subreddit: str, sort: str, time: str, limit: int) -> list[dict]:
    from scrapling.fetchers import AsyncFetcher

    url = f"https://www.reddit.com/r/{subreddit}/{sort}/.rss"
    params = f"?limit={limit}" + (f"&t={time}" if sort in ("top", "controversial") else "")
    resp = await AsyncFetcher.get(
        url + params, impersonate="chrome", stealthy_headers=True, timeout=20, retries=2
    )
    if resp.status != 200:
        raise RedditError(f"Reddit RSS HTTP {resp.status}")
    raw = resp.body if isinstance(resp.body, (bytes, bytearray)) else str(resp.body).encode()
    root = ET.fromstring(raw)
    entries = []
    for e in root.findall(f"{_ATOM}entry"):
        link = e.find(f"{_ATOM}link")
        author = e.find(f"{_ATOM}author/{_ATOM}name")
        content = e.find(f"{_ATOM}content")
        entries.append(
            {
                "id": (e.findtext(f"{_ATOM}id") or "").removeprefix("t3_"),
                "title": e.findtext(f"{_ATOM}title") or "",
                "author": (author.text if author is not None else "") or "",
                "created": e.findtext(f"{_ATOM}published") or "",
                "url": link.get("href") if link is not None else "",
                "text": html_to_text(content.text if content is not None else ""),
            }
        )
    return entries


async def _arctic_posts(
    client: httpx.AsyncClient, subreddit: Optional[str], query: Optional[str], limit: int, author: Optional[str] = None
) -> list[dict]:
    params: dict[str, Any] = {"limit": limit, "sort": "desc", "fields": _POST_FIELDS}
    if subreddit:
        params["subreddit"] = subreddit
    if author:
        params["author"] = author
    if query:
        params["query"] = query
    data = await _get_json(client, "/api/posts/search", params)
    return [
        {
            "id": p.get("id"),
            "title": _title(p),
            "author": f"/u/{p.get('author')}",
            "created": _when(p.get("created_utc")),
            "url": f"https://www.reddit.com/comments/{p.get('id')}/",
            "text": (p.get("selftext") or "").strip(),
            "score": p.get("score"),
            "num_comments": p.get("num_comments"),
        }
        for p in data or []
    ]


def _format_listing(header: str, items: list[dict], note: str, max_chars: int) -> str:
    out = [header, note, ""]
    for i, it in enumerate(items, 1):
        stats = ""
        if it.get("score") is not None:
            stats = f" · score {it['score']} · {it.get('num_comments')} comments"
        out.append(f"{i}. **{it['title']}** (id `{it['id']}`)")
        out.append(f"   {it['author']} · {it['created']}{stats} · {it['url']}")
        text = it.get("text") or ""
        if text:
            short = text if len(text) <= max_chars else text[:max_chars].rstrip() + " [...]"
            out.append("   " + short.replace("\n", "\n   "))
    if not items:
        out.append("_No posts found._")
    return "\n".join(out)


async def posts(
    subreddit: Optional[str] = None,
    *,
    sort: str = "hot",
    time: str = "week",
    limit: int = 25,
    query: Optional[str] = None,
    author: Optional[str] = None,
    max_text_chars: int = 400,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """Subreddit listing (live via RSS) or a keyword search (archive)."""
    subreddit = (subreddit or "").strip().removeprefix("r/").removeprefix("/r/") or None
    sort = sort if sort in SORTS else "hot"
    time = time if time in TIMES else "week"
    limit = max(1, min(limit, 100))
    if not subreddit and not author:
        raise ValueError("give a subreddit or an author")

    own = client is None
    client = client or httpx.AsyncClient(headers={"User-Agent": USER_AGENT})
    try:
        if query or author or not subreddit:
            items = await _arctic_posts(client, subreddit, query, limit, author)
            what = f'"{query}"' if query else "newest posts"
            scope = f"r/{subreddit}" if subreddit else f"u/{author}"
            return _format_listing(
                f"# {scope}: {what}",
                items,
                "_Source: Arctic Shift archive, newest first. Scores settle ~36 h after posting._",
                max_text_chars,
            )
        try:
            items = await _rss_entries(subreddit, sort, time, limit)
            label = f"{sort}" + (f" ({time})" if sort in ("top", "controversial") else "")
            return _format_listing(
                f"# r/{subreddit}: {label}",
                items,
                "_Source: Reddit RSS (live ranking, no scores). Open a thread with reddit_thread._",
                max_text_chars,
            )
        except Exception as exc:
            LOGGER.info("Reddit RSS failed for r/%s (%s); falling back to Arctic Shift", subreddit, exc)
            items = await _arctic_posts(client, subreddit, None, limit)
            return _format_listing(
                f"# r/{subreddit}: newest posts",
                items,
                f"_Reddit RSS failed ({exc}); source: Arctic Shift archive, newest first, "
                f"not ranked by {sort}._",
                max_text_chars,
            )
    finally:
        if own:
            await client.aclose()
