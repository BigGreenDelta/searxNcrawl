"""Logged-in site sessions that live in memory only.

A site session is the cookie part of a Playwright ``storage_state`` for one
site (AliExpress first). It is a secret: it never goes to a file, a log, the
fetch cache, a command line or an environment variable of this process.

``SITE_SESSIONS`` names a 1Password reference per site
(``aliexpress=op://<vault>/<item>/<field>``). At fetch time the route runs
``op read`` and keeps the answer in memory for that one fetch.

``op`` signs in with a service account. On Windows its token sits in the
Windows Credential Manager (generic credential named by
``SITE_SESSIONS_OP_CREDENTIAL``, default ``web-mcp/1password-service-account``)
and is put only into the environment of the ``op`` child process, so the
server and the browsers it starts never hold it in their environment.
Elsewhere ``op`` uses whatever sign-in it already has
(``OP_SERVICE_ACCOUNT_TOKEN`` or the desktop app).

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
from dataclasses import dataclass
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)

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


def site_cookies_or_empty(state: Any, spec: Site) -> list[dict]:
    """:func:`site_cookies`, or an empty list when there are none."""
    try:
        return site_cookies(state, spec)
    except SessionError:
        return []


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

DEFAULT_OP_CREDENTIAL = "web-mcp/1password-service-account"
TOKEN_ENV = "OP_SERVICE_ACCOUNT_TOKEN"


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


def op_credential_name() -> str:
    return os.getenv("SITE_SESSIONS_OP_CREDENTIAL", "").strip() or DEFAULT_OP_CREDENTIAL


def _cred_read(target: str) -> Optional[str]:
    """A generic credential's secret from the Windows Credential Manager, or None.

    The blob is read as UTF-16 (what ``CredWrite`` callers on Windows store).
    Raises :class:`SessionError` with the Win32 error code only.
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredReadW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(CREDENTIAL)),
    ]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    found = ctypes.POINTER(CREDENTIAL)()
    if not advapi.CredReadW(target, 1, 0, ctypes.byref(found)):  # 1 = CRED_TYPE_GENERIC
        code = ctypes.get_last_error()
        if code == 1168:  # ERROR_NOT_FOUND
            return None
        raise SessionError(f"cannot read the Windows credential {target} (error {code})")
    try:
        cred = found.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
    finally:
        advapi.CredFree(found)
    return blob.decode("utf-16-le").strip() or None


def op_env() -> dict[str, str]:
    """The environment for one ``op`` child: ours, plus the service account token.

    The token is fetched for this child only and is never put into
    ``os.environ``.
    """
    env = dict(os.environ)
    if not env.get(TOKEN_ENV):
        token = _cred_read(op_credential_name())
        if token:
            env[TOKEN_ENV] = token
    return env


def token_source() -> str:
    """Where ``op`` gets its sign-in from (non-secret)."""
    if os.getenv(TOKEN_ENV):
        return TOKEN_ENV
    if os.name == "nt":
        try:
            found = _cred_read(op_credential_name()) is not None
        except SessionError as exc:
            return str(exc)
        if found:
            return f"Windows credential {op_credential_name()}"
        return f"missing: no Windows credential {op_credential_name()}"
    return "op's own sign-in"


def op_read(ref: str) -> bytes:
    """``op read <ref>`` into memory. The reference is not a secret; the answer is."""
    try:
        done = subprocess.run(
            ["op", "read", "--no-newline", ref],
            capture_output=True,
            timeout=OP_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            env=op_env(),
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
    cookies: list[dict]

    def __repr__(self) -> str:  # never print the cookies
        return f"Session(site={self.site!r}, cookies={len(self.cookies)})"


def get(name: str) -> tuple[Optional[Session], Optional[str]]:
    """The session for a site and, when there is none, why (non-secret)."""
    spec = site(name)
    ref = configured_refs().get(spec.name)
    if not ref:
        return None, f"no {spec.title} session configured on this server"
    try:
        state = json.loads(op_read(ref))
    except SessionError as exc:
        return None, str(exc)
    except ValueError:
        return None, f"{ref} does not hold a storage_state JSON"
    try:
        return Session(spec.name, site_cookies(state, spec)), None
    except SessionError as exc:
        return None, f"{ref}: {exc}"


def status() -> list[dict]:
    """Non-secret status of every site: its 1Password reference and op's sign-in."""
    refs = configured_refs()
    source = token_source() if refs else None
    return [
        {"site": name, "op_ref": refs.get(name), "op_sign_in": source if name in refs else None}
        for name in SITES
    ]
