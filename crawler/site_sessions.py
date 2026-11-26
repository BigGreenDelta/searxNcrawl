"""Logged-in site sessions that live in memory only.

A site session is the cookie part of a Playwright ``storage_state`` for one
site (AliExpress first). It is a secret: it never goes to a file, a log, the
fetch cache, a command line or an environment variable. It reaches the server
in one of two ways, both in memory:

- ``SITE_SESSIONS`` names a 1Password reference per site
  (``aliexpress=op://<vault>/<item>/<field>``). At fetch time the route runs
  ``op read`` and keeps the answer in memory for that fetch. This works only
  where ``op`` can sign in without a person (a service account).
- ``POST /sessions/<site>`` on the HTTP server (behind the bearer token) puts
  a session into this process's memory. ``site-login push`` sends it from a
  machine that can read 1Password. A restart forgets it.

Only cookies of the site's own domains are kept. Local storage and every
other domain are dropped on the way in, so a session can never reach another
site. Errors name what was wrong, never the value.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)

# A storage_state for one site is a few KB; anything far larger is not one.
MAX_STATE_BYTES = 512 * 1024
OP_TIMEOUT_S = 30


@dataclass(frozen=True)
class Site:
    """What the server and the capture script need to know about one site."""

    name: str
    title: str
    login_url: str
    # Registrable domains. A cookie belongs to the site when its domain is one
    # of these or a subdomain of one.
    domains: tuple[str, ...]
    # Hosts of the login pages; the browser has left the login when the page
    # is on another host.
    login_hosts: tuple[str, ...] = ()
    # Cookie names that only a signed-in browser holds, with an optional text
    # the value must contain. Unverified per site until a real login shows it.
    login_cookies: tuple[tuple[str, str], ...] = ()


SITES: dict[str, Site] = {
    "aliexpress": Site(
        name="aliexpress",
        title="AliExpress",
        login_url="https://login.aliexpress.com/",
        domains=("aliexpress.com", "aliexpress.us"),
        login_hosts=("login.aliexpress.com", "login.aliexpress.us"),
        # Havana (Alibaba's login) sets _hvn_login after sign-in, and
        # xman_us_t carries x_lid (the login id) only for a signed-in buyer.
        login_cookies=(("_hvn_login", ""), ("xman_us_t", "x_lid=")),
    ),
}


class SessionError(ValueError):
    """A session could not be read or accepted. The message holds no secret."""


def site(name: str) -> Site:
    found = SITES.get((name or "").strip().lower())
    if found is None:
        raise SessionError(
            f"unknown site {name!r}; known sites: {', '.join(sorted(SITES))}"
        )
    return found


def _cookie_domain_ok(domain: str, spec: Site) -> bool:
    host = domain.strip().lstrip(".").lower()
    return any(host == d or host.endswith("." + d) for d in spec.domains)


_COOKIE_KEYS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure")
_SAME_SITE = ("Strict", "Lax", "None")


def site_cookies(state: Any, spec: Site) -> list[dict]:
    """The site's own cookies from a storage_state, checked field by field.

    Raises :class:`SessionError` (never echoing a value) when the input is not
    a storage_state. Cookies of other domains and all local storage are
    dropped.
    """
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise SessionError("not a storage_state: expected an object with a cookies list")
    out = []
    for index, raw in enumerate(state["cookies"]):
        if not isinstance(raw, dict):
            raise SessionError(f"cookie {index} is not an object")
        name, value, domain = raw.get("name"), raw.get("value"), raw.get("domain")
        if not (isinstance(name, str) and name and isinstance(value, str)):
            raise SessionError(f"cookie {index} has no name or value")
        if not isinstance(domain, str) or not domain:
            raise SessionError(f"cookie {index} has no domain")
        if not _cookie_domain_ok(domain, spec):
            continue
        cookie: dict[str, Any] = {k: raw[k] for k in _COOKIE_KEYS if k in raw}
        cookie.setdefault("path", "/")
        expires = cookie.get("expires", -1)
        if not isinstance(expires, (int, float)) or isinstance(expires, bool):
            raise SessionError(f"cookie {index} has a bad expiry")
        cookie["expires"] = expires
        if raw.get("sameSite") in _SAME_SITE:
            cookie["sameSite"] = raw["sameSite"]
        out.append(cookie)
    if not out:
        raise SessionError(f"no {spec.title} cookies in this storage_state")
    return out


def has_login_cookie(cookies: list[dict], spec: Site, now: Optional[float] = None) -> bool:
    """Whether the cookies include one that only a signed-in browser holds."""
    now = time.time() if now is None else now
    for cookie in cookies:
        for name, must in spec.login_cookies:
            if cookie.get("name") != name or must not in str(cookie.get("value", "")):
                continue
            expires = cookie.get("expires", -1)
            if expires is None or expires < 0 or expires > now:
                return True
    return False


def login_expiry(cookies: list[dict], spec: Site) -> Optional[float]:
    """When the login cookies run out (Unix seconds); None for browser-session cookies."""
    names = {n for n, _ in spec.login_cookies}
    times = [
        c["expires"]
        for c in cookies
        if c.get("name") in names and isinstance(c.get("expires"), (int, float))
        and c["expires"] > 0
    ]
    return max(times) if times else None


def describe(cookies: list[dict], spec: Site) -> dict:
    """Non-secret facts about a session: counts and dates only."""
    expiry = login_expiry(cookies, spec)
    return {
        "site": spec.name,
        "cookies": len(cookies),
        "login_cookie": has_login_cookie(cookies, spec),
        "expires": time.strftime("%Y-%m-%d", time.gmtime(expiry)) if expiry else None,
    }


# --- where the server gets a session ----------------------------------------


@dataclass
class _Pushed:
    cookies: list[dict]
    at: float = field(default_factory=time.time)


_PUSHED: dict[str, _Pushed] = {}


def push(name: str, body: bytes) -> dict:
    """Keep a pushed storage_state (JSON bytes) in memory; non-secret status back."""
    spec = site(name)
    if len(body) > MAX_STATE_BYTES:
        raise SessionError("storage_state is too large")
    try:
        state = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise SessionError("storage_state is not valid JSON") from None
    cookies = site_cookies(state, spec)
    _PUSHED[spec.name] = _Pushed(cookies)
    LOGGER.info("%s session received in memory (%d cookies)", spec.title, len(cookies))
    return describe(cookies, spec)


def forget(name: str) -> bool:
    """Drop a pushed session. True when there was one."""
    return _PUSHED.pop(site(name).name, None) is not None


def configured_refs(env: Optional[str] = None) -> dict[str, str]:
    """``SITE_SESSIONS`` as {site: op reference}; entries that are not op:// are ignored."""
    raw = os.getenv("SITE_SESSIONS", "") if env is None else env
    refs: dict[str, str] = {}
    for part in raw.replace(";", ",").split(","):
        name, sep, ref = part.partition("=")
        name, ref = name.strip().lower(), ref.strip()
        if sep and name in SITES and ref.startswith("op://"):
            refs[name] = ref
    return refs


def op_read(ref: str) -> bytes:
    """``op read <ref>`` into memory. The reference is not a secret; the answer is."""
    try:
        done = subprocess.run(
            ["op", "read", "--no-newline", ref],
            capture_output=True,
            timeout=OP_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise SessionError("the 1Password CLI (op) is not installed here") from None
    except subprocess.TimeoutExpired:
        raise SessionError("op read timed out") from None
    if done.returncode:
        first = (done.stderr.decode(errors="replace").strip().splitlines() or [""])[0]
        raise SessionError(f"op read {ref} failed: {first[:200]}")
    return done.stdout


@dataclass(frozen=True)
class Session:
    """A session ready for one fetch. ``cookies`` is the secret part."""

    site: str
    source: str  # "1password" or "pushed"
    cookies: list[dict]

    def __repr__(self) -> str:  # never print the cookies
        return f"Session(site={self.site!r}, source={self.source!r}, cookies={len(self.cookies)})"


def get(name: str) -> tuple[Optional[Session], Optional[str]]:
    """The session for a site and, when there is none, why (non-secret).

    A configured 1Password reference wins; when ``op`` cannot read it, a
    pushed session is used if there is one.
    """
    spec = site(name)
    problems = []
    ref = configured_refs().get(spec.name)
    if ref:
        try:
            state = json.loads(op_read(ref))
            return Session(spec.name, "1password", site_cookies(state, spec)), None
        except SessionError as exc:
            problems.append(str(exc))
        except ValueError:
            problems.append(f"{ref} does not hold a storage_state JSON")
    pushed = _PUSHED.get(spec.name)
    if pushed is not None:
        return Session(spec.name, "pushed", [dict(c) for c in pushed.cookies]), None
    if not problems:
        problems.append(f"no {spec.title} session on this server")
    return None, "; ".join(problems)


def status() -> list[dict]:
    """Non-secret status of every site: configured reference, pushed session."""
    refs = configured_refs()
    out = []
    for name, spec in SITES.items():
        row: dict[str, Any] = {"site": name, "op_ref": refs.get(name)}
        pushed = _PUSHED.get(name)
        if pushed is not None:
            row["pushed"] = describe(pushed.cookies, spec)
            row["pushed"]["at"] = time.strftime(
                "%Y-%m-%d %H:%M UTC", time.gmtime(pushed.at)
            )
        out.append(row)
    return out
