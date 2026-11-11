"""Firecrawl spend approval on MCP 2026-07-28: InputRequiredResult round trip."""

from __future__ import annotations

import pytest
from fastmcp import Client

from crawler import fetch_chain, mcp_server
from crawler.document import CrawledDocument

FREE = "https://free.test/page"
WALLED = "https://walled.test/page"


@pytest.fixture
def fake_fetch(monkeypatch: pytest.MonkeyPatch):
    """WALLED needs Firecrawl (asks approval); FREE is read by a free tier."""
    calls: list[tuple[str, tuple | None]] = []

    async def fetch(url, *, approve=None, tiers=None, timeout=20, fresh=False):
        calls.append((url, tuple(tiers) if tiers else None))
        if url == FREE:
            return CrawledDocument(
                url, url, "success", "free page " * 50, metadata={"fetched_via": "http"}
            )
        answer = await approve("prompt") if approve else "no"
        if answer == "no":
            return CrawledDocument(
                url,
                url,
                "failed",
                "",
                error_message="Blocked: Firecrawl would spend credits and the user did not approve",
                metadata={
                    "blocked": True,
                    "approval_reason": "Firecrawl has used 1460 credits.",
                },
            )
        return CrawledDocument(
            url,
            url,
            "success",
            "paid page " * 50,
            metadata={"fetched_via": "firecrawl"},
        )

    monkeypatch.setattr(fetch_chain, "fetch", fetch)
    return calls


def _handler(choice: str | None, seen: list[str]):
    async def handle(message, response_type, params, context):
        seen.append(message)
        if choice is None:
            from fastmcp.client.elicitation import ElicitResult

            return ElicitResult(action="decline")
        return {"value": choice}

    return handle


async def _call(handler, urls):
    async with Client(mcp_server.mcp, elicitation_handler=handler) as client:
        return await client.call_tool("crawl", {"urls": urls}, raise_on_error=False)


async def test_spend_question_reaches_the_user_and_yes_fetches(fake_fetch):
    seen: list[str] = []
    result = await _call(_handler("Yes, just this time", seen), [FREE, WALLED])
    text = result.content[0].text
    assert "paid page" in text and "free page" in text
    assert len(seen) == 1 and WALLED in seen[0] and "1460" in seen[0]
    # second round: the walled page goes straight to Firecrawl
    assert (WALLED, ("firecrawl",)) in fake_fetch


async def test_spend_declined_stays_blocked(fake_fetch):
    seen: list[str] = []
    result = await _call(_handler(None, seen), [WALLED])
    assert len(seen) == 1
    assert "Blocked" in result.content[0].text


async def test_no_question_when_nothing_needs_paying(fake_fetch):
    seen: list[str] = []
    result = await _call(_handler("Yes, just this time", seen), [FREE])
    assert seen == [] and "free page" in result.content[0].text


def test_question_says_whether_the_spend_is_paid(monkeypatch: pytest.MonkeyPatch):
    from crawler import firecrawl_tier as ft

    monkeypatch.setattr(ft, "_grant", None)
    paid = ft.question([WALLED], ft._reason(ft.CreditStatus(4277, 723, 5000)))
    assert paid.startswith(f"Read {WALLED} with Firecrawl?")
    assert "4277 of 5000 credits used - 723 left.\nThe 1500 free" in paid
    assert "free credits are used up, so this is paid" in paid
    free = ft._reason(ft.CreditStatus(1460, 3540, 5000))
    assert "40 free credits left, then paid" in free
    many = ft.question([FREE, WALLED], free)
    assert "these 2 pages" in many and f"- {WALLED}" in many and "get them" in many
    assert "Cost: 2 credits (1 a page)." in many


def test_reset_days_from_billing_period_end():
    from datetime import datetime, timezone

    from crawler import firecrawl_tier as ft

    end = ft._parse_time("2026-11-30T23:59:59Z")
    now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
    assert ft._reset_line(end, now) == "Credits reset in 20 days (2026-11-30)."
    status = ft.CreditStatus(4284, 716, 5000, period_end=end)
    assert "716 left.\nCredits reset in" in ft._reason(status)
    assert ft._reset_line(None) == ""
    assert ft._parse_time("garbage") is None
