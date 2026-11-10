"""Reddit reader: ID parsing, comment tree formatting, RSS parsing, fallbacks."""

from __future__ import annotations

import types

import httpx
import pytest

from crawler import reddit

POST = {
    "id": "abc123",
    "title": "Test post",
    "author": "op_user",
    "score": 900,
    "num_comments": 3,
    "created_utc": 1700000000,
    "selftext": "Body of the post",
    "url": "https://www.reddit.com/r/test/comments/abc123/test_post/",
    "subreddit": "test",
}


def _c(cid, author, score, body, replies=None, **extra):
    data = {"id": cid, "author": author, "score": score, "body": body, "created_utc": 1700000100, **extra}
    data["replies"] = {"kind": "Listing", "data": {"children": replies}} if replies else ""
    return {"kind": "t1", "data": data}


TREE = [
    _c("c1", "low", 5, "low score top comment"),
    _c(
        "c2",
        "high",
        500,
        "high score top comment",
        replies=[
            _c("c3", "op_user", 50, "reply from OP", is_submitter=True),
            {"kind": "more", "data": {"count": 7, "children": ["x", "y"]}},
        ],
    ),
    _c("c0", "AutoModerator", 1, "pinned mod note", distinguished="moderator"),
    {"kind": "more", "data": {"count": 40, "children": ["z"]}},
]


def _arctic_transport(posts=None, tree=None, search=None, status=200):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status, headers={"X-RateLimit-Reset": "12"})
        path = request.url.path
        if path.endswith("/posts/ids"):
            return httpx.Response(200, json={"data": posts if posts is not None else [POST]})
        if path.endswith("/comments/tree"):
            return httpx.Response(200, json={"data": tree if tree is not None else TREE})
        if path.endswith("/posts/search"):
            return httpx.Response(200, json={"data": search if search is not None else [POST]})
        return httpx.Response(404, json={"error": "nope"})

    return httpx.MockTransport(handler), seen


@pytest.mark.parametrize(
    "ref",
    [
        "https://www.reddit.com/r/test/comments/abc123/test_post/",
        "https://old.reddit.com/comments/abc123",
        "t3_abc123",
        "ABC123",
    ],
)
def test_post_id_accepts_urls_fullnames_and_ids(ref):
    assert reddit.post_id(ref) == "abc123"


def test_post_id_rejects_garbage():
    with pytest.raises(ValueError):
        reddit.post_id("https://example.com/no/thread/here")


async def test_thread_orders_best_first_nests_and_reports_collapsed():
    transport, seen = _arctic_transport()
    async with httpx.AsyncClient(transport=transport) as client:
        md = await reddit.thread("abc123", client=client)
    assert md.startswith("# Test post")
    assert "Body of the post" in md
    # mod note pinned first, then by score
    assert md.index("pinned mod note") < md.index("high score") < md.index("low score")
    assert "  - **u/op_user** (OP) [50] reply from OP" in md
    assert "[7 more replies not loaded]" in md
    assert "[40 more comments not loaded" in md
    tree_req = next(r for r in seen if r.url.path.endswith("/comments/tree"))
    assert tree_req.url.params["link_id"] == "t3_abc123"


async def test_thread_missing_post_raises():
    transport, _ = _arctic_transport(posts=[])
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(reddit.RedditError):
            await reddit.thread("abc123", client=client)


async def test_rate_limit_is_reported():
    transport, _ = _arctic_transport(status=429)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(reddit.RedditError, match="rate limit"):
            await reddit.thread("abc123", client=client)


ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry>
  <author><name>/u/poster</name></author>
  <content type="html">&lt;div class="md"&gt;&lt;p&gt;First para&lt;/p&gt;&lt;p&gt;Second para&lt;/p&gt;&lt;/div&gt; [link] [comments]</content>
  <id>t3_zz9</id>
  <link href="https://www.reddit.com/r/test/comments/zz9/hello/" />
  <published>2026-11-08T13:31:26+00:00</published>
  <title>Hello RSS</title>
 </entry>
</feed>"""


async def test_listing_parses_reddit_rss(monkeypatch):
    calls = []

    async def fake_get(url, **kwargs):
        calls.append(url)
        return types.SimpleNamespace(status=200, body=ATOM)

    import scrapling.fetchers

    monkeypatch.setattr(scrapling.fetchers.AsyncFetcher, "get", staticmethod(fake_get))
    transport, seen = _arctic_transport()
    async with httpx.AsyncClient(transport=transport) as client:
        md = await reddit.posts("test", sort="top", time="month", limit=5, client=client)
    assert calls == ["https://www.reddit.com/r/test/top/.rss?limit=5&t=month"]
    assert "**Hello RSS** (id `zz9`)" in md
    assert "First para" in md and "Second para" in md
    assert "[link]" not in md
    assert seen == []  # archive not touched


async def test_listing_falls_back_to_archive_when_rss_fails(monkeypatch):
    async def fake_get(url, **kwargs):
        return types.SimpleNamespace(status=429, body=b"")

    import scrapling.fetchers

    monkeypatch.setattr(scrapling.fetchers.AsyncFetcher, "get", staticmethod(fake_get))
    transport, seen = _arctic_transport()
    async with httpx.AsyncClient(transport=transport) as client:
        md = await reddit.posts("test", client=client)
    assert "Reddit RSS failed" in md and "**Test post**" in md
    assert seen and seen[0].url.params["subreddit"] == "test"


async def test_query_timeout_is_retried(monkeypatch):
    monkeypatch.setattr(reddit, "RETRY_DELAYS", (0.0, 0.0))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(422, json={"data": None, "error": "Timeout. Maybe slow down a bit"})
        return httpx.Response(200, json={"data": [POST]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        md = await reddit.posts("test", query="x", client=client)
    assert calls["n"] == 2 and "**Test post**" in md


def test_hidden_title_falls_back_to_slug_then_text():
    post = {**POST, "title": "[ Removed by moderator ]"}
    assert reddit._title(post).startswith("Test post ... (title hidden")
    post["url"] = "https://www.reddit.com/r/test/comments/abc123/removed_by_moderator/"
    assert reddit._title(post) == '"Body of the post" (title hidden in archive)'
    assert reddit._title(POST) == "Test post"


async def test_thread_takes_live_title_from_rss_when_archive_hid_it(monkeypatch):
    async def fake_get(url, **kwargs):
        assert url == "https://www.reddit.com/comments/abc123/.rss"
        return types.SimpleNamespace(status=200, body=ATOM)

    import scrapling.fetchers

    monkeypatch.setattr(scrapling.fetchers.AsyncFetcher, "get", staticmethod(fake_get))
    transport, _ = _arctic_transport(posts=[{**POST, "title": "[ Removed by moderator ]"}])
    async with httpx.AsyncClient(transport=transport) as client:
        md = await reddit.thread("abc123", client=client)
    assert md.startswith("# Hello RSS")


async def test_query_goes_to_archive_search():
    transport, seen = _arctic_transport()
    async with httpx.AsyncClient(transport=transport) as client:
        md = await reddit.posts("test", query="filerun licence", client=client)
    assert seen[0].url.params["query"] == "filerun licence"
    assert "score 900" in md
