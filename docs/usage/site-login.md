# Logged-in site sessions (`site-login`, `SITE_SESSIONS`)

Some prices depend on who is looking. Logged out, AliExpress puts its
new-shopper (welcome deal) price where a returning buyer sees their own
price. With your own session, the AliExpress item route shows your price per
SKU and the quantity (2+ pcs) price.

The session is a secret, so it never touches disk on either machine:

1. You log in yourself with `site-login` on a desktop that has a screen and the
   1Password CLI. The script never types, fills or submits anything.
2. `site-login` writes the cookies to a 1Password item over `op`'s stdin.
3. At fetch time the server runs `op read` on that item into memory, puts the
   cookies into an off-the-record browser context for the site's own
   domains, and forgets them when the page is read.

## Capture a session

```bash
uv run site-login --site aliexpress --vault web-sessions --item "AliExpress session"
uv run site-login vaults                    # vault names (JSON)
uv run site-login items --vault web-sessions  # item titles (JSON)
```

Without `--vault` or `--item` the terminal asks. The item is created or
updated in place: the concealed field `storage_state` (`--field`) holds the
cookies, and the text fields `site`, `captured` and `expires` hold non-secret
facts. The output is non-secret only: the `op://` reference, the cookie count
and the login cookie's expiry date.

In Claude Code the `login` skill (`skill://login/SKILL.md`) runs the same
flow as a short questionnaire: type `/web:login` when the server is added as
`web`. It asks for the site, vault, item, where the router's `SITE_SESSIONS`
pointer goes, and a final confirm, one question at a time.
`/web:login aliexpress` names the site up front and skips the site
question; an unknown site name is reported in one line and the site
question is asked as usual.

## Let the server read it

| Variable | Meaning |
|---|---|
| `SITE_SESSIONS` | Per site, the 1Password reference the route reads at fetch time, for example `aliexpress=op://web-sessions/AliExpress session/storage_state`. Several sites are comma separated. Unset: every site is fetched logged out. |
| `SITE_SESSIONS_OP_CREDENTIAL` | Windows only. The generic Windows Credential Manager entry that holds the 1Password service account token (default `web-mcp/1password-service-account`). |

`op` signs in with a service account. Give it read access to the sessions
vault only. On Windows the token sits in the Credential Manager of the
account the server runs as. It is read for each `op` call and put only in
the environment of that `op` child process, never in the server's own
environment, so the browsers it starts never inherit it. If
`OP_SERVICE_ACCOUNT_TOKEN` is already set, or on other systems, `op` uses the
sign-in it already has.

The `site_sessions` MCP tool shows, per site, the configured reference and
where `op` gets its sign-in from. It never shows a value.

## What the sheet says

Every AliExpress sheet starts with a line that says whose prices it shows:

- Logged in: "your own, logged in". The variants table has
  `Your price (logged in)` and `Bulk (logged in)` columns
  (`£5.11 each - £10.22 for 2`).
- No session configured, session expired, or session unreadable: the
  logged-out sheet, with the reason, and its sale prices are labelled
  new-shopper prices.

A login counts only when its cookie is on the site's main domain
(`aliexpress.com`), the one the route loads pages from. AliExpress signs in
on `aliexpress.us` first and carries the login over to `aliexpress.com`
afterwards, so `site-login` keeps the window open until the login reaches
the main domain; after 5 seconds with a login on the sister domain only, it
opens the home page once to let the site carry it over. A saved session with
no login on the main domain is shown as not accepted, with that reason.

A session counts as expired when its login cookie has run out, or when the
page load ends without it. The route adds its UK / GBP region cookies only
where the session has none of its own. A logged-in load records two
diagnostics in the metadata, key names only and never values:
`session_check` (whether the login cookie was still there, and which pdp
answer keys look like login markers) and, when the page has a
`LADDER_PRICE` module the route could not read, `ladder_unread` (its key
names). When a session is configured, logged-in and
logged-out sheets are cached apart, and a sheet shown logged out because the
session could not be used is not cached. After a fresh login the next crawl
reads the page again.

## Refresh

Run `site-login` again with the same vault and item. The server reads the
item at every fetch, so no restart or redeploy is needed.
