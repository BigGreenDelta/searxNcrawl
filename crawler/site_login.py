"""``site-login``: log in to a site yourself, keep the session in 1Password.

Run on a desktop with a screen and the 1Password CLI::

    uv run site-login --site aliexpress --vault <vault> --item <item>
    uv run site-login vaults
    uv run site-login items --vault <vault>

``site-login`` (capture) opens a Chromium window at the site's login page.
The person logs in there; the script never types, fills or submits anything.
It finishes on its own once the browser holds the site's login cookie, or
when the window is closed after logging in. The cookies are taken from the
browser in memory, kept to the site's own domains, and written to 1Password
through ``op`` on stdin. The web router reads that item with its own
read-only 1Password service account at fetch time (``SITE_SESSIONS``).

The secret never touches disk: the browser context is off the record (its
cookies stay in memory), the value goes to ``op`` on stdin, never in argv or
the environment, and the script prints only non-secret facts: the item
reference, the cookie count and the login cookie's expiry date.

Missing ``--vault`` or ``--item`` are asked for in the terminal when there is
one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
from typing import Optional
from urllib.parse import urlparse

from . import site_sessions
from .site_sessions import Site, SessionError

DEFAULT_FIELD = "storage_state"
NEW = "new"


class OpError(RuntimeError):
    """An ``op`` call failed. The message is op's first stderr line."""


def op(args: list[str], stdin: Optional[bytes] = None) -> bytes:
    """Run ``op`` with ``args``; ``stdin`` carries any secret."""
    try:
        done = subprocess.run(
            ["op", *args],
            input=stdin,
            capture_output=True,
            timeout=120,
            stdin=None if stdin is not None else subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise OpError("the 1Password CLI (op) is not installed") from None
    if done.returncode:
        first = (done.stderr.decode(errors="replace").strip().splitlines() or [""])[0]
        raise OpError(f"op {args[0]} {args[1] if len(args) > 1 else ''}: {first[:300]}")
    return done.stdout


def vaults() -> list[dict]:
    return [
        {"id": v["id"], "name": v["name"]}
        for v in json.loads(op(["vault", "list", "--format", "json"]) or b"[]")
    ]


def items(vault: str) -> list[dict]:
    return [
        {"id": i["id"], "title": i["title"]}
        for i in json.loads(
            op(["item", "list", "--vault", vault, "--format", "json"]) or b"[]"
        )
    ]


def create_vault(name: str) -> None:
    op(["vault", "create", name, "--format", "json"])


def _text_field(label: str, value: str) -> dict:
    return {"id": label, "label": label, "type": "STRING", "value": value}


def save(vault: str, item: str, field: str, secret: str, facts: dict) -> str:
    """Create or update ``item`` in ``vault`` with ``secret`` in a concealed ``field``.

    The secret and the item JSON go to op on stdin only. Returns the
    ``op://`` reference.
    """
    found = next((i for i in items(vault) if i["title"] == item), None)
    extra = [_text_field(k, str(v)) for k, v in facts.items() if v is not None]
    if found is None:
        template = {
            "title": item,
            "category": "SECURE_NOTE",
            "fields": [
                {"id": field, "label": field, "type": "CONCEALED", "value": secret},
                *extra,
            ],
        }
        made = op(
            ["item", "create", "--vault", vault, "--format", "json"],
            stdin=json.dumps(template).encode(),
        )
        item_id = str(json.loads(made).get("id") or "")
    else:
        item_id = found["id"]
        current = json.loads(
            op(["item", "get", found["id"], "--vault", vault, "--format", "json"])
        )
        fields = [
            f
            for f in current.get("fields") or []
            if f.get("label") not in {field, *facts}
        ]
        fields.append(
            {"id": field, "label": field, "type": "CONCEALED", "value": secret}
        )
        current["fields"] = fields + extra
        op(
            ["item", "edit", found["id"], "--vault", vault, "--format", "json"],
            stdin=json.dumps(current).encode(),
        )
    vault_part = vault if _REF_PART.match(vault) else _vault_id(vault)
    item_part = item if _REF_PART.match(item) else item_id
    return f"op://{vault_part}/{item_part}/{field}"


# Characters a secret reference may hold; other names go in as their IDs.
_REF_PART = re.compile(r"^[A-Za-z0-9_.\- ]+$")


def _vault_id(name: str) -> str:
    return next((v["id"] for v in vaults() if v["name"] == name), name)


# --- the login window --------------------------------------------------------


def _left_login(url: str, spec: Site, login_url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host or url.startswith("about:"):
        return False
    hosts = set(spec.login_hosts) | {(urlparse(login_url).hostname or "").lower()}
    return host not in hosts


async def capture(
    spec: Site,
    *,
    login_url: Optional[str] = None,
    headless: bool = False,
    timeout_s: float = 600,
    poll_s: float = 1.0,
    settle_s: float = 2.0,
    accept_unconfirmed: bool = False,
) -> tuple[list[dict], str]:
    """Open the login page and wait for the person to log in.

    Returns the site's cookies (in memory) and how the login was seen.
    Raises :class:`SessionError` on timeout or when the window closes before
    a login was seen.
    """
    from playwright.async_api import async_playwright

    login_url = login_url or spec.login_url
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        try:
            # A new context is off the record: Chromium keeps its cookies in
            # memory and writes none to the profile on disk.
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(login_url, wait_until="domcontentloaded")
            last: list[dict] = []
            last_url = login_url
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    state = await context.storage_state()
                    last_url = page.url
                except Exception:  # noqa: BLE001 - window closed
                    break
                try:
                    last = site_sessions.site_cookies(state, spec)
                except SessionError:
                    last = []
                if last and site_sessions.has_login_cookie(last, spec) and _left_login(
                    last_url, spec, login_url
                ):
                    await asyncio.sleep(settle_s)
                    try:
                        last = site_sessions.site_cookies(
                            await context.storage_state(), spec
                        )
                    except Exception:  # noqa: BLE001 - keep the earlier snapshot
                        pass
                    return last, "login seen"
                if page.is_closed():
                    break
                await asyncio.sleep(poll_s)
            else:
                raise SessionError(
                    f"no login within {int(timeout_s)} s; nothing was saved"
                )
        finally:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001 - already gone
                pass
    if last and accept_unconfirmed and _left_login(last_url, spec, login_url):
        return last, "window closed, login not confirmed"
    names = ", ".join(n for n, _ in spec.login_cookies) or "none defined"
    raise SessionError(
        "the window closed before a login was seen (login cookies looked for: "
        f"{names}); nothing was saved. If you were logged in, run again with "
        "--accept-unconfirmed."
    )


# --- terminal picks ----------------------------------------------------------


def _pick(title: str, options: list[str], new_label: str) -> str:
    print(title)
    for n, name in enumerate(options, 1):
        print(f"  {n}) {name}")
    print(f"  n) {new_label}")
    while True:
        answer = input("> ").strip()
        if answer.lower() == "n":
            name = input("name: ").strip()
            if name:
                return NEW + ":" + name
        elif answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]


def _interactive(args) -> None:
    if args.vault and args.item:
        return
    if not sys.stdin.isatty():
        raise SessionError("--vault and --item are needed when there is no terminal")
    if not args.vault:
        picked = _pick("Vault:", [v["name"] for v in vaults()], "new vault")
        if picked.startswith(NEW + ":"):
            args.vault = picked.split(":", 1)[1]
            create_vault(args.vault)
            print(f"created vault {args.vault}")
        else:
            args.vault = picked
    if not args.item:
        picked = _pick(
            f"Item in {args.vault}:",
            [i["title"] for i in items(args.vault)],
            "new item",
        )
        args.item = picked.split(":", 1)[1] if picked.startswith(NEW + ":") else picked


# --- commands ----------------------------------------------------------------


def _facts(spec: Site, cookies: list[dict]) -> dict:
    info = site_sessions.describe(cookies, spec)
    return {
        "site": spec.name,
        "captured": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "expires": info["expires"],
    }


def _report(lines: dict) -> None:
    for key, value in lines.items():
        if value is not None:
            print(f"{key}: {value}")


def cmd_capture(args) -> int:
    spec = site_sessions.site(args.site)
    _interactive(args)
    if args.new_vault:
        create_vault(args.vault)
    print(f"Opening the {spec.title} login page. Log in in the window that opened.")
    sys.stdout.flush()
    cookies, how = asyncio.run(
        capture(
            spec,
            login_url=args.login_url,
            headless=args.headless,
            timeout_s=args.timeout,
            accept_unconfirmed=args.accept_unconfirmed,
        )
    )
    info = site_sessions.describe(cookies, spec)
    secret = json.dumps({"cookies": cookies})
    ref = save(args.vault, args.item, args.field, secret, _facts(spec, cookies))
    out = {
        "saved": ref,
        "login": how,
        "cookies": info["cookies"],
        "expires": info["expires"] or "when the browser session ends",
    }
    _report(out)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="site-login",
        description="Log in to a site yourself and keep the session in 1Password.",
    )
    sub = parser.add_subparsers(dest="command")

    def common(p):
        p.add_argument("--site", default="aliexpress", choices=sorted(site_sessions.SITES))

    cap = sub.add_parser("capture", help="log in and save the session (default)")
    common(cap)
    cap.add_argument("--vault", help="1Password vault name")
    cap.add_argument("--new-vault", action="store_true", help="create --vault first")
    cap.add_argument("--item", help="item title; created when missing")
    cap.add_argument("--field", default=DEFAULT_FIELD)
    cap.add_argument("--timeout", type=float, default=600, help="seconds to wait")
    cap.add_argument("--accept-unconfirmed", action="store_true")
    cap.add_argument("--login-url", help="start page (tests)")
    cap.add_argument("--headless", action="store_true", help="no window (tests)")
    cap.set_defaults(func=cmd_capture)

    vl = sub.add_parser("vaults", help="list vault names (JSON)")
    vl.set_defaults(func=lambda a: print(json.dumps(vaults())) or 0)
    il = sub.add_parser("items", help="list item titles in a vault (JSON)")
    il.add_argument("--vault", required=True)
    il.set_defaults(func=lambda a: print(json.dumps(items(a.vault))) or 0)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"capture", "vaults", "items", "-h", "--help"}
    if not argv or argv[0] not in commands:
        argv.insert(0, "capture")
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except (SessionError, OpError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stopped; nothing was saved", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
