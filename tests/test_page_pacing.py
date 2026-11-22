"""AliExpress page pacing and fetch log (offline, no real sleeps)."""

from __future__ import annotations

import asyncio
import json
import weakref
from datetime import datetime, timezone

import pytest

from crawler import aliexpress as ae
from crawler import fetch_chain, mcp_server, page_pacing, site_routes
from crawler.document import CrawledDocument

ITEM = "https://www.aliexpress.com/item/1005003561184819.html"
ITEM2 = "https://www.aliexpress.com/item/1005013417484054.html"
LONG = "word " * 200


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    """A fake clock: sleeping moves it on. Gaps come from a list, in order."""
    state = {"now": 100.0, "sleeps": [], "gaps": [], "asked": []}

    def now():
        return state["now"]

    async def sleep(seconds):
        state["sleeps"].append(round(seconds, 3))
        state["now"] += seconds
        await asyncio.sleep(0)

    def uniform(low, high):
        state["asked"].append((low, high))
        return state["gaps"].pop(0)

    monkeypatch.setattr(page_pacing, "_clock", now)
    monkeypatch.setattr(page_pacing, "_sleep", sleep)
    monkeypatch.setattr(page_pacing, "_uniform", uniform)
    # A fresh queue: no last start left over from another test.
    monkeypatch.setattr(page_pacing, "_turns", weakref.WeakKeyDictionary())
    monkeypatch.delenv("ALIEXPRESS_GAP_MIN_S", raising=False)
    monkeypatch.delenv("ALIEXPRESS_GAP_MAX_S", raising=False)
    return state


def _lines(tmp_path) -> list[dict]:
    path = tmp_path / "aliexpress-fetches.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


@pytest.mark.parametrize(
    "url, expected",
    [
        (ITEM, True),
        ("https://aliexpress.com/w/wholesale-switch.html", True),
        ("https://www.aliexpress.us/item/1.html", True),
        ("https://aliexpress.ru/item/1.html", True),
        ("https://ae-pic-a1.aliexpress-media.com/kf/H1.jpg", False),
        ("https://ae01.alicdn.com/kf/H1.jpg", False),
        ("https://pdp.aliexpress-media.com/product/description/desc.htm", False),
        ("https://notaliexpress.com/item/1.html", False),
        ("https://www.amazon.co.uk/dp/B0", False),
        ("not a url", False),
    ],
)
def test_only_aliexpress_page_hosts_are_paced(url, expected):
    assert page_pacing.paced(url) is expected


async def test_requests_go_out_one_by_one_a_random_gap_apart(clock):
    clock["gaps"] = [2.0, 0.5]
    order: list[str] = []

    async def one(name):
        await page_pacing.wait_turn(ITEM)
        order.append(f"{name}@{clock['now']:.1f}")

    await asyncio.gather(one("a"), one("b"), one("c"))
    # a goes at once, b 2.0 s after a, c 0.5 s after b: arrival order kept.
    assert order == ["a@100.0", "b@102.0", "c@102.5"]
    assert clock["asked"] == [(0.5, 3.0), (0.5, 3.0)]
    assert clock["sleeps"] == [2.0, 0.5]


async def test_time_already_passed_counts_toward_the_gap(clock):
    clock["gaps"] = [3.0]
    await page_pacing.wait_turn(ITEM)
    clock["now"] += 10  # the next request comes much later: no wait
    waited = await page_pacing.wait_turn(ITEM2)
    assert waited == 0 and clock["sleeps"] == []


async def test_other_hosts_never_wait(clock):
    await page_pacing.wait_turn(ITEM)
    assert await page_pacing.wait_turn("https://example.com/") == 0
    assert await page_pacing.wait_turn("https://ae01.alicdn.com/kf/a.jpg") == 0
    assert clock["asked"] == []


async def test_zero_gap_turns_pacing_off(clock, monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_GAP_MIN_S", "0")
    monkeypatch.setenv("ALIEXPRESS_GAP_MAX_S", "0")
    for _ in range(3):
        assert await page_pacing.wait_turn(ITEM) == 0
    assert clock["asked"] == []


def test_gap_bounds_from_env(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_GAP_MIN_S", "1")
    monkeypatch.setenv("ALIEXPRESS_GAP_MAX_S", "junk")
    assert page_pacing.gap_bounds() == (1.0, 3.0)
    monkeypatch.setenv("ALIEXPRESS_GAP_MAX_S", "0.2")
    assert page_pacing.gap_bounds() == (1.0, 1.0)


def test_record_writes_one_json_line(tmp_path):
    page_pacing.record(ITEM + "?spm=x", "route:aliexpress", 2.345, "ok", waited=1.5)
    page_pacing.record("https://ae01.alicdn.com/kf/a.jpg", "http", 0.1, "ok")
    (line,) = _lines(tmp_path)
    assert line["item"] == "1005003561184819"
    assert line["tier"] == "route:aliexpress"
    assert (line["seconds"], line["waited"], line["outcome"]) == (2.35, 1.5, "ok")
    assert datetime.fromisoformat(line["time"]).tzinfo is not None


def test_log_off(tmp_path, monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_LOG_PATH", "off")
    page_pacing.record(ITEM, "http", 1, "ok")
    assert page_pacing.read_entries() == []


def test_log_rotates_and_reads_back_every_file(tmp_path, monkeypatch):
    path = tmp_path / "small.jsonl"
    monkeypatch.setenv("ALIEXPRESS_LOG_PATH", str(path))
    monkeypatch.setenv("ALIEXPRESS_LOG_MAX_BYTES", "1024")
    for n in range(20):
        page_pacing.record(ITEM, "http", n, "ok")
    assert path.with_name("small.jsonl.1").exists()
    assert [e["seconds"] for e in page_pacing.read_entries()] == list(range(20))


def _entry(at: str, outcome: str, tier: str = "route:aliexpress", waited=0.0):
    return {"time": at, "tier": tier, "outcome": outcome, "waited": waited}


def test_summary_counts_rates_and_hours():
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    entries = [
        _entry("2026-11-10T09:00:00+00:00", "challenged"),  # outside 7 days
        _entry("2026-11-21T09:15:00+00:00", "ok", waited=2.0),
        _entry("2026-11-21T09:40:00+00:00", "challenged", "http"),
        _entry("2026-11-22T11:05:00+00:00", "ok"),
        _entry("2026-11-22T11:06:00+00:00", "error", "stealth", waited=1.0),
        {"time": "junk"},
    ]
    text = page_pacing.summary(7, entries, now=now)
    assert "- Fetches: 4 (ok 2, challenged 1, error 1)" in text
    assert "- Challenge rate: 25%" in text
    assert "- Last challenge: 2026-11-21 09:40" in text
    assert "average 0.8 s, longest 2.0 s" in text
    assert "| http | 1 | 0 | 1 | 0 | 100% |" in text
    assert "| 2026-11-22 | 2 | 1 | 0 | 1 | 0% |" in text
    assert "| 09:00 | 2 | 1 | 1 | 0 | 50% |" in text
    assert "2026-11-10" not in text


def test_summary_when_nothing_was_logged():
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    text = page_pacing.summary(3, [], now=now)
    assert "No AliExpress page fetches logged since 2026-11-19 12:00" in text


async def test_mcp_tool_reads_the_log(tmp_path):
    page_pacing.record(ITEM, "route:aliexpress", 2, "challenged")
    text = await mcp_server.aliexpress_fetch_log(days=1)
    assert "- Fetches: 1 (ok 0, challenged 1, error 0)" in text
    with pytest.raises(ValueError):
        await mcp_server.aliexpress_fetch_log(days=0)


# --- through the fetch chain -------------------------------------------------


def _doc(markdown: str, url: str = ITEM, **meta) -> CrawledDocument:
    return CrawledDocument(
        request_url=url,
        final_url=url,
        status="success" if markdown else "failed",
        markdown=markdown,
        error_message=meta.pop("error", None),
        metadata=meta,
    )


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch, clock):
    """Scripted route and tiers; every AliExpress turn is recorded."""
    calls: list = []
    script: dict = {}
    turns: list = []
    real_wait = page_pacing.wait_turn

    async def wait_turn(url):
        turns.append(url)
        return await real_wait(url)

    def make(name):
        async def run(url, timeout, **kwargs):
            calls.append(name)
            return script[name]

        return run

    async def route_run(url, timeout, ocr=True):
        calls.append("route")
        return script["route"](url) if callable(script["route"]) else script["route"]

    monkeypatch.setattr(page_pacing, "wait_turn", wait_turn)
    monkeypatch.setattr(
        fetch_chain, "_RUNNERS", {n: make(n) for n in fetch_chain.FREE_TIERS}
    )
    monkeypatch.setattr(
        site_routes,
        "ROUTES",
        (site_routes.Route("aliexpress", ae.canonical_url, route_run, uses_ocr=True),),
    )
    monkeypatch.setenv("FETCH_TIERS", "http,browser,stealth,firecrawl")
    monkeypatch.setenv("FETCH_MIN_CHARS", "400")
    monkeypatch.setenv("FETCH_CACHE_TTL", "3600")
    return calls, script, turns


async def test_two_items_are_paced_and_logged_a_cached_repeat_is_not(
    chain, clock, tmp_path
):
    calls, script, turns = chain
    script["route"] = lambda url: _doc(LONG, url)
    clock["gaps"] = [1.25]
    docs = await fetch_chain.fetch_many([ITEM, ITEM2])
    assert [d.status for d in docs] == ["success", "success"]
    assert clock["sleeps"] == [1.25]
    lines = _lines(tmp_path)
    assert [(e["item"], e["tier"], e["outcome"]) for e in lines] == [
        ("1005003561184819", "route:aliexpress", "ok"),
        ("1005013417484054", "route:aliexpress", "ok"),
    ]
    assert lines[1]["waited"] == 1.25
    # A cached repeat: no turn, no wait, no line.
    await fetch_chain.fetch(ITEM)
    assert len(turns) == 2 and len(_lines(tmp_path)) == 2


async def test_blocked_route_is_logged_as_challenged(chain, tmp_path):
    calls, script, _ = chain
    script["route"] = _doc(
        "",
        error="blocked by AliExpress anti-bot (slider CAPTCHA)",
        blocked=True,
        final=True,
    )
    await fetch_chain.fetch(ITEM)
    (line,) = _lines(tmp_path)
    assert (line["tier"], line["outcome"]) == ("route:aliexpress", "challenged")
    assert line["note"].startswith("blocked by AliExpress anti-bot")


async def test_tier_attempts_are_paced_and_logged(chain, clock, tmp_path):
    calls, script, turns = chain
    url = "https://www.aliexpress.com/w/wholesale-switch.html"
    clock["gaps"] = [0.5, 0.5]
    script["http"] = (_doc("Captcha Verification. Slide to verify", url), "")
    script["browser"] = (_doc("", url, error="Timeout"), "")
    script["stealth"] = (_doc(LONG, url), "")
    doc = await fetch_chain.fetch(url)
    assert doc.status == "success"
    assert calls == ["http", "browser", "stealth"]
    assert turns == [url] * 3
    assert [(e["tier"], e["outcome"]) for e in _lines(tmp_path)] == [
        ("http", "challenged"),
        ("browser", "error"),
        ("stealth", "ok"),
    ]


async def test_unapproved_firecrawl_is_neither_paced_nor_logged(
    chain, clock, monkeypatch, tmp_path
):
    from crawler import firecrawl_tier

    calls, script, turns = chain
    url = "https://www.aliexpress.com/w/wholesale-x.html"
    clock["gaps"] = [0.5, 0.5]
    for tier in ("http", "browser", "stealth"):
        script[tier] = (_doc("", url, error="Timeout"), "")

    async def scrape(u, approve, timeout):
        return _doc("", u, error="Blocked: not approved", blocked=True)

    monkeypatch.setattr(firecrawl_tier, "scrape", scrape)
    await fetch_chain.fetch(url)
    assert len(turns) == 3
    assert [e["tier"] for e in _lines(tmp_path)] == ["http", "browser", "stealth"]


async def test_other_sites_are_not_logged(chain, tmp_path):
    calls, script, turns = chain
    url = "https://example.com/page"
    script["http"] = (_doc(LONG, url), "")
    await fetch_chain.fetch(url)
    assert turns == [url]  # asked, but wait_turn lets it straight through
    assert _lines(tmp_path) == []
