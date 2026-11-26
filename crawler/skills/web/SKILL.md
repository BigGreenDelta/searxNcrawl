---
name: web
description: How to read web pages, search the web, and pull Reddit threads or AliExpress listings through this server well. Which tool fits which site, what an empty or blocked result means, when a retry spends paid credits, and how to keep results inside the context budget. Load before the first crawl or search of a task.
---

# Using the web router

This server fetches pages through a chain of tiers and a few site routes. The
rules below keep results complete and cheap. They apply to every caller.

## Pick the tool

| Need | Tool |
| --- | --- |
| Read one or more known URLs | `crawl` |
| Read a whole small site or a docs section | `crawl_site` |
| Find pages | `search` (merged results from the configured providers) |
| Anything on reddit.com | `reddit_posts`, `reddit_thread` |
| How AliExpress fetches went lately | `aliexpress_fetch_log` |

Call `crawl` once with all the URLs you need, not once per URL in a loop. Do
not pre-filter URLs by guessing which tier will work: the server picks the tier.

## How `crawl` gets a page

Each URL goes through tiers, cheapest first, until one returns a real page:
http (no browser), browser (Chromium, runs scripts), stealth (anti-bot
browser), firecrawl (paid, last). The `json` output names the tier in
`fetched_via` and lists `tiers_tried`.

- Pages are cached for a day. A repeat is free and says `cached Ns ago`.
- `fresh=true` skips the cache. Use it when the page changes (prices, stock,
  status pages) or when a cached copy looked wrong. Do not use it by default.
- A page whose text fills in by script can come back as `Incomplete page: ...`
  with the partial text attached. That is a failure, not the page. Retry with
  `wait_for` (a CSS selector, or `text:<words>` that only the finished page
  shows) or `wait_ms`.
- `storage_state` (a Playwright session file) reads pages behind a login; it
  skips the tiers and uses the browser with that session.

## Empty is a suspected block, not absence

Zero search results, an empty page, a login wall or a challenge page means
the site probably blocked the fetch. Do not report "nothing found" or "the
page is empty" on that basis. Try a different source for the same fact, or
say plainly that the site blocked the read.

## Paid tier: Firecrawl

Firecrawl is the last tier and spends credits. Past the free allowance the
server asks the user directly, through an MCP question the model cannot
answer. The answer is the user's decision:

- `Blocked: Firecrawl would spend credits and the user did not approve` means
  the user said no. Do not retry that URL, and do not route around it with
  another fetcher, a browser tool or curl for the same page, unless the user
  asks.
- Do not repeat a crawl only to get past a failure. Each retry of a page that
  failed every free tier can reach Firecrawl again.

## Sites with their own route or trick

- **Reddit:** always `reddit_posts` (subreddit listings via RSS; keyword or
  author search via the Arctic Shift archive) and `reddit_thread` (post plus
  nested comments). Never `crawl` for reddit.com: old.reddit.com shows a
  login wall and the `.json` endpoints return 403. Archive scores settle about
  36 hours after posting; very fresh comments can be missing.
- **MediaWiki sites** (Wikipedia, fan wikis): crawl `<page URL>?action=raw`
  (or `index.php?title=<Page>&action=raw`) for the wikitext. It is smaller and
  more complete than the rendered page.
- **PMC papers:** when publisher, PubMed or PMC pages are bot-walled, open
  access papers are in the public `pmc-oa-opendata` S3 bucket. List with
  `https://pmc-oa-opendata.s3.amazonaws.com/?list-type=2&prefix=PMC<id>`,
  then fetch `https://pmc-oa-opendata.s3.amazonaws.com/PMC<id>.<version>/<file>`
  (`.xml`, `.txt`, `.pdf`, figures, supplements). For a PubMed abstract use
  E-utilities: `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id=<pmid>&rettype=abstract&retmode=text`.
- **Thingiverse thing pages** (`/thing:<id>`): a site route returns the
  counts, file list and print settings that the plain page does not hold.
- **AliExpress item pages** (`/item/<id>.html`): see below.

## AliExpress

An item URL comes back as a product sheet: price, rating, orders, store, UK
shipping, every option group, every SKU combination with price, sale price,
currency and stock, the specifications and the full description.

- Text inside description images is read by OCR to a time budget (about
  15 s). Every image is listed at the end, marked read or skipped. `ocr=false`
  is faster when the description text is not needed.
- `images=true` also returns the description images as image blocks. Leave it
  off unless the text is not enough. It is expensive: about 14 images already
  overflow Claude Code's 25k token limit for one MCP result, and the whole
  result is then lost. If you need images, crawl one item per call.
- Pages go out one at a time, 0.5 to 3 s apart by default; cached pages do not
  wait. A batch of items therefore takes a while.
- `blocked by AliExpress anti-bot` is a slider CAPTCHA. Do not retry right
  away: retries feed the bot score. Wait, or work from what you have.
- `aliexpress_fetch_log` shows recent outcomes (ok, challenged, error), the
  challenge rate and when the last challenge was. Check it before a large
  batch or after a challenge.

## Keep results small

- `remove_links=true` drops links and inline images from the Markdown.
- `crawl_site` defaults to 25 pages at depth 2. Lower `max_pages` and
  `max_depth` for a first look.
- `search` with `include_content=true` crawls every result page. Lower
  `max_results` when you use it.
- Split large batches into several `crawl` calls so one oversized result does
  not drop everything.
