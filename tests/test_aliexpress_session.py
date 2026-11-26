"""AliExpress route with a logged-in session: bulk prices, labels, fallbacks, cache.

Sessions here are fake cookies made in memory; the browser is scripted. No
test uses the network or a real account.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from crawler import aliexpress as ae
from crawler import fetch_chain, site_routes, site_sessions
from crawler.document import CrawledDocument

FIXTURES = Path(__file__).parent / "fixtures" / "aliexpress"
SWITCH = "1005013417484054"  # two option groups, 8 combinations
SCREEN = "1005012556272268"  # no options


def _fixture(pid: str) -> dict:
    return json.loads((FIXTURES / f"ae-{pid}.json").read_text(encoding="utf-8"))

FAKE = "fake-cookie-not-a-secret"
REF = "aliexpress=op://web-sessions/aliexpress/storage_state"
REGION = {"country": "UK", "currency": "GBP", "lang": "en_GB"}


def _cookie(name: str, value: str = FAKE, expires: float | None = None) -> dict:
    return {
        "name": name,
        "value": value,
        "domain": ".aliexpress.com",
        "path": "/",
        "expires": time.time() + 86400 if expires is None else expires,
    }


def _with_ladder(pdp: dict, tiers: dict) -> dict:
    pdp = copy.deepcopy(pdp)
    ae.pdp_result(pdp)["LADDER_PRICE"] = {"ladderPriceViewMap": tiers}
    return pdp


def _switch_skus() -> list[str]:
    return [v["sku_id"] for v in ae.item_data(ae.pdp_result(_fixture(SWITCH)["pdp"]), SWITCH)["variants"]]


# --- LADDER_PRICE --------------------------------------------------------------


def test_ladder_prices_per_sku():
    first, second, *_ = _switch_skus()
    result = ae.pdp_result(
        _with_ladder(
            _fixture(SWITCH)["pdp"],
            {
                first: [
                    {"bulkQuantity": 10, "bulkPrice": "£4.00"},
                    {"bulkQuantity": 2, "bulkPrice": "£5.11"},
                    "junk",
                    {"bulkQuantity": 0, "bulkPrice": "£1.00"},
                ],
                second: [{"bulkQuantity": "2", "bulkPrice": {"value": 9.6, "currency": "GBP"}}],
                "999": "junk",
            },
        )
    )
    ladder = ae.ladder_prices(result)
    assert ladder == {
        first: [{"quantity": 2, "price": 5.11}, {"quantity": 10, "price": 4.0}],
        second: [{"quantity": 2, "price": 9.6}],
    }
    item = ae.item_data(result, SWITCH)
    by_sku = {v["sku_id"]: v["bulk"] for v in item["variants"]}
    assert by_sku[first][0] == {"quantity": 2, "price": 5.11}
    assert all(by_sku[s] == [] for s in by_sku if s not in (first, second))


def test_no_ladder_module_means_no_bulk():
    assert ae.ladder_prices(ae.pdp_result(_fixture(SWITCH)["pdp"])) == {}


def test_bulk_text():
    assert ae.bulk_text([{"quantity": 2, "price": 5.11}], "GBP") == "£5.11 each - £10.22 for 2"
    assert ae.bulk_text([], "GBP") == ""


# --- labels ----------------------------------------------------------------------


def _switch_item_with_bulk() -> dict:
    first = _switch_skus()[0]
    pdp = _with_ladder(_fixture(SWITCH)["pdp"], {first: [{"bulkQuantity": 2, "bulkPrice": "£5.11"}]})
    return ae.item_data(ae.pdp_result(pdp), SWITCH)


def test_logged_in_sheet_labels_its_prices():
    md = ae.item_markdown(_switch_item_with_bulk(), "https://x", session=ae.LOGGED_IN)
    assert "- **Prices:** your own, logged in with your AliExpress session." in md
    assert "| Color | Gem Color | Price | Your price (logged in) | Bulk (logged in) | Discount | Stock | SKU |" in md
    assert "£5.11 each - £10.22 for 2" in md
    assert "new-shopper" not in md


@pytest.mark.parametrize(
    "state, why, line",
    [
        (ae.NO_SESSION, "", "logged out (no AliExpress session on this server)"),
        (ae.EXPIRED, "", "your AliExpress session has expired or was not accepted"),
        (ae.SESSION_ERROR, "op read failed: x", "could not be read (op read failed: x)"),
    ],
)
def test_logged_out_sheets_say_new_shopper(state, why, line):
    md = ae.item_markdown(_switch_item_with_bulk(), "https://x", session=state, session_why=why)
    assert line in md
    assert "sale prices are new-shopper (welcome deal) prices, not your regular price" in md
    assert "| Price | Sale price (new-shopper) | Bulk | Discount |" in md
    assert "(logged in)" not in md


def test_sheet_without_session_info_is_unchanged():
    md = ae.item_markdown(ae.item_data(ae.pdp_result(_fixture(SWITCH)["pdp"]), SWITCH), "https://x")
    assert "**Prices:**" not in md
    assert "| Color | Gem Color | Price | Sale price | Discount | Stock | SKU |" in md


def test_single_variant_shows_its_bulk_line():
    item = ae.item_data(ae.pdp_result(_fixture(SCREEN)["pdp"]), SCREEN)
    sku = item["variants"][0]["sku_id"]
    pdp = _with_ladder(_fixture(SCREEN)["pdp"], {sku: [{"bulkQuantity": 3, "bulkPrice": "£2.00"}]})
    md = ae.item_markdown(ae.item_data(ae.pdp_result(pdp), SCREEN), "https://x", session=ae.LOGGED_IN)
    assert "- **Bulk:** £2.00 each - £6.00 for 3" in md


# --- the route with a session -----------------------------------------------------


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch):
    """The route with op, both page loads, the description and OCR scripted."""
    state: dict = {
        "session": None,
        "why": None,
        "still_in": True,
        "load_error": False,
        "plain_loads": 0,
        "session_loads": [],
    }

    def get(name):
        return state["session"], state["why"]

    async def load_pdp(url, timeout):
        state["plain_loads"] += 1
        return _fixture(SCREEN)["pdp"], None, 200, REGION

    async def load_pdp_session(url, timeout, cookies):
        state["session_loads"].append(cookies)
        if state["load_error"]:
            raise ae.SessionLoadError("logged-in page load failed (TimeoutError)")
        return _fixture(SCREEN)["pdp"], None, 200, REGION, state["still_in"]

    async def fetch(url, limit, accept="*/*"):
        return _fixture(SCREEN)["desc"].encode()

    async def no_ocr(urls):
        return {}, {}, 0.0

    monkeypatch.setattr(site_sessions, "get", get)
    monkeypatch.setattr(ae, "_load_pdp", load_pdp)
    monkeypatch.setattr(ae, "_load_pdp_session", load_pdp_session)
    monkeypatch.setattr(ae, "_get", fetch)
    monkeypatch.setattr(ae, "_ocr", no_ocr)
    monkeypatch.setenv("SITE_SESSIONS", REF)
    return state


ITEM_URL = f"https://www.aliexpress.com/item/{SCREEN}.html"


def _no_secret(doc: CrawledDocument) -> None:
    assert FAKE not in doc.markdown
    assert FAKE not in json.dumps(doc.metadata, default=str)


async def test_logged_in_load_uses_the_session(scripted, caplog):
    caplog.set_level(logging.DEBUG)
    scripted["session"] = site_sessions.Session("aliexpress", [_cookie("_hvn_login")])
    doc = await ae.run(ITEM_URL, 45)
    assert doc.status == "success"
    assert doc.metadata["session"] == ae.LOGGED_IN
    assert "no_cache" not in doc.metadata
    assert scripted["plain_loads"] == 0
    assert scripted["session_loads"][0][0]["value"] == FAKE
    assert "your own, logged in" in doc.markdown
    _no_secret(doc)
    assert FAKE not in caplog.text


async def test_session_the_site_drops_is_expired(scripted):
    scripted["session"] = site_sessions.Session("aliexpress", [_cookie("_hvn_login")])
    scripted["still_in"] = False
    doc = await ae.run(ITEM_URL, 45)
    assert doc.metadata["session"] == ae.EXPIRED
    assert doc.metadata["no_cache"] is True
    assert "has expired or was not accepted" in doc.markdown
    _no_secret(doc)


async def test_session_with_run_out_login_cookie_is_not_used(scripted):
    scripted["session"] = site_sessions.Session(
        "aliexpress", [_cookie("_hvn_login", expires=time.time() - 60)]
    )
    doc = await ae.run(ITEM_URL, 45)
    assert doc.metadata["session"] == ae.EXPIRED
    assert scripted["session_loads"] == [] and scripted["plain_loads"] == 1


async def test_failed_logged_in_load_falls_back_to_the_logged_out_sheet(scripted):
    scripted["session"] = site_sessions.Session("aliexpress", [_cookie("_hvn_login")])
    scripted["load_error"] = True
    doc = await ae.run(ITEM_URL, 45)
    assert doc.status == "success"
    assert doc.metadata["session"] == ae.SESSION_ERROR
    assert doc.metadata["no_cache"] is True
    assert scripted["plain_loads"] == 1
    assert "could not be read (logged-in page load failed (TimeoutError))" in doc.markdown
    _no_secret(doc)


async def test_unreadable_session_is_said_plainly(scripted):
    scripted["why"] = "op read op://web-sessions/aliexpress/storage_state failed: not signed in"
    doc = await ae.run(ITEM_URL, 45)
    assert doc.metadata["session"] == ae.SESSION_ERROR
    assert doc.metadata["no_cache"] is True
    assert "not signed in" in doc.markdown
    assert "new-shopper" in doc.markdown


async def test_no_session_configured(scripted, monkeypatch):
    monkeypatch.delenv("SITE_SESSIONS")
    scripted["why"] = "no AliExpress session configured on this server"
    doc = await ae.run(ITEM_URL, 45)
    assert doc.metadata["session"] == ae.NO_SESSION
    assert "no_cache" not in doc.metadata
    assert "logged out (no AliExpress session on this server)" in doc.markdown


# --- the fetch cache -----------------------------------------------------------------


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch, tmp_path):
    calls: list = []
    script: dict = {}

    async def route_run(url, timeout, ocr=True):
        calls.append("route")
        return script["route"]

    async def tier(url, timeout, **kwargs):
        calls.append("tier")
        return CrawledDocument(request_url=url, final_url=url, status="failed", markdown="")

    monkeypatch.setattr(fetch_chain, "_RUNNERS", {n: tier for n in fetch_chain.FREE_TIERS})
    monkeypatch.setattr(
        site_routes,
        "ROUTES",
        (
            site_routes.Route(
                "aliexpress",
                ae.canonical_url,
                route_run,
                uses_ocr=True,
                cache_variant=site_routes._aliexpress_cache_variant,
            ),
        ),
    )
    monkeypatch.setattr(fetch_chain, "_cache", fetch_chain.FetchCache(tmp_path / "c.sqlite", 3600))
    monkeypatch.setenv("FETCH_TIERS", "http,browser,stealth")
    monkeypatch.setenv("FETCH_MIN_CHARS", "400")
    return calls, script


def _sheet(text: str, **meta) -> CrawledDocument:
    return CrawledDocument(
        request_url=ITEM_URL,
        final_url=ITEM_URL,
        status="success",
        markdown="word " * 200 + text,
        metadata={"fetched_via": "route:aliexpress", **meta},
    )


async def test_logged_in_and_logged_out_sheets_are_cached_apart(chain, monkeypatch):
    calls, script = chain
    monkeypatch.delenv("SITE_SESSIONS", raising=False)
    script["route"] = _sheet("logged out", session=ae.NO_SESSION)
    assert (await fetch_chain.fetch(ITEM_URL)).markdown.endswith("logged out")
    monkeypatch.setenv("SITE_SESSIONS", REF)
    script["route"] = _sheet("logged in", session=ae.LOGGED_IN)
    assert (await fetch_chain.fetch(ITEM_URL)).markdown.endswith("logged in")
    assert (await fetch_chain.fetch(ITEM_URL)).markdown.endswith("logged in")
    assert calls == ["route", "route"]


async def test_sheet_marked_no_cache_is_not_kept(chain, monkeypatch):
    calls, script = chain
    monkeypatch.setenv("SITE_SESSIONS", REF)
    script["route"] = _sheet("expired", session=ae.EXPIRED, no_cache=True)
    await fetch_chain.fetch(ITEM_URL)
    script["route"] = _sheet("logged in", session=ae.LOGGED_IN)
    assert (await fetch_chain.fetch(ITEM_URL)).markdown.endswith("logged in")
    assert calls == ["route", "route"]


def test_cache_variant_words():
    url = ITEM_URL
    assert fetch_chain._cache_variant("https://example.com/", False) == ""
    assert fetch_chain._cache_variant(url, True) in ("", "session")


# --- diagnostics -------------------------------------------------------------------


def test_login_keys_are_names_only():
    result = {"GLOBAL_DATA": {"buyerId": FAKE, "nested": {"isLogin": True}}, "PRICE": {"x": 1}}
    keys = ae.login_keys(result)
    assert keys == ["GLOBAL_DATA.buyerId", "GLOBAL_DATA.nested.isLogin"]
    assert FAKE not in json.dumps(keys)


def test_unread_ladder_reports_its_shape():
    result = {"LADDER_PRICE": {"ladderPriceViewMap": {"1": [{"minQty": 2, "bulkPrice": ["?"]}]}}}
    assert ae.ladder_shape(result) == {
        "module_keys": ["ladderPriceViewMap"],
        "tier_keys": ["bulkPrice", "minQty"],
        "bulkPrice_type": "list",
    }
    read = {"LADDER_PRICE": {"ladderPriceViewMap": {"1": [{"bulkQuantity": 2, "bulkPrice": "£1.00"}]}}}
    assert ae.ladder_shape(read) is None
    assert ae.ladder_shape({}) is None


# --- the logged-in page load, in a real headless browser ------------------------------


class _ItemSite(BaseHTTPRequestHandler):
    """An item page whose script asks for the pdp answer, like the real one."""

    seen: list = []
    pdp: bytes = b""

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).seen.append((self.path, self.headers.get("Cookie", "")))
        if ae._PDP_API in self.path:
            body, kind = type(self).pdp, "application/json"
        else:
            body = (
                "<html><body>item<script>"
                f"fetch('/h5/{ae._PDP_API}/1.0/?jsv=1')"
                "</script></body></html>"
            ).encode()
            kind = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def item_site(monkeypatch):
    _ItemSite.seen = []
    _ItemSite.pdp = json.dumps(_fixture(SCREEN)["pdp"]).encode()
    server = HTTPServer(("127.0.0.1", 0), _ItemSite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # The local server stands in for AliExpress, so its host counts as the site.
    spec = site_sessions.SITES["aliexpress"]
    monkeypatch.setitem(
        site_sessions.SITES,
        "aliexpress",
        site_sessions.Site(**{**spec.__dict__, "domains": ("127.0.0.1",)}),
    )
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _local_cookie(name: str, value: str = FAKE) -> dict:
    return {
        "name": name,
        "value": value,
        "domain": "127.0.0.1",
        "path": "/",
        "expires": time.time() + 3600,
    }


async def test_logged_in_load_sends_the_session_and_reads_the_answer(item_site):
    cookies = [_local_cookie("_hvn_login"), _local_cookie("aep_usuc_f", "own-region")]
    answer, problem, status, region, still_in = await ae._load_pdp_session(
        f"{item_site}/item/{SCREEN}.html", 20, cookies
    )
    assert problem is None and status == 200
    assert ae.pdp_result(answer) is not None
    assert still_in is True
    page_cookies = _ItemSite.seen[0][1]
    assert f"_hvn_login={FAKE}" in page_cookies
    # The user's own region cookie is kept, never replaced by the route's.
    assert "aep_usuc_f=own-region" in page_cookies
    assert any(ae._PDP_API in path for path, _ in _ItemSite.seen)


async def test_logged_in_load_sees_a_dropped_login(item_site):
    cookies = [_local_cookie("other")]
    *_, still_in = await ae._load_pdp_session(f"{item_site}/item/{SCREEN}.html", 20, cookies)
    assert still_in is False


async def test_logged_in_load_errors_name_only_the_type(monkeypatch):
    import patchright.async_api as pw_api

    def broken():
        raise RuntimeError(FAKE)

    monkeypatch.setattr(pw_api, "async_playwright", broken)
    with pytest.raises(ae.SessionLoadError) as err:
        await ae._load_pdp_session("https://www.aliexpress.com/item/1.html", 5, [_cookie("a")])
    assert str(err.value) == "logged-in page load failed (RuntimeError)"
    assert FAKE not in str(err.value)
