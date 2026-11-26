---
name: login
description: Log the user in to a site (AliExpress) for this web router, as a short questionnaire. The user logs in themselves in a browser window; the session goes to 1Password, where the router reads it at fetch time, never to disk and never through the model. Use when the user wants the router to see their own prices or logged-in pages, says "log in to AliExpress", "refresh my AliExpress session", "the session expired", or a crawl says the session is missing or expired.
---

# Site login questionnaire

The router can fetch some sites with the user's own login, so prices and
pages are the ones they see. The session is a secret. You never see it:
`site-login` takes it from a browser window the user logs in to, writes it
to 1Password over stdin, and the router reads that item with its own
read-only service account at fetch time. You only pick options and pass
names.

## Hard rules

- Never type, fill or submit a password, one-time code or CAPTCHA, and never
  click in the login window. The user logs in.
- Never read the session: no `op read`, `op item get --reveal` or
  `op item get --format json` on the session item, no printing it, no
  copying it anywhere. Report only what `site-login` prints.
- Ask one question at a time with AskUserQuestion. Do not start the
  login before the confirm step says yes.

## How to run site-login

It ships with this server's package (`searxncrawl`). Run it on the user's
desktop machine, which has a screen and the 1Password CLI:

```bash
uvx --from git+https://github.com/BigGreenDelta/searxNcrawl site-login <args>
```

A local checkout works too: `uv run site-login <args>` inside it. Below,
`site-login` means either form. If `op whoami` fails, stop and tell the user
that the 1Password CLI must be signed in first.

## Steps

1. **Site.** Ask which site. Options: the sites `site-login capture --help` lists
   under `--site` (today only `aliexpress`).
2. **Vault.** Run `site-login vaults` (JSON names). Ask which vault. Options:
   up to 3 vault names, the likeliest first (one already holding sessions or
   meant for automation), plus "New vault". For a new vault, the user types
   its name in "Other"; pass `--vault <name> --new-vault`.
3. **Item.** Run `site-login items --vault <vault>`. Ask which item. Options:
   an existing item that looks like this site's session, if any, then
   "New item: <Site> session". An existing item is updated in place: only its
   session field and its `site` / `captured` / `expires` fields change.
4. **Router check.** Call the router's `site_sessions` tool. If its `op_ref`
   for this site is not `op://<vault>/<item>/storage_state` for the vault and
   item picked, say so: the router will not see this login until its
   `SITE_SESSIONS` points at that item and its service account can read the
   vault. Ask whether to go on anyway.
5. **Confirm.** One question that restates site, vault and item, with
   "Start login" and "Cancel".
6. **Run.** Start `site-login capture --site <site> --vault <vault> --item
   <item> [--new-vault]` in the background (it waits up to 10
   minutes). Tell the user: "Log in in the window that opened. It closes by
   itself once you are logged in."
7. **Report.** When it ends, give the lines it printed: `saved` (the `op://`
   reference), `cookies` and `expires`. Nothing else.

## When it does not finish

- `no login within ...`: nothing was saved. Offer to start again.
- `the window closed before a login was seen`: the login check did not see
  the site's login cookie. Ask whether the user really was logged in; only on
  yes, run step 6 again with `--accept-unconfirmed`.
- An `op` error: report its one line. A service account may lack write rights
  to that vault or the right to create vaults; offer another vault.

## Refreshing

The router reads the item at every fetch, so a restart or deploy changes
nothing and a refresh is the same flow with the same vault and item.

AliExpress crawls say in their first lines whether the prices are the user's
own (logged in), or new-shopper prices because there is no session, it has
expired or it could not be read. An expired session needs a new login
(steps 1 to 7).
