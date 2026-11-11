"""MCP Server for the standalone crawler + multi-provider web search.

Provides tools for:
- Crawling web pages and extracting markdown content
- Searching the web via the configured providers (SearXNG, You.com),
  queried in parallel and merged into one attributed result list

Supports both STDIO and HTTP transports.

Usage:
    # STDIO (for Claude Desktop, etc.)
    python -m crawler.mcp_server

    # HTTP (for remote access)
    python -m crawler.mcp_server --transport http --port 8000

    # HTTP with CORS enabled for specific origins
    python -m crawler.mcp_server --transport http --cors-origins "http://localhost:3000"

    # Or via FastMCP CLI
    fastmcp run crawler/mcp_server.py:mcp --transport http --port 8000

Environment Variables:
    SEARXNG_URL: SearXNG instance URL; enables SearXNG (no default)
    SEARXNG_USERNAME: Optional basic auth username (set both or neither)
    SEARXNG_PASSWORD: Optional basic auth password (set both or neither)
    SEARXNG_EXTRA_HEADERS: JSON object of extra SearXNG headers; supports @/absolute or @~/ secret files
    YDC_API_KEY: You.com API key; enables You.com (queries go to You.com, US)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, List, Optional

import mcp.types as mcp_types
from fastmcp import Context, FastMCP
from fastmcp.server.elicitation import AcceptedElicitation, parse_elicit_response_type

from . import fetch_chain, firecrawl_tier, search_orchestrator
from .document import CrawledDocument
from .env import load_config
from .markdown_dedup import demote_headings
from .search_orchestrator import (
    SearchProviderError,
    configured_providers,
    crawl_result_pages,
    serialize_search_results,
)

# Load .env with config-dir fallback before reading environment variables
load_config()


def _resolve_log_level() -> tuple[str, int]:
    """Resolve LOG_LEVEL env var to a validated (label, int) pair."""
    raw_level = os.getenv("LOG_LEVEL", "INFO")
    if not raw_level:
        return "info", logging.INFO

    label = raw_level.strip().lower()
    level_int = getattr(logging, label.upper(), None)
    if isinstance(level_int, int):
        return label, level_int

    # Unknown value — fall back to INFO for both label and int
    return "info", logging.INFO


LOG_LEVEL_STR, LOG_LEVEL_INT = _resolve_log_level()

# Configure logging
logging.basicConfig(
    level=LOG_LEVEL_INT,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
LOGGER = logging.getLogger(__name__)

# Create the MCP server
mcp = FastMCP(
    name="Web Crawler & Search",
    instructions="""
    A web crawler and search server that provides:

    1. Web Crawling Tools:
       - crawl: Crawl one or more URLs and get markdown content
       - crawl_site: Crawl an entire website with depth/page limits

    2. Web Search Tool:
       - search: Search the web via the configured providers (SearXNG, You.com),
         queried in parallel; results are merged and attributed per provider.
         include_content=true also returns the Markdown of each result page

    Output formats for crawl tools:
    - markdown: Clean concatenated markdown (default)
    - json: Full details including metadata and references
    """,
)


class OutputFormat(str, Enum):
    """Output format for crawl results."""

    markdown = "markdown"
    json = "json"


def _configure_stdio_encoding() -> None:
    """Best-effort UTF-8 encoding for stdio transport streams."""
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        # Best-effort only: do not block MCP startup if unsupported.
        pass


class BearerTokenMiddleware:
    """Pure ASGI check of ``Authorization: Bearer <token>`` on every HTTP request.

    Pure ASGI (not BaseHTTPMiddleware) so streamed MCP responses pass through
    untouched. Requests to ``/healthz`` skip the check.
    """

    def __init__(self, app, token: str):
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/healthz":
            return await self.app(scope, receive, send)
        import hmac

        supplied = dict(scope.get("headers") or []).get(b"authorization", b"")
        if hmac.compare_digest(supplied, self._expected):
            return await self.app(scope, receive, send)
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"unauthorized"})


def _parse_csv_allowlist(value: Optional[str]) -> Optional[List[str]]:
    """Normalize a comma-separated HTTP allowlist without adding defaults."""
    if value is None:
        return None

    entries = [entry.strip() for entry in value.split(",") if entry.strip()]
    return entries or None


def _format_timestamp() -> str:
    """Get current timestamp in ISO format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _strip_markdown_links(text: str) -> str:
    """Remove markdown links from text, keeping only the link text.

    Converts [text](url) to text and removes standalone URLs.
    """
    import re

    # Replace [text](url) with just text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Remove standalone URLs (http/https)
    text = re.sub(r"https?://\S+", "", text)
    # Clean up any double spaces left behind
    text = re.sub(r"  +", " ", text)
    return text


def _doc_to_dict(doc: CrawledDocument) -> dict:
    """Convert CrawledDocument to JSON-serializable dict."""
    return {
        "request_url": doc.request_url,
        "final_url": doc.final_url,
        "status": doc.status,
        "markdown": doc.markdown,
        "error_message": doc.error_message,
        "metadata": doc.metadata,
        "references": [
            {"index": ref.index, "href": ref.href, "label": ref.label}
            for ref in doc.references
        ],
    }


def _format_single_doc_markdown(doc: CrawledDocument) -> str:
    """Format a single document as markdown with header."""
    meta = doc.metadata or {}
    via = meta.get("fetched_via")
    if via and meta.get("from_cache"):
        via = f"{via}, cached {meta.get('cache_age_seconds', 0)}s ago"
    lines = [
        f"# {doc.final_url}",
        f"_Crawled: {_format_timestamp()}_" + (f" _via {via}_" if via else ""),
        "",
    ]
    if meta.get("warning"):
        lines.extend([f"_Warning: {meta['warning']}_", ""])

    if doc.status == "failed":
        lines.append(f"**Error:** {doc.error_message}")
        if meta.get("incomplete") and doc.markdown:
            lines.extend(
                ["", "**Partial text:**", "", demote_headings(doc.markdown, 1)]
            )
    else:
        lines.append(demote_headings(doc.markdown, 1))

    return "\n".join(lines)


def _format_multiple_docs_markdown(docs: List[CrawledDocument]) -> str:
    """Format multiple documents as concatenated markdown."""
    sections = []

    for i, doc in enumerate(docs):
        if i > 0:
            sections.append("\n---\n")
        sections.append(_format_single_doc_markdown(doc))

    return "\n".join(sections)


def _format_output(
    docs: List[CrawledDocument],
    output_format: OutputFormat,
    stats: Optional[dict] = None,
    remove_links: bool = False,
) -> str:
    """Format crawl results based on output format."""
    if output_format == OutputFormat.json:
        doc_dicts = [_doc_to_dict(doc) for doc in docs]
        if remove_links:
            for d in doc_dicts:
                if d.get("markdown"):
                    d["markdown"] = _strip_markdown_links(d["markdown"])
        result = {
            "crawled_at": _format_timestamp(),
            "documents": doc_dicts,
            "summary": {
                "total": len(docs),
                "successful": sum(1 for d in docs if d.status == "success"),
                "failed": sum(1 for d in docs if d.status == "failed"),
            },
        }
        if stats:
            result["stats"] = stats
        return json.dumps(result, indent=2, ensure_ascii=False)
    else:
        # Markdown format
        output = _format_multiple_docs_markdown(docs)
        if remove_links:
            output = _strip_markdown_links(output)
        return output


# =============================================================================
# CRAWL TOOLS
# =============================================================================


def _make_approver(ctx: Optional[Context]):
    """Ask the human (not the model) before Firecrawl spends paid credits."""
    if ctx is None:
        return None

    async def approve(prompt: str) -> str:
        choices = _approval_choices()
        result = await ctx.elicit(prompt, response_type=list(choices))
        if isinstance(result, AcceptedElicitation):
            return choices.get(str(result.data), "no")
        return "no"

    return approve


def _approval_choices() -> dict[str, str]:
    return {
        "Yes, just this time": "once",
        f"Yes, and stop asking for the next {firecrawl_tier.grant_credits()} credits": "grant",
        "No": "no",
    }


# MCP 2026-07-28 has no server-initiated requests, so ctx.elicit() cannot ask
# mid-call. There the tool returns an InputRequiredResult instead: the client
# shows the question to the user and repeats the call with the answer in
# input_responses (SEP-2322). The answer comes from the client, not from tool
# arguments, so the model cannot approve spending on its own.
_SPEND_INPUT = "firecrawl_spend"


def _modern(ctx: Optional[Context]) -> bool:
    return ctx is not None and ctx._is_modern_protocol()


def _spend_answer(ctx: Context) -> Optional[str]:
    """The user's answer from a previous round, or None if not asked yet."""
    responses = ctx.input_responses or {}
    result = responses.get(_SPEND_INPUT)
    if result is None:
        return None
    if getattr(result, "action", None) != "accept":
        return "no"
    value = (getattr(result, "content", None) or {}).get("value")
    return _approval_choices().get(str(value), "no")


def _spend_request(blocked: List[CrawledDocument]) -> mcp_types.InputRequiredResult:
    urls = [d.request_url for d in blocked]
    why = blocked[0].metadata.get("approval_reason") or "Firecrawl would spend credits."
    message = firecrawl_tier.question(urls, why)
    schema = parse_elicit_response_type(list(_approval_choices())).schema
    return mcp_types.InputRequiredResult(
        input_requests={
            _SPEND_INPUT: mcp_types.ElicitRequest(
                params=mcp_types.ElicitRequestFormParams(
                    message=message, requested_schema=schema
                )
            )
        },
        request_state=json.dumps(urls),
    )


async def _deny(prompt: str) -> str:
    return "no"


def _answer_approver(answer: str):
    """Replays the user's answer: "grant" opens one grant, later asks are no."""
    used = {"grant": False}

    async def approve(prompt: str) -> str:
        if answer == "grant":
            if used["grant"]:
                return "no"
            used["grant"] = True
        return answer

    return approve


async def _fetch_with_spend_input(
    ctx: Context, urls: List[str], *, concurrency: int, timeout: int, fresh: bool
):
    """crawl's fetch on 2026-07-28: ask via InputRequiredResult when needed."""
    answer = _spend_answer(ctx)
    if answer is None:
        docs = await fetch_chain.fetch_many(
            urls, approve=_deny, concurrency=concurrency, timeout=timeout, fresh=fresh
        )
        needs_ok = [d for d in docs if d.metadata.get("approval_reason")]
        return _spend_request(needs_ok) if needs_ok else docs

    try:
        asked = set(json.loads(ctx.request_state or "[]"))
    except ValueError:
        asked = set()
    paid = [u for u in urls if u in asked]
    rest = [u for u in urls if u not in asked]
    # Pages that needed no approval last round come from the cache; the asked
    # ones already failed every free tier, so they go straight to Firecrawl.
    rest_docs = await fetch_chain.fetch_many(
        rest, approve=_deny, concurrency=concurrency, timeout=timeout, fresh=fresh
    )
    paid_docs = await fetch_chain.fetch_many(
        paid,
        approve=_answer_approver(answer),
        tiers=["firecrawl"],
        concurrency=concurrency,
        timeout=timeout,
        fresh=True,
    )
    by_url = dict(zip(rest, rest_docs)) | dict(zip(paid, paid_docs))
    return [by_url[u] for u in urls]


@mcp.tool
async def crawl(
    urls: List[str],
    output_format: str = "markdown",
    concurrency: int = 3,
    timeout: int = 15,
    remove_links: bool = False,
    dedup_mode: str = "exact",
    storage_state: Optional[str] = None,
    fresh: bool = False,
    ctx: Optional[Context] = None,
):
    """
    Fetch one or more URLs as markdown or JSON. This is the tool for reading any web page.

    Each URL goes through tiers, cheapest first, until one returns a real page:
    http (fast, no browser) -> browser (Chromium, JavaScript) -> stealth
    (anti-bot browser, Cloudflare) -> firecrawl (paid; past the free credit
    allowance the user is asked to approve the spend). Pages are cached, so
    asking for the same URL again is free. A "Blocked:" error means the user
    declined the paid tier: do not retry the same URL. For reddit.com use
    reddit_posts / reddit_thread instead: Reddit blocks plain page fetches.

    Args:
        urls: List of URLs to fetch (required). Accepts a single URL or multiple URLs.
        output_format: 'markdown' (default) or 'json'
            - markdown: Clean concatenated markdown with URL headers and timestamps
            - json: Full JSON with metadata (including fetched_via and tiers_tried)
        concurrency: Maximum concurrent fetches (default: 3)
        timeout: Per-URL, per-tier timeout in seconds (default: 15, must be >= 1)
        remove_links: Remove all links from the markdown output (default: false)
        dedup_mode: Markdown dedup mode - "exact" (default) or "off"
            (applies to the browser tier and authenticated crawls)
        storage_state: Path to Playwright storage_state JSON for authenticated
            crawling; skips the tiers and uses the browser with that session
        fresh: Ignore the cache and fetch again (default: false)

    Returns:
        Fetched content in the specified format.

    Examples:
        # Single page
        crawl(urls=["https://docs.example.com"])

        # Multiple pages
        crawl(urls=["https://example.com/page1", "https://example.com/page2"])

        # With JSON output
        crawl(urls=["https://example.com"], output_format="json")

        # Clean output without links
        crawl(urls=["https://example.com"], remove_links=True)
    """
    from . import crawl_page_async, crawl_pages_async

    if timeout < 1:
        raise ValueError("timeout must be >= 1")

    # Validate output format
    try:
        fmt = OutputFormat(output_format.lower())
    except ValueError:
        fmt = OutputFormat.markdown

    LOGGER.info("Crawling %d URL(s)...", len(urls))
    auth = {"storage_state": storage_state} if storage_state else None

    if auth is None and _modern(ctx):
        result = await _fetch_with_spend_input(
            ctx, urls, concurrency=concurrency, timeout=timeout, fresh=fresh
        )
        if isinstance(result, mcp_types.InputRequiredResult):
            LOGGER.info("Asking the user before Firecrawl spends credits")
            return result
        docs = result
    elif auth is None:
        docs = await fetch_chain.fetch_many(
            urls,
            approve=_make_approver(ctx),
            concurrency=concurrency,
            timeout=timeout,
            fresh=fresh,
        )
    elif len(urls) == 1:
        try:
            doc = await crawl_page_async(
                urls[0], dedup_mode=dedup_mode, auth=auth, timeout=timeout
            )
            docs = [doc]
        except Exception as exc:
            docs = [
                CrawledDocument(
                    request_url=urls[0],
                    final_url=urls[0],
                    status="failed",
                    markdown="",
                    error_message=str(exc),
                )
            ]
    else:
        docs = await crawl_pages_async(
            urls,
            concurrency=concurrency,
            dedup_mode=dedup_mode,
            auth=auth,
            timeout=timeout,
        )

    successful = sum(1 for d in docs if d.status == "success")
    LOGGER.info("Completed: %d/%d successful", successful, len(docs))

    return _format_output(docs, fmt, remove_links=remove_links)


@mcp.tool
async def crawl_site(
    url: str,
    max_depth: int = 2,
    max_pages: int = 25,
    include_subdomains: bool = False,
    output_format: str = "markdown",
    timeout: int = 120,
    remove_links: bool = False,
    dedup_mode: str = "exact",
    storage_state: Optional[str] = None,
):
    """
    Crawl an entire website starting from a seed URL using DFS strategy.

    Args:
        url: The seed URL to start crawling from
        max_depth: Maximum depth to crawl (default: 2, 0 = seed page only)
        max_pages: Maximum number of pages to crawl (default: 25)
        include_subdomains: Whether to include subdomains in the crawl (default: false)
        output_format: Output format - "markdown" (default) or "json"
            - markdown: Clean concatenated markdown with URL headers and timestamps
            - json: Full JSON with metadata, references, and crawl statistics
        timeout: Overall site crawl timeout in seconds (default: 120, must be >= 1)
        remove_links: Remove all links from the markdown output (default: false)
        dedup_mode: Markdown dedup mode - "exact" (default) or "off"
        storage_state: Path to Playwright storage_state JSON for authenticated crawling

    Returns:
        Crawled content from all pages in the specified format.

    Examples:
        # Basic site crawl
        crawl_site(url="https://docs.example.com")

        # Deep crawl with more pages
        crawl_site(url="https://docs.example.com", max_depth=3, max_pages=50)

        # Include subdomains
        crawl_site(url="https://example.com", include_subdomains=True)

        # JSON output with stats
        crawl_site(url="https://docs.example.com", output_format="json")

        # Clean output without links
        crawl_site(url="https://docs.example.com", remove_links=True)
    """
    from . import crawl_site_async

    if timeout < 1:
        raise ValueError("timeout must be >= 1")

    # Validate output format
    try:
        fmt = OutputFormat(output_format.lower())
    except ValueError:
        fmt = OutputFormat.markdown

    LOGGER.info(
        "Starting site crawl: %s (max_depth=%d, max_pages=%d)",
        url,
        max_depth,
        max_pages,
    )

    try:
        result = await crawl_site_async(
            url,
            max_depth=max_depth,
            max_pages=max_pages,
            include_subdomains=include_subdomains,
            dedup_mode=dedup_mode,
            auth={"storage_state": storage_state} if storage_state else None,
            timeout=timeout,
        )
    except TimeoutError:
        timeout_doc = CrawledDocument(
            request_url=url,
            final_url=url,
            status="failed",
            markdown="",
            error_message="Site crawl timed out",
        )
        timeout_stats = {
            "total_pages": 1,
            "successful_pages": 0,
            "failed_pages": 1,
            "error_count": 1,
        }
        return _format_output(
            [timeout_doc], fmt, stats=timeout_stats, remove_links=remove_links
        )

    LOGGER.info(
        "Site crawl complete: %d pages (%d successful, %d failed)",
        result.stats.get("total_pages", 0),
        result.stats.get("successful_pages", 0),
        result.stats.get("failed_pages", 0),
    )

    return _format_output(
        result.documents, fmt, stats=result.stats, remove_links=remove_links
    )


# =============================================================================
# SEARCH TOOL
# =============================================================================


@mcp.tool
async def search(
    query: str,
    language: str = "en",
    time_range: Optional[str] = None,
    categories: Optional[List[str]] = None,
    engines: Optional[List[str]] = None,
    safesearch: int = 1,
    pageno: int = 1,
    max_results: int = 10,
    max_retries: int = 3,
    providers: Optional[List[str]] = None,
    include_content: bool = False,
):
    """
    Search the web via the configured search providers (SearXNG, You.com).

    All configured providers are queried in parallel. Their results are merged
    (URL dedup, round-robin, results found by several providers first) and each
    result lists the providers that returned it.

    Args:
        query: Search query string (required)
        language: Language code for results (e.g., 'en', 'de', 'fr'). Default: 'en'
        time_range: Time range filter - 'day', 'week', 'month', or 'year'. Default: None (no filter)
        categories: SearXNG only. Categories to search (e.g., ['general', 'images', 'news']). Default: None (all)
        engines: SearXNG only. Specific search engines to use. Default: None (all available)
        safesearch: Safe search level - 0 (off), 1 (moderate), 2 (strict). Default: 1
        pageno: Page number for results (minimum 1). Default: 1
        max_results: Maximum results to return (1-50). Default: 10
        max_retries: Maximum attempts per provider for transient request errors (default: 3)
        providers: Subset of configured providers to query ('searxng', 'youcom').
            Default: None (all configured providers)
        include_content: Also crawl each result page and add its Markdown as
            "markdown" (or "crawl_error" if the page fails). Default: false

    Returns:
        JSON string {query, number_of_results, results}. Each result has
        title, url, content, publishedDate and providers, plus markdown or
        crawl_error with include_content.
        SEARCH_RESULT_FIELDS (comma-separated) limits the fields per result;
        unset/empty returns all of them.
        If some providers fail, the results of the others are returned. If all
        selected providers fail (or none is configured), the JSON contains
        "error" with a short reason per provider, and "query".

    Examples:
        # Basic search
        search(query="python tutorials")

        # Search with time filter
        search(query="latest AI news", time_range="week")

        # Only one provider
        search(query="python tutorials", providers=["searxng"])

        # Search in German
        search(query="Rezepte", language="de")

        # Results together with the Markdown of each result page
        search(query="crawl4ai release notes", max_results=3, include_content=True)
    """
    LOGGER.info("Searching for: %s", query)
    start = time.monotonic()

    try:
        results = await search_orchestrator.search(
            query,
            language=language,
            time_range=time_range,
            categories=categories,
            engines=engines,
            safesearch=safesearch,
            pageno=pageno,
            max_results=max_results,
            max_retries=max_retries,
            providers=providers,
        )
        documents = await crawl_result_pages(results) if include_content else None
        data = serialize_search_results(query, results, documents)

        elapsed = time.monotonic() - start
        LOGGER.info(
            "Search returned %d results in %.1fs",
            data["number_of_results"],
            elapsed,
        )

        return json.dumps(data, indent=2, ensure_ascii=False)

    except SearchProviderError as exc:
        elapsed = time.monotonic() - start
        LOGGER.error("Search failed in %.1fs: %s", elapsed, exc)
        return json.dumps({"error": str(exc), "query": query}, ensure_ascii=False)

    except Exception as exc:
        elapsed = time.monotonic() - start
        error_msg = f"Unexpected error: {str(exc)}"
        LOGGER.error("Search failed in %.1fs: %s", elapsed, error_msg)
        return json.dumps({"error": error_msg, "query": query}, ensure_ascii=False)


# =============================================================================
# REDDIT TOOLS
# =============================================================================


@mcp.tool
async def reddit_posts(
    subreddit: Optional[str] = None,
    sort: str = "hot",
    time: str = "week",
    limit: int = 25,
    query: Optional[str] = None,
    author: Optional[str] = None,
):
    """
    List Reddit posts. Use this, not crawl, for anything on reddit.com.

    Without query: a live subreddit listing from Reddit RSS (hot, new, top,
    rising, controversial; time applies to top/controversial). With query
    and/or author: keyword search in the Arctic Shift archive, newest first.
    Each post shows its id; pass the id or URL to reddit_thread for comments.

    Args:
        subreddit: Subreddit name without r/ (e.g. "selfhosted")
        sort: hot (default), new, top, rising or controversial
        time: hour, day, week (default), month, year or all
        limit: Number of posts, 1-100 (default 25)
        query: Keywords to search in titles and text (archive search)
        author: Only posts by this user (archive search)
    """
    from . import reddit

    try:
        return await reddit.posts(
            subreddit, sort=sort, time=time, limit=limit, query=query, author=author
        )
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool
async def reddit_thread(
    post: str,
    comment_limit: int = 200,
    max_depth: int = 6,
    max_comment_chars: int = 1500,
):
    """
    Read a Reddit post with its comment tree (best comments first, nested).

    Source: the Arctic Shift archive, because Reddit blocks logged-out JSON.
    Scores and counts settle about 36 h after posting; very fresh comments can
    be missing. Collapsed branches say how many replies were not loaded.

    Args:
        post: Post URL (any reddit.com/.../comments/<id>/... form) or post id
        comment_limit: Maximum comments to load (default 200, up to 25000)
        max_depth: Reply depth to expand before collapsing (default 6)
        max_comment_chars: Truncate longer comments to this many characters (default 1500)
    """
    from . import reddit

    try:
        return await reddit.thread(
            post,
            comment_limit=comment_limit,
            max_depth=max_depth,
            max_comment_chars=max_comment_chars,
        )
    except Exception as exc:
        return f"Error: {exc}"


# =============================================================================
# CLI ENTRY POINT
# =============================================================================


def main():
    """CLI entry point for running the MCP server."""
    if sys.argv[1:2] == ["setup"]:
        from .setup import main as setup_main

        sys.exit(setup_main(sys.argv[2:]))

    parser = argparse.ArgumentParser(
        description="Run the web crawler & search MCP server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment Variables:
    SEARXNG_URL       SearXNG instance URL; enables SearXNG (no default)
    SEARXNG_USERNAME  Optional basic auth username (set both or neither)
    SEARXNG_PASSWORD  Optional basic auth password (set both or neither)
    SEARXNG_EXTRA_HEADERS  JSON extra SearXNG headers; supports @/absolute or @~/ secret files
    YDC_API_KEY       You.com API key; enables You.com (queries go to You.com, US)

Examples:
    # Register the server with your installed coding agents
    searxncrawl setup

    # STDIO transport (default, for Claude Desktop)
    python -m crawler.mcp_server

    # HTTP transport (for remote access)
    python -m crawler.mcp_server --transport http --port 8000

    # Custom host/port
    python -m crawler.mcp_server --transport http --host 0.0.0.0 --port 9000

    # Enable CORS for specific origins (required for browser-based MCP clients)
    python -m crawler.mcp_server --transport http --cors-origins "http://localhost:3000,https://myapp.com"

    # Enable CORS for all origins (use with caution)
    python -m crawler.mcp_server --transport http --cors-origins "*"

    # With a SearXNG instance as search provider
    SEARXNG_URL=https://search.example.com python -m crawler.mcp_server
""",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to for HTTP transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind to for HTTP transport (default: 8000)",
    )
    parser.add_argument(
        "--allowed-hosts",
        default=None,
        help=(
            "Comma-separated Host headers allowed for HTTP transport. "
            "This is separate from the bind address. "
            'Use "*" only to explicitly allow every Host.'
        ),
    )
    parser.add_argument(
        "--cors-origins",
        default=None,
        help=(
            "Comma-separated list of allowed CORS origins for HTTP transport. "
            'Use "*" to allow all origins. '
            "If not set, no CORS headers are sent. "
            'Example: --cors-origins "http://localhost:3000,https://myapp.com"'
        ),
    )

    args = parser.parse_args()

    # Log configuration
    try:
        from importlib.metadata import version as _pkg_version

        LOGGER.info("searxNcrawl v%s starting", _pkg_version("searxNcrawl"))
    except Exception:
        LOGGER.info("searxNcrawl starting")
    LOGGER.info(
        "Search providers: %s",
        ", ".join(configured_providers())
        or "none (set a valid http(s) SEARXNG_URL and/or YDC_API_KEY)",
    )

    if args.transport == "http":
        LOGGER.info("Starting MCP server on http://%s:%d/mcp", args.host, args.port)

        run_kwargs: dict[str, Any] = {
            "transport": "http",
            "host": args.host,
            "port": args.port,
        }

        allowed_hosts = _parse_csv_allowlist(args.allowed_hosts)
        if allowed_hosts is not None:
            LOGGER.info("Allowing HTTP Host headers: %s", allowed_hosts)
            run_kwargs["allowed_hosts"] = allowed_hosts

        from starlette.middleware import Middleware

        middleware: list = []
        token = os.getenv("MCP_BEARER_TOKEN", "").strip()
        if token:
            LOGGER.info("HTTP requests require the MCP_BEARER_TOKEN bearer token")
            middleware.append(Middleware(BearerTokenMiddleware, token=token))
        elif args.host not in ("127.0.0.1", "localhost", "::1"):
            LOGGER.error(
                "Refusing to listen on %s without MCP_BEARER_TOKEN: anyone on the "
                "network could fetch through this server and approve Firecrawl spending",
                args.host,
            )
            sys.exit(2)

        origins = _parse_csv_allowlist(args.cors_origins)
        if origins is not None:
            from starlette.middleware.cors import CORSMiddleware

            LOGGER.info("CORS enabled for origins: %s", origins)
            run_kwargs["allowed_origins"] = origins
            middleware.append(
                Middleware(
                    CORSMiddleware,
                    allow_origins=origins,
                    allow_credentials=True,
                    allow_methods=["*"],
                    allow_headers=["*"],
                ),
            )
        if middleware:
            run_kwargs["middleware"] = middleware

        run_kwargs["log_level"] = LOG_LEVEL_STR
        mcp.run(**run_kwargs)
    else:
        _configure_stdio_encoding()
        LOGGER.info("Starting MCP server with STDIO transport")
        mcp.run(transport="stdio", log_level=LOG_LEVEL_STR)


if __name__ == "__main__":
    main()
