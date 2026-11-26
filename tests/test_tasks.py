"""crawl and crawl_site as MCP Tasks (SEP-2663): policy, progress, cancel."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp_tasks import call_tool_task

from crawler import fetch_chain, mcp_server
from crawler.document import CrawledDocument
from crawler.site import SiteCrawlResult

URLS = [f"https://pages.test/{i}" for i in range(6)]


def _page(url: str) -> CrawledDocument:
    return CrawledDocument(
        url, url, "success", f"text of {url} " * 30, metadata={"fetched_via": "http"}
    )


@pytest.fixture
def slow_fetch(monkeypatch: pytest.MonkeyPatch):
    """Each fetch takes ``delay`` seconds; ``started`` and ``done`` count them."""
    state = {"delay": 0.0, "started": 0, "done": 0}

    async def fetch(url, **_kwargs):
        state["started"] += 1
        await asyncio.sleep(state["delay"])
        state["done"] += 1
        return _page(url)

    monkeypatch.setattr(fetch_chain, "fetch", fetch)
    return state


@pytest.fixture
def no_task_client(monkeypatch: pytest.MonkeyPatch):
    """Clients built inside the test do not declare the tasks extension."""
    from fastmcp.client import extension_hooks

    monkeypatch.setattr(extension_hooks, "_internal_client_extension_factories", [])


# --- policy ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("crawl_site", {"url": "https://a.test"}, True),
        ("crawl", {"urls": URLS[:1]}, False),
        ("crawl", {"urls": URLS[: mcp_server.TASK_MIN_URLS - 1]}, False),
        ("crawl", {"urls": URLS[: mcp_server.TASK_MIN_URLS]}, True),
        ("crawl", {"urls": ["https://www.aliexpress.com/item/1005001.html"]}, False),
        (
            "crawl",
            {
                "urls": [
                    "https://www.aliexpress.com/item/1005001.html",
                    "https://aliexpress.com/item/1005002.html?spm=x",
                ]
            },
            True,
        ),
        ("crawl", {"urls": "https://a.test"}, False),
        ("search", {"query": "x"}, False),
        ("reddit_thread", {"post": "abc"}, False),
    ],
)
def test_wants_task(tool, arguments, expected):
    assert mcp_server.wants_task(tool, arguments) is expected


# --- crawl -----------------------------------------------------------------


async def test_batch_crawl_runs_as_task_with_progress(slow_fetch):
    slow_fetch["delay"] = 0.3
    urls = URLS[:5]
    async with Client(mcp_server.mcp) as client:
        task = await call_tool_task(client, "crawl", {"urls": urls, "concurrency": 1})
        assert task.create_result.ttl_ms == int(mcp_server.TASK_TTL.total_seconds() * 1000)
        messages = set()
        while True:
            status = await task.status()
            if status.status != "working":
                break
            if status.status_message:
                messages.add(status.status_message)
            await asyncio.sleep(0.05)
        result = await task.result()

    assert status.status == "completed"
    text = result.content[0].text
    assert all(f"text of {u}" in text for u in urls)
    assert messages, "no progress was reported while the task ran"
    assert messages <= {f"{n}/5 pages done" for n in range(1, 6)}


async def test_small_crawl_answers_inline(slow_fetch):
    async with Client(mcp_server.mcp) as client:
        with pytest.raises(ToolError, match="did not run as a task"):
            await call_tool_task(client, "crawl", {"urls": URLS[:2]})
        result = await client.call_tool("crawl", {"urls": URLS[:2]})
    assert "text of https://pages.test/1" in result.content[0].text


async def test_batch_crawl_without_tasks_client_runs_inline(slow_fetch, no_task_client):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("crawl", {"urls": URLS[:5]})
    assert slow_fetch["done"] == 5
    assert "text of https://pages.test/4" in result.content[0].text


async def test_foreground_crawl_sends_progress(slow_fetch, no_task_client):
    seen: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    async with Client(mcp_server.mcp, progress_handler=on_progress) as client:
        await client.call_tool("crawl", {"urls": URLS[:3]})
    assert sorted(p for p, _, _ in seen) == [1, 2, 3]
    assert {t for _, t, _ in seen} == {3}
    assert "3/3 pages done" in {m for _, _, m in seen}


async def test_cancel_stops_the_crawl(slow_fetch):
    slow_fetch["delay"] = 0.4
    async with Client(mcp_server.mcp) as client:
        task = await call_tool_task(client, "crawl", {"urls": URLS, "concurrency": 1})
        while slow_fetch["done"] < 1:
            await asyncio.sleep(0.05)
        await task.cancel()
        status = await task.wait(timeout=10)
        stopped_at = slow_fetch["started"]
        await asyncio.sleep(1.5)

    assert status.status == "cancelled"
    # The fetch in flight when the cancel arrived is interrupted; no new one starts.
    assert slow_fetch["started"] == stopped_at
    assert slow_fetch["started"] < len(URLS)
    assert slow_fetch["done"] < len(URLS)


# --- Firecrawl spend question inside a task -----------------------------------


async def test_spend_question_inside_a_task(monkeypatch: pytest.MonkeyPatch):
    walled = "https://walled.test/page"
    calls: list[tuple[str, tuple | None]] = []

    async def fetch(url, *, approve=None, tiers=None, **_kwargs):
        calls.append((url, tuple(tiers) if tiers else None))
        if url != walled:
            return _page(url)
        answer = await approve("prompt") if approve else "no"
        if answer == "no":
            return CrawledDocument(
                url,
                url,
                "failed",
                "",
                error_message="Blocked: Firecrawl would spend credits and the user did not approve",
                metadata={"blocked": True, "approval_reason": "1460 credits used."},
            )
        return CrawledDocument(
            url, url, "success", "paid page " * 50, metadata={"fetched_via": "firecrawl"}
        )

    monkeypatch.setattr(fetch_chain, "fetch", fetch)
    asked: list[str] = []

    async def handler(message, response_type, params, context):
        asked.append(message)
        return {"value": "Yes, just this time"}

    urls = [*URLS[:4], walled]
    async with Client(mcp_server.mcp, elicitation_handler=handler) as client:
        task = await call_tool_task(client, "crawl", {"urls": urls})
        parked = await task.wait(state="input_required", timeout=10)
        # fastmcp-tasks surfaces the request under its own key.
        requests = list((parked.input_requests or {}).values())
        assert len(requests) == 1
        assert requests[0]["method"] == "elicitation/create"
        result = await task.result()

    assert len(asked) == 1 and walled in asked[0]
    assert "paid page" in result.content[0].text
    assert (walled, ("firecrawl",)) in calls


# --- crawl_site -------------------------------------------------------------


@pytest.fixture
def fake_site(monkeypatch: pytest.MonkeyPatch):
    """crawl_site_async that 'crawls' ``pages`` pages, ``delay`` s each."""
    import crawler

    state = {"pages": 3, "delay": 0.2, "crawled": 0}

    async def crawl_site_async(url, *, max_pages=25, on_page=None, **_kwargs):
        docs = []
        for i in range(min(state["pages"], max_pages)):
            await asyncio.sleep(state["delay"])
            state["crawled"] += 1
            docs.append(_page(f"{url}/p{i}"))
            if on_page is not None:
                await on_page(i + 1, f"{url}/p{i}")
        return SiteCrawlResult(
            documents=docs,
            stats={
                "total_pages": len(docs),
                "successful_pages": len(docs),
                "failed_pages": 0,
                "error_count": 0,
            },
        )

    monkeypatch.setattr(crawler, "crawl_site_async", crawl_site_async)
    return state


async def test_crawl_site_runs_as_task_with_progress(fake_site):
    async with Client(mcp_server.mcp) as client:
        task = await call_tool_task(
            client, "crawl_site", {"url": "https://site.test", "max_pages": 10}
        )
        messages = set()
        while (status := await task.status()).status == "working":
            if status.status_message:
                messages.add(status.status_message)
            await asyncio.sleep(0.05)
        result = await task.result()

    assert status.status == "completed"
    assert "text of https://site.test/p2" in result.content[0].text
    assert messages <= {f"{n} pages crawled (limit 10)" for n in range(1, 4)}
    assert messages


async def test_crawl_site_cancel_stops_crawling(fake_site):
    fake_site.update(pages=20, delay=0.3)
    async with Client(mcp_server.mcp) as client:
        task = await call_tool_task(client, "crawl_site", {"url": "https://site.test"})
        while fake_site["crawled"] < 1:
            await asyncio.sleep(0.05)
        await task.cancel()
        await task.wait(timeout=10)
        stopped_at = fake_site["crawled"]
        await asyncio.sleep(1.2)
    assert fake_site["crawled"] == stopped_at < 20


async def test_crawl_site_without_tasks_client_runs_inline(fake_site, no_task_client):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("crawl_site", {"url": "https://site.test"})
    assert "text of https://site.test/p0" in result.content[0].text


# --- progress hooks -----------------------------------------------------------


async def test_page_counter_counts_each_page_and_survives_errors():
    from crawler.page_hooks import install_page_counter

    hooks = {}

    class Strategy:
        def set_hook(self, name, fn):
            hooks[name] = fn

    class Crawler:
        crawler_strategy = Strategy()

    class Page:
        def __init__(self, url):
            self.url = url

    seen = []

    async def on_page(done, url):
        seen.append((done, url))
        if done == 2:
            raise RuntimeError("progress sink down")

    install_page_counter(Crawler(), on_page)
    hook = hooks["before_return_html"]
    for i in range(3):
        page = Page(f"https://s.test/{i}")
        assert await hook(page, html="<p>x</p>", context=None, config=None) is page
    assert seen == [(1, "https://s.test/0"), (2, "https://s.test/1"), (3, "https://s.test/2")]


async def test_fetch_many_reports_each_page_and_ignores_callback_errors(slow_fetch):
    done = []

    async def on_done(doc):
        done.append(doc.request_url)
        raise RuntimeError("progress sink down")

    docs = await fetch_chain.fetch_many(URLS[:3], on_done=on_done)
    assert [d.request_url for d in docs] == URLS[:3]
    assert sorted(done) == URLS[:3]
