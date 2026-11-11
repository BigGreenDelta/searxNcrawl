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
    FIRECRAWL_GRANT_CREDITS: how many more credits a "keep going" answer allows
        before the next prompt (default 100)
    FIRECRAWL_FREE_CREDITS: free credits per billing period, used only to tell
        the user whether a spend is paid (default 1500)

A "keep going" answer sets a ceiling of current usage plus the grant. Calls
stay silent until usage reaches that ceiling, then the next call asks again.
The grant is dropped when usage falls below where it was given (a new billing
period) and on server restart, so it can only err towards asking.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal, Optional

import httpx

from .document import CrawledDocument

LOGGER = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.firecrawl.dev/v2"
DEFAULT_ASK_AT = 1450
DEFAULT_GRANT_CREDITS = 100
DEFAULT_FREE_CREDITS = 1500

Approval = Literal["once", "grant", "no"]
Approver = Callable[[str], Awaitable[Approval]]

# Active "keep going" grant: (usage when given, usage ceiling it allows).
_grant: Optional[tuple[int, int]] = None
# One prompt at a time: parallel fetches wait, then see the new grant.
_approval_lock = asyncio.Lock()


@dataclass(slots=True)
class CreditStatus:
    used: Optional[int]
    remaining: Optional[int]
    plan: Optional[int]
    error: Optional[str] = None
    period_end: Optional[datetime] = None


def _parse_time(raw) -> Optional[datetime]:
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _reset_line(end: Optional[datetime], now: Optional[datetime] = None) -> str:
    if end is None:
        return ""
    now = now or datetime.now(timezone.utc)
    days = max(0, math.ceil((end - now).total_seconds() / 86400))
    when = "today" if days == 0 else f"in {days} day{'s' if days != 1 else ''}"
    return f" Credits reset {when} ({end:%Y-%m-%d})."


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


def grant_credits() -> int:
    return max(1, _int_env("FIRECRAWL_GRANT_CREDITS", DEFAULT_GRANT_CREDITS))


def free_credits() -> int:
    return _int_env("FIRECRAWL_FREE_CREDITS", DEFAULT_FREE_CREDITS)


def _reason(status: CreditStatus) -> str:
    """Why the user is asked: credit usage and whether the spend is paid."""
    if status.used is None:
        return f"Could not read Firecrawl credit usage ({status.error}), so asking to be safe."
    free = free_credits()
    plan = f" of {status.plan}" if status.plan else ""
    lines = [
        f"Credits this billing period: {status.used}{plan} used, {status.remaining} left."
        + _reset_line(status.period_end)
    ]
    if status.used >= free:
        lines.append(f"The {free} free credits are used up, so this is paid.")
    else:
        lines.append(f"{free - status.used} free credits left, then paid.")
    if _grant is not None:
        lines.append('Your last "stop asking" approval has run out.')
    return "\n".join(lines)


def question(urls: list[str], reason: str) -> str:
    """The spend question shown to the user."""
    if len(urls) == 1:
        head = f"Read {urls[0]} with Firecrawl?"
        it = "it"
    else:
        listed = "\n".join(f"- {u}" for u in urls)
        head = f"Read these {len(urls)} pages with Firecrawl?\n{listed}"
        it = "them"
    return (
        f"{head}\n"
        f"The free fetchers (plain HTTP, browser, stealth browser) could not get {it}.\n\n"
        f"{reason}\n"(
            "Cost: 1 credit."
            if len(urls) == 1
            else f"Cost: {len(urls)} credits (1 a page)."
        )
    )


def reset_approval() -> None:
    global _grant
    _grant = None


def _granted(status: CreditStatus) -> bool:
    """True while usage sits inside the active grant; drops a stale grant."""
    global _grant
    if _grant is None or status.used is None:
        return False
    start, ceiling = _grant
    if status.used < start:  # usage went down: new billing period
        _grant = None
        return False
    return status.used < ceiling


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
            return CreditStatus(
                None, remaining, plan, "usage response has no credit figures"
            )
        return CreditStatus(
            max(0, int(plan) - int(remaining)),
            int(remaining),
            int(plan),
            period_end=_parse_time(data.get("billingPeriodEnd")),
        )
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
    global _grant
    if not enabled():
        return _failed(url, "Firecrawl tier disabled (FIRECRAWL_API_KEY not set)")

    own_client = client is None
    client = client or httpx.AsyncClient()
    try:
        async with _approval_lock:
            status = await credit_status(client)
            granted = _granted(status)  # always run: it also drops a stale grant
            if _needs_ok(status) and not granted:
                why = _reason(status)
                prompt = question([url], why)
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
                        approval_reason=why,
                    )
                if answer == "grant" and status.used is not None:
                    _grant = (status.used, status.used + grant_credits())
                    LOGGER.info(
                        "Firecrawl grant: silent until %d credits used", _grant[1]
                    )

        resp = await client.post(
            f"{_api_url()}/scrape",
            headers={"Authorization": f"Bearer {api_key()}"},
            # proxy "auto" retries with Firecrawl's stealth proxy when the basic
            # one is blocked (Amazon UK needs it); stealth costs more credits.
            json={
                "url": url,
                "formats": ["markdown"],
                "onlyMainContent": True,
                "proxy": "auto",
            },
            timeout=timeout,
        )
        body = resp.json()
        if resp.status_code != 200 or not body.get("success"):
            return _failed(
                url, f"Firecrawl HTTP {resp.status_code}: {body.get('error')}"
            )
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
