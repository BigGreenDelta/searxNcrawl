"""Tiered page fetching: cheapest route first, paid route last.

    1. http      Scrapling HTTP fetch with a browser TLS fingerprint, text via
                 trafilatura. No browser, well under a second.
    2. browser   Crawl4AI + Chromium (the upstream searxNcrawl crawler), for
                 pages that need JavaScript.
    3. stealth   Scrapling StealthyFetcher (patched Chromium, Cloudflare
                 Turnstile/interstitial solving), for bot walls.
    4. vpn       Optional (``FETCH_VPN_PROXY``): http, then stealth, through a
                 VPN exit, only after a tier hit a block or challenge. A soft
                 block is usually on this IP, so a different one gets through.
    5. firecrawl Paid, behind the spending guard in ``firecrawl_tier``.

A tier's result is accepted when it is not a challenge page and has at least
``FETCH_MIN_CHARS`` characters of Markdown. Successful results are cached.

Site routes (``site_routes``) go first for the URLs they know, such as
Thingiverse thing pages. A route that fails hands over to the tiers.

An http result that is a script shell (a big HTML document whose readable text
is a tiny fraction of it, like a store search page that loads its listings with
JavaScript), whose body shows no text at all (an app root that only scripts
fill, the text coming from JSON-LD and meta tags), or whose extracted text is
a small share of the text the page shows (a category page reduced to its
intro paragraph), is kept only as a
fallback: the browser tier gets a go, and stealth too if the browser comes
back thin. If nothing beats it, it is returned with a warning.

Environment:
    FETCH_MIN_CHARS: shortest Markdown accepted as a real page (default 400)
    FETCH_TIERS: comma-separated tiers to use, in order (default all; vpn
        only when FETCH_VPN_PROXY is set)
    FETCH_VPN_PROXY: proxy URL of a VPN exit for the vpn tier, for example
        http://127.0.0.1:25345 from wireproxy (unset: no vpn tier)
    FETCH_SHELL_RATIO: text/HTML ratio under which a big http page counts as a
        script shell (default 0.01; 0 turns the check off)
    FETCH_MIN_COVERAGE: extracted/visible text share under which an http page
        counts as partial (default 0.25; 0 turns the check off)
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Iterable, List, Optional

from .config import PLACEHOLDER_PATTERNS, SETTLE_CAP_MS, TEMPLATE_PATTERN, PageWait
from .document import CrawledDocument
from .fetch_cache import FetchCache
from . import firecrawl_tier

LOGGER = logging.getLogger(__name__)

ALL_TIERS = ("http", "browser", "stealth", "vpn", "firecrawl")
FREE_TIERS = ("http", "browser", "stealth", "vpn")
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
    # AliExpress / Alibaba slider CAPTCHA, served after a burst of searches.
    "captcha verification",
    "drag the slider to verify",
    "slide to verify",
    "_____tmd_____/punish",
    # Amazon soft blocks after many fetches from one IP: a "continue shopping"
    # interstitial on product pages, a 503 page (served as HTTP 200) on search.
    "click the button below to continue shopping",
    "an error occurred when we tried to process your request",
    "503 - service unavailable error",
    # Login walls: the page exists but is not readable without an account.
    "log in to use old reddit",
    "log in to continue",
    "sign in to continue",
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


def vpn_proxy() -> Optional[str]:
    return os.getenv("FETCH_VPN_PROXY", "").strip() or None


def configured_tiers() -> List[str]:
    raw = os.getenv("FETCH_TIERS", "").strip()
    if not raw:
        tiers = list(ALL_TIERS)
    else:
        tiers = [t.strip().lower() for t in raw.split(",") if t.strip()]
    return [t for t in tiers if t in ALL_TIERS and (t != "vpn" or vpn_proxy())]


def free_tiers() -> List[str]:
    """Configured tiers without the paid one, for fetches nobody asked for."""
    return [t for t in configured_tiers() if t != "firecrawl"]


def looks_blocked(text: str, html: str = "") -> bool:
    """True for challenge / bot-wall pages."""
    if len(text) > _CHALLENGE_SCAN_LIMIT:
        return False
    # The head of the HTML holds the <title>; the text holds what a visitor
    # sees. A challenge page can bury its message far into a big HTML file.
    sample = f"{html[:_CHALLENGE_SCAN_LIMIT]}\n{text}".lower()
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


DEFAULT_SHELL_RATIO = 0.01
# Below this much HTML a low ratio is just a small page with some boilerplate.
_SHELL_MIN_HTML = 100_000
# This much text is a real page whatever the ratio: Amazon product pages carry
# 2.5 MB of HTML around 18 KB of perfectly good text.
_SHELL_MAX_TEXT = 4000


def shell_ratio() -> float:
    raw = os.getenv("FETCH_SHELL_RATIO", "").strip()
    try:
        return max(0.0, float(raw)) if raw else DEFAULT_SHELL_RATIO
    except ValueError:
        return DEFAULT_SHELL_RATIO


def script_shell(doc: CrawledDocument, html: str) -> Optional[str]:
    """Why an http page looks like a JavaScript shell, or None if it does not."""
    ratio = shell_ratio()
    if ratio <= 0 or len(html) < _SHELL_MIN_HTML:
        return None
    text = len((doc.markdown or "").strip())
    if text >= _SHELL_MAX_TEXT or text / len(html) >= ratio:
        return None
    return f"script shell ({text} chars of text in {len(html) // 1024} KB of HTML)"


DEFAULT_MIN_COVERAGE = 0.25
# Pages with less visible text than this are too small to judge coverage on.
_COVERAGE_MIN_VISIBLE = 2000


def min_coverage() -> float:
    raw = os.getenv("FETCH_MIN_COVERAGE", "").strip()
    try:
        return max(0.0, float(raw)) if raw else DEFAULT_MIN_COVERAGE
    except ValueError:
        return DEFAULT_MIN_COVERAGE


def _visible_chars(html: str) -> int:
    import lxml.html

    try:
        tree = lxml.html.fromstring(html)
    except Exception:
        return 0
    for node in tree.xpath("//script|//style|//noscript|//template|//svg"):
        node.drop_tree()
    return len(" ".join(tree.text_content().split()))


def partial_extraction(doc: CrawledDocument, html: str) -> Optional[str]:
    """Why the extracted text looks like a fraction of what the page shows.

    Text extraction keeps what it takes for the main article. On store
    category pages, contact pages and news front pages that is an intro
    paragraph or a cookie banner, while the product grid, prices or link
    lists are dropped. Articles come out at 0.9 or more of the visible text,
    Amazon product pages at 0.5; the broken pages were at 0.03-0.10.
    """
    floor = min_coverage()
    if floor <= 0 or not html or doc.metadata.get("product_listing"):
        return None
    visible = _visible_chars(html)
    if visible < _COVERAGE_MIN_VISIBLE:
        return None
    text = len((doc.markdown or "").strip())
    if text / visible >= floor:
        return None
    return f"partial text ({text} of {visible} visible chars)"


_CODE_FENCE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
_TEMPLATE = re.compile(TEMPLATE_PATTERN)
_MD_PREFIX = re.compile(r"^[\s>#*+\-|]*")
# Loading placeholders that count against a page's text, shared with the
# browser's settle condition. Unrendered templates are counted separately.
_PLACEHOLDER_LINES = [re.compile(p, re.IGNORECASE) for p in PLACEHOLDER_PATTERNS]
# A shell holds at least this many placeholder lines or templates...
_SHELL_MIN_MARKERS = 2
_SHELL_MIN_TEMPLATES = 3
# ...and less real text than this once they are taken out.
_SHELL_MAX_REAL_TEXT = 1500
# A rendered page with this little text in this much HTML has not shown its
# content yet (Royal Mail tracking: 832 chars in 210 KB). Stricter than the
# http check: a rendered DOM carries more markup than the served HTML.
_RENDERED_SHELL_RATIO = 0.005
_RENDERED_SHELL_MIN_HTML = 150_000


def _placeholder_line(line: str) -> bool:
    bare = _MD_PREFIX.sub("", line).strip().replace("\\", "")
    return any(p.search(bare) for p in _PLACEHOLDER_LINES)


def loading_shell(markdown: str) -> Optional[str]:
    """Why ``markdown`` looks like a page whose content has not loaded yet.

    Two signs, both only on pages with little other text: loading
    placeholders ("Loading...", "Checking the tracking number ...", lines of
    dots) and unrendered ``{{ template }}`` expressions. Code blocks are left
    out, so documentation that shows template syntax is not a shell.
    """
    text = _CODE_FENCE.sub(" ", markdown or "")
    templates = len(_TEMPLATE.findall(text.replace("\\", "")))
    text = _TEMPLATE.sub(" ", text.replace("\\", ""))
    lines = [line for line in text.splitlines() if line.strip()]
    markers = [line for line in lines if _placeholder_line(line)]
    real = sum(len(line.strip()) for line in lines if not _placeholder_line(line))
    if real >= _SHELL_MAX_REAL_TEXT:
        return None
    if templates >= _SHELL_MIN_TEMPLATES:
        return f"unrendered templates ({templates} {{{{ }}}} expressions)"
    if len(markers) >= _SHELL_MIN_MARKERS:
        return f"loading placeholders ({len(markers)} lines, {real} chars of other text)"
    return None


def rendered_shell(doc: CrawledDocument, html: str) -> Optional[str]:
    """Why a browser-rendered page looks like it has not loaded its content."""
    reason = loading_shell(doc.markdown or "")
    if reason or doc.metadata.get("product_listing"):
        return reason
    text = len((doc.markdown or "").strip())
    if (
        len(html) >= _RENDERED_SHELL_MIN_HTML
        and text < _SHELL_MAX_REAL_TEXT
        and text / len(html) < _RENDERED_SHELL_RATIO
    ):
        return f"script shell ({text} chars of text in {len(html) // 1024} KB of rendered HTML)"
    return None


# A page body that shows less text than this has not rendered its content.
_EMPTY_BODY_CHARS = 200


def _body_chars(html: str) -> Optional[int]:
    """Visible text in ``<body>``, or None when the HTML has no body."""
    import lxml.html

    try:
        tree = lxml.html.fromstring(html)
    except Exception:
        return None
    bodies = tree.xpath("//body")
    if not bodies:
        return None
    body = bodies[0]
    for node in body.xpath(".//script|.//style|.//noscript|.//template|.//svg"):
        node.drop_tree()
    return len(" ".join(body.text_content().split()))


def metadata_only(doc: CrawledDocument, html: str) -> Optional[str]:
    """Why an http page's text looks like it came only from its metadata.

    A JavaScript app (Thingiverse thing pages, Next.js and React sites) can
    serve an empty app root next to JSON-LD and meta tags. Text extraction
    then builds a page from the description in those tags and passes the
    length check, while everything the app renders is missing.
    """
    if not html:
        return None
    shown = _body_chars(html)
    if shown is None or shown >= _EMPTY_BODY_CHARS:
        return None
    return (
        f"empty app root ({shown} chars shown in <body>, text only from page metadata)"
    )


def needs_browser(doc: CrawledDocument, html: str) -> Optional[str]:
    """Why an accepted http page should still be rendered, or None."""
    return (
        loading_shell(doc.markdown or "")
        or script_shell(doc, html)
        or metadata_only(doc, html)
        or partial_extraction(doc, html)
    )


_HASH_ROUTE = re.compile(r"#!?/")


def hash_route(url: str) -> bool:
    """True for single-page-app routes such as ``/track#/results/X``.

    The part after ``#`` never reaches the server, so the http tier gets the
    bare app; only a browser renders the route.
    """
    _, _, fragment = url.partition("#")
    return bool(fragment) and _HASH_ROUTE.match("#" + fragment) is not None


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


def _content_type(resp) -> str:
    try:
        return str((resp.headers or {}).get("content-type", "")).lower()
    except Exception:
        return ""


def _raw_text(resp) -> str:
    body = getattr(resp, "body", b"")
    if isinstance(body, (bytes, bytearray)):
        encoding = getattr(resp, "encoding", None) or "utf-8"
        return bytes(body).decode(encoding, errors="replace")
    return str(body or "")


def _passthrough(resp) -> Optional[str]:
    """JSON, XML, RSS and plain text come back as they are, not as escaped Markdown."""
    ctype = _content_type(resp)
    if not any(t in ctype for t in ("json", "xml", "rss", "atom", "text/plain")):
        return None
    text = _raw_text(resp)
    if "json" in ctype:
        import json

        try:
            text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
        except ValueError:
            pass
    return text


def _doc_from_scrapling(url: str, resp, tier: str) -> tuple[CrawledDocument, str]:
    status = getattr(resp, "status", None)
    final_url = str(getattr(resp, "url", None) or url)
    raw = _passthrough(resp)
    if raw is not None:
        meta = {
            "fetched_via": tier,
            "status_code": status,
            "content_type": _content_type(resp),
        }
        failed = status is not None and status >= 400
        return (
            CrawledDocument(
                request_url=url,
                final_url=final_url,
                status="failed" if failed else "success",
                markdown=raw,
                error_message=f"HTTP {status}" if failed else None,
                metadata=meta,
            ),
            "",
        )
    html = ""
    try:
        html = resp.html_content or ""
    except Exception:
        html = ""
    markdown = _html_to_markdown(html, final_url) if html else ""
    if not markdown:
        # Some Scrapling responses carry ready Markdown; on others `markdown`
        # is a method, which must never end up as the page text.
        try:
            value = getattr(resp, "markdown", None)
        except Exception:
            value = None
        markdown = value if isinstance(value, str) else ""
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


async def _tier_http(
    url: str, timeout: int, proxy: Optional[str] = None
) -> tuple[CrawledDocument, str]:
    from scrapling.fetchers import AsyncFetcher

    extra = {"proxy": proxy} if proxy else {}
    resp = await AsyncFetcher.get(
        url,
        impersonate="chrome",
        stealthy_headers=True,
        follow_redirects=True,
        timeout=timeout,
        retries=1,
        **extra,
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


# Time a browser tier keeps after its settle deadline to scroll, capture the
# HTML and convert it, so the tier timeout does not throw the page away.
_CAPTURE_MARGIN_S = 6


def settle_cap_ms(timeout: float, wait: Optional[PageWait] = None) -> int:
    """How long a browser tier may wait for the page to settle, in ms."""
    wait_ms = wait.wait_ms if wait else 0
    budget = (timeout - _CAPTURE_MARGIN_S) * 1000
    return int(max(1000, min(SETTLE_CAP_MS + wait_ms, budget)))


def browser_timeout(timeout: int, wait: Optional[PageWait] = None) -> int:
    """The browser tier's timeout: room for two loads plus the settle wait."""
    return timeout * 2 + (wait.wait_ms // 1000 if wait else 0)


async def _tier_browser(
    url: str, timeout: int, wait: Optional[PageWait] = None
) -> tuple[CrawledDocument, str]:
    from . import crawl_page_async
    from .config import build_markdown_run_config

    deadline = int(time.time() * 1000) + settle_cap_ms(timeout, wait)
    config = build_markdown_run_config(wait=wait, deadline_epoch_ms=deadline)
    doc = await crawl_page_async(url, config=config, timeout=timeout)
    doc.metadata = {**(doc.metadata or {}), "fetched_via": "browser"}
    status = doc.metadata.get("status_code")
    if isinstance(status, int) and status >= 400:
        # The browser renders error pages too; an HTTP error is not content.
        doc.status = "failed"
        doc.error_message = f"HTTP {status}"
    return doc, getattr(doc, "html", None) or ""


def _settle_action(cap_ms: int, wait: Optional[PageWait]):
    """A Scrapling ``page_action`` that waits for the page to settle.

    Network idle alone returns before pages that fetch their results after a
    timer or a token check; this runs the browser tier's settle condition.
    A page that never settles is captured as it is once ``cap_ms`` is up.
    """
    from .config import build_content_wait_condition

    condition = build_content_wait_condition(wait, cap_ms=cap_ms)[len("js:") :]

    async def settle(page):
        try:
            await page.wait_for_function(condition, polling=100, timeout=cap_ms + 2000)
        except Exception as exc:  # noqa: BLE001 - capture the page as it is
            LOGGER.debug("Settle wait ended early: %s", exc)
        return page

    return settle


async def _tier_stealth(
    url: str,
    timeout: int,
    proxy: Optional[str] = None,
    wait: Optional[PageWait] = None,
) -> tuple[CrawledDocument, str]:
    from scrapling.fetchers import StealthyFetcher

    extra = {"proxy": proxy} if proxy else {}
    resp = await StealthyFetcher.async_fetch(
        url,
        headless=True,
        solve_cloudflare=True,
        network_idle=True,
        block_webrtc=True,
        timeout=timeout * 1000,
        page_action=_settle_action(settle_cap_ms(timeout, wait), wait),
        **extra,
    )
    return _doc_from_scrapling(url, resp, "stealth")


async def _tier_vpn(
    url: str, timeout: int, wait: Optional[PageWait] = None
) -> tuple[CrawledDocument, str]:
    """http through the VPN exit, then the stealth browser if that is not enough."""
    proxy = vpn_proxy()
    if not (wait and wait.explicit) and not hash_route(url):
        doc, html = await _tier_http(url, timeout, proxy)
        if _verdict(doc, html) is None and needs_browser(doc, html) is None:
            doc.metadata["fetched_via"] = "vpn-http"
            return doc, html
    extra = {"wait": wait} if wait else {}
    doc, html = await _tier_stealth(url, timeout * 3, proxy, **extra)
    doc.metadata["fetched_via"] = "vpn-stealth"
    return doc, html


def _add_product_facts(doc: CrawledDocument, html: str) -> None:
    """Put rating/price/availability from the markup above the page text."""
    from . import product_facts

    url = doc.final_url or doc.request_url
    try:
        results = product_facts.listing(html, url)
        facts = None if results else product_facts.extract(html, url)
    except Exception as exc:  # never lose a page over its extras
        LOGGER.debug("product facts failed for %s: %s", doc.request_url, exc)
        return
    if results:
        # A store search page: the clean list replaces the tracking-link text.
        doc.markdown = results
        doc.metadata["product_facts"] = True
        doc.metadata["product_listing"] = True
    elif facts:
        doc.markdown = f"{facts}\n{doc.markdown or ''}"
        doc.metadata["product_facts"] = True


_CONTACT_PATH = re.compile(
    r"contact|about|support|help|impressum|kontakt|find-us|locations?|stores?|branch",
    re.IGNORECASE,
)
_TEL_HREF = re.compile(r'href=["\']tel:([^"\']+)["\']', re.IGNORECASE)
_MAIL_HREF = re.compile(r'href=["\']mailto:([^"\'?]+)', re.IGNORECASE)


def _add_contact_links(doc: CrawledDocument, html: str) -> None:
    """On contact-type pages, keep phone/e-mail links the text conversion cut.

    Both extractors trim headers and footers, which is where many sites put
    the phone number (telonic.co.uk/contact/ lost its only one).
    """
    from urllib.parse import unquote, urlparse

    if not _CONTACT_PATH.search(urlparse(doc.final_url or doc.request_url).path or ""):
        return
    text = doc.markdown or ""
    digits = re.sub(r"\D", "", text)
    found: list[str] = []
    for raw in dict.fromkeys(_TEL_HREF.findall(html)):
        number = unquote(raw).strip()
        if number and re.sub(r"\D", "", number) not in digits:
            found.append(f"phone {number}")
    for raw in dict.fromkeys(_MAIL_HREF.findall(html)):
        mail = unquote(raw).strip()
        if mail and mail.lower() not in text.lower():
            found.append(f"e-mail {mail}")
    if found:
        doc.markdown = (
            f"{text.rstrip()}\n\nContact links on the page: {'; '.join(found[:6])}\n"
        )


def _incomplete(
    doc: CrawledDocument,
    reason: str,
    attempts: list[str],
    url: str,
    *,
    extra: Optional[dict] = None,
    note: str = "no other tier rendered more",
) -> CrawledDocument:
    """The partial http text, reported as a failure that still carries it.

    Status is "failed" so a caller cannot mistake it for the page; the text
    stays in ``markdown`` for whatever it is worth. Never cached, so the next
    request tries every tier again.
    """
    message = (
        f"Incomplete page: {reason}; {note}. Products, prices or listings are "
        "probably missing; the partial text is attached."
    )
    doc.request_url = url
    doc.status = "failed"
    doc.error_message = message
    doc.metadata = {
        **doc.metadata,
        **(extra or {}),
        "incomplete": True,
        "tiers_tried": attempts,
    }
    doc.metadata.pop("warning", None)
    return doc


_RUNNERS = {
    "http": _tier_http,
    "browser": _tier_browser,
    "stealth": _tier_stealth,
    "vpn": _tier_vpn,
}


async def fetch(
    url: str,
    *,
    approve: Optional[firecrawl_tier.Approver] = None,
    tiers: Optional[Iterable[str]] = None,
    timeout: int = 20,
    fresh: bool = False,
    wait: Optional[PageWait] = None,
) -> CrawledDocument:
    """Fetch ``url`` through the tiers until one returns a real page.

    ``wait`` tells the browser tiers what to wait for before capture; with an
    explicit ``wait_for`` or ``wait_ms`` the http tier is skipped, as it is
    for hash routes (``#/...``), which only a browser renders.
    """
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

    routed = await _run_route(url, order, timeout, attempts)
    if routed is not None:
        cache.put(routed)
        return routed

    blocked_seen = False
    # Why the http text looked partial (script shell, low coverage), if it did.
    partial: Optional[str] = None
    partial_doc: Optional[CrawledDocument] = None
    # The partial http text is a loading skeleton, worth less than any real text.
    partial_junk = False
    # A browser tier rendered the page, but its content had not loaded by the
    # end of the settle wait (loading placeholders, templates, a bare shell).
    shell: Optional[str] = None
    shell_doc: Optional[CrawledDocument] = None
    for tier in order:
        if tier == "http" and (hash_route(url) or (wait and wait.explicit)):
            why = "hash route" if hash_route(url) else "wait_for/wait_ms"
            attempts.append(f"http: skipped, {why} needs a browser")
            continue
        # A short but real page (no challenge, no error) is not worth a stealth
        # browser or paid credits: those tiers exist for bot walls and failures.
        # A partial http page is different: if the browser came back thin too
        # (it can render before the listings arrive), stealth waits for the
        # network to go idle and gets one more try.
        if (
            tier == "stealth"
            and best is not None
            and not blocked_seen
            and not partial
            and not shell
        ):
            attempts.append("stealth: skipped, page is short but not blocked")
            continue
        # The VPN exit is for blocks on this IP, not for pages that are just short.
        if tier == "vpn" and not blocked_seen:
            attempts.append("vpn: skipped, no tier was blocked")
            continue
        # The http text already judged partial is not "a free tier returned the
        # page": Firecrawl stays in play (behind the usual spending guard).
        if tier == "firecrawl" and best is not None and best is not partial_doc:
            attempts.append("firecrawl: skipped, a free tier returned the page")
            continue
        # A browser loaded the page, it just did not show its content in time.
        # Credits are for pages no free tier can open, not for waiting longer.
        if tier == "firecrawl" and shell:
            attempts.append("firecrawl: skipped, a free browser loaded the page")
            continue
        start = time.monotonic()
        try:
            if tier == "firecrawl":
                doc = await firecrawl_tier.scrape(
                    url, approve, timeout=max(timeout, 60)
                )
                html = ""
            else:
                tier_timeout = _tier_timeout(tier, timeout, wait)
                extra = {"wait": wait} if wait and tier != "http" else {}
                doc, html = await _RUNNERS[tier](url, tier_timeout, **extra)
        except Exception as exc:
            attempts.append(f"{tier}: {type(exc).__name__}: {exc}"[:300])
            blocked_seen = True
            continue
        if html and doc.status != "failed":
            _add_product_facts(doc, html)
            _add_contact_links(doc, html)
        problem = _verdict(doc, html)
        elapsed = time.monotonic() - start
        reason = (
            needs_browser(doc, html) if problem is None and tier == "http" else None
        )
        if tier == "http" and problem and problem.startswith("thin page"):
            # A short loading skeleton is a shell too, not a short page.
            reason = loading_shell(doc.markdown or "")
            problem = None if reason else problem
        if reason is not None and any(t in order for t in ("browser", "stealth")):
            # Readable but probably not the real content: let a browser render
            # it, and fall back to this text if the browser does no better.
            LOGGER.info("Tier http got %s for %s, trying a browser", reason, url)
            attempts.append(f"http: {reason}")
            partial, partial_doc = reason, doc
            partial_junk = loading_shell(doc.markdown or "") is not None
            if best is None or len(doc.markdown) > len(best.markdown):
                best = doc
            continue
        rendered = tier != "http" and tier != "firecrawl" and doc.status != "failed"
        if rendered and (problem is None or problem.startswith("thin page")):
            why = rendered_shell(doc, html) if problem is None else None
            why = why or loading_shell(doc.markdown or "")
            if why is not None:
                # Rendered, but the content had not arrived by the end of the
                # settle wait. Keep it as a fallback; stealth gets a go.
                LOGGER.info("Tier %s got %s for %s", tier, why, url)
                attempts.append(f"{tier}: {why}")
                if shell_doc is None or len(doc.markdown) > len(shell_doc.markdown):
                    shell, shell_doc = why, doc
                continue
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
        if doc.metadata.get("blocked") and partial_doc is not None:
            # Paid tier declined (or not yet approved): hand back the partial
            # text, marked incomplete, with what the spend question needs.
            return _incomplete(
                partial_doc,
                partial or "partial page",
                attempts,
                url,
                extra={
                    k: doc.metadata[k]
                    for k in ("blocked", "approval_reason", "credits_used_this_period")
                    if k in doc.metadata
                },
                note="Firecrawl was not approved",
            )
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
        if doc.markdown and (
            best is None
            or len(doc.markdown) > len(best.markdown)
            or (best is partial_doc and partial_junk)
        ):
            best = doc

    if best is not None and best is partial_doc and shell_doc is not None:
        # Two shells: return the one with more text.
        if len(shell_doc.markdown) > len(best.markdown):
            best, partial = None, None
    if best is None and shell_doc is not None:
        return _incomplete(
            shell_doc,
            shell or "script shell",
            attempts,
            url,
            note="the page did not finish loading its content in any browser tier",
        )
    if best is not None:
        best.metadata["tiers_tried"] = attempts
        best.request_url = url
        if best is partial_doc:
            return _incomplete(best, partial or "partial page", attempts, url)
        # Nothing passed the length check, but a short page beats no page.
        best.metadata["warning"] = "short page: no tier returned more than this"
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


async def _run_route(
    url: str, order: List[str], timeout: int, attempts: list[str]
) -> Optional[CrawledDocument]:
    """A site route's page for ``url``, or None to go on with the tiers.

    Routes load pages in the stealth browser, so they run only when the
    stealth tier is in play. A failed route is noted in ``attempts``.
    """
    from . import site_routes

    found = site_routes.match(url)
    if found is None or "stealth" not in order:
        return None
    route, target = found
    label = f"route {route.name}"
    start = time.monotonic()
    try:
        doc = await route.run(target, timeout * 3)
    except Exception as exc:  # noqa: BLE001 - the tiers still get their go
        attempts.append(f"{label}: {type(exc).__name__}: {exc}"[:300])
        return None
    problem = _verdict(doc)
    if problem is not None:
        attempts.append(f"{label}: {problem}")
        return None
    LOGGER.info("Fetched %s via %s in %.1fs", url, label, time.monotonic() - start)
    doc.request_url = url
    doc.metadata["tiers_tried"] = attempts + [f"{label}: ok"]
    return doc


def _tier_timeout(tier: str, timeout: int, wait: Optional[PageWait]) -> int:
    extra = wait.wait_ms // 1000 if wait else 0
    if tier == "browser":
        return browser_timeout(timeout, wait)
    if tier in ("stealth", "vpn"):
        return (timeout * 3 if tier == "stealth" else timeout) + extra
    return timeout


async def fetch_many(
    urls: List[str],
    *,
    approve: Optional[firecrawl_tier.Approver] = None,
    tiers: Optional[Iterable[str]] = None,
    concurrency: int = 3,
    timeout: int = 20,
    fresh: bool = False,
    wait: Optional[PageWait] = None,
) -> List[CrawledDocument]:
    import asyncio

    semaphore = asyncio.Semaphore(max(1, concurrency))
    tier_list = list(tiers) if tiers is not None else None

    async def one(u: str) -> CrawledDocument:
        async with semaphore:
            extra = {"wait": wait} if wait else {}
            return await fetch(
                u,
                approve=approve,
                tiers=tier_list,
                timeout=timeout,
                fresh=fresh,
                **extra,
            )

    return list(await asyncio.gather(*(one(u) for u in urls)))
