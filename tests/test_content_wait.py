"""Tests for the single-page ``wait_for`` condition.

The browser tests run the generated JavaScript in headless Chromium against
local HTML (no network). They are skipped when Chromium is not installed,
unless SEARXNCRAWL_REQUIRE_BROWSER_TESTS is set (as in CI).
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from crawler.config import (
    CONTENT_WAIT_GRACE_MS,
    MAIN_SELECTORS,
    MIN_CONTENT_CHARS,
    SETTLE_PLACEHOLDER_STABLE_MS,
    SETTLE_STABLE_MS,
    PageWait,
    build_content_wait_condition,
    build_markdown_run_config,
)

LONG_TEXT = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 3


def test_condition_is_js_and_covers_all_main_selectors() -> None:
    condition = build_content_wait_condition()

    assert condition.startswith("js:")
    for selector in MAIN_SELECTORS:
        assert selector in condition
    assert str(MIN_CONTENT_CHARS) in condition
    assert str(CONTENT_WAIT_GRACE_MS) in condition


def test_markdown_run_config_uses_content_wait_condition() -> None:
    assert build_markdown_run_config().wait_for == build_content_wait_condition()


def test_condition_no_longer_requires_a_main_element() -> None:
    condition = build_content_wait_condition()

    assert "document.querySelector('main')" not in condition
    assert "document.body.innerText" in condition


async def _time_until_ready(
    html: str, timeout_s: float, condition: str | None = None
) -> float | None:
    """Poll the condition like Crawl4AI does; return seconds until true."""
    playwright_api = pytest.importorskip("playwright.async_api")
    function = (condition or build_content_wait_condition())[len("js:") :]
    async with playwright_api.async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch()
        except Exception as exc:  # pragma: no cover - depends on local setup
            if os.getenv("SEARXNCRAWL_REQUIRE_BROWSER_TESTS"):
                raise
            pytest.skip(f"Chromium not available: {exc}")
        try:
            page = await browser.new_page()
            await page.set_content(html)
            start = time.monotonic()
            while time.monotonic() - start < timeout_s:
                if await page.evaluate(f"({function})()"):
                    return time.monotonic() - start
                await asyncio.sleep(0.1)
            return None
        finally:
            await browser.close()


GRACE_S = CONTENT_WAIT_GRACE_MS / 1000
STABLE_S = SETTLE_STABLE_MS / 1000
PLACEHOLDER_STABLE_S = SETTLE_PLACEHOLDER_STABLE_MS / 1000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "html",
    [
        f"<html><body><main>{LONG_TEXT}</main></body></html>",
        f"<html><body><article>{LONG_TEXT}</article></body></html>",
        f"<html><body><div role='main'>{LONG_TEXT}</div></body></html>",
        f"<html><body><div class='markdown-body'>{LONG_TEXT}</div></body></html>",
    ],
    ids=["main", "article", "role-main", "markdown-body"],
)
async def test_ready_once_a_content_area_has_settled(html: str) -> None:
    elapsed = await _time_until_ready(html, timeout_s=GRACE_S + 2)

    assert elapsed is not None
    assert STABLE_S - 0.2 <= elapsed < STABLE_S + 1.0


@pytest.mark.asyncio
async def test_falls_back_to_body_after_grace_period() -> None:
    """example.com-style page: no content area, only body text."""
    html = (
        "<html><body><div><h1>Example Domain</h1><p>Short page.</p></div></body></html>"
    )

    elapsed = await _time_until_ready(html, timeout_s=GRACE_S + 3)

    assert elapsed is not None
    assert GRACE_S - 0.2 <= elapsed < GRACE_S + 2


@pytest.mark.asyncio
async def test_short_main_falls_back_to_body_instead_of_timing_out() -> None:
    html = "<html><body><main>Hi</main><p>Some more body text.</p></body></html>"

    elapsed = await _time_until_ready(html, timeout_s=GRACE_S + 3)

    assert elapsed is not None


@pytest.mark.asyncio
async def test_empty_page_never_becomes_ready() -> None:
    elapsed = await _time_until_ready(
        "<html><body></body></html>", timeout_s=GRACE_S + 1.5
    )

    assert elapsed is None


def _later(html_after: str, delay_ms: int, html_before: str) -> str:
    """A page that replaces its <main> after ``delay_ms``, like a script fetch."""
    return (
        f"<html><body><main id='m'>{html_before}</main><script>"
        f"setTimeout(() => {{ document.getElementById('m').innerHTML = "
        f"{html_after!r}; }}, {delay_ms});</script></body></html>"
    )


@pytest.mark.asyncio
async def test_waits_while_loading_placeholders_change_into_results() -> None:
    """track24-style: "Loading..." first, the results arrive after 2 s."""
    html = _later(
        f"<p>Delivered to the front door.</p><p>{LONG_TEXT}</p>",
        2000,
        f"<p>Recently tracked items {LONG_TEXT}</p><p>Loading...</p><p>Loading...</p>",
    )

    elapsed = await _time_until_ready(html, timeout_s=10)

    assert elapsed is not None
    assert 2.0 + STABLE_S - 0.2 <= elapsed < 2.0 + STABLE_S + 1.5


@pytest.mark.asyncio
async def test_placeholders_that_never_resolve_get_the_longer_wait() -> None:
    html = f"<html><body><main>{LONG_TEXT}<p>Loading...</p></main></body></html>"

    elapsed = await _time_until_ready(html, timeout_s=PLACEHOLDER_STABLE_S + 3)

    assert elapsed is not None
    assert PLACEHOLDER_STABLE_S - 0.2 <= elapsed < PLACEHOLDER_STABLE_S + 1.5


@pytest.mark.asyncio
async def test_page_that_never_settles_is_released_at_the_cap() -> None:
    html = (
        f"<html><body><main id='m'>{LONG_TEXT}</main><script>"
        "setInterval(() => { document.getElementById('m').textContent += 'x'; }, 200);"
        "</script></body></html>"
    )
    condition = build_content_wait_condition(cap_ms=2000)

    elapsed = await _time_until_ready(html, timeout_s=6, condition=condition)

    assert elapsed is not None
    assert 1.8 <= elapsed < 3.0


@pytest.mark.asyncio
async def test_past_deadline_releases_at_once() -> None:
    condition = build_content_wait_condition(
        deadline_epoch_ms=int(time.time() * 1000) - 1
    )

    elapsed = await _time_until_ready(
        "<html><body></body></html>", timeout_s=2, condition=condition
    )

    assert elapsed is not None and elapsed < 0.5


@pytest.mark.asyncio
async def test_wait_for_selector() -> None:
    html = _later("<div class='result'>Delivered</div>", 1000, "Loading...")
    condition = build_content_wait_condition(PageWait(wait_for=".result"))

    elapsed = await _time_until_ready(html, timeout_s=5, condition=condition)

    assert elapsed is not None and 0.9 <= elapsed < 1.6


@pytest.mark.asyncio
async def test_wait_for_text() -> None:
    html = _later("<p>Status: Delivered</p>", 1000, "Status: in transit")
    condition = build_content_wait_condition(PageWait(wait_for="text:Delivered"))

    elapsed = await _time_until_ready(html, timeout_s=5, condition=condition)

    assert elapsed is not None and 0.9 <= elapsed < 1.6


@pytest.mark.asyncio
async def test_wait_for_that_never_matches_ends_at_the_cap() -> None:
    html = f"<html><body><main>{LONG_TEXT}</main></body></html>"
    condition = build_content_wait_condition(
        PageWait(wait_for="#never"), cap_ms=1500
    )

    elapsed = await _time_until_ready(html, timeout_s=4, condition=condition)

    assert elapsed is not None and 1.4 <= elapsed < 2.2


@pytest.mark.asyncio
async def test_wait_ms_is_a_minimum() -> None:
    html = f"<html><body><main>{LONG_TEXT}</main></body></html>"
    condition = build_content_wait_condition(PageWait(wait_ms=2500))

    elapsed = await _time_until_ready(html, timeout_s=6, condition=condition)

    assert elapsed is not None and 2.4 <= elapsed < 3.5


def test_wait_for_prefix_picks_text_or_selector() -> None:
    assert PageWait(wait_for="text:Delivered").text == "Delivered"
    assert PageWait(wait_for="text:Delivered").selector is None
    assert PageWait(wait_for="#results li").selector == "#results li"
    assert not PageWait().explicit
    assert PageWait(wait_ms=100).explicit
