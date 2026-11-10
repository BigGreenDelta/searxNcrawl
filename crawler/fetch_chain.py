"""Tiered page fetching: cheapest route first, paid route last.

    1. http      Scrapling HTTP fetch with a browser TLS fingerprint, text via
                 trafilatura. No browser, well under a second.
    2. browser   Crawl4AI + Chromium (the upstream searxNcrawl crawler), for
                 pages that need JavaScript.
    3. stealth   Scrapling StealthyFetcher (patched Chromium, Cloudflare
                 Turnstile/interstitial solving), for bot walls.
    4. firecrawl Paid, behind the spending guard in ``firecrawl_tier``.

A tier's result is accepted when it is not a challenge page and has at least
``FETCH_MIN_CHARS`` characters of Markdown. Successful results are cached.

Environment:
    FETCH_MIN_CHARS: shortest Markdown accepted as a real page (default 400)
    FETCH_TIERS: comma-separated tiers to use, in order (default all four)
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Iterable, List, Optional

from .document import CrawledDocument
from .fetch_cache import FetchCache
from . import firecrawl_tier

LOGGER = logging.getLogger(__name__)

ALL_TIERS = ("http", "browser", "stealth", "firecrawl")
FREE_TIERS = ("http", "browser", "stealth")
DEFAULT_MIN_CHARS = 400

# Phrases that only appear on bot-wall / challenge pages. Checked on short
# pages only, so an article that mentions Cloudflare is not mistaken for one.
_CHALLENGE_MARKERS = (
    "just a moment...",
    "checking your browser",
    "cf-browser-verification",
    "challenge-platform",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
    "please enable js and disable any ad blocker",
    "verify you are human",
    "are you a robot",
    "px-captcha",
    "captcha-delivery.com",
    "access denied",
    "request unsuccessful. incapsula",
    "pardon our interruption",
)
_CHALLENGE_SCAN_LIMIT = 6000
# Statuses that end the chain: the page does not exist, another tier won't help.
_FINAL_STATUSES = (404, 410)
_PDF_RE = re.compile(r"\.pdf($|\?)", re.IGNORECASE)

_cache: Optional[FetchCache] = None


def _get_cache() -> FetchCache:
    global _cache
    if _cache is None:
        _cache = FetchCache()
    return _cache


def min_chars() -> int:
    raw = os.getenv("FETCH_MIN_CHARS", "").strip()
    try:
        return max(0, int(raw)) if raw else DEFAULT_MIN_CHARS
    except ValueError:
        return DEFAULT_MIN_CHARS


def configured_tiers() -> List[str]:
    raw = os.getenv("FETCH_TIERS", "").strip()
    if not raw:
        return list(ALL_TIERS)
    tiers = [t.strip().lower() for t in raw.split(",") if t.strip()]
    return [t for t in tiers if t in ALL_TIERS]


def free_tiers() -> List[str]:
    """Configured tiers without the paid one, for fetches nobody asked for."""
    return [t for t in configured_tiers() if t != "firecrawl"]


def looks_blocked(text: str, html: str = "") -> bool:
    """True for challenge / bot-wall pages."""
    sample = (html or text)[:_CHALLENGE_SCAN_LIMIT].lower()
    if len(text) > _CHALLENGE_SCAN_LIMIT:
        return False
    return any(marker in sample for marker in _CHALLENGE_MARKERS)


def _verdict(doc: CrawledDocument, html: str = "") -> Optional[str]:
    """None when ``doc`` is acceptable, otherwise why it is not."""
    if doc.status == "failed":
        return doc.error_message or "failed"
    text = doc.markdown or ""
    if looks_blocked(text, html):
        return "challenge page"
    if len(text.strip()) < min_chars():
        return f"thin page ({len(text.strip())} chars)"
    return None


def _html_to_markdown(html: str, url: str) -> str:
    import trafilatura

    extracted = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_tables=True,
        include_images=False,
        favor_recall=True,
    )
    return extracted or ""


def _doc_from_scrapling(url: str, resp, tier: str) -> tuple[CrawledDocument, str]:
    status = getattr(resp, "status", None)
    html = ""
    try:
        html = resp.html_content or ""
    except Exception:
        html = ""
    final_url = str(getattr(resp, "url", None) or url)
    markdown = _html_to_markdown(html, final_url) if html else ""
    if not markdown:
        try:
            markdown = resp.markdown or ""
        except Exception:
            markdown = ""
    meta = {"fetched_via": tier, "status_code": status}
    if status is not None and status >= 400:
        return (
            CrawledDocument(
                request_url=url,
                final_url=final_url,
                status="failed",
                markdown=markdown,
                error_message=f"HTTP {status}",
                metadata=meta,
            ),
            html,
        )
    return (
        CrawledDocument(
            request_url=url,
            final_url=final_url,
            status="success",
            markdown=markdown,
            metadata=meta,
        ),
        html,
    )


async def _tier_http(url: str, timeout: int) -> tuple[CrawledDocument, str]:
    from scrapling.fetchers import AsyncFetcher

    resp = await AsyncFetcher.get(
        url,
        impersonate="chrome",
        stealthy_headers=True,
        follow_redirects=True,
        timeout=timeout,
        retries=1,
    )
    content_type = ""
    try:
        content_type = (resp.headers or {}).get("content-type", "")
    except Exception:
        pass
    if "pdf" in content_type.lower():
        return (
            CrawledDocument(
                request_url=url,
                final_url=url,
                status="failed",
                markdown="",
                error_message="PDF: download it and convert locally",
                metadata={"fetched_via": "http", "content_type": content_type},
            ),
            "",
        )
    return _doc_from_scrapling(url, resp, "http")


async def _tier_browser(url: str, timeout: int) -> tuple[CrawledDocument, str]:
    from . import crawl_page_async

    doc = await crawl_page_async(url, timeout=timeout)
    doc.metadata = {**(doc.metadata or {}), "fetched_via": "browser"}
    return doc, getattr(doc, "html", None) or ""


async def _tier_stealth(url: str, timeout: int) -> tuple[CrawledDocument, str]:
    from scrapling.fetchers import StealthyFetcher

    resp = await StealthyFetcher.async_fetch(
        url,
        headless=True,
        solve_cloudflare=True,
        network_idle=True,
        block_webrtc=True,
        timeout=timeout * 1000,
    )
    return _doc_from_scrapling(url, resp, "stealth")


_RUNNERS = {"http": _tier_http, "browser": _tier_browser, "stealth": _tier_stealth}


async def fetch(
    url: str,
    *,
    approve: Optional[firecrawl_tier.Approver] = None,
    tiers: Optional[Iterable[str]] = None,
    timeout: int = 20,
    fresh: bool = False,
) -> CrawledDocument:
    """Fetch ``url`` through the tiers until one returns a real page."""
    cache = _get_cache()
    if not fresh:
        cached = cache.get(url)
        if cached is not None:
            cached.metadata["from_cache"] = True
            return cached

    order = [t for t in (tiers or configured_tiers()) if t in ALL_TIERS]
    attempts: list[str] = []
    best: Optional[CrawledDocument] = None

    if _PDF_RE.search(url):
        order = [t for t in order if t == "firecrawl"]
        attempts.append("free tiers: PDF, download it and convert locally")

    blocked_seen = False
    for tier in order:
        # A short but real page (no challenge, no error) is not worth a stealth
        # browser or paid credits: those tiers exist for bot walls and failures.
        if tier == "stealth" and best is not None and not blocked_seen:
            attempts.append("stealth: skipped, page is short but not blocked")
            continue
        if tier == "firecrawl" and best is not None:
            attempts.append("firecrawl: skipped, a free tier returned the page")
            continue
        start = time.monotonic()
        try:
            if tier == "firecrawl":
                doc = await firecrawl_tier.scrape(url, approve, timeout=max(timeout, 60))
                html = ""
            else:
                tier_timeout = timeout * 3 if tier == "stealth" else timeout
                doc, html = await _RUNNERS[tier](url, tier_timeout)
        except Exception as exc:
            attempts.append(f"{tier}: {type(exc).__name__}: {exc}"[:300])
            blocked_seen = True
            continue
        problem = _verdict(doc, html)
        elapsed = time.monotonic() - start
        if problem is None:
            LOGGER.info("Fetched %s via %s in %.1fs", url, tier, elapsed)
            doc.metadata["tiers_tried"] = attempts + [f"{tier}: ok"]
            doc.request_url = url
            cache.put(doc)
            return doc
        LOGGER.info("Tier %s rejected %s in %.1fs: %s", tier, url, elapsed, problem)
        attempts.append(f"{tier}: {problem}")
        if doc.metadata.get("status_code") in _FINAL_STATUSES:
            # A real "not found" is an answer, not a bot wall: stop here.
            doc.metadata["tiers_tried"] = attempts
            doc.status = "failed"
            doc.markdown = ""
            return doc
        if doc.metadata.get("blocked"):
            return CrawledDocument(
                request_url=url,
                final_url=url,
                status="failed",
                markdown="",
                error_message=doc.error_message,
                metadata={**doc.metadata, "tiers_tried": attempts},
            )
        if problem == "challenge page" or doc.status == "failed":
            blocked_seen = True
            continue
        if doc.markdown and (best is None or len(doc.markdown) > len(best.markdown)):
            best = doc

    if best is not None:
        # Nothing passed the length check, but a short page beats no page.
        best.metadata["tiers_tried"] = attempts
        best.metadata["warning"] = "short page: no tier returned more than this"
        best.request_url = url
        cache.put(best)
        return best

    return CrawledDocument(
        request_url=url,
        final_url=url,
        status="failed",
        markdown="",
        error_message="; ".join(attempts) or "no fetch tier configured",
        metadata={"tiers_tried": attempts},
    )


async def fetch_many(
    urls: List[str],
    *,
    approve: Optional[firecrawl_tier.Approver] = None,
    tiers: Optional[Iterable[str]] = None,
    concurrency: int = 3,
    timeout: int = 20,
    fresh: bool = False,
) -> List[CrawledDocument]:
    import asyncio

    semaphore = asyncio.Semaphore(max(1, concurrency))
    tier_list = list(tiers) if tiers is not None else None

    async def one(u: str) -> CrawledDocument:
        async with semaphore:
            return await fetch(u, approve=approve, tiers=tier_list, timeout=timeout, fresh=fresh)

    return list(await asyncio.gather(*(one(u) for u in urls)))
