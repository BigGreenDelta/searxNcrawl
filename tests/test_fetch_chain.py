"""Tiered fetch router: tier order, escalation, cache and the Firecrawl guard."""

from __future__ import annotations

import httpx
import pytest

from crawler import fetch_chain, firecrawl_tier
from crawler.document import CrawledDocument

LONG = "word " * 200  # comfortably above FETCH_MIN_CHARS


def _doc(url: str, markdown: str, tier: str, status: str = "success", error=None):
    return CrawledDocument(
        request_url=url,
        final_url=url,
        status=status,
        markdown=markdown,
        error_message=error,
        metadata={"fetched_via": tier},
    )


@pytest.fixture
def tiers(monkeypatch: pytest.MonkeyPatch):
    """Replace the three free tiers with scripted fakes; record the call order."""
    calls: list[str] = []
    script: dict[str, tuple[CrawledDocument, str] | Exception] = {}

    def make(name: str):
        async def run(url: str, timeout: int):
            calls.append(name)
            outcome = script[name]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return run

    monkeypatch.setattr(
        fetch_chain, "_RUNNERS", {n: make(n) for n in fetch_chain.FREE_TIERS}
    )
    monkeypatch.setenv("FETCH_TIERS", "http,browser,stealth,firecrawl")
    monkeypatch.setenv("FETCH_MIN_CHARS", "400")
    return calls, script


async def test_fast_tier_wins_when_page_is_full(tiers):
    calls, script = tiers
    script["http"] = (_doc("https://a.test", LONG, "http"), "")
    doc = await fetch_chain.fetch("https://a.test")
    assert doc.status == "success"
    assert doc.metadata["fetched_via"] == "http"
    assert calls == ["http"]


async def test_thin_page_escalates_to_browser_but_not_stealth_or_paid(tiers):
    calls, script = tiers
    script["http"] = (_doc("https://a.test", "short", "http"), "")
    script["browser"] = (_doc("https://a.test", "still short", "browser"), "")
    doc = await fetch_chain.fetch("https://a.test")
    assert calls == ["http", "browser"]
    assert doc.markdown == "still short"
    assert "short page" in doc.metadata["warning"]


async def test_challenge_page_escalates_to_stealth(tiers):
    calls, script = tiers
    wall = "<title>Just a moment...</title> challenge-platform"
    script["http"] = (_doc("https://a.test", "Just a moment...", "http"), wall)
    script["browser"] = (_doc("https://a.test", "Just a moment...", "browser"), wall)
    script["stealth"] = (_doc("https://a.test", LONG, "stealth"), "")
    doc = await fetch_chain.fetch("https://a.test")
    assert calls == ["http", "browser", "stealth"]
    assert doc.metadata["fetched_via"] == "stealth"


async def test_cache_returns_page_without_fetching(tiers, monkeypatch):
    calls, script = tiers
    monkeypatch.setenv("FETCH_CACHE_TTL", "3600")
    script["http"] = (_doc("https://a.test/x", LONG, "http"), "")
    await fetch_chain.fetch("https://a.test/x#frag")
    doc = await fetch_chain.fetch("https://A.test/x")
    assert calls == ["http"]
    assert doc.metadata["from_cache"] is True
    fresh = await fetch_chain.fetch("https://a.test/x", fresh=True)
    assert calls == ["http", "http"]
    assert not fresh.metadata.get("from_cache")


def _firecrawl_transport(used: int, plan: int = 5000, scrapes: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/team/credit-usage"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"remainingCredits": plan - used, "planCredits": plan},
                },
            )
        if request.url.path.endswith("/scrape"):
            if scrapes is not None:
                scrapes.append(request)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"markdown": LONG, "metadata": {"url": "https://a.test"}},
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def _scrape(used: int, approve, scrapes: list):
    async with httpx.AsyncClient(
        transport=_firecrawl_transport(used, scrapes=scrapes)
    ) as c:
        return await firecrawl_tier.scrape("https://a.test", approve, client=c)


async def test_firecrawl_under_threshold_does_not_ask(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    asked: list[str] = []

    async def approve(prompt):
        asked.append(prompt)
        return "no"

    scrapes: list = []
    doc = await _scrape(1449, approve, scrapes)
    assert doc.status == "success" and asked == [] and len(scrapes) == 1


async def test_firecrawl_at_threshold_asks_and_respects_no(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    asked: list[str] = []

    async def approve(prompt):
        asked.append(prompt)
        return "no"

    scrapes: list = []
    doc = await _scrape(1450, approve, scrapes)
    assert doc.status == "failed" and doc.metadata["blocked"] is True
    assert "1450" in asked[0] and scrapes == []


async def test_firecrawl_without_approver_is_blocked(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    scrapes: list = []
    doc = await _scrape(3000, None, scrapes)
    assert doc.metadata["blocked"] is True and scrapes == []


async def test_firecrawl_grant_is_silent_until_its_ceiling(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    monkeypatch.setenv("FIRECRAWL_GRANT_CREDITS", "50")
    asked: list[str] = []

    async def approve(prompt):
        asked.append(prompt)
        return "grant"

    scrapes: list = []
    assert (await _scrape(1460, approve, scrapes)).status == "success"
    assert (await _scrape(1509, approve, scrapes)).status == "success"
    assert len(asked) == 1
    # ceiling 1460 + 50 reached: asks again, and grants a fresh 50
    assert (await _scrape(1510, approve, scrapes)).status == "success"
    assert len(asked) == 2 and "used up" in asked[1]
    assert (await _scrape(1559, approve, scrapes)).status == "success"
    assert len(asked) == 2 and len(scrapes) == 4


async def test_firecrawl_grant_ceiling_reached_and_declined_blocks(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    answers = iter(["grant", "no"])

    async def approve(prompt):
        return next(answers)

    scrapes: list = []
    assert (await _scrape(1450, approve, scrapes)).status == "success"
    doc = await _scrape(1550, approve, scrapes)
    assert doc.metadata["blocked"] is True and len(scrapes) == 1


async def test_firecrawl_grant_dropped_when_usage_resets(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    asked: list[str] = []

    async def approve(prompt):
        asked.append(prompt)
        return "grant" if len(asked) == 1 else "no"

    scrapes: list = []
    assert (await _scrape(1450, approve, scrapes)).status == "success"
    assert firecrawl_tier._grant == (1450, 1550)
    # new billing period: usage back under the threshold, no prompt, grant gone
    assert (await _scrape(10, approve, scrapes)).status == "success"
    assert firecrawl_tier._grant is None
    # so reaching the threshold again asks instead of riding the old grant
    assert (await _scrape(1460, approve, scrapes)).metadata["blocked"] is True
    assert len(asked) == 2


async def test_firecrawl_usage_api_failure_counts_as_needing_approval(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"success": False})

    asked: list[str] = []

    async def approve(prompt):
        asked.append(prompt)
        return "no"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        doc = await firecrawl_tier.scrape("https://a.test", approve, client=c)
    assert doc.metadata["blocked"] is True
    assert "Could not read" in asked[0]


async def test_firecrawl_only_when_free_tiers_return_nothing(tiers, monkeypatch):
    calls, script = tiers
    script["http"] = RuntimeError("boom")
    script["browser"] = (
        _doc("https://a.test", "", "browser", "failed", "HTTP 403"),
        "",
    )
    script["stealth"] = (
        _doc("https://a.test", "", "stealth", "failed", "HTTP 403"),
        "",
    )
    paid: list[str] = []

    async def fake_scrape(url, approve, timeout=60):
        paid.append(url)
        return _doc(url, LONG, "firecrawl")

    monkeypatch.setattr(firecrawl_tier, "scrape", fake_scrape)
    doc = await fetch_chain.fetch("https://a.test")
    assert calls == ["http", "browser", "stealth"] and paid == ["https://a.test"]
    assert doc.metadata["fetched_via"] == "firecrawl"


async def test_firecrawl_skipped_when_a_free_tier_had_content(tiers, monkeypatch):
    calls, script = tiers
    script["http"] = (_doc("https://a.test", "short but real", "http"), "")
    script["browser"] = (
        _doc("https://a.test", "", "browser", "failed", "HTTP 403"),
        "",
    )
    script["stealth"] = (
        _doc("https://a.test", "", "stealth", "failed", "HTTP 403"),
        "",
    )
    paid: list[str] = []

    async def fake_scrape(url, approve, timeout=60):
        paid.append(url)
        return _doc(url, LONG, "firecrawl")

    monkeypatch.setattr(firecrawl_tier, "scrape", fake_scrape)
    doc = await fetch_chain.fetch("https://a.test")
    assert paid == [] and doc.markdown == "short but real"


async def test_not_found_stops_the_chain(tiers):
    calls, script = tiers
    gone = _doc("https://a.test", "Not Found", "http", "failed", "HTTP 404")
    gone.metadata["status_code"] = 404
    script["http"] = (gone, "")
    doc = await fetch_chain.fetch("https://a.test")
    assert calls == ["http"]
    assert doc.status == "failed" and doc.error_message == "HTTP 404"


def test_json_and_xml_pass_through_raw():
    import types

    resp = types.SimpleNamespace(
        status=200,
        url="https://api.test/x",
        headers={"content-type": "application/json; charset=utf-8"},
        body=b'{"a": [1, 2]}',
        encoding="utf-8",
    )
    doc, _ = fetch_chain._doc_from_scrapling("https://api.test/x", resp, "http")
    assert doc.status == "success"
    assert '"a"' in doc.markdown and "\\[" not in doc.markdown
    resp.headers = {"content-type": "application/atom+xml"}
    resp.body = b"<feed><title>x</title></feed>"
    doc, _ = fetch_chain._doc_from_scrapling("https://api.test/x", resp, "http")
    assert doc.markdown == "<feed><title>x</title></feed>"


async def test_browser_tier_treats_http_errors_as_failure(monkeypatch):
    import crawler

    async def fake_crawl(url, timeout=None):
        return CrawledDocument(
            request_url=url,
            final_url=url,
            status="success",
            markdown="Cloudflare error 526 page " * 50,
            metadata={"status_code": 526},
        )

    monkeypatch.setattr(crawler, "crawl_page_async", fake_crawl)
    doc, _ = await fetch_chain._tier_browser("https://a.test", 5)
    assert doc.status == "failed" and doc.error_message == "HTTP 526"


def test_login_wall_counts_as_blocked():
    assert fetch_chain.looks_blocked("Log in to use old Reddit. To keep Reddit safe...")


def test_long_article_mentioning_cloudflare_is_not_a_challenge():
    text = "Cloudflare says just a moment... " + "x" * 7000
    assert fetch_chain.looks_blocked(text) is False
