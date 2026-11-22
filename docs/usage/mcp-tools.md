# MCP Tools

<!-- Updated: 2026-11-22 -->

## `crawl`

Crawl one or more web pages and extract their content.

- `timeout` covers the crawl operation, excluding Chromium installation and browser startup.
- Unknown `output_format` values fall back to Markdown.
- The browser tiers wait for script-loaded content before capture: the page text and its finished fetch/XHR requests must stay unchanged for 1.5 s (5 s while "Loading..."-style placeholders are visible), up to 15 s. A page that still shows only loading placeholders, unrendered `{{ template }}` expressions or a bare page shell after that comes back as a failed "incomplete page" with its text attached; it is not cached, and Firecrawl is not paid to wait longer for it.
- Hash routes (`https://site/app#/results/X`) skip the `http` tier, are rendered in a browser and are cached under their own key.
- AliExpress item pages (`/item/<id>.html`, tracking parameters dropped) come back as a product sheet built from the page's own item data request: title, price (UK region, GBP), rating, review count, orders, store, shipping to the UK, every option group with its option images, every SKU combination with price, sale price, discount, stock and SKU id, the specifications table and the seller's full description. Text in the description images is read by OCR on the server (RapidOCR, offline) and quoted under each image, marked OCR. All description images download at once and are read on a worker pool shared by every crawl, in page order, within a wall-clock budget of `OCR_BUDGET_S` seconds per page (default 15, download and recognition together); an image whose recognition has not started by then is skipped, so skips fall on the last images. `OCR_MAX_IMAGES` (default 60) is a safety cap and `OCR_MAX_BYTES` (default 4 MB) the largest image downloaded. OCR text is cached per image for 30 days, and cached images cost no budget. The page ends with a numbered "Description images" list of every image URL, each marked `OCR read` (with its line count, or "no text found") or `OCR skipped` (with the reason, such as the time budget or `ocr=false`), so a caller can open the rest. With `remove_links=true` the URLs in that list are stripped too.
  - Pool size: `OCR_WORKERS` recognition threads (default: logical CPUs / 4, 1 to 4), each with its own engine and `OCR_THREADS` onnxruntime threads (default: logical CPUs / 2 / workers). The default keeps workers x threads at about the number of physical cores, so 8 cores / 16 threads gives 4 x 2. `OCR_DOWNLOADS` (default 8) caps image downloads at once per page. When AliExpress answers with its slider CAPTCHA the result is a failed page "blocked by AliExpress anti-bot", and no other tier is tried.
- AliExpress page requests are paced: they go out one at a time, in arrival order, each a random 0.5 to 3 s (uniform) after the one before (`ALIEXPRESS_GAP_MIN_S` / `ALIEXPRESS_GAP_MAX_S`; 0 and 0 turn it off). This covers the item route's page load and every fetch tier that asks an AliExpress page host (`aliexpress.com`, `.us`, `.ru`), such as search pages. Cached pages skip the queue. Description files and images (`aliexpress-media.com`, `alicdn.com`) and Firecrawl requests are not paced. The gap only spaces requests out: nothing is retried or changed when AliExpress shows a challenge. Every attempt is logged, see `aliexpress_fetch_log`.

### Parameters

| Parameter      | Type       | Default      | Description                                         |
| -------------- | ---------- | ------------ | --------------------------------------------------- |
| `urls`         | `List[str]` | required     | URLs to crawl                                       |
| `output_format` | `str`       | `"markdown"` | `"markdown"` or `"json"`                              |
| `concurrency`  | `int`       | `3`          | Max concurrent crawls                               |
| `timeout`      | `int`       | `15`         | Per-URL timeout in seconds (≥ 1)                  |
| `remove_links` | `bool`      | `false`      | Remove all links from markdown output               |
| `dedup_mode`   | `str`       | `"exact"`    | `"exact"` or `"off"`                                  |
| `storage_state` | `str`       | `null`       | Path to Playwright storage state JSON for auth      |
| `fresh`        | `bool`      | `false`      | Ignore the cache and fetch again                    |
| `wait_for`     | `str`       | `null`       | CSS selector to wait for, or `text:<text>` to wait for visible text; skips the `http` tier |
| `wait_ms`      | `int`       | `0`          | Minimum wait before capture in ms (0-30000); skips the `http` tier when set |
| `ocr`          | `bool`      | `true`       | Read text from images where a site route supports it (AliExpress description images); `false` skips it and is cached apart |
| `images`       | `bool`      | `false`      | Also return the images a site route lists (AliExpress description images) as MCP image content blocks for callers that can see images; see below |

#### Images as content blocks (`images=true`)

A site route lists the images worth seeing in the page's `page_images` metadata (the AliExpress route lists its description images). With `images=true`, `crawl` downloads them, converts them to JPEG with the long side at most `CRAWL_IMAGES_SIDE` px (default 1568) and returns them as `image` content blocks after the usual text block. Each image block follows a text block with its label and URL, such as `[description image 3] https://...`, numbered like the "Description images" list. Banners more than twice as tall as wide (or wide as tall) are cut into tiles along the long side first, `[description image 3 (tile 2 of 4)]`, so their text stays readable. At most `CRAWL_IMAGES_MAX` blocks (default 30, tiles included) and `CRAWL_IMAGES_MAX_BYTES` of JPEG (default 12 MB) go out per call; the text block ends with an `Images:` line that names every image left out (cap or download failure) with its URL. The default is off: images cost many tokens, and the OCR text usually covers them. Pages from cache work too, since the list is kept with the page. Routes without a `page_images` list return `Images: none`.

### Examples

```
# Single page
crawl(urls=["https://docs.example.com"])

# Multiple pages with JSON output
crawl(urls=["https://example.com/page1", "https://example.com/page2"], output_format="json")

# Clean output without links
crawl(urls=["https://example.com"], remove_links=True)

# Disable markdown dedup
crawl(urls=["https://example.com"], dedup_mode="off")

# With authenticated state
crawl(urls=["https://example.com"], storage_state="/path/to/state.json")

# With custom timeout
crawl(urls=["https://slow-site.com"], timeout=60)

# Wait for script-loaded results
crawl(urls=["https://track.example/QP123GB"], wait_for="text:Delivered")
crawl(urls=["https://shop.example/search?q=cable"], wait_for=".result-list li")

# AliExpress item: variants, prices, description; skip OCR for speed
crawl(urls=["https://www.aliexpress.com/item/1005012556272268.html"], ocr=False)

# AliExpress item with the description images attached for a vision-capable caller
crawl(urls=["https://www.aliexpress.com/item/1005012556272268.html"], images=True)
```

---

## `crawl_site`

Crawl an entire website starting from a seed URL using DFS strategy.

- Results are collected before the response; this tool does not stream pages.
- An ordinary site timeout returns no documents and `stats.error_count=1`. The Python API also exposes an `errors` list; the MCP JSON response does not expose that list.

### Parameters

| Parameter          | Type  | Default      | Description                                         |
| ------------------ | ----- | ------------ | --------------------------------------------------- |
| `url`              | `str`  | required     | Seed URL to start from                              |
| `max_depth`        | `int`  | `2`          | Maximum crawl depth (0 = seed only)                 |
| `max_pages`        | `int`  | `25`         | Maximum pages to crawl                              |
| `include_subdomains` | `bool` | `false`     | Include subdomains                                  |
| `timeout`          | `int`  | `120`        | Overall site timeout in seconds (≥ 1)             |
| `output_format`    | `str`  | `"markdown"` | `"markdown"` or `"json"`                              |
| `remove_links`     | `bool` | `false`      | Remove all links from markdown output               |
| `dedup_mode`       | `str`  | `"exact"`    | `"exact"` or `"off"`                                  |
| `storage_state`    | `str`  | `null`       | Path to Playwright storage state JSON for auth      |

### Examples

```
# Basic site crawl
crawl_site(url="https://docs.example.com")

# Deep crawl with more pages
crawl_site(url="https://docs.example.com", max_depth=3, max_pages=50)

# JSON output with stats
crawl_site(url="https://docs.example.com", output_format="json")

# Without links
crawl_site(url="https://docs.example.com", remove_links=True)

# With auth
crawl_site(url="https://docs.example.com", storage_state="/path/to/state.json")
```

---

## `search`

Search the web via the configured providers (SearXNG and/or You.com). All configured providers are queried in parallel; results are merged (URL dedup, round-robin, results found by several providers first) and each result lists its sources in `providers`. A provider is active only when configured: `SEARXNG_URL` for SearXNG, `YDC_API_KEY` for You.com (queries go to You.com, US).

### Parameters

| Parameter    | Type       | Default | Description                                           |
| ------------ | ---------- | ------- | ----------------------------------------------------- |
| `query`      | `str`       | required | Search query string                                   |
| `language`   | `str`       | `"en"`    | Language code (e.g. `"en"`, `"de"`, `"fr"`). You.com: codes it does not support search without a language filter |
| `time_range` | `str`       | `null`   | `"day"`, `"week"`, `"month"`, `"year"`                       |
| `categories` | `List[str]` | `null`   | SearXNG only. `"general"`, `"images"`, `"news"`, etc.          |
| `engines`    | `List[str]` | `null`   | SearXNG only. Specific engines to use                 |
| `safesearch` | `int`       | `1`      | `0` (off), `1` (moderate), `2` (strict)                  |
| `pageno`     | `int`       | `1`      | Page number (≥ 1). You.com returns no results beyond page 10 |
| `max_results` | `int`      | `10`     | Max results, clamped to 1–50                                    |
| `max_retries` | `int`      | `3`      | Max attempts per provider for transient request errors (timeouts included) |
| `providers`  | `List[str]` | `null`   | Subset of configured providers: `"searxng"`, `"youcom"` (default: all configured) |
| `include_content` | `bool` | `false` | Also crawl every result page and add its Markdown to the result (see below) |

### Examples

```
# Basic search
search(query="python tutorials")

# With time filter
search(query="latest AI news", time_range="week")

# Only SearXNG
search(query="python tutorials", providers=["searxng"])

# Specific category (SearXNG only)
search(query="cute cats", categories=["images"])

# In German
search(query="Rezepte", language="de")

# Results together with the Markdown of each result page
search(query="crawl4ai release notes", max_results=3, include_content=True)
```

### Response (JSON)

```json
{
  "query": "python tutorials",
  "number_of_results": 10,
  "results": [
    {
      "title": "Python Tutorial - W3Schools",
      "url": "https://www.w3schools.com/python/",
      "content": "Well organized tutorials...",
      "publishedDate": "",
      "providers": ["searxng", "youcom"]
    }
  ]
}
```

- CLI `search --include-content` nests page Markdown below each result, shifting headings down two levels and preserving closed fenced code blocks.
- `include_content=true` crawls up to three pages concurrently with a 15 s crawl timeout per page and default Markdown deduplication.
- Each result receives `markdown` on success or `crawl_error` on failure; individual page failures preserve the remaining search results.
- Provider requests, Chromium installation and browser startup add time beyond the per-page timeout.
- `SEARCH_RESULT_FIELDS` limits the five base result fields; it does not remove `markdown` or `crawl_error`.
- If some providers fail, the others' results are returned. If all selected providers fail, or none is configured, the response is `{"error": "...", "query": "..."}` with a short reason per provider, e.g. `All selected search providers failed: searxng: request failed; youcom: API key rejected, check YDC_API_KEY`.

---

## `aliexpress_fetch_log`

Summarise this server's AliExpress page fetches over the last N days: totals, the challenge rate, the last challenge, the wait for a turn, and counts by tier, by day and by hour of day (UTC).

### Parameters

| Parameter | Type  | Default | Description                 |
|-----------|-------|---------|-----------------------------|
| `days`    | `int` | `7`     | Days back to count (1-90)   |

### The log

Each AliExpress page attempt (see the pacing note under `crawl`) is one JSON line:

```json
{"time": "2026-11-22T11:57:53+00:00", "item": "1005003561184819", "url": "https://www.aliexpress.com/item/1005003561184819.html", "tier": "route:aliexpress", "seconds": 2.1, "waited": 1.4, "outcome": "ok"}
```

- `tier`: `route:aliexpress` or the fetch tier (`http`, `browser`, `stealth`, `vpn`, `firecrawl`).
- `seconds`: the attempt, without the wait; for the item route it includes the description and OCR.
- `waited`: seconds spent waiting for the turn.
- `outcome`: `ok`, `challenged` (slider CAPTCHA or another bot wall) or `error` (with a `note`). A Firecrawl call the user did not approve is not logged.

`ALIEXPRESS_LOG_PATH` sets the file (default `~/.cache/searxncrawl/aliexpress-fetches.jsonl`; `off` turns logging off). It rotates at `ALIEXPRESS_LOG_MAX_BYTES` (default 1 MB) and keeps five old files, which the summary reads too.

### Example

```
aliexpress_fetch_log(days=1)
```
