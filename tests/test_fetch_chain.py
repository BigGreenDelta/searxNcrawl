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
    async with httpx.AsyncClient(transport=_firecrawl_transport(used, scrapes=scrapes)) as c:
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


async def test_firecrawl_window_approval_covers_later_calls(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    asked: list[str] = []

    async def approve(prompt):
        asked.append(prompt)
        return "window"

    scrapes: list = []
    assert (await _scrape(3000, approve, scrapes)).status == "success"
    assert (await _scrape(3001, approve, scrapes)).status == "success"
    assert len(asked) == 1 and len(scrapes) == 2


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
    script["browser"] = (_doc("https://a.test", "", "browser", "failed", "HTTP 403"), "")
    script["stealth"] = (_doc("https://a.test", "", "stealth", "failed", "HTTP 403"), "")
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
    script["browser"] = (_doc("https://a.test", "", "browser", "failed", "HTTP 403"), "")
    script["stealth"] = (_doc("https://a.test", "", "stealth", "failed", "HTTP 403"), "")
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


def test_long_article_mentioning_cloudflare_is_not_a_challenge():
    text = "Cloudflare says just a moment... " + "x" * 7000
    assert fetch_chain.looks_blocked(text) is False
