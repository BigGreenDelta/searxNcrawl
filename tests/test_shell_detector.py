"""Shell detector: pages whose script-loaded content has not arrived yet.

The samples are cut down from real captures of parcel tracking pages
(2026-11-20) that the router used to return as if they were the page.
"""

from __future__ import annotations

from crawler import fetch_chain
from crawler.document import CrawledDocument
from crawler.fetch_cache import normalize_url

PROSE = (
    "The parcel was accepted by the carrier and is on its way to the sorting "
    "centre, where it will be scanned again before it leaves the country. "
)

TRACK24_SHELL = """# All In One Package Tracking
Try: LA094212783SE, EN359743995JP, GP0005499672, LP00730703835435
Your recently tracking numbers
Supports 723 postal Carriers & Multiple Express Couriers Worldwide including
China Post, Hongkong Post, Singapore Post, USPS, EMS, Fedex, DHL, UPS etc...
Loading...
Loading...
Loading...
Loading...
Track24.net provides global-based tracking service for postal/express packages.
"""

TRACK24_RESULTS = TRACK24_SHELL + (
    "**13.11.2026** 17:23:53\nAccepted by carrier\nNew Cainiao\n"
    "**20.11.2026** 12:26:00\nLeft from departure country/region\n"
) + PROSE * 12

ORDERTRACKER_SHELL = """Loading
  1. Checking the tracking number
  2. Looking up the carrier
  3. Locating your package
...
...
Settings
Select the carrier manually (recommended)Try another tracking number
## .................................................................
..................................................................
"""

TRACKINGMORE_SHELL = r"""No courier found
- Copy Link
- {{ item.track\_number }} Pending {{ item.substatusDesc }} · {{ item.itemTimeLength }}day
Tracking...Please wait a moment. {{ item.company\_name }}
{{ item.countryCode ? item.countryCode : 'Unknown' }}
Result Pending We haven't received any tracking updates yet.
"""

VUE_DOCS = (
    "# Template Syntax\n\nThe most basic form of data binding is text "
    "interpolation using the Mustache syntax:\n\n"
    "```vue-html\n<span>Message: {{ msg }}</span>\n<p>{{ count + 1 }}</p>\n"
    "<p>{{ ok ? 'YES' : 'NO' }}</p>\n```\n\n"
    "Use `{{ message.split('').reverse().join('') }}` for expressions.\n"
)


def _doc(markdown: str, **meta) -> CrawledDocument:
    return CrawledDocument(
        request_url="https://a.test",
        final_url="https://a.test",
        status="success",
        markdown=markdown,
        metadata=meta,
    )


def test_loading_placeholders_are_a_shell():
    assert "loading placeholders" in fetch_chain.loading_shell(TRACK24_SHELL)


def test_progress_steps_and_dot_lines_are_a_shell():
    assert "loading placeholders" in fetch_chain.loading_shell(ORDERTRACKER_SHELL)


def test_unrendered_templates_are_a_shell():
    reason = fetch_chain.loading_shell(TRACKINGMORE_SHELL)
    assert reason and "unrendered templates" in reason


def test_placeholders_next_to_real_results_are_not_a_shell():
    # track24 keeps its "Loading..." boxes after the results are in.
    assert fetch_chain.loading_shell(TRACK24_RESULTS) is None


def test_template_syntax_in_code_blocks_is_not_a_shell():
    assert fetch_chain.loading_shell(VUE_DOCS) is None


def test_ordinary_prose_is_not_a_shell():
    text = "# Fast-loading pages\n\nLoading speed matters.\n\n" + PROSE * 3
    assert fetch_chain.loading_shell(text) is None


def test_a_single_placeholder_is_not_a_shell():
    assert fetch_chain.loading_shell("Your basket\nLoading...\n" + PROSE) is None


def test_rendered_page_with_little_text_in_much_html_is_a_shell():
    # Royal Mail tracking: 832 chars of chrome in 208 KB of rendered DOM.
    doc = _doc("Recently tracked items\n" + "Need help? " * 60)
    reason = fetch_chain.rendered_shell(doc, "<div></div>" * 20_000)
    assert reason and "script shell" in reason


def test_rendered_page_with_enough_text_is_not_a_shell():
    doc = _doc(PROSE * 20)
    assert fetch_chain.rendered_shell(doc, "<div></div>" * 20_000) is None


def test_small_rendered_page_is_not_a_shell():
    assert fetch_chain.rendered_shell(_doc("Example Domain"), "<p>x</p>") is None


def test_http_shell_needs_a_browser():
    assert fetch_chain.needs_browser(_doc(TRACKINGMORE_SHELL), "")


def test_hash_routes():
    assert fetch_chain.hash_route("https://a.test/track#/results/QP1")
    assert fetch_chain.hash_route("https://a.test/app#!/results/QP1")
    assert not fetch_chain.hash_route("https://a.test/docs#section-2")
    assert not fetch_chain.hash_route("https://a.test/docs")


def test_cache_key_keeps_hash_routes_and_drops_anchors():
    a = normalize_url("https://A.test/track#/results/QP1")
    b = normalize_url("https://a.test/track#/results/QP2")
    assert a != b and a.endswith("#/results/QP1")
    assert normalize_url("https://a.test/docs#intro") == "https://a.test/docs"
