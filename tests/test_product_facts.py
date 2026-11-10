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
