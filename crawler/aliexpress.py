"""AliExpress item pages: variants, prices, full description and OCR text.

Item pages (``/item/<id>.html``) are rendered in the browser: the served
HTML carries an empty ``window.runParams`` and every product detail arrives
in one JSON answer to the page's own ``mtop.aliexpress.pdp.pc.query``
request. The full description is a separate HTML file (``DESC.pcDescUrl``
in that answer) on AliExpress's media CDN.

The route opens the item page in the stealth browser with the UK / GBP
region cookie, keeps the page's own pdp answer, fetches the description
file, and writes everything out as Markdown: title, price, rating, orders,
store, shipping to the UK, every SKU property group with its option images,
every SKU combination with price, sale price, currency and stock, the
specifications table and the description. Sellers put much of the
description into images, so the description images are read with OCR
(``crawler.ocr``) and the text goes under each image, marked as OCR. Every
description image is also listed at the end with its OCR status, so a caller
can open the ones the time budget left unread.

When AliExpress answers the pdp request with its slider CAPTCHA
(``FAIL_SYS_USER_VALIDATE`` / ``RGV587_ERROR`` and a ``_____tmd_____/punish``
URL), the route returns a failed page "blocked by AliExpress anti-bot" and
the chain stops there. It never tries to solve or get past the check.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import urlparse

from .document import CrawledDocument
from .chromium_profile import seeded_profile, stealth_profile_kwargs

LOGGER = logging.getLogger(__name__)

_HOST = re.compile(r"^([a-z]{2,3}\.|www\.|m\.)?aliexpress\.(com|us)$", re.IGNORECASE)
_ITEM_PATH = re.compile(r"^/item/(\d{6,})\.html$", re.IGNORECASE)
_PDP_API = "mtop.aliexpress.pdp.pc.query"
_PUNISH = "_____tmd_____"
# ret codes of an mtop answer that mean "solve the slider first".
_BLOCK_CODES = ("FAIL_SYS_USER_VALIDATE", "RGV587_ERROR", "FAIL_SYS_ILLEGAL_ACCESS")
BLOCKED = "blocked by AliExpress anti-bot"
# Region cookie: ship to the UK, prices in GBP, English text.
REGION_COOKIES = (
    {"name": "aep_usuc_f", "value": "site=glo&c_tp=GBP&region=UK&b_locale=en_US"},
    {"name": "intl_locale", "value": "en_US"},
)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
# How long to wait for the pdp answer once the page has loaded.
_PDP_WAIT_MS = 20000


def item_id(url: str) -> Optional[str]:
    """The product id of an AliExpress item page URL, or None."""
    parts = urlparse(url)
    if not _HOST.match(parts.hostname or ""):
        return None
    found = _ITEM_PATH.match(parts.path or "")
    return found.group(1) if found else None


def canonical_url(url: str) -> Optional[str]:
    """``https://www.aliexpress.com/item/<id>.html``: no tracking parameters."""
    found = item_id(url)
    return f"https://www.aliexpress.com/item/{found}.html" if found else None


# --- the pdp answer ---------------------------------------------------------


def parse_mtop(text: str) -> Optional[dict]:
    """The JSON of an mtop answer, plain or JSONP (``mtopjsonp1({...})``)."""
    text = (text or "").strip()
    found = re.match(r"^[\w$.]+\((.*)\)\s*;?$", text, re.DOTALL)
    if found:
        text = found.group(1)
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def blocked_reason(answer: Optional[dict]) -> Optional[str]:
    """Why an mtop answer is the anti-bot check, or None when it is not."""
    if not isinstance(answer, dict):
        return None
    ret = [str(r) for r in answer.get("ret") or []]
    codes = [r.split("::")[0] for r in ret]
    data = answer.get("data") if isinstance(answer.get("data"), dict) else {}
    punish = _PUNISH in str(data.get("url") or "")
    if punish or any(c in _BLOCK_CODES for c in codes):
        code = next((c for c in codes if c in _BLOCK_CODES), "punish page")
        return f"{BLOCKED} (slider CAPTCHA on the item data request, {code})"
    return None


def pdp_result(answer: Optional[dict]) -> Optional[dict]:
    """The module map (``SKU``, ``PRICE``, ...) of a successful pdp answer."""
    if not isinstance(answer, dict):
        return None
    if not any(str(r).startswith("SUCCESS") for r in answer.get("ret") or []):
        return None
    data = answer.get("data")
    result = data.get("result") if isinstance(data, dict) else None
    return result if isinstance(result, dict) and result else None


def _module(result: dict, name: str) -> dict:
    value = result.get(name)
    return value if isinstance(value, dict) else {}


def _https(url: Any) -> str:
    url = str(url or "").strip()
    return "https:" + url if url.startswith("//") else url


def _num(text: Any) -> Optional[float]:
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    found = re.search(r"\d[\d,]*(?:\.\d+)?", str(text or ""))
    return float(found.group(0).replace(",", "")) if found else None


_SYMBOLS = {"GBP": "£", "USD": "US $", "EUR": "€"}


def money(value: Optional[float], currency: str) -> str:
    if value is None:
        return ""
    symbol = _SYMBOLS.get(currency)
    return f"{symbol}{value:,.2f}" if symbol else f"{value:,.2f} {currency}".strip()


def _price(info: Any) -> dict:
    """price / sale price / currency / discount of one skuIdStrPriceInfoMap entry."""
    if not isinstance(info, dict):
        return {}
    original = (
        info.get("originalPrice") if isinstance(info.get("originalPrice"), dict) else {}
    )
    currency = str(original.get("currency") or "")
    price = _num(original.get("value"))
    if price is None:
        price = _num(original.get("formatedAmount"))
    sale = _num(info.get("salePriceString"))
    if sale is None:
        sale = _num(str(info.get("salePriceLocal") or "").split("|")[0])
    out: dict[str, Any] = {"currency": currency, "price": price, "sale_price": sale}
    if info.get("discount"):
        out["discount"] = str(info["discount"])
    return out


def _options(sku: dict) -> list[dict]:
    groups = []
    for prop in sku.get("skuProperties") or []:
        if not isinstance(prop, dict):
            continue
        values = []
        for value in prop.get("skuPropertyValues") or []:
            if not isinstance(value, dict):
                continue
            name = (
                value.get("propertyValueDisplayName")
                or value.get("propertyValueDefinitionName")
                or value.get("propertyValueName")
                or ""
            )
            values.append(
                {
                    "id": str(
                        value.get("propertyValueIdLong")
                        or value.get("propertyValueId")
                        or ""
                    ),
                    "name": str(name).strip(),
                    "image": _https(value.get("skuPropertyImagePath")),
                }
            )
        groups.append(
            {
                "id": str(prop.get("skuPropertyId") or ""),
                "name": str(prop.get("skuPropertyName") or "").strip(),
                "values": values,
            }
        )
    return groups


def _variants(sku: dict, price_map: dict, groups: list[dict]) -> list[dict]:
    names = {(g["id"], v["id"]): v["name"] for g in groups for v in g["values"]}
    rows = []
    for path in sku.get("skuPaths") or []:
        if not isinstance(path, dict):
            continue
        picked: dict[str, str] = {}
        for pair in str(path.get("path") or "").split(";"):
            prop, _, value = pair.partition(":")
            if prop and value:
                picked[prop] = value
        options = {
            g["name"]: names.get((g["id"], picked.get(g["id"], "")), "") for g in groups
        }
        sku_id = str(path.get("skuIdStr") or path.get("skuId") or "")
        row: dict[str, Any] = {"sku_id": sku_id, "options": options}
        row.update(_price(price_map.get(sku_id)))
        stock = path.get("skuStock")
        row["stock"] = stock if isinstance(stock, int) else None
        row["available"] = bool(path.get("salable", True))
        rows.append(row)
    # Same order as the option lists on the page.
    order = {
        g["name"]: {v["name"]: i for i, v in enumerate(g["values"])} for g in groups
    }
    rows.sort(
        key=lambda r: [
            order[g["name"]].get(r["options"].get(g["name"], ""), 999) for g in groups
        ]
    )
    return rows


def _bulk_price(value: Any) -> Optional[float]:
    if isinstance(value, dict):
        for key in ("value", "amount", "formatedAmount"):
            found = _num(value.get(key))
            if found is not None:
                return found
        return None
    return _num(value)


def ladder_prices(result: dict) -> dict[str, list[dict]]:
    """Quantity prices per SKU id from ``LADDER_PRICE.ladderPriceViewMap``.

    Each SKU maps to its tiers, lowest quantity first: ``quantity`` pieces or
    more cost ``price`` each.
    """
    view = _module(result, "LADDER_PRICE").get("ladderPriceViewMap")
    out: dict[str, list[dict]] = {}
    if not isinstance(view, dict):
        return out
    for sku_id, tiers in view.items():
        rows = []
        for tier in tiers if isinstance(tiers, list) else []:
            if not isinstance(tier, dict):
                continue
            quantity = _num(tier.get("bulkQuantity"))
            price = _bulk_price(tier.get("bulkPrice"))
            if quantity and price is not None:
                rows.append({"quantity": int(quantity), "price": price})
        if rows:
            out[str(sku_id)] = sorted(rows, key=lambda r: r["quantity"])
    return out


def _shipping(result: dict) -> list[dict]:
    out = []
    for layout in _module(result, "SHIPPING").get("deliveryLayoutInfo") or []:
        biz = layout.get("bizData") if isinstance(layout, dict) else None
        if not isinstance(biz, dict):
            continue
        free = biz.get("shippingFee") == "free"
        threshold = str(biz.get("logisticsComposeThreshold") or "").replace("￡", "£")
        out.append(
            {
                "method": str(
                    biz.get("deliveryProviderName") or biz.get("company") or ""
                ),
                "fee": 0.0 if free else _num(biz.get("displayAmount")),
                "currency": str(biz.get("displayCurrency") or ""),
                # "￡0.00" means there is no free-shipping threshold.
                "free_over": threshold if _num(threshold) else "",
                "eta": " - ".join(
                    d
                    for d in (
                        biz.get("displayEtaMinDate"),
                        biz.get("displayEtaMaxDate"),
                    )
                    if d
                ),
                "days": [biz.get("deliveryDayMin"), biz.get("deliveryDayMax")],
                "from": str(biz.get("shipFrom") or ""),
                "to": str(biz.get("shipToCode") or ""),
            }
        )
    return out


def item_data(result: dict, pid: str = "") -> dict:
    """The product as a plain dict, from the pdp module map."""
    sku = _module(result, "SKU")
    price = _module(result, "PRICE")
    price_map = price.get("skuIdStrPriceInfoMap") or {}
    groups = _options(sku)
    variants = _variants(sku, price_map if isinstance(price_map, dict) else {}, groups)
    ladder = ladder_prices(result)
    for variant in variants:
        variant["bulk"] = ladder.get(variant["sku_id"], [])
    rating = _module(result, "PC_RATING")
    shop = _module(result, "SHOP_CARD_PC")
    seller = shop.get("sellerInfo") if isinstance(shop.get("sellerInfo"), dict) else {}
    props = _module(result, "PRODUCT_PROP_PC")
    specs = [
        (str(p.get("attrName") or "").strip(), str(p.get("attrValue") or "").strip())
        for p in props.get("showedProps") or props.get("props") or []
        if isinstance(p, dict) and p.get("attrName")
    ]
    desc = _module(result, "DESC")
    guide = (
        desc.get("instructionInfo")
        if isinstance(desc.get("instructionInfo"), dict)
        else {}
    )
    title = _module(result, "PRODUCT_TITLE").get("text") or (
        _module(result, "GLOBAL_DATA").get("globalData") or {}
    ).get("subject")
    return {
        "id": str(pid or price.get("productId") or ""),
        "title": str(title or "").strip(),
        "price": _price(price.get("targetSkuPriceInfo")),
        "rating": _num(rating.get("rating")),
        "reviews": rating.get("totalValidNum")
        if isinstance(rating.get("totalValidNum"), int)
        else None,
        "sold": str(rating.get("otherText") or "").strip(),
        "store": {
            "name": str(shop.get("storeName") or "").strip(),
            "url": _https(seller.get("storeURL")),
            "facts": [
                (str(b.get("title") or "").strip(), str(b.get("value") or "").strip())
                for b in shop.get("benefitInfoList") or []
                if isinstance(b, dict)
            ],
            "opened": str(seller.get("formatOpenTime") or ""),
            "country": str(seller.get("countryCompleteName") or ""),
        },
        "images": [
            _https(u)
            for u in _module(result, "HEADER_IMAGE_PC").get("imagePathList") or []
        ],
        "option_groups": groups,
        "variants": variants,
        "specs": specs,
        "shipping": _shipping(result),
        "guide": {
            "title": str(guide.get("title") or ""),
            "url": _https(guide.get("mediaUrl")),
        }
        if guide.get("mediaUrl")
        else None,
        "desc_url": _https(desc.get("pcDescUrl")),
    }


# --- description ------------------------------------------------------------

_IMG_SRC = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def description_images(html: Optional[str]) -> list[str]:
    """Image URLs of the description, in page order, without repeats."""
    seen: list[str] = []
    for src in _IMG_SRC.findall(html or ""):
        url = _https(src.replace("&amp;", "&"))
        if url.startswith("http") and url not in seen:
            seen.append(url)
    return seen


def description_markdown(
    html: Optional[str],
    ocr_texts: Optional[dict[str, list[str]]] = None,
    ocr_skipped: Optional[dict[str, str]] = None,
) -> str:
    """The description HTML as Markdown, each image on its own line with its OCR text."""
    if not html:
        return ""
    from crawl4ai.html2text import HTML2Text

    html = re.sub(r"<script\b.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
    converter = HTML2Text()
    converter.body_width = 0
    converter.ignore_images = False
    text = converter.handle(html)
    ocr_texts = ocr_texts or {}
    ocr_skipped = ocr_skipped or {}

    def image(found: re.Match) -> str:
        url = _https(found.group(1).replace("&amp;", "&"))
        block = f"\n\n![]({url})\n\n"
        lines = ocr_texts.get(url)
        if lines:
            quoted = "\n".join(f"> {line}" for line in lines)
            block += f"> **OCR text from this image** (machine-read):\n>\n{quoted}\n\n"
        elif url in ocr_texts:
            block += "> **OCR:** no text found in this image.\n\n"
        elif url in ocr_skipped:
            block += f"> **OCR skipped:** {ocr_skipped[url]}.\n\n"
        return block

    text = re.sub(r"!\[[^\]]*\]\(([^)\s]+)\)", image, text)
    lines = [line.rstrip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# --- Markdown ---------------------------------------------------------------


def _cell(text: Any) -> str:
    return (
        str(text if text is not None else "")
        .replace("|", "\\|")
        .replace("\n", " ")
        .strip()
    )


def _price_text(p: dict) -> str:
    cur = p.get("currency") or ""
    sale, price = p.get("sale_price"), p.get("price")
    if sale is not None and price is not None and sale < price:
        extra = f", {p['discount']}" if p.get("discount") else ""
        return f"{money(sale, cur)} (was {money(price, cur)}{extra})"
    return money(sale if sale is not None else price, cur)


# Which prices a sheet shows, by session state (see ``run``).
LOGGED_IN = "logged_in"
NO_SESSION = "none"
EXPIRED = "expired"
SESSION_ERROR = "error"


def session_line(state: Optional[str], why: str = "") -> str:
    """The sheet's first line: whose prices these are."""
    if state == LOGGED_IN:
        return (
            "- **Prices:** your own, logged in with your AliExpress session. "
            "\"Your price\" is what you pay for one piece; \"Bulk\" is the "
            "price per piece from the quantity shown."
        )
    fallback = (
        "this is the logged-out sheet: sale prices are new-shopper (welcome "
        "deal) prices, not your regular price"
    )
    if state == EXPIRED:
        return (
            "- **Prices:** your AliExpress session has expired or was not "
            f"accepted, so {fallback}. Log in again (`login` skill)."
        )
    if state == SESSION_ERROR:
        reason = f" ({why})" if why else ""
        return (
            f"- **Prices:** the AliExpress session could not be read{reason}, "
            f"so {fallback}."
        )
    if state == NO_SESSION:
        return (
            f"- **Prices:** logged out (no AliExpress session on this server), "
            f"so {fallback}. Log in with the `login` skill."
        )
    return ""


def bulk_text(bulk: list[dict], currency: str) -> str:
    """``£5.11 each - £10.22 for 2``: the first quantity tier."""
    if not bulk:
        return ""
    tier = bulk[0]
    qty, price = tier["quantity"], tier["price"]
    return f"{money(price, currency)} each - {money(price * qty, currency)} for {qty}"


def item_markdown(
    item: dict,
    url: str,
    description: str = "",
    ocr_note: str = "",
    session: Optional[str] = None,
    session_why: str = "",
) -> str:
    lines = [f"# {item.get('title') or 'AliExpress item ' + str(item.get('id'))}", ""]
    head = session_line(session, session_why)
    if head:
        lines.append(head)
    variants = item.get("variants") or []
    price = _price_text(item.get("price") or {})
    sales = [v["sale_price"] for v in variants if v.get("sale_price") is not None]
    cur = (item.get("price") or {}).get("currency") or (
        variants[0]["currency"] if variants else ""
    )
    if len(set(sales)) > 1:
        span = f"{money(min(sales), cur)} - {money(max(sales), cur)} across {len(variants)} variants"
        price = f"{price}; {span}" if price else span
    if price:
        lines.append(
            f"- **Price:** {price} ({cur})" if cur else f"- **Price:** {price}"
        )
    rating = []
    if item.get("rating"):
        rating.append(f"{item['rating']:.1f}/5")
    if item.get("reviews"):
        rating.append(f"{item['reviews']:,} reviews")
    elif item.get("reviews") == 0:
        rating.append("no reviews yet")
    if item.get("sold"):
        rating.append(item["sold"])
    if rating:
        lines.append(f"- **Rating:** {', '.join(rating)}")
    store = item.get("store") or {}
    if store.get("name"):
        label = (
            f"[{store['name']}]({store['url']})" if store.get("url") else store["name"]
        )
        # "# sold in 180 days" puts the value in place of the "#".
        facts = [
            t.replace("#", v).strip() if "#" in t else f"{t}: {v}"
            for t, v in store.get("facts") or []
            if v
        ]
        if store.get("opened"):
            facts.append(f"open since {store['opened']}")
        lines.append(
            f"- **Store:** {label}" + (f" ({'; '.join(facts)})" if facts else "")
        )
    for ship in item.get("shipping") or []:
        fee = (
            "free"
            if ship.get("fee") == 0
            else money(ship.get("fee"), ship.get("currency") or "")
        )
        bits = [
            b
            for b in (
                fee,
                f"free over {ship['free_over']}" if ship.get("free_over") else "",
            )
            if b
        ]
        when = f", delivery {ship['eta']}" if ship.get("eta") else ""
        origin = f", from {ship['from']}" if ship.get("from") else ""
        to = ship.get("to") or "UK"
        lines.append(
            f"- **Shipping to {to}:** {ship.get('method') or 'standard'}: {', '.join(bits) or 'price not shown'}{when}{origin}"
        )
    if len(variants) == 1:
        only = variants[0]
        if only.get("bulk"):
            lines.append(f"- **Bulk:** {bulk_text(only['bulk'], only.get('currency') or '')}")
        if not only.get("available"):
            lines.append("- **Stock:** sold out")
        elif only.get("stock") is not None:
            lines.append(f"- **Stock:** {only['stock']:,} available")
    if item.get("guide"):
        g = item["guide"]
        lines.append(f"- **{g.get('title') or 'Item guide'}:** {g['url']}")
    lines.append(f"- **Page:** {url}")

    images = item.get("images") or []
    if images:
        lines += ["", f"## Images ({len(images)})", "", *[f"- {u}" for u in images]]

    groups = [g for g in item.get("option_groups") or [] if g.get("values")]
    if groups:
        noun = "group" if len(groups) == 1 else "groups"
        lines += [
            "",
            f"## Options ({len(groups)} {noun}, {len(variants)} combinations)",
        ]
        for group in groups:
            lines += ["", f"### {group['name']} ({len(group['values'])})", ""]
            for value in group["values"]:
                image = f" - image: {value['image']}" if value.get("image") else ""
                lines.append(f"- {value['name']}{image}")
    if variants and (groups or len(variants) > 1):
        names = [g["name"] for g in groups]
        has_bulk = any(v.get("bulk") for v in variants)
        if session == LOGGED_IN:
            sale_head = "Your price (logged in)"
        elif session in (NO_SESSION, EXPIRED, SESSION_ERROR):
            sale_head = "Sale price (new-shopper)"
        else:
            sale_head = "Sale price"
        heads = [*names, "Price", sale_head]
        if has_bulk:
            heads.append("Bulk (logged in)" if session == LOGGED_IN else "Bulk")
        heads += ["Discount", "Stock", "SKU"]
        lines += [
            "",
            f"## Variants ({len(variants)})",
            "",
            "| " + " | ".join(heads) + " |",
            "|" + "---|" * len(heads),
        ]
        for v in variants:
            stock = v.get("stock")
            stock_text = (
                "sold out"
                if not v.get("available")
                else (f"{stock:,}" if stock is not None else "")
            )
            cur_v = v.get("currency") or ""
            cells = [
                *[v["options"].get(n, "") for n in names],
                money(v.get("price"), cur_v),
                money(v.get("sale_price"), cur_v),
            ]
            if has_bulk:
                cells.append(bulk_text(v.get("bulk") or [], cur_v))
            cells += [v.get("discount") or "", stock_text, v.get("sku_id") or ""]
            lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
        if variants[0].get("currency"):
            lines += ["", f"Prices in {variants[0]['currency']}."]

    specs = item.get("specs") or []
    if specs:
        lines += ["", "## Specifications", "", "| Name | Value |", "|---|---|"]
        lines += [f"| {_cell(k)} | {_cell(v)} |" for k, v in specs]

    lines += ["", "## Description", ""]
    if ocr_note:
        lines += [ocr_note, ""]
    lines.append(description or "(the seller's description could not be loaded)")
    return "\n".join(lines).strip() + "\n"


# --- fetching ---------------------------------------------------------------


_HEADERS = {"user-agent": _UA, "referer": "https://www.aliexpress.com/"}
IMAGE_ACCEPT = "image/jpeg,image/png,image/webp,*/*;q=0.5"


def _client():
    import httpx

    return httpx.AsyncClient(timeout=20, follow_redirects=True, headers=_HEADERS)


async def _get(
    url: str, limit: int, accept: str = "*/*", client: Any = None
) -> Optional[bytes]:
    """GET ``url`` with a byte cap; None when it fails or is larger than ``limit``."""
    if client is None:
        async with _client() as own:
            return await _get(url, limit, accept, own)
    async with client.stream("GET", url, headers={"accept": accept}) as resp:
        if resp.status_code != 200:
            return None
        size = int(resp.headers.get("content-length") or 0)
        if size > limit:
            return None
        data = bytearray()
        async for chunk in resp.aiter_bytes():
            data += chunk
            if len(data) > limit:
                return None
        return bytes(data)


async def _ocr(urls: list[str]) -> tuple[dict, dict, float]:
    """OCR text by image URL (cached per image), skip reasons, seconds spent.

    Cached images cost nothing; the rest share the OCR time budget and the
    image safety cap.
    """
    from . import fetch_chain, ocr

    if not ocr.available():
        return {}, {u: "OCR engine not installed" for u in urls}, 0.0
    cache = fetch_chain._get_cache()
    cached: dict[str, list[str]] = {}
    for url in urls:
        hit = cache.get_ocr(url)
        if hit is not None:
            # Lines cached before the noise filter existed get it now.
            cached[url] = ocr.clean(hit)
    todo = [u for u in urls if u not in cached]
    cap = max(0, ocr.max_images() - len(cached))
    async with _client() as client:
        result = await ocr.read_images(
            todo,
            lambda u: _get(u, ocr.max_bytes(), IMAGE_ACCEPT, client),
            cap=cap,
        )
    for url, lines in result.texts.items():
        cache.put_ocr(url, lines)
    texts = {u: cached.get(u, result.texts.get(u)) for u in urls}
    texts = {u: t for u, t in texts.items() if t is not None}
    return texts, result.skipped, result.seconds


def image_status(
    urls: list[str], texts: dict, skipped: dict, ocr_on: bool = True
) -> list[dict]:
    """Every description image with what OCR made of it, in page order."""
    out = []
    for index, url in enumerate(urls, 1):
        lines = texts.get(url)
        if not ocr_on:
            status, note = "not read", "OCR off for this crawl"
        elif lines:
            status, note = "read", f"{len(lines)} line{'s' if len(lines) != 1 else ''}"
        elif url in texts:
            status, note = "read", "no text found"
        else:
            status, note = "not read", skipped.get(url, "not read")
        out.append({"n": index, "url": url, "ocr": status, "note": note})
    return out


def image_list_markdown(images: list[dict]) -> str:
    """The numbered list of every description image and its OCR status."""
    if not images:
        return ""
    unread = sum(1 for i in images if i["ocr"] != "read")
    head = f"## Description images ({len(images)})"
    lines = [head, ""]
    if unread:
        lines += [
            f"{unread} not read by OCR here; open those URLs to read them "
            "(crawl with images=true returns them as images).",
            "",
        ]
    for i in images:
        mark = "OCR read" if i["ocr"] == "read" else "OCR skipped"
        lines.append(f"{i['n']}. {i['url']} - {mark}: {i['note']}")
    return "\n".join(lines)


def request_region(url: str) -> dict:
    """``country`` / ``_currency`` / ``_lang`` the page sent with its pdp request.

    The page builds them from the region cookie, so they show whether the
    UK / GBP setting took, even when the answer is the CAPTCHA.
    """
    from urllib.parse import parse_qs

    raw = (parse_qs(urlparse(url).query).get("data") or [""])[0]
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    keys = (("country", "country"), ("_currency", "currency"), ("_lang", "lang"))
    return {name: str(data[key]) for key, name in keys if data.get(key)}


def page_blocked(final_url: str, html: str) -> Optional[str]:
    """Why the item page itself is the CAPTCHA page, or None."""
    text = (html or "")[:20000].lower()
    if _PUNISH in (final_url or "") or (
        _PUNISH in text and ("captcha" in text or "slide" in text)
    ):
        return f"{BLOCKED} (slider CAPTCHA instead of the item page)"
    return None


def _pdp_listener(got: dict, reads: list) -> Any:
    """A page ``response`` handler that keeps the pdp answer in ``got``."""

    def on_response(response):
        if _PUNISH in response.url:
            got.setdefault(
                "blocked",
                f"{BLOCKED} (slider CAPTCHA on the item data request)"
                if _PDP_API in response.url
                else f"{BLOCKED} (slider CAPTCHA page)",
            )
            return
        if _PDP_API not in response.url:
            return
        got.setdefault("region", request_region(response.url))

        async def read():
            try:
                answer = parse_mtop(await response.text())
            except Exception as exc:  # noqa: BLE001 - reported as no data below
                LOGGER.debug("AliExpress pdp answer unreadable: %s", type(exc).__name__)
                return
            reason = blocked_reason(answer)
            if reason:
                got.setdefault("blocked", reason)
            elif pdp_result(answer):
                got["pdp"] = answer
            elif answer is not None:
                got.setdefault("other", answer)

        reads.append(asyncio.ensure_future(read()))

    return on_response


async def _wait_pdp(page: Any, got: dict, reads: list) -> None:
    for _ in range(_PDP_WAIT_MS // 100):
        if (
            reads and all(r.done() for r in reads) and ({"pdp", "blocked"} & got.keys())
        ) or ("blocked" in got and not reads):
            break
        await page.wait_for_timeout(100)
    await asyncio.gather(*reads, return_exceptions=True)


def _pdp_outcome(
    got: dict, status: Any, final_url: str, html: str
) -> tuple[Optional[dict], Optional[str], Any, dict]:
    region = got.get("region") or {}
    if "pdp" in got:
        return got["pdp"], None, status, region
    if "blocked" in got:
        return None, got["blocked"], status, region
    reason = page_blocked(final_url, html)
    if reason:
        return None, reason, status, region
    other = got.get("other")
    why = None
    if isinstance(other, dict):
        why = "the item data request failed: " + "; ".join(
            str(r) for r in other.get("ret") or []
        )
    return None, why, status, region


async def _load_pdp(
    url: str, timeout: int
) -> tuple[Optional[dict], Optional[str], Any, dict]:
    """The page's own pdp answer, why it is missing, the HTTP status, the region sent."""
    from scrapling.fetchers import StealthyFetcher

    got: dict[str, Any] = {}
    reads: list[asyncio.Future] = []
    on_response = _pdp_listener(got, reads)

    async def setup(page):
        await page.context.add_cookies(
            [dict(c, domain=".aliexpress.com", path="/") for c in REGION_COOKIES]
        )
        page.on("response", on_response)

    async def action(page):
        await _wait_pdp(page, got, reads)
        return page

    with seeded_profile() as profile:
        resp = await StealthyFetcher.async_fetch(
            url,
            headless=True,
            network_idle=False,
            block_webrtc=True,
            disable_resources=True,
            timeout=timeout * 1000,
            page_setup=setup,
            page_action=action,
            **stealth_profile_kwargs(profile),
        )
    status = getattr(resp, "status", None)
    html = str(getattr(resp, "html_content", "") or "")
    return _pdp_outcome(got, status, str(getattr(resp, "url", "") or ""), html)


class SessionLoadError(RuntimeError):
    """The logged-in page load failed. The message names only the error type."""


_SKIP_TYPES = ("image", "media", "font")


async def _load_pdp_session(
    url: str, timeout: int, cookies: list[dict]
) -> tuple[Optional[dict], Optional[str], Any, dict, bool]:
    """Like :func:`_load_pdp`, with the user's cookies; also whether they still log in.

    The session never touches disk. Scrapling's stealth fetch always runs a
    persistent context, whose cookies Chromium writes to the profile on
    disk, so this load uses patchright directly: ``launch()`` plus
    ``new_context()`` is an off-the-record context that keeps every cookie in
    memory. Its default headless browser is ``chrome-headless-shell``, which
    has no password manager and so never runs Chromium's Windows blank
    password logon probe (see :mod:`crawler.chromium_profile`). Errors are
    re-raised with their type only, so no cookie can reach a log.
    """
    from patchright.async_api import async_playwright

    from . import site_sessions

    spec = site_sessions.SITES["aliexpress"]
    got: dict[str, Any] = {}
    reads: list[asyncio.Future] = []
    on_response = _pdp_listener(got, reads)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                major = str(browser.version).split(".")[0]
                agent = _UA.replace("Chrome/142.0.0.0", f"Chrome/{major}.0.0.0")
                context = await browser.new_context(
                    user_agent=agent,
                    locale="en-US",
                    viewport={"width": 1366, "height": 768},
                    storage_state={"cookies": cookies, "origins": []},
                )
                # The UK / GBP region cookies only where the session has none of
                # its own: never overwrite a cookie of the user's.
                have = {c.get("name") for c in cookies}
                region = [
                    dict(c, domain=".aliexpress.com", path="/")
                    for c in REGION_COOKIES
                    if c["name"] not in have
                ]
                if region:
                    await context.add_cookies(region)

                async def skip_heavy(route):
                    if route.request.resource_type in _SKIP_TYPES:
                        await route.abort()
                    else:
                        await route.continue_()

                await context.route("**/*", skip_heavy)
                page = await context.new_page()
                page.on("response", on_response)
                resp = await page.goto(
                    url, wait_until="domcontentloaded", timeout=timeout * 1000
                )
                await _wait_pdp(page, got, reads)
                after = site_sessions.site_cookies_or_empty(
                    {"cookies": await context.cookies()}, spec
                )
                html = "" if "pdp" in got else await page.content()
                final_url = page.url
                status = resp.status if resp is not None else None
            finally:
                await browser.close()
    except Exception as exc:  # noqa: BLE001 - see the docstring
        raise SessionLoadError(
            f"logged-in page load failed ({type(exc).__name__})"
        ) from None
    answer, problem, status, region = _pdp_outcome(got, status, final_url, html)
    return answer, problem, status, region, site_sessions.has_login_cookie(after, spec)


_LOGIN_KEY = re.compile(r"buyer|member|login|logged|user|shopper|newuser", re.IGNORECASE)


def login_keys(result: dict, limit: int = 20) -> list[str]:
    """Paths of keys in the pdp answer that may tell a logged-in page (names only).

    Kept in the metadata of logged-in loads, so one real load shows which
    field marks the login; values are never recorded.
    """
    found: list[str] = []

    def walk(value: Any, path: str, depth: int) -> None:
        if len(found) >= limit or depth > 4:
            return
        if isinstance(value, dict):
            for key, inner in value.items():
                here = f"{path}.{key}" if path else str(key)
                if _LOGIN_KEY.search(str(key)):
                    found.append(here)
                walk(inner, here, depth + 1)

    walk(result, "", 0)
    return found[:limit]


def ladder_shape(result: dict) -> Optional[dict]:
    """Key names of an unread ``LADDER_PRICE`` module (no values), or None."""
    module = _module(result, "LADDER_PRICE")
    if not module or ladder_prices(result):
        return None
    shape: dict[str, Any] = {"module_keys": sorted(map(str, module))}
    view = module.get("ladderPriceViewMap")
    if isinstance(view, dict) and view:
        tiers = next(iter(view.values()))
        tier = tiers[0] if isinstance(tiers, list) and tiers else tiers
        if isinstance(tier, dict):
            shape["tier_keys"] = sorted(map(str, tier))
            shape["bulkPrice_type"] = type(tier.get("bulkPrice")).__name__
    return shape


def _session() -> tuple[Any, str, str]:
    """The AliExpress session to use, its state word and why there is none.

    A session whose login cookies have run out is not used: the page is
    loaded logged out and labelled expired.
    """
    from . import site_sessions

    try:
        session, why = site_sessions.get("aliexpress")
    except Exception as exc:  # noqa: BLE001 - logged-out sheet still works
        return None, SESSION_ERROR, type(exc).__name__
    if session is None:
        has_ref = "aliexpress" in site_sessions.configured_refs()
        return None, (SESSION_ERROR if has_ref else NO_SESSION), (why or "")
    if not site_sessions.has_login_cookie(session.cookies, site_sessions.SITES["aliexpress"]):
        return None, EXPIRED, ""
    return session, LOGGED_IN, ""


async def run(url: str, timeout: int, ocr: bool = True) -> CrawledDocument:
    """The route: one item page as Markdown (see the module docstring)."""
    start = time.monotonic()
    pid = item_id(url) or ""
    meta: dict[str, Any] = {"fetched_via": "route:aliexpress"}
    session, state, why = _session()
    loaded = None
    if session is not None:
        try:
            *loaded, still_in = await _load_pdp_session(url, timeout, session.cookies)
            # Unverified until checked on a real login: a session the site no
            # longer accepts is taken to lose its login cookie on the page load.
            state = LOGGED_IN if still_in else EXPIRED
        except SessionLoadError as exc:
            # The logged-out sheet still works; it says why it is logged out.
            session, state, why, loaded = None, SESSION_ERROR, str(exc), None
    answer, problem, status, region = loaded or await _load_pdp(url, timeout)
    meta["session"] = state
    if state in (EXPIRED, SESSION_ERROR):
        # A session is configured but was not used: do not keep this sheet,
        # so the next fetch after a fresh login is not served from the cache.
        meta["no_cache"] = True
    meta["status_code"] = status
    if region:
        meta["region"] = region
    result = pdp_result(answer)
    if result is None:
        blocked = bool(problem and problem.startswith(BLOCKED))
        if session is not None:
            # Never fall back to a logged-out load of the same page: a
            # CAPTCHA would only meet more checks, and a failed logged-in
            # load is reported as it is.
            meta["final"] = True
        if blocked:
            meta["blocked"] = True
            # AliExpress turns away this kind of client (the stealth browser,
            # plain HTTP alike), so the other tiers would only meet the same
            # check: stop here.
            meta["final"] = True
        why = problem or (
            f"HTTP {status}"
            if status and status >= 400
            else "the page sent no item data"
        )
        return CrawledDocument(
            request_url=url,
            final_url=url,
            status="failed",
            markdown="",
            error_message=why,
            metadata=meta,
        )
    item = item_data(result, pid)
    meta["page_seconds"] = round(time.monotonic() - start, 1)
    if session is not None:
        # Unverified signals, names only: which answer keys could mark a
        # logged-in page, kept so a real load can confirm the login check.
        meta["session_check"] = {"login_cookie_after": state == LOGGED_IN, "login_keys": login_keys(result)}
    shape = ladder_shape(result)
    if shape:
        meta["ladder_unread"] = shape

    desc_html = None
    if item["desc_url"]:
        try:
            raw = await _get(item["desc_url"], 5 * 1024 * 1024, "text/html,*/*")
            desc_html = raw.decode("utf-8", errors="replace") if raw else None
        except Exception as exc:  # noqa: BLE001 - the rest of the page still stands
            LOGGER.debug("AliExpress description failed: %s", exc)
    if desc_html is None:
        meta["warning"] = "the seller's description could not be loaded"

    images = description_images(desc_html)
    texts: dict = {}
    skipped: dict = {}
    note = ""
    if images and ocr:
        texts, skipped, seconds = await _ocr(images)
        meta["ocr_seconds"] = round(seconds, 1)
        read = sum(1 for u in images if u in texts)
        note = (
            f"OCR: {read} of {len(images)} description images read on this server "
            "(text under each image, marked OCR; every image is listed under "
            "Description images)."
        )
    elif images:
        note = (
            "OCR: off for this crawl (ocr=false); every image is listed under "
            "Description images."
        )
    description = description_markdown(desc_html, texts, skipped)
    listed = image_status(images, texts, skipped, ocr_on=ocr)
    if listed:
        description += "\n\n" + image_list_markdown(listed)
        # Route-neutral list of the page's images: crawl(images=true) reads it.
        meta["page_images"] = [
            {"url": i["url"], "label": f"description image {i['n']}"} for i in listed
        ]
    meta["aliexpress"] = {
        "product_id": item["id"],
        "option_groups": [
            {"name": g["name"], "values": len(g["values"])}
            for g in item["option_groups"]
        ],
        "variants": item["variants"],
        "description_chars": len(description),
        "description_images": len(images),
        "ocr_images": sum(1 for u in images if texts.get(u)),
        "ocr_read": sum(1 for i in listed if i["ocr"] == "read"),
        "ocr_skipped": sum(1 for i in listed if i["ocr"] != "read"),
    }
    meta["route_seconds"] = round(time.monotonic() - start, 1)
    return CrawledDocument(
        request_url=url,
        final_url=url,
        status="success",
        markdown=item_markdown(item, url, description, note, state, why),
        metadata=meta,
    )
