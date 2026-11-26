"""Chromium profiles that stop the Windows blank-password logon probe.

On Windows, Chromium checks at startup whether the OS account has a blank
password (``CheckBlankPasswordWithPrefs`` in
``chrome/browser/password_manager/password_manager_util_win.cc``). Unless the
profile's ``Local State`` already records an OS password change time no older
than the real one, it calls ``LogonUser(<user>, ".", "", INTERACTIVE)``. With
a real password that logon fails and Windows counts it as a failed logon
(event 4625). Chromium then caches the answer in ``Local State``.

Every browser launch here used a throwaway profile, so the answer was never
cached and every launch made one failed logon. About ten browser fetches in
ten minutes reach a typical lockout threshold and lock the account that runs
the server.

:func:`seeded_profile` gives each launch its own temporary profile whose
``Local State`` already says "password not blank, checked now". Chromium then
skips the ``LogonUser`` call. Each launch still gets a fresh, empty profile,
so no cookies or storage carry over between fetches.

The probe exists only on Windows. On other platforms :func:`seeded_profile`
yields ``None`` and the launch code keeps its usual path.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

LOGGER = logging.getLogger(__name__)

LOCAL_STATE = "Local State"
PROFILE_PREFIX = "searxncrawl-profile-"

# Seconds from 1601-01-01 (the Windows FILETIME epoch, which Chromium's
# base::Time uses internally) to 1970-01-01.
_WINDOWS_EPOCH_OFFSET_S = 11_644_473_600


def needs_seeded_profile() -> bool:
    """Whether Chromium on this platform probes for a blank OS password."""
    return sys.platform == "win32"


def chromium_time(now: Optional[float] = None) -> str:
    """``now`` (Unix seconds) as Chromium stores times in prefs.

    Chromium's ``base::Time::ToInternalValue()`` is microseconds since
    1601-01-01 UTC; int64 prefs are written to JSON as strings.
    """
    seconds = time.time() if now is None else now
    return str(int((seconds + _WINDOWS_EPOCH_OFFSET_S) * 1_000_000))


def password_check_prefs(now: Optional[float] = None) -> dict[str, Any]:
    """The ``password_manager`` prefs that make Chromium skip the probe."""
    return {
        "os_password_blank": False,
        "os_password_last_changed": chromium_time(now),
    }


def seed_local_state(user_data_dir: str | Path, now: Optional[float] = None) -> Path:
    """Write the password-check prefs into ``<user_data_dir>/Local State``.

    Other keys in an existing ``Local State`` are kept. Returns the file path.
    """
    path = Path(user_data_dir) / LOCAL_STATE
    state: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except (OSError, ValueError) as exc:
            LOGGER.debug("Replacing unreadable %s: %s", path, exc)
    manager = state.get("password_manager")
    if not isinstance(manager, dict):
        manager = {}
    manager.update(password_check_prefs(now))
    state["password_manager"] = manager
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


@contextmanager
def seeded_profile() -> Iterator[Optional[str]]:
    """A fresh, seeded Chromium user-data dir for one launch, or ``None``.

    On Windows this yields a new temporary directory with a seeded
    ``Local State`` and removes it afterwards. Elsewhere it yields ``None``:
    the caller launches the browser the way it always did.
    """
    if not needs_seeded_profile():
        yield None
        return
    profile = tempfile.mkdtemp(prefix=PROFILE_PREFIX)
    try:
        seed_local_state(profile)
        yield profile
    finally:
        # Chromium can hold files for a moment after it exits; a leftover
        # temp dir is harmless, a failed crawl is not.
        shutil.rmtree(profile, ignore_errors=True)


def stealth_profile_kwargs(profile: Optional[str]) -> dict[str, Any]:
    """Keyword arguments for Scrapling's ``StealthyFetcher`` launch."""
    return {"user_data_dir": profile} if profile else {}


def crawl4ai_browser_config(profile: str, **kwargs: Any) -> Any:
    """A Crawl4AI ``BrowserConfig`` that launches Chromium in ``profile``.

    Crawl4AI's default launch lets Playwright pick a temporary profile that
    cannot be seeded, so a seeded profile needs a persistent context.
    Playwright's ``launch_persistent_context`` takes no ``storage_state``,
    so the session is not passed here: :func:`install_storage_state_hook`
    loads it into the context instead.
    """
    from crawl4ai import BrowserConfig

    return BrowserConfig(
        use_persistent_context=True,
        user_data_dir=profile,
        **kwargs,
    )


def _local_storage_script(origins: list[dict[str, Any]]) -> Optional[str]:
    """An init script that puts each origin's saved localStorage in place."""
    entries = {
        o["origin"]: {
            item["name"]: item["value"]
            for item in o.get("localStorage") or []
            if "name" in item and "value" in item
        }
        for o in origins
        if isinstance(o, dict) and o.get("origin")
    }
    entries = {origin: items for origin, items in entries.items() if items}
    if not entries:
        return None
    return (
        "(() => { const saved = "
        + json.dumps(entries)
        + "; const items = saved[location.origin]; if (!items) return;"
        " try { for (const [k, v] of Object.entries(items))"
        " localStorage.setItem(k, v); } catch (e) {} })();"
    )


def load_storage_state(path: str) -> dict[str, Any]:
    """Read a Playwright ``storage_state`` JSON file."""
    with open(path, encoding="utf-8") as fh:
        state = json.load(fh)
    return state if isinstance(state, dict) else {}


async def apply_storage_state(context: Any, state: dict[str, Any]) -> None:
    """Load a Playwright ``storage_state`` into an existing browser context.

    Cookies go in through ``add_cookies``. localStorage goes in through an
    init script that runs before the page's own scripts on each matching
    origin, which is where Playwright's ``new_context(storage_state=...)``
    puts it too.
    """
    cookies = [c for c in state.get("cookies") or [] if isinstance(c, dict)]
    if cookies:
        await context.add_cookies(cookies)
    script = _local_storage_script(state.get("origins") or [])
    if script:
        await context.add_init_script(script)


def install_storage_state_hook(crawler: Any, storage_state: str) -> None:
    """Load ``storage_state`` into a persistent-context crawler's context.

    Runs once per context, before the first page navigates.
    """
    state = load_storage_state(storage_state)

    async def on_page_context_created(
        page: Any, context: Any = None, **kwargs: Any
    ) -> Any:
        target = context if context is not None else page.context
        if not getattr(target, "_searxncrawl_state_applied", False):
            await apply_storage_state(target, state)
            target._searxncrawl_state_applied = True
        return page

    crawler.crawler_strategy.set_hook(
        "on_page_context_created", on_page_context_created
    )
