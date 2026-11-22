"""AliExpress route: URL matching, the pdp answer, Markdown, OCR wiring, blocks.

The fixtures are the page's own ``mtop.aliexpress.pdp.pc.query`` answers and
description files for three real item pages (UK / GBP), saved from a browser
on 2026-11-22 with the buyer id and ship-to region replaced. ``punish.jsonp``
is the slider CAPTCHA answer to the same request. No test uses the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crawler import aliexpress as ae
from crawler import fetch_chain, site_routes
from crawler.document import CrawledDocument

FIXTURES = Path(__file__).parent / "fixtures" / "aliexpress"
SWITCH = "1005013417484054"  # two option groups, 8 combinations
SENSOR = "1005003561184819"  # one option group, 7 combinations, text description
SCREEN = "1005012556272268"  # no options, description made of images only


def _fixture(pid: str) -> dict:
    return json.loads((FIXTURES / f"ae-{pid}.json").read_text(encoding="utf-8"))


def _item(pid: str) -> dict:
    return ae.item_data(ae.pdp_result(_fixture(pid)["pdp"]), pid)


# --- URLs -------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        (f"https://www.aliexpress.com/item/{SWITCH}.html", SWITCH),
        (
            f"https://www.aliexpress.com/item/{SWITCH}.html?spm=a2g0o.home&gatewayAdapt=glo2gbr",
            SWITCH,
        ),
        (f"https://aliexpress.com/item/{SENSOR}.html#nav", SENSOR),
        (f"https://m.aliexpress.com/item/{SENSOR}.html", SENSOR),
        (f"https://de.aliexpress.com/item/{SCREEN}.html", SCREEN),
        (f"https://www.aliexpress.us/item/{SCREEN}.html", SCREEN),
        ("https://www.aliexpress.com/store/911941543", None),
        ("https://www.aliexpress.com/w/wholesale-switch.html", None),
        (f"https://aliexpress.ru/item/{SCREEN}.html", None),
        (f"https://example.com/item/{SCREEN}.html", None),
    ],
)
def test_item_id(url, expected):
    assert ae.item_id(url) == expected


def test_route_matches_the_canonical_item_page():
    route, target = site_routes.match(
        f"https://www.aliexpress.com/item/{SWITCH}.html?spm=x&algo_pvid=y"
    )
    assert route.name == "aliexpress"
    assert route.uses_ocr is True
    assert target == f"https://www.aliexpress.com/item/{SWITCH}.html"


# --- the pdp answer ---------------------------------------------------------


def test_punish_answer_is_blocked():
    answer = ae.parse_mtop((FIXTURES / "punish.jsonp").read_text(encoding="utf-8"))
    assert answer["ret"][0] == "FAIL_SYS_USER_VALIDATE"
    reason = ae.blocked_reason(answer)
    assert reason.startswith("blocked by AliExpress anti-bot")
    assert "FAIL_SYS_USER_VALIDATE" in reason
    assert ae.pdp_result(answer) is None


def test_successful_answer_is_not_blocked():
    answer = _fixture(SWITCH)["pdp"]
    assert ae.blocked_reason(answer) is None
    assert "SKU" in ae.pdp_result(answer)


def test_parse_mtop_plain_json_and_junk():
    assert ae.parse_mtop('{"ret": ["SUCCESS::ok"]}') == {"ret": ["SUCCESS::ok"]}
    assert ae.parse_mtop("mtopjsonp3(not json)") is None
    assert ae.parse_mtop("") is None


def test_switch_variants_cover_every_combination():
    item = _item(SWITCH)
    assert item["title"].startswith("10/100pcs Mechanical Switch Fidget Clicker")
    groups = {
        g["name"]: [v["name"] for v in g["values"]] for g in item["option_groups"]
    }
    assert groups == {
        "Color": ["10PCS", "30PCS"],
        "Gem Color": ["10PCS", "30PCS", "50PCS", "100PCS"],
    }
    colour = item["option_groups"][0]["values"][0]
    assert colour["image"] == (
        "https://ae-pic-a1.aliexpress-media.com/kf/S73db1dd8f21d48dc95bfbdec1ddce466D.jpg"
    )
    variants = item["variants"]
    assert len(variants) == 8
    combos = {(v["options"]["Color"], v["options"]["Gem Color"]) for v in variants}
    assert len(combos) == 8
    last = variants[-1]
    assert last["options"] == {"Color": "30PCS", "Gem Color": "100PCS"}
    assert last["sku_id"] == "12000061097254429"
    assert (last["price"], last["sale_price"], last["currency"]) == (8.93, 8.5, "GBP")
    assert last["discount"] == "4% off"
    assert last["stock"] == 9961
    assert last["available"] is True


def test_switch_facts():
    item = _item(SWITCH)
    assert item["price"] == {
        "currency": "GBP",
        "price": 1.54,
        "sale_price": 1.39,
        "discount": "9% off",
    }
    assert (item["rating"], item["reviews"], item["sold"]) == (5.0, 2, "202 sold")
    assert item["store"]["name"] == "Shop5655018 Store"
    assert item["store"]["url"] == "https://www.aliexpress.com/store/5655018"
    assert len(item["images"]) == 6
    assert ("Material", "Resin") in item["specs"]
    assert len(item["specs"]) == 15
    ship = item["shipping"][0]
    assert (ship["fee"], ship["currency"], ship["free_over"]) == (1.99, "GBP", "£8.00")
    assert item["guide"]["url"].endswith(".pdf")
    assert item["desc_url"].startswith("https://")


def test_sensor_has_one_group_and_free_shipping():
    item = _item(SENSOR)
    assert [g["name"] for g in item["option_groups"]] == ["Color Name"]
    assert len(item["variants"]) == 7
    assert all(v["currency"] == "GBP" for v in item["variants"])
    assert all(isinstance(v["stock"], int) for v in item["variants"])
    fees = [(s["method"], s["fee"]) for s in item["shipping"]]
    assert fees == [
        ("AliExpress Standard Shipping", 0.0),
        ("AliExpress Premium Shipping", 36.85),
    ]
    assert item["reviews"] == 0


def test_screen_without_options():
    item = _item(SCREEN)
    assert item["option_groups"] == []
    assert len(item["variants"]) == 1
    assert item["price"]["sale_price"] == 4.89
    assert item["price"]["price"] == 12.04
    assert (item["rating"], item["reviews"]) == (4.5, 144)
    md = ae.item_markdown(item, "https://www.aliexpress.com/item/x.html")
    assert "## Variants" not in md
    assert "## Options" not in md
    assert "- **Price:** £4.89 (was £12.04) (GBP)" in md


# --- description ------------------------------------------------------------


@pytest.mark.parametrize("pid, count", [(SWITCH, 14), (SENSOR, 19), (SCREEN, 5)])
def test_description_images(pid, count):
    images = ae.description_images(_fixture(pid)["desc"])
    assert len(images) == count
    assert all(u.startswith("https://ae-pic-a1.aliexpress-media.com/") for u in images)


def test_description_markdown_puts_ocr_under_each_image():
    html = _fixture(SCREEN)["desc"]
    images = ae.description_images(html)
    md = ae.description_markdown(
        html,
        {images[0]: ["Size: 2.10 inch Resolution: 360*360"], images[1]: []},
        {images[2]: "over the 2-image limit"},
    )
    first = md.index(f"![]({images[0]})")
    ocr = md.index("> **OCR text from this image** (machine-read):")
    assert first < ocr < md.index(f"![]({images[1]})")
    assert "> Size: 2.10 inch Resolution: 360*360" in md
    assert "> **OCR:** no text found in this image." in md
    assert "> **OCR skipped:** over the 2-image limit." in md
    assert "adminAccountId" not in md  # the description's script is dropped


def test_description_text_survives():
    md = ae.description_markdown(_fixture(SENSOR)["desc"])
    assert "for Lexus CT 200h ZWA10 2014~2020" in md
    assert "Working temperature range: -20℃-80℃" in md
    assert len(md) > 4000


def test_item_markdown_sections():
    item = _item(SWITCH)
    md = ae.item_markdown(
        item, "https://www.aliexpress.com/item/1.html", "desc", "OCR: note"
    )
    assert md.startswith("# 10/100pcs Mechanical Switch")
    assert (
        "- **Price:** £1.39 (was £1.54, 9% off); £1.39 - £8.50 across 8 variants (GBP)"
        in md
    )
    assert "- **Rating:** 5.0/5, 2 reviews, 202 sold" in md
    assert "20,000+ sold in 180 days" in md
    assert "store rating: 4.9" in md
    assert (
        "- **Shipping to UK:** Aliexpress Selection Premium shipping: £1.99, "
        "free over £8.00, delivery Nov. 29 - Dec. 02, from China"
    ) in md
    assert "## Options (2 groups, 8 combinations)" in md
    assert "| Color | Gem Color | Price | Sale price | Discount | Stock | SKU |" in md
    assert (
        "| 30PCS | 100PCS | £8.93 | £8.50 | 4% off | 9,961 | 12000061097254429 |" in md
    )
    assert "Prices in GBP." in md
    assert "| Material | Resin |" in md
    assert md.index("## Description") < md.index("OCR: note") < md.index("desc\n")


# --- the route --------------------------------------------------------------


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch):
    """The route with the browser, the description download and OCR scripted."""
    state: dict = {"pdp": _fixture(SCREEN)["pdp"], "problem": None, "ocr_calls": []}

    async def load_pdp(url, timeout):
        return state["pdp"], state["problem"], 200

    async def get(url, limit, accept="*/*"):
        return _fixture(SCREEN)["desc"].encode()

    async def fake_ocr(urls):
        state["ocr_calls"].append(list(urls))
        return (
            {u: [f"text of {u[-6:]}"] for u in urls[:2]},
            {u: "over the 2-image limit" for u in urls[2:]},
            1.5,
        )

    monkeypatch.setattr(ae, "_load_pdp", load_pdp)
    monkeypatch.setattr(ae, "_get", get)
    monkeypatch.setattr(ae, "_ocr", fake_ocr)
    return state


async def test_run_builds_the_page(offline):
    url = f"https://www.aliexpress.com/item/{SCREEN}.html"
    doc = await ae.run(url, 45)
    assert doc.status == "success"
    assert doc.metadata["fetched_via"] == "route:aliexpress"
    info = doc.metadata["aliexpress"]
    assert (info["description_images"], info["ocr_images"]) == (5, 2)
    assert len(info["variants"]) == 1
    assert "OCR: 2 of 5 description images read on this server" in doc.markdown
    assert "> text of " in doc.markdown
    assert "> **OCR skipped:** over the 2-image limit." in doc.markdown
    assert len(offline["ocr_calls"]) == 1


async def test_run_without_ocr(offline):
    doc = await ae.run(f"https://www.aliexpress.com/item/{SCREEN}.html", 45, ocr=False)
    assert doc.status == "success"
    assert offline["ocr_calls"] == []
    assert "OCR: off for this crawl (ocr=false)." in doc.markdown
    assert "**OCR" not in doc.markdown


async def test_run_blocked_is_final(offline):
    offline["pdp"] = None
    offline["problem"] = ae.blocked_reason(
        ae.parse_mtop((FIXTURES / "punish.jsonp").read_text(encoding="utf-8"))
    )
    doc = await ae.run(f"https://www.aliexpress.com/item/{SCREEN}.html", 45)
    assert doc.status == "failed"
    assert doc.error_message.startswith("blocked by AliExpress anti-bot")
    assert doc.metadata["blocked"] is True
    assert doc.metadata["final"] is True


async def test_run_without_item_data_is_not_final(offline):
    offline["pdp"] = None
    doc = await ae.run(f"https://www.aliexpress.com/item/{SCREEN}.html", 45)
    assert doc.status == "failed"
    assert doc.error_message == "the page sent no item data"
    assert "final" not in doc.metadata


# --- the chain --------------------------------------------------------------


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch):
    """Scripted tiers and a scripted AliExpress route; records the calls."""
    calls: list = []
    script: dict = {}

    def make(name):
        async def run(url, timeout, **kwargs):
            calls.append(name)
            return script[name]

        return run

    async def route_run(url, timeout, ocr=True):
        calls.append(("route", ocr))
        return script["route"]

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
    return calls, script


URL = f"https://www.aliexpress.com/item/{SWITCH}.html?spm=tracking"
LONG = "word " * 200


def _doc(markdown: str, **meta) -> CrawledDocument:
    status = "success" if markdown else "failed"
    return CrawledDocument(
        request_url=URL,
        final_url=URL,
        status=status,
        markdown=markdown,
        error_message=meta.pop("error", None),
        metadata={"fetched_via": "route:aliexpress", **meta},
    )


async def test_blocked_route_stops_the_chain(chain):
    calls, script = chain
    script["route"] = _doc(
        "",
        error="blocked by AliExpress anti-bot (slider CAPTCHA)",
        blocked=True,
        final=True,
    )
    doc = await fetch_chain.fetch(URL)
    assert doc.status == "failed"
    assert doc.error_message.startswith("blocked by AliExpress anti-bot")
    assert calls == [("route", True)]
    assert (
        doc.metadata["tiers_tried"][-1] == "other tiers: skipped, the route was blocked"
    )
    # A block is not cached: the next call tries again.
    await fetch_chain.fetch(URL)
    assert calls == [("route", True), ("route", True)]


async def test_ocr_off_is_cached_apart(chain):
    calls, script = chain
    script["route"] = _doc(LONG + "with ocr")
    first = await fetch_chain.fetch(URL)
    assert first.markdown.endswith("with ocr")
    script["route"] = _doc(LONG + "no ocr")
    second = await fetch_chain.fetch(URL, ocr=False)
    assert second.markdown.endswith("no ocr")
    assert calls == [("route", True), ("route", False)]
    assert (await fetch_chain.fetch(URL)).markdown.endswith("with ocr")
    assert (await fetch_chain.fetch(URL, ocr=False)).markdown.endswith("no ocr")
    assert len(calls) == 2


async def test_fetch_many_passes_ocr(chain):
    calls, script = chain
    script["route"] = _doc(LONG)
    await fetch_chain.fetch_many([URL], ocr=False)
    assert calls == [("route", False)]
