"""Site routes: URL matching, Thingiverse Markdown, and the chain's use of routes."""

from __future__ import annotations

import pytest

from crawler import fetch_chain, site_routes
from crawler.document import CrawledDocument

LONG = "word " * 200

THING = {
    "id": 42,
    "name": "Test Holder",
    "public_url": "https://www.thingiverse.com/thing:42",
    "created_at": "2015-09-09T00:55:09+00:00",
    "creator": {"name": "maker1", "public_url": "https://www.thingiverse.com/maker1"},
    "license": "Creative Commons - Attribution",
    "like_count": 58468,
    "collect_count": 12,
    "make_count": 441,
    "remix_count": 37,
    "comment_count": 229,
    "file_count": 2,
    "is_featured": True,
    "tags": [{"name": "battery"}, {"name": "stackable"}],
    "ancestors": [
        {
            "id": 7,
            "name": "Original",
            "public_url": "https://www.thingiverse.com/thing:7",
        }
    ],
    "description_html": "<p>fallback description</p>",
    "details_parts": [
        {
            "type": "summary",
            "name": "Summary",
            "data": [
                {
                    "content": '<div><h2>About</h2><p>See <a href="/thing:9">this</a>.</p></div>'
                }
            ],
        },
        {
            "type": "settings",
            "name": "Print Settings",
            "data": [
                {
                    "printer": "<div><p>Hephestos 2</p></div>",
                    "supports": "No",
                    "infill": "<div><p>15%</p></div>",
                    "notes": "",
                }
            ],
        },
        {"type": "tips", "name": "Post-Printing"},
        {
            "type": "design",
            "name": "How I Designed This",
            "data": [{"content": "<p>Fusion 360.</p>"}],
        },
    ],
    "zip_data": {"files": [{"name": "zip-only.stl"}]},
}
FILES = [
    {
        "name": "AA-Base.stl",
        "size": 92584,
        "public_url": "https://www.thingiverse.com/download:1",
    },
    {
        "name": "big.stl",
        "size": 22_990_000,
        "public_url": "https://www.thingiverse.com/download:2",
    },
]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.thingiverse.com/thing:1004996", "1004996"),
        ("https://thingiverse.com/thing:1004996/", "1004996"),
        ("https://www.thingiverse.com/thing:1004996/files", "1004996"),
        ("http://WWW.Thingiverse.com/thing:5/comments?x=1", "5"),
        ("https://www.thingiverse.com/adoniram", None),
        ("https://www.thingiverse.com/thing:abc", None),
        ("https://www.thingiverse.com/thing:5/files/extra", None),
        ("https://evil-thingiverse.com/thing:5", None),
        ("https://www.printables.com/model/12345", None),
    ],
)
def test_thing_id(url, expected):
    assert site_routes.thing_id(url) == expected


def test_match_loads_the_canonical_thing_page():
    route, target = site_routes.match("https://thingiverse.com/thing:5/files")
    assert route.name == "thingiverse"
    assert target == "https://www.thingiverse.com/thing:5"
    assert site_routes.match("https://example.com/") is None


def test_thingiverse_markdown_has_the_page_facts():
    md = site_routes.thingiverse_markdown(
        THING, FILES, "https://www.thingiverse.com/thing:42"
    )
    assert md.startswith("# Test Holder\n")
    assert "- **Designer:** [maker1](https://www.thingiverse.com/maker1)" in md
    assert "- **Published:** 2015-09-09" in md
    assert "- **Licence:** Creative Commons - Attribution" in md
    assert "Likes 58,468" in md and "Makes 441" in md and "Remixes 37" in md
    assert "- **Tags:** battery, stackable" in md
    assert "- **Remix of:** [Original](https://www.thingiverse.com/thing:7)" in md
    # The summary part wins over description_html; site links get the host.
    assert "## About" in md and "fallback description" not in md
    assert "[this](https://www.thingiverse.com/thing:9)" in md
    # Print settings: one line each, empty values left out, no empty sections.
    assert "- **Printer:** Hephestos 2\n- **Supports:** No\n- **Infill:** 15%" in md
    assert "Notes" not in md and "Post-Printing" not in md
    assert "## How I Designed This\n\nFusion 360." in md
    assert "## Files (2)" in md
    assert "- [AA-Base.stl](https://www.thingiverse.com/download:1) (90.4 KB)" in md
    assert "(21.9 MB)" in md
    assert "zip-only.stl" not in md


def test_thingiverse_markdown_without_file_list_uses_zip_names():
    thing = {**THING, "details_parts": []}
    md = site_routes.thingiverse_markdown(
        thing, None, "https://www.thingiverse.com/thing:42"
    )
    assert "## Files (1)\n\n- zip-only.stl" in md
    assert "fallback description" in md


# --- the generic check: text only from page metadata -----------------------

APP_SHELL = (
    "<html><head><title>T</title>"
    '<script type="application/ld+json">{"@type": "Product", "name": "T"}</script>'
    '<meta name="description" content="long description"></head>'
    '<body><div id="root"></div><script>self.__next_f.push([1, "data"])</script>'
    "</body></html>"
)


def _doc(url, markdown, tier="http", status="success"):
    return CrawledDocument(
        request_url=url,
        final_url=url,
        status=status,
        markdown=markdown,
        metadata={"fetched_via": tier},
    )


def test_empty_app_root_is_metadata_only():
    reason = fetch_chain.metadata_only(_doc("https://a.test", LONG), APP_SHELL)
    assert reason and reason.startswith("empty app root (0 chars")
    assert fetch_chain.needs_browser(_doc("https://a.test", LONG), APP_SHELL) == reason


def test_page_with_body_text_is_not_metadata_only():
    html = f"<html><body><article><p>{LONG}</p></article></body></html>"
    assert fetch_chain.metadata_only(_doc("https://a.test", LONG), html) is None
    # No HTML (JSON pass-through): nothing to judge.
    assert fetch_chain.metadata_only(_doc("https://a.test", LONG), "") is None


# --- the chain -------------------------------------------------------------


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch):
    """Scripted tiers plus a scripted Thingiverse route; records the calls."""
    calls: list[str] = []
    script: dict = {}

    def make(name):
        async def run(url, timeout, **kwargs):
            calls.append(name)
            outcome = script[name]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return run

    async def route_run(url, timeout):
        calls.append(f"route:{url}")
        outcome = script["route"]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(
        fetch_chain, "_RUNNERS", {n: make(n) for n in fetch_chain.FREE_TIERS}
    )
    monkeypatch.setattr(
        site_routes,
        "ROUTES",
        (site_routes.Route("thingiverse", site_routes._thingiverse_match, route_run),),
    )
    monkeypatch.setenv("FETCH_TIERS", "http,browser,stealth,firecrawl")
    monkeypatch.setenv("FETCH_MIN_CHARS", "400")
    monkeypatch.setenv("FETCH_CACHE_TTL", "3600")
    return calls, script


URL = "https://www.thingiverse.com/thing:42/files"
CANON = "https://www.thingiverse.com/thing:42"


async def test_route_answers_before_the_tiers_and_is_cached(chain):
    calls, script = chain
    script["route"] = _doc(CANON, LONG, "route:thingiverse")
    doc = await fetch_chain.fetch(URL)
    assert doc.status == "success"
    assert doc.request_url == URL
    assert doc.metadata["tiers_tried"] == ["route thingiverse: ok"]
    assert calls == [f"route:{CANON}"]
    again = await fetch_chain.fetch(URL)
    assert again.metadata["from_cache"] is True
    assert calls == [f"route:{CANON}"]


async def test_failed_route_hands_over_to_the_tiers(chain):
    calls, script = chain
    script["route"] = _doc(CANON, "", "route:thingiverse", status="failed")
    script["http"] = (_doc(URL, LONG), APP_SHELL)
    script["browser"] = (_doc(URL, LONG + " rendered", "browser"), "")
    doc = await fetch_chain.fetch(URL)
    assert calls == [f"route:{CANON}", "http", "browser"]
    assert doc.metadata["fetched_via"] == "browser"
    tried = doc.metadata["tiers_tried"]
    assert tried[0].startswith("route thingiverse: ")
    assert tried[1].startswith("http: empty app root")


async def test_crashing_route_hands_over_to_the_tiers(chain):
    calls, script = chain
    script["route"] = RuntimeError("browser died")
    script["http"] = (_doc(URL, LONG), "")
    doc = await fetch_chain.fetch(URL)
    assert doc.status == "success"
    assert (
        doc.metadata["tiers_tried"][0]
        == "route thingiverse: RuntimeError: browser died"
    )


async def test_route_needs_the_stealth_tier(chain, monkeypatch):
    calls, script = chain
    monkeypatch.setenv("FETCH_TIERS", "http,browser")
    script["http"] = (_doc(URL, LONG), "")
    await fetch_chain.fetch(URL)
    assert calls == ["http"]


async def test_metadata_only_http_page_is_not_accepted(chain):
    calls, script = chain
    url = "https://spa.test/item/1"
    script["http"] = (_doc(url, LONG), APP_SHELL)
    script["browser"] = (_doc(url, "# Something went wrong", "browser"), "")
    script["stealth"] = (_doc(url, LONG + " " + LONG, "stealth"), "")
    doc = await fetch_chain.fetch(url)
    assert calls == ["http", "browser", "stealth"]
    assert doc.metadata["fetched_via"] == "stealth"
