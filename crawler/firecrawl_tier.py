"""Firecrawl as the last, paid fetch tier, behind a spending guard.

Firecrawl is only reached when every free tier failed. Before each call the
guard reads the team's credit usage. Once this billing period has used
``FIRECRAWL_ASK_AT`` credits, the call needs a human yes: the MCP server asks
the user through MCP elicitation, so the calling model cannot approve it.
Anything unclear (usage API down, no elicitation support) counts as "no".

Environment:
    FIRECRAWL_API_KEY: enables the tier (no key, no tier)
    FIRECRAWL_API_URL: API base (default https://api.firecrawl.dev/v2)
    FIRECRAWL_ASK_AT: credits used this period after which every call asks (default 1450)
    FIRECRAWL_APPROVAL_MINUTES: how long "yes for a while" lasts (default 30)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Optional

import httpx

from .document import CrawledDocument

LOGGER = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.firecrawl.dev/v2"
DEFAULT_ASK_AT = 1450
DEFAULT_APPROVAL_MINUTES = 30

Approval = Literal["once", "window", "no"]
Approver = Callable[[str], Awaitable[Approval]]

_approved_until = 0.0
# One prompt at a time: parallel fetches wait, then see a "window" approval.
_approval_lock = asyncio.Lock()


@dataclass(slots=True)
class CreditStatus:
    used: Optional[int]
    remaining: Optional[int]
    plan: Optional[int]
    error: Optional[str] = None


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        LOGGER.warning("Ignoring invalid %s=%r", name, raw)
        return default


def api_key() -> Optional[str]:
    return os.getenv("FIRECRAWL_API_KEY", "").strip() or None


def enabled() -> bool:
    return api_key() is not None


def _api_url() -> str:
    return (os.getenv("FIRECRAWL_API_URL", "").strip() or DEFAULT_API_URL).rstrip("/")


def ask_at() -> int:
    return _int_env("FIRECRAWL_ASK_AT", DEFAULT_ASK_AT)


def reset_approval() -> None:
    global _approved_until
    _approved_until = 0.0


async def credit_status(client: httpx.AsyncClient) -> CreditStatus:
    """Credits used this billing period, as plan minus remaining."""
    try:
        resp = await client.get(
            f"{_api_url()}/team/credit-usage",
            headers={"Authorization": f"Bearer {api_key()}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        remaining = data.get("remainingCredits")
        plan = data.get("planCredits")
        if remaining is None or plan is None:
            return CreditStatus(None, remaining, plan, "usage response has no credit figures")
        return CreditStatus(max(0, int(plan) - int(remaining)), int(remaining), int(plan))
    except Exception as exc:
        return CreditStatus(None, None, None, f"credit usage check failed: {exc}")


def _failed(url: str, message: str, **meta) -> CrawledDocument:
    return CrawledDocument(
        request_url=url,
        final_url=url,
        status="failed",
        markdown="",
        error_message=message,
        metadata={"fetched_via": "firecrawl", **meta},
    )


def _needs_ok(status: CreditStatus) -> bool:
    if status.used is None:
        return True
    return status.used >= ask_at()


async def scrape(
    url: str,
    approve: Optional[Approver],
    *,
    timeout: int = 60,
    client: Optional[httpx.AsyncClient] = None,
) -> CrawledDocument:
    """Scrape ``url`` with Firecrawl, asking for approval past the threshold."""
    global _approved_until
    if not enabled():
        return _failed(url, "Firecrawl tier disabled (FIRECRAWL_API_KEY not set)")

    own_client = client is None
    client = client or httpx.AsyncClient()
    try:
        async with _approval_lock:
            status = await credit_status(client)
            if _needs_ok(status) and time.time() >= _approved_until:
                if status.used is None:
                    why = f"Could not read Firecrawl credit usage ({status.error})."
                else:
                    why = (
                        f"Firecrawl has used {status.used} credits this billing period "
                        f"(asks from {ask_at()}; {status.remaining} left)."
                    )
                prompt = (
                    f"{why} Free fetch tiers could not read {url}. "
                    "Spend Firecrawl credits on it?"
                )
                answer: Approval = "no"
                if approve is not None:
                    try:
                        answer = await approve(prompt)
                    except Exception as exc:
                        LOGGER.warning("Firecrawl approval prompt failed: %s", exc)
                        answer = "no"
                if answer == "no":
                    return _failed(
                        url,
                        "Blocked: Firecrawl would spend credits and the user did not approve",
                        credits_used_this_period=status.used,
                        blocked=True,
                    )
                if answer == "window":
                    minutes = _int_env(
                        "FIRECRAWL_APPROVAL_MINUTES", DEFAULT_APPROVAL_MINUTES
                    )
                    _approved_until = time.time() + minutes * 60

        resp = await client.post(
            f"{_api_url()}/scrape",
            headers={"Authorization": f"Bearer {api_key()}"},
            json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
            timeout=timeout,
        )
        body = resp.json()
        if resp.status_code != 200 or not body.get("success"):
            return _failed(url, f"Firecrawl HTTP {resp.status_code}: {body.get('error')}")
        data = body.get("data") or {}
        meta = data.get("metadata") or {}
        markdown = data.get("markdown") or ""
        if not markdown.strip():
            return _failed(url, "Firecrawl returned no content")
        return CrawledDocument(
            request_url=url,
            final_url=meta.get("url") or meta.get("sourceURL") or url,
            status="success",
            markdown=markdown,
            metadata={
                "fetched_via": "firecrawl",
                "title": meta.get("title"),
                "status_code": meta.get("statusCode"),
                "credits_used": meta.get("creditsUsed"),
            },
        )
    except Exception as exc:
        return _failed(url, f"Firecrawl request failed: {exc}")
    finally:
        if own_client:
            await client.aclose()
