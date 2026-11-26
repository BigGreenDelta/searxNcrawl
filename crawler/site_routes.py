"""Site routes: sites whose pages the generic tiers cannot read in full.

A route runs before the tiers for the URLs it matches. When it fails, the
tiers run as usual, so a route can only add content, never lose a page.

Thingiverse
    Thing pages (``/thing:<id>`` and its tabs) are a Next.js app: the served
    HTML holds only the JSON-LD product block and the description, and every
    count, the file list and the print settings are fetched by the page's
    own scripts from ``www.thingiverse.com/api``. Those calls carry a token
    the page gets for itself, and Cloudflare turns them away from plain
    headless Chromium (the page then shows "Something went wrong"). The
    route opens the thing page in the stealth browser, keeps the JSON
    answers to two of the page's own requests (``/api/v2/things/<id>/complete``
    and ``/api/things/<id>/files``) and writes them out as Markdown. It sends
    no request of its own and uses no account or API key.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from .document import CrawledDocument
from .chromium_profile import seeded_profile, stealth_profile_kwargs

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Route:
    name: str
    # The canonical URL the route loads, or None when it does not apply.
    match: Callable[[str], Optional[str]]
    # run(url, timeout, ocr): ``ocr`` asks for text read from images.
    run: Callable[[str, int, bool], Awaitable[CrawledDocument]]
    # The route reads text from images, so ``ocr=False`` pages are cached apart.
    uses_ocr: bool = False


# --- Thingiverse ----------------------------------------------------------

_THINGIVERSE_HOST = re.compile(r"^(www\.)?thingiverse\.com$", re.IGNORECASE)
_THING_PATH = re.compile(r"^/thing:(\d+)(/[a-z]*)?/?$", re.IGNORECASE)


def thing_id(url: str) -> Optional[str]:
    """The thing id of a Thingiverse thing page URL, or None."""
    parts = urlparse(url)
    if not _THINGIVERSE_HOST.match(parts.hostname or ""):
        return None
    found = _THING_PATH.match(parts.path or "")
    return found.group(1) if found else None


def _thingiverse_match(url: str) -> Optional[str]:
    found = thing_id(url)
    return f"https://www.thingiverse.com/thing:{found}" if found else None


def _html_to_md(html: Optional[str]) -> str:
    if not html:
        return ""
    from crawl4ai.html2text import HTML2Text

    converter = HTML2Text()
    converter.body_width = 0
    converter.ignore_images = True
    text = converter.handle(html)
    # Thingiverse links other things as "/thing:123".
    text = text.replace("](/", "](https://www.thingiverse.com/")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _inline(html: Optional[str]) -> str:
    return " ".join(_html_to_md(html).split())


def _size(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _date(value: Any) -> str:
    try:
        return datetime.fromisoformat(str(value)).date().isoformat()
    except ValueError:
        return str(value or "")


_SETTING_LABELS = {
    "printer": "Printer",
    "printer_brand": "Printer brand",
    "rafts": "Rafts",
    "supports": "Supports",
    "resolution": "Resolution",
    "infill": "Infill",
    "filament_brand": "Filament brand",
    "filament_color": "Filament colour",
    "filament_material": "Filament material",
    "notes": "Notes",
}


def _part_markdown(part: dict) -> str:
    """One "Thing details" section (print settings, post-printing, ...)."""
    rows: list[str] = []
    for item in part.get("data") or []:
        if not isinstance(item, dict):
            continue
        if part.get("type") == "settings":
            for key, value in item.items():
                text = _inline(value) if isinstance(value, str) else ""
                if text:
                    label = _SETTING_LABELS.get(key, key.replace("_", " ").capitalize())
                    rows.append(f"- **{label}:** {text}")
            continue
        title = (item.get("title") or "").strip()
        body = _html_to_md(item.get("content"))
        if title and body:
            rows.append(f"**{title}**\n\n{body}")
        elif title or body:
            rows.append(title or body)
    if not rows:
        return ""
    joiner = "\n" if part.get("type") == "settings" else "\n\n"
    return f"## {part.get('name') or part.get('type', '').title()}\n\n" + joiner.join(
        rows
    )


def thingiverse_markdown(thing: dict, files: Optional[list], url: str) -> str:
    """Markdown for a thing from the page's ``complete`` and ``files`` JSON."""
    name = thing.get("name") or f"thing:{thing.get('id')}"
    creator = thing.get("creator") or {}
    lines = [f"# {name}", ""]
    designer = creator.get("name") or ""
    if designer:
        link = creator.get("public_url") or ""
        lines.append(
            f"- **Designer:** [{designer}]({link})"
            if link
            else f"- **Designer:** {designer}"
        )
    if thing.get("created_at"):
        lines.append(f"- **Published:** {_date(thing['created_at'])}")
    if thing.get("license"):
        lines.append(f"- **Licence:** {thing['license']}")
    counts = [
        ("Likes", "like_count"),
        ("Collects", "collect_count"),
        ("Makes", "make_count"),
        ("Remixes", "remix_count"),
        ("Comments", "comment_count"),
        ("Files", "file_count"),
    ]
    stats = [
        f"{label} {thing[key]:,}"
        for label, key in counts
        if isinstance(thing.get(key), int)
    ]
    if stats:
        lines.append(f"- **Stats:** {', '.join(stats)}")
    flags = [
        label
        for label, key in (
            ("featured", "is_featured"),
            ("customizer", "is_customizer"),
            ("work in progress", "is_wip"),
        )
        if thing.get(key)
    ]
    if flags:
        lines.append(f"- **Marked:** {', '.join(flags)}")
    tags = [
        t.get("name")
        for t in thing.get("tags") or []
        if isinstance(t, dict) and t.get("name")
    ]
    if tags:
        lines.append(f"- **Tags:** {', '.join(tags)}")
    ancestors = [a for a in thing.get("ancestors") or [] if isinstance(a, dict)]
    if ancestors:
        sources = ", ".join(
            f"[{a.get('name') or a.get('id')}]({a.get('public_url') or ''})"
            for a in ancestors
        )
        lines.append(f"- **Remix of:** {sources}")
    lines.append(f"- **Page:** {thing.get('public_url') or url}")

    summary = ""
    others: list[str] = []
    for part in thing.get("details_parts") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "summary":
            summary = "\n\n".join(
                _html_to_md(item.get("content"))
                for item in part.get("data") or []
                if isinstance(item, dict)
            ).strip()
        else:
            section = _part_markdown(part)
            if section:
                others.append(section)
    summary = summary or _html_to_md(thing.get("description_html"))
    if summary:
        lines += ["", "## Summary", "", summary]
    lines += [f"\n{section}" for section in others]

    file_rows = []
    for item in files or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        size = _size(item.get("size"))
        link = item.get("public_url") or ""
        label = f"[{item['name']}]({link})" if link else item["name"]
        file_rows.append(f"- {label}" + (f" ({size})" if size else ""))
    if not file_rows:
        # The file list request failed: the zip listing still has the names.
        zipped = (thing.get("zip_data") or {}).get("files") or []
        file_rows = [
            f"- {f['name']}" for f in zipped if isinstance(f, dict) and f.get("name")
        ]
    if file_rows:
        lines += ["", f"## Files ({len(file_rows)})", "", *file_rows]
    return "\n".join(lines).strip() + "\n"


# How long to wait for the page's own API answers once it has loaded.
_THINGIVERSE_WAIT_MS = 12000


async def _thingiverse_run(url: str, timeout: int, ocr: bool = True) -> CrawledDocument:
    from scrapling.fetchers import StealthyFetcher

    found = thing_id(url) or ""
    complete_re = re.compile(rf"/api/v2/things/{found}/complete(\?|$)")
    files_re = re.compile(rf"/api/things/{found}/files(\?|$)")
    got: dict[str, Any] = {}
    reads: list[asyncio.Future] = []

    async def setup(page):
        def on_response(response):
            key = (
                "complete"
                if complete_re.search(response.url)
                else "files"
                if files_re.search(response.url)
                else None
            )
            if key is None or response.status != 200:
                return

            async def read():
                try:
                    got[key] = await response.json()
                except Exception as exc:  # noqa: BLE001 - a missing part is reported below
                    LOGGER.debug("Thingiverse %s JSON unreadable: %s", key, exc)

            reads.append(asyncio.ensure_future(read()))

        page.on("response", on_response)

    async def action(page):
        for _ in range(_THINGIVERSE_WAIT_MS // 100):
            if (
                reads
                and all(r.done() for r in reads)
                and {"complete", "files"} <= got.keys()
            ):
                break
            await page.wait_for_timeout(100)
        await asyncio.gather(*reads, return_exceptions=True)
        return page

    with seeded_profile() as profile:
        resp = await StealthyFetcher.async_fetch(
            url,
            headless=True,
            solve_cloudflare=True,
            network_idle=False,
            block_webrtc=True,
            timeout=timeout * 1000,
            page_setup=setup,
            page_action=action,
            **stealth_profile_kwargs(profile),
        )
    status = getattr(resp, "status", None)
    meta: dict[str, Any] = {"fetched_via": "route:thingiverse", "status_code": status}
    thing = got.get("complete")
    if status == 404 or not isinstance(thing, dict) or not thing.get("name"):
        why = (
            f"HTTP {status}"
            if status and status >= 400
            else "the page sent no thing data"
        )
        return CrawledDocument(
            request_url=url,
            final_url=url,
            status="failed",
            markdown="",
            error_message=why,
            metadata=meta,
        )
    files = got.get("files")
    if not isinstance(files, list):
        meta["warning"] = (
            "file sizes missing: the page's file list request did not answer"
        )
        files = None
    return CrawledDocument(
        request_url=url,
        final_url=url,
        status="success",
        markdown=thingiverse_markdown(thing, files, url),
        metadata=meta,
    )


# --- AliExpress -----------------------------------------------------------


async def _aliexpress_run(url: str, timeout: int, ocr: bool = True) -> CrawledDocument:
    from . import aliexpress

    return await aliexpress.run(url, timeout, ocr)


def _aliexpress_match(url: str) -> Optional[str]:
    from . import aliexpress

    return aliexpress.canonical_url(url)


ROUTES: tuple[Route, ...] = (
    Route("thingiverse", _thingiverse_match, _thingiverse_run),
    Route("aliexpress", _aliexpress_match, _aliexpress_run, uses_ocr=True),
)


def match(url: str) -> Optional[tuple[Route, str]]:
    """The route for ``url`` and the URL it loads, or None."""
    for route in ROUTES:
        target = route.match(url)
        if target:
            return route, target
    return None
