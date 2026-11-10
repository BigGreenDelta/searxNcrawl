"""Product facts read from markup: Amazon widgets and schema.org JSON-LD."""

from __future__ import annotations

import json

from crawler import fetch_chain, product_facts
from crawler.document import CrawledDocument

CAROUSEL = """
<div id="sims-discoveryAndInspiration_feature_div_0">
  <i class="a-icon-star"><span class="a-icon-alt">3.9 out of 5 stars</span></i>
</div>"""

AMAZON = f"""<html><body>
{CAROUSEL}
<span id="productTitle">  Silicone Soldering Mat 30x20cm  </span>
<div id="averageCustomerReviews_feature_div">
 <div id="averageCustomerReviews">
  <span id="acrPopover" title="4.5 out of 5 stars"><span class="a-icon-alt">4.5 out of 5 stars</span></span>
  <span id="acrCustomerReviewText">(1,190)</span>
 </div>
</div>
<div id="corePriceDisplay_desktop_feature_div">
  <span class="a-price apex-pricetopay-value"><span class="a-offscreen">£8.99</span></span>
</div>
<div id="availability"><span> In stock </span></div>
</body></html>"""

AMAZON_UNRATED = """<html><body>
<span id="productTitle">New stand</span>
<div id="averageCustomerReviews_feature_div"></div>
<div id="corePrice_feature_div"><span class="a-offscreen">£37.20</span></div>
<div class="cr-lightbox-review-row-no-display"><span class="a-icon-alt">5.0 out of 5 stars</span></div>
</body></html>"""

URL = "https://www.amazon.co.uk/dp/B07WQG48JL"


def test_amazon_rating_comes_from_the_product_not_the_carousel():
    facts = product_facts.extract(AMAZON, URL)
    assert "**Rating:** 4.5 out of 5 stars (1,190 ratings)" in facts
    assert "3.9" not in facts
    assert "**Price:** £8.99" in facts
    assert "**Availability:** In stock" in facts
    assert "**Title:** Silicone Soldering Mat 30x20cm" in facts


def test_amazon_unrated_listing_says_so():
    facts = product_facts.extract(
        AMAZON_UNRATED, "https://www.amazon.co.uk/gp/product/B0H841CPY9"
    )
    assert "no customer ratings yet" in facts and "5.0" not in facts
    assert "£37.20" in facts


def test_json_ld_product():
    ld = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebPage"},
            {
                "@type": "Product",
                "name": "Iron",
                "aggregateRating": {"ratingValue": 4.7, "reviewCount": 312},
                "offers": {
                    "price": "24.99",
                    "priceCurrency": "GBP",
                    "availability": "https://schema.org/InStock",
                },
            },
        ],
    }
    html = f'<html><script type="application/ld+json">{json.dumps(ld)}</script></html>'
    facts = product_facts.extract(html, "https://shop.test/iron")
    assert "4.7 out of 5 (312 ratings)" in facts
    assert "24.99 GBP" in facts and "InStock" in facts


def _card(
    asin, title, price=None, stars=None, count=None, bought=None, sponsored=False
):
    bits = [f'<div data-component-type="s-search-result" data-asin="{asin}">']
    bits.append(f'<h2 aria-label="Sponsored Ad - {title}"><span>{title}</span></h2>')
    if sponsored:
        bits.append('<span class="puis-sponsored-label-text">Sponsored</span>')
    if stars:
        bits.append(
            f'<a aria-label="{stars}, rating details"><i><span class="a-icon-alt">{stars}</span></i></a>'
        )
        bits.append(
            f'<a aria-label="{count} ratings"><span class="s-underline-text">(1)</span></a>'
        )
    if bought:
        bits.append(
            f"<div><span>{bought}</span> <span>Price, product page</span></div>"
        )
    if price:
        bits.append(
            f'<span class="a-price"><span class="a-offscreen">{price}</span></span>'
        )
    bits.append("</div>")
    return "".join(bits)


SEARCH = (
    "<html><body>"
    + _card(
        "B000000001",
        "Mat one",
        "£8.99",
        "4.6 out of 5 stars",
        "5,857",
        "100+ bought in past month",
    )
    + _card("B000000002", "Mat two", "£5.49", sponsored=True)
    + _card("B000000002", "Mat two again", "£5.49")
    + _card("B000000003", "Mat three", None, "4.0 out of 5 stars", "12")
    + "</body></html>"
)


def test_amazon_search_results_one_line_each():
    out = product_facts.listing(SEARCH, "https://www.amazon.co.uk/s?k=mat")
    lines = out.splitlines()[2:]
    assert lines[0] == (
        "1. **Mat one** - £8.99 - 4.6 out of 5 stars (5,857 ratings)"
        " - 100+ bought in past month - https://www.amazon.co.uk/dp/B000000001"
    )
    assert "no ratings yet - sponsored" in lines[1]
    assert lines[2].startswith(
        "3. **Mat three** - no price shown - 4.0 out of 5 stars (12 ratings)"
    )
    assert len(lines) == 3  # duplicate ASIN dropped


def test_search_listing_only_for_amazon_search_pages():
    assert (
        product_facts.listing(SEARCH, "https://www.amazon.co.uk/dp/B000000001") is None
    )
    assert product_facts.listing(SEARCH, "https://shop.test/s?k=mat") is None


def test_fetch_replaces_search_text_with_listing():
    doc = CrawledDocument(
        "https://www.amazon.co.uk/s?k=mat",
        "https://www.amazon.co.uk/s?k=mat",
        "success",
        "tracking-link soup",
        metadata={},
    )
    fetch_chain._add_product_facts(doc, SEARCH)
    assert doc.markdown.startswith("## Search results") and "soup" not in doc.markdown


def test_scrapling_markdown_method_never_becomes_page_text():
    class Resp:
        status = 200
        url = "https://a.test"
        headers = {"content-type": "text/html"}
        html_content = "<html><body></body></html>"

        def markdown(self):  # Scrapling exposes this as a method on some responses
            return "x"

    doc, _ = fetch_chain._doc_from_scrapling("https://a.test", Resp(), "http")
    assert isinstance(doc.markdown, str)


def test_plain_page_has_no_facts():
    assert product_facts.extract("<html><p>Hello</p></html>", "https://a.test") is None
    assert product_facts.extract(AMAZON, "https://www.amazon.co.uk/s?k=mat") is None


def test_fetch_puts_facts_above_text_and_amazon_is_not_a_shell(monkeypatch):
    big = AMAZON.replace("</body>", "<script>" + "x" * 2_000_000 + "</script></body>")
    doc = CrawledDocument(
        URL, URL, "success", "word " * 4000, metadata={"fetched_via": "http"}
    )
    assert fetch_chain.script_shell(doc, big) is None
    fetch_chain._add_product_facts(doc, big)
    assert doc.markdown.startswith("## Product facts")
    assert doc.metadata["product_facts"] is True
