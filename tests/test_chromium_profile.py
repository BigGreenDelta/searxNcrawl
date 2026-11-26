"""Seeded Chromium profiles that stop the Windows blank-password probe.

Every browser launch path must hand Chromium a user-data dir whose
``Local State`` already holds the password-check prefs, on Windows only.
The launch tests force the Windows branch and check the file at the moment
the (mocked) browser is launched.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import crawler
from crawler import aliexpress, chromium_profile, fetch_chain, session_capture
from crawler import site as site_module
from crawler import site_routes
from crawler.auth import ResolvedAuth


class _Stop(Exception):
    """Ends a launch test right after the mocked launch."""


def _assert_seeded(user_data_dir: str) -> None:
    """``user_data_dir`` exists now and its Local State skips the probe."""
    path = Path(user_data_dir) / "Local State"
    assert path.is_file(), f"no Local State in {user_data_dir}"
    manager = json.loads(path.read_text(encoding="utf-8"))["password_manager"]
    assert manager["os_password_blank"] is False
    stamp = manager["os_password_last_changed"]
    assert isinstance(stamp, str)
    # Within a minute of now, in microseconds since 1601.
    assert abs(int(stamp) - int(chromium_profile.chromium_time())) < 60_000_000


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chromium_profile, "needs_seeded_profile", lambda: True)


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chromium_profile, "needs_seeded_profile", lambda: False)


@pytest.fixture(autouse=True)
def no_chromium_install(monkeypatch: pytest.MonkeyPatch) -> None:
    async def noop() -> None:
        return None

    monkeypatch.setattr(crawler, "ensure_chromium", noop)
    monkeypatch.setattr(site_module, "ensure_chromium", noop)
    monkeypatch.setattr(session_capture, "ensure_chromium", noop)


# ── helpers ──────────────────────────────────────────────────────────────


def test_needs_seeded_profile_only_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert chromium_profile.needs_seeded_profile() is True
    monkeypatch.setattr(sys, "platform", "linux")
    assert chromium_profile.needs_seeded_profile() is False


def test_chromium_time_is_microseconds_since_1601() -> None:
    assert chromium_profile.chromium_time(0) == "11644473600000000"
    # The value Chrome itself wrote on bgd-ws-01 for PasswordLastSet
    # 2026-08-19 converts back to that date.
    unix = int("13431577264390587") / 1_000_000 - 11_644_473_600
    assert time.strftime("%Y-%m-%d", time.gmtime(unix)) == "2026-08-19"


def test_seed_local_state_writes_prefs(tmp_path: Path) -> None:
    path = chromium_profile.seed_local_state(tmp_path, now=0)
    assert path == tmp_path / "Local State"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "password_manager": {
            "os_password_blank": False,
            "os_password_last_changed": "11644473600000000",
        }
    }


def test_seed_local_state_keeps_other_keys(tmp_path: Path) -> None:
    (tmp_path / "Local State").write_text(
        json.dumps(
            {
                "browser": {"x": 1},
                "password_manager": {"is_biometric_avaliable": False},
            }
        ),
        encoding="utf-8",
    )
    chromium_profile.seed_local_state(tmp_path, now=0)
    state = json.loads((tmp_path / "Local State").read_text(encoding="utf-8"))
    assert state["browser"] == {"x": 1}
    assert state["password_manager"]["is_biometric_avaliable"] is False
    assert state["password_manager"]["os_password_blank"] is False


def test_seed_local_state_replaces_garbage(tmp_path: Path) -> None:
    (tmp_path / "Local State").write_text("not json", encoding="utf-8")
    chromium_profile.seed_local_state(tmp_path)
    _assert_seeded(str(tmp_path))


def test_seeded_profile_is_none_off_windows(linux: None) -> None:
    with chromium_profile.seeded_profile() as profile:
        assert profile is None


def test_seeded_profile_on_windows_is_fresh_and_removed(windows: None) -> None:
    with chromium_profile.seeded_profile() as first:
        assert first is not None
        _assert_seeded(first)
        with chromium_profile.seeded_profile() as second:
            assert second != first
    assert not Path(first).exists()


def test_stealth_profile_kwargs() -> None:
    assert chromium_profile.stealth_profile_kwargs(None) == {}
    assert chromium_profile.stealth_profile_kwargs("/p") == {"user_data_dir": "/p"}


def test_crawl4ai_browser_config_uses_persistent_context(tmp_path: Path) -> None:
    cfg = chromium_profile.crawl4ai_browser_config(str(tmp_path))
    assert cfg.use_persistent_context is True
    assert cfg.user_data_dir == str(tmp_path)
    assert cfg.storage_state is None


class _Context:
    def __init__(self) -> None:
        self.cookies: list = []
        self.scripts: list[str] = []

    async def add_cookies(self, cookies):
        self.cookies.extend(cookies)

    async def add_init_script(self, script):
        self.scripts.append(script)


@pytest.mark.asyncio
async def test_apply_storage_state_loads_cookies_and_local_storage() -> None:
    ctx = _Context()
    state = {
        "cookies": [{"name": "sid", "value": "1", "domain": "a.test", "path": "/"}],
        "origins": [
            {
                "origin": "https://a.test",
                "localStorage": [{"name": "token", "value": "t"}],
            },
            {"origin": "https://empty.test", "localStorage": []},
        ],
    }
    await chromium_profile.apply_storage_state(ctx, state)
    assert ctx.cookies == state["cookies"]
    assert len(ctx.scripts) == 1
    assert '"https://a.test": {"token": "t"}' in ctx.scripts[0]
    assert "empty.test" not in ctx.scripts[0]


@pytest.mark.asyncio
async def test_apply_storage_state_empty_adds_nothing() -> None:
    ctx = _Context()
    await chromium_profile.apply_storage_state(ctx, {"cookies": [], "origins": []})
    assert ctx.cookies == [] and ctx.scripts == []


@pytest.mark.asyncio
async def test_storage_state_hook_runs_once_per_context(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"cookies": [{"name": "a", "value": "b", "url": "https://a.test"}]}),
        encoding="utf-8",
    )
    hooks: dict[str, Any] = {}
    fake = SimpleNamespace(
        crawler_strategy=SimpleNamespace(set_hook=lambda name, fn: hooks.__setitem__(name, fn))
    )
    chromium_profile.install_storage_state_hook(fake, str(state_file))
    ctx = _Context()
    hook = hooks["on_page_context_created"]
    page = object()
    assert await hook(page, context=ctx) is page
    await hook(page, context=ctx)
    assert len(ctx.cookies) == 1


# ── crawl4ai launch paths ────────────────────────────────────────────────


def _recording_crawler(seen: list[dict]):
    class Crawler:
        def __init__(self, *args, config=None, **kwargs):
            self.config = config
            self.hooks: dict[str, Any] = {}
            self.crawler_strategy = SimpleNamespace(set_hook=self.hooks.__setitem__)
            seen.append({"args": args, "config": config, "kwargs": kwargs, "crawler": self})

        async def __aenter__(self):
            # The browser launches here: the profile must be ready now.
            if self.config is not None and self.config.user_data_dir:
                _assert_seeded(self.config.user_data_dir)
                seen[-1]["seeded_at_launch"] = True
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def arun(self, url, config):
            return [SimpleNamespace(url=url)]

    return Crawler


def _fake_builder(result, *, dedup_mode="exact"):
    return SimpleNamespace(status="success", request_url=result.url, metadata={})


@pytest.mark.asyncio
async def test_crawl_page_seeds_profile_on_windows(
    windows: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(crawler, "AsyncWebCrawler", _recording_crawler(seen))
    monkeypatch.setattr(crawler, "build_document_from_result", _fake_builder)

    await crawler.crawl_page_async("https://example.com")

    cfg = seen[0]["config"]
    assert seen[0].get("seeded_at_launch")
    assert cfg.use_persistent_context is True
    assert not Path(cfg.user_data_dir).exists()  # removed after the crawl


@pytest.mark.asyncio
async def test_crawl_page_with_session_on_windows_loads_it_by_hook(
    windows: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    seen: list[dict] = []
    monkeypatch.setattr(crawler, "AsyncWebCrawler", _recording_crawler(seen))
    monkeypatch.setattr(crawler, "build_document_from_result", _fake_builder)

    await crawler.crawl_page_async(
        "https://example.com", auth=ResolvedAuth(storage_state=str(state_file))
    )

    cfg = seen[0]["config"]
    assert seen[0].get("seeded_at_launch")
    # launch_persistent_context takes no storage_state.
    assert cfg.storage_state is None
    assert "on_page_context_created" in seen[0]["crawler"].hooks


@pytest.mark.asyncio
async def test_crawl_page_unchanged_off_windows(
    linux: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(crawler, "AsyncWebCrawler", _recording_crawler(seen))
    monkeypatch.setattr(crawler, "build_document_from_result", _fake_builder)

    await crawler.crawl_page_async("https://example.com")
    assert seen[0]["args"] == () and seen[0]["config"] is None and not seen[0]["kwargs"]

    state_file = tmp_path / "state.json"
    state_file.write_text("{}", encoding="utf-8")
    await crawler.crawl_page_async(
        "https://example.com", auth=ResolvedAuth(storage_state=str(state_file))
    )
    cfg = seen[1]["config"]
    assert cfg.storage_state == str(state_file)
    assert cfg.use_persistent_context is False and cfg.user_data_dir is None
    assert "on_page_context_created" not in seen[1]["crawler"].hooks


@pytest.mark.asyncio
async def test_crawl_site_seeds_profile_on_windows(
    windows: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text("{}", encoding="utf-8")
    seen: list[dict] = []
    monkeypatch.setattr(site_module, "AsyncWebCrawler", _recording_crawler(seen))
    monkeypatch.setattr(site_module, "build_document_from_result", _fake_builder)

    await site_module.crawl_site_async(
        "https://example.com", auth=ResolvedAuth(storage_state=str(state_file))
    )

    cfg = seen[0]["config"]
    assert seen[0].get("seeded_at_launch")
    assert cfg.use_persistent_context is True and cfg.storage_state is None
    assert "on_page_context_created" in seen[0]["crawler"].hooks
    assert not Path(cfg.user_data_dir).exists()


@pytest.mark.asyncio
async def test_crawl_site_unchanged_off_windows(
    linux: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(site_module, "AsyncWebCrawler", _recording_crawler(seen))
    monkeypatch.setattr(site_module, "build_document_from_result", _fake_builder)

    await site_module.crawl_site_async("https://example.com")

    cfg = seen[0]["config"]
    assert cfg.use_persistent_context is False and cfg.user_data_dir is None


# ── Scrapling StealthyFetcher launch paths ───────────────────────────────


def _patch_stealthy(monkeypatch: pytest.MonkeyPatch, seen: list[dict]) -> None:
    from scrapling.fetchers import StealthyFetcher

    async def fake_fetch(url, **kwargs):
        if kwargs.get("user_data_dir"):
            _assert_seeded(kwargs["user_data_dir"])
        seen.append(kwargs)
        raise _Stop

    monkeypatch.setattr(StealthyFetcher, "async_fetch", fake_fetch)


async def _stealth_tier() -> None:
    await fetch_chain._tier_stealth("https://example.com", 5)


async def _aliexpress_item() -> None:
    await aliexpress._load_pdp("https://www.aliexpress.com/item/1.html", 5)


async def _thingiverse_thing() -> None:
    await site_routes._thingiverse_run("https://www.thingiverse.com/thing:1", 5)


STEALTH_LAUNCHES = [_stealth_tier, _aliexpress_item, _thingiverse_thing]


@pytest.mark.asyncio
@pytest.mark.parametrize("launch", STEALTH_LAUNCHES)
async def test_stealth_launch_seeds_profile_on_windows(
    windows: None, monkeypatch: pytest.MonkeyPatch, launch
) -> None:
    seen: list[dict] = []
    _patch_stealthy(monkeypatch, seen)
    with pytest.raises(_Stop):
        await launch()
    assert seen and seen[0].get("user_data_dir")
    assert not Path(seen[0]["user_data_dir"]).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("launch", STEALTH_LAUNCHES)
async def test_stealth_launch_unchanged_off_windows(
    linux: None, monkeypatch: pytest.MonkeyPatch, launch
) -> None:
    seen: list[dict] = []
    _patch_stealthy(monkeypatch, seen)
    with pytest.raises(_Stop):
        await launch()
    assert seen and "user_data_dir" not in seen[0]


# ── session capture ──────────────────────────────────────────────────────


class _FakePage:
    url = "https://example.com/done"

    def is_closed(self) -> bool:
        return False

    async def goto(self, url):
        return None


class _FakeContext:
    def __init__(self, calls: list) -> None:
        self.calls = calls
        self.pages = [_FakePage()]

    async def new_page(self):
        return _FakePage()

    async def storage_state(self):
        return {"cookies": [], "origins": []}

    async def close(self):
        self.calls.append(("close",))


class _FakeBrowser:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    async def new_context(self):
        return _FakeContext(self.calls)

    async def close(self):
        self.calls.append(("close",))


def _patch_playwright(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    class Chromium:
        async def launch(self, **kwargs):
            calls.append(("launch", kwargs))
            return _FakeBrowser(calls)

        async def launch_persistent_context(self, user_data_dir, **kwargs):
            _assert_seeded(user_data_dir)
            calls.append(("persistent", user_data_dir, kwargs))
            return _FakeContext(calls)

    class Manager:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *exc):
            return False

    import playwright.async_api

    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: Manager())


async def _capture() -> dict:
    return await session_capture._execute_capture_flow(
        start_url="https://example.com",
        completion_url_pattern="done",
        timeout_seconds=5,
        poll_interval=0.01,
        headless=True,
        confirm_callback=None,
    )


@pytest.mark.asyncio
async def test_session_capture_seeds_profile_on_windows(
    windows: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list = []
    _patch_playwright(monkeypatch, calls)
    flow = await _capture()
    assert flow["status"] == "success"
    assert calls[0][0] == "persistent" and calls[0][2] == {"headless": True}
    assert calls[-1] == ("close",)
    assert not Path(calls[0][1]).exists()


@pytest.mark.asyncio
async def test_session_capture_unchanged_off_windows(
    linux: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list = []
    _patch_playwright(monkeypatch, calls)
    flow = await _capture()
    assert flow["status"] == "success"
    assert calls[0] == ("launch", {"headless": True})
    assert calls[-1] == ("close",)
