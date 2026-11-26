---
name: login
description: Log the user in to a site (AliExpress) for this web router, as a short questionnaire. The user logs in themselves in a browser window; the session goes to 1Password, where the router reads it at fetch time, never to disk and never through the model. Use when the user wants the router to see their own prices or logged-in pages, says "log in to AliExpress", "refresh my AliExpress session", "the session expired", or a crawl says the session is missing or expired. Takes an optional site name, as in /web:login aliexpress.
argument-hint: "[site]"
---

# Site login questionnaire

The router can fetch some sites with the user's own login, so prices and
pages are the ones they see. The session is a secret. You never see it:
`site-login` takes it from a browser window the user logs in to and writes it
to 1Password over stdin. The router reads that item with its own read-only
service account at fetch time. You only ask, pass names and report.

## Hard rules

- Every choice is the user's, asked here. Never pick a site, vault, item or
  pointer place for them, and never start a login without the confirm step.
- Never type, fill or submit a password, one-time code or CAPTCHA, and never
  click in the login window. The user logs in.
- Never read the session: no `op read`, `op item get --reveal` or
  `op item get --format json` on the session item, no printing it, no
  copying it anywhere. Report only what `site-login` prints.
- One question per AskUserQuestion call, then wait for the answer. Each
  question is one short line; the context goes in the option descriptions.
  Put the recommended option first and end its label with "(Recommended)".

## How to run site-login

Run it on the user's desktop machine, which has a screen and the 1Password
CLI. Inside a checkout of this server's repository:

```bash
uv run site-login <args>
```

Without a checkout:
`uvx --from git+https://github.com/BigGreenDelta/searxNcrawl site-login <args>`.
Below, `site-login` means either form. If `op whoami` fails, stop and tell
the user that the 1Password CLI must be signed in first.

## Before the questions

- Run `site-login vaults` and, once a vault is picked, `site-login items
  --vault <vault>`. Both print names only (JSON).
- Call the router's `site_sessions` tool. Its `op_ref` per site is where the
  router reads the session today (`null`: nowhere yet).

## Site argument

The command may come with a site name, as in `/web:login aliexpress`.

- A known site (one of the `--site` choices, any letter case): skip
  question 1 and use that site.
- An unknown or misspelled name: say so in one line, such as "No site
  `aliexprss`; known: aliexpress.", then ask question 1 as usual.
- No argument: ask question 1.

## Questions

1. **Site.** "Which site do you want to log in to?"
   - Options: each site under `--site` in `site-login capture --help` (today
     only "AliExpress (Recommended)", description: your own prices and the
     2+ pcs price instead of new-shopper prices), plus "Cancel".
2. **Vault.** "Which 1Password vault keeps the session?"
   - Options: up to 3 vault names. Recommend the vault in the router's
     `op_ref` if there is one, else a vault named for sessions. The
     description says whether the router's service account reads it (it does
     when it is the vault in `op_ref`).
   - Add "New vault" (description: type its name in Other; the router's
     service account needs read access to it). For a new vault pass
     `--vault <name> --new-vault`.
3. **Item.** "Which item in <vault>?"
   - Options: the item in the router's `op_ref` or one that looks like this
     site's session (description: updated in place; only the session field
     and its `site` / `captured` / `expires` fields change).
   - Add "New item: <Site> session" (Recommended when there is none).
4. **Router pointer.** Skip this question when the router's `op_ref` already
   is `op://<vault>/<item>/storage_state`. Otherwise: "Where should the
   router's SITE_SESSIONS pointer go?"
   - "Router .env line (Recommended)": one non-secret line
     `SITE_SESSIONS=<site>=op://<vault>/<item>/storage_state` in the `.env`
     file the router starts with; no other line is read or changed.
   - "Router user variable": a user environment variable of the account the
     router runs as; the `.env` file stays as it is.
   - "Not now": the session is saved, but the router keeps showing
     new-shopper prices until a pointer is set.
5. **Confirm.** "Start the login?"
   - "Start login (Recommended)": the description restates site, vault,
     item and the pointer answer.
   - "Cancel": nothing is opened or saved.

## Run

1. Start `site-login capture --site <site> --vault <vault> --item <item>
   [--new-vault]` in the background (it waits up to 10 minutes). Tell the
   user: "Log in in the window that opened. It closes by itself once you are
   logged in."
2. When it ends, report the lines it printed: `saved` (the `op://`
   reference), `cookies` and `expires`. Nothing else.
3. Pointer: put it only where question 4 said, with the router host's own
   deploy or remote tooling. A router restart is needed for it to take
   effect: ask before restarting. If no tooling is known, give the user the
   line and where it goes.

## When it does not finish

- `no login within ...`: nothing was saved. Offer to start again (question 5).
- `the window closed before a login was seen`: the login check did not see
  the site's login cookie. Ask whether the user really was logged in; only on
  yes, run again with `--accept-unconfirmed`.
- An `op` error: report its one line. A service account may lack write rights
  to that vault or the right to create vaults; offer another vault.

## Refreshing

The router reads the item at every fetch, so a refresh is the same
questionnaire with the same vault and item; question 4 is skipped.

AliExpress crawls say in their first lines whether the prices are the user's
own (logged in), or new-shopper prices because there is no session, it has
expired or it could not be read. An expired session needs a new login.
