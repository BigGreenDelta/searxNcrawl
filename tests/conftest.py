"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_browser_auto_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests offline: never download Chromium during a test run."""
    monkeypatch.setenv("PLAYWRIGHT_AUTO_INSTALL", "false")


@pytest.fixture(autouse=True)
def _clear_search_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests offline: a local .env must not activate search providers."""
    for name in (
        "YDC_API_KEY", "SEARXNG_URL", "SEARXNG_USERNAME", "SEARXNG_PASSWORD",
        "SEARXNG_EXTRA_HEADERS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _browser_tier_only(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Keep tests offline and deterministic: the fetch chain uses only the
    (mocked) Crawl4AI browser tier, no cache, no Firecrawl.

    Router tests override these per test."""
    from crawler import fetch_chain, firecrawl_tier

    monkeypatch.setenv("FETCH_TIERS", "browser")
    monkeypatch.setenv("FETCH_CACHE_TTL", "0")
    monkeypatch.setenv("FETCH_CACHE_PATH", str(tmp_path / "fetch.sqlite"))
    monkeypatch.setenv("FETCH_MIN_CHARS", "0")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setattr(fetch_chain, "_cache", None)
    firecrawl_tier.reset_approval()
