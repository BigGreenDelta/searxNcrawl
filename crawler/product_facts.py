"""Product facts (title, rating, price, availability) read from page markup.

Text extraction keeps prose and drops or mixes up the small widgets a shopper
cares about: Amazon's star rating sits in an attribute, and the "similar
products" carousels around it carry their own stars, so the extracted text can
show another product's rating or none. This module reads those fields from the
HTML itself and returns a short Markdown block that the fetch chain puts above
the page text.

Sources, in order: Amazon's product page widgets, then schema.org ``Product``
JSON-LD (used by most other shops).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

LOGGER = logging.getLogger(__name__)

_AMAZON_HOST = re.compile(r"(^|\.)amazon\.[a-z.]+$")
_AMAZON_PRODUCT_PATH = re.compile(r"/(dp|gp/product|gp/aw/d)/[A-Z0-9]{10}")
_SPACES = re.compile(r"\s+")


def _clean(text: Optional[str]) -> str:
    return _SPACES.sub(" ", text or "").strip()


def _first_text(tree, xpaths: Iterable[str]) -> str:
    for xp in xpaths:
        for node in tree.xpath(xp):
            text = _clean(node if isinstance(node, str) else node.text_content())
            if text:
                return text
    return ""


def _amazon(tree) -> dict[str, str]:
    facts: dict[str, str] = {}
    title = _first_text(tree, ['//*[@id="productTitle"]'])
    if title:
        facts["Title"] = title
    stars = _first_text(
        tree,
        [
            '//*[@id="acrPopover"]/@title',
            '//*[@id="averageCustomerReviews"]//*[contains(@class,"a-icon-alt")]',
        ],
    )
    count = _first_text(tree, ['//*[@id="acrCustomerReviewText"]'])
    if stars:
        rating = stars
        if count:
            n = count.strip("()")
            rating += f" ({n}{'' if 'rating' in n.lower() else ' ratings'})"
        facts["Rating"] = rating
    elif tree.xpath('//*[@id="averageCustomerReviews_feature_div"]'):
        # The widget is there but empty: a new listing nobody has rated yet.
        # (Stars elsewhere on such pages belong to carousels or hidden templates.)
        facts["Rating"] = "no customer ratings yet"
    price = _first_text(
        tree,
        [
            '//*[@id="corePriceDisplay_desktop_feature_div"]//*[contains(@class,"apex-pricetopay-value")]//*[contains(@class,"a-offscreen")]',
            '//*[@id="corePriceDisplay_desktop_feature_div"]//*[contains(@class,"a-price")]//*[contains(@class,"a-offscreen")]',
            '//*[@id="corePrice_feature_div"]//*[contains(@class,"a-offscreen")]',
            '//*[contains(@class,"apex-pricetopay-value")]//*[contains(@class,"a-offscreen")]',
            '//*[@id="priceblock_ourprice"]',
            '//*[@id="priceblock_dealprice"]',
        ],
    )
    if price:
        facts["Price"] = price
    availability = _first_text(
        tree, ['//*[@id="availability"]/span', '//*[@id="availability"]']
    )
    if availability:
        facts["Availability"] = availability[:120]
    bought = _first_text(tree, ['//*[@id="social-proofing-faceout-title-tk_bought"]'])
    if bought:
        facts["Recent sales"] = bought
    return facts


def _walk_ld(data: Any) -> Iterable[dict]:
    if isinstance(data, list):
        for item in data:
            yield from _walk_ld(item)
    elif isinstance(data, dict):
        kind = data.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if "Product" in kinds or "ProductGroup" in kinds:
            yield data
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in data:
                yield from _walk_ld(data[key])


def _json_ld(tree) -> dict[str, str]:
    for script in tree.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(script.text_content() or "")
        except ValueError:
            continue
        for product in _walk_ld(data):
            facts: dict[str, str] = {}
            if product.get("name"):
                facts["Title"] = _clean(str(product["name"]))
            rating = product.get("aggregateRating") or {}
            if isinstance(rating, dict) and rating.get("ratingValue") is not None:
                best = rating.get("bestRating") or 5
                text = f"{rating['ratingValue']} out of {best}"
                n = rating.get("reviewCount") or rating.get("ratingCount")
                if n is not None:
                    text += f" ({n} ratings)"
                facts["Rating"] = text
            offers = product.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("lowPrice")
                if price is not None:
                    facts["Price"] = (
                        f"{price} {offers.get('priceCurrency', '')}".strip()
                    )
                avail = offers.get("availability")
                if avail:
                    facts["Availability"] = str(avail).rsplit("/", 1)[-1]
            if len(facts) > 1:
                return facts
    return {}


def extract(html: str, url: str) -> Optional[str]:
    """A Markdown "Product facts" block for ``html``, or None if there is none."""
    if not html or "<" not in html:
        return None
    try:
        import lxml.html

        tree = lxml.html.fromstring(html)
    except Exception as exc:
        LOGGER.debug("product facts: cannot parse %s: %s", url, exc)
        return None
    parsed = urlparse(url)
    facts: dict[str, str] = {}
    if _AMAZON_HOST.search(parsed.hostname or "") and _AMAZON_PRODUCT_PATH.search(
        parsed.path
    ):
        facts = _amazon(tree)
    if not facts.get("Rating") or not facts.get("Price"):
        for key, value in _json_ld(tree).items():
            facts.setdefault(key, value)
    if not any(k in facts for k in ("Rating", "Price")):
        return None
    lines = "\n".join(f"- **{k}:** {v}" for k, v in facts.items())
    return f"## Product facts (from page markup)\n\n{lines}\n"
