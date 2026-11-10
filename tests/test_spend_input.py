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
    result = await _call(_handler("Spend on this page", seen), [FREE, WALLED])
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
    result = await _call(_handler("Spend on this page", seen), [FREE])
    assert seen == [] and "free page" in result.content[0].text
