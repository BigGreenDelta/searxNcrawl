"""Polite pacing and a fetch log for AliExpress page requests.

Only page requests to AliExpress itself go through here: the item route's
page load and every fetch tier that asks www.aliexpress.com (or another
AliExpress page host) for a page. Description files and images come from
aliexpress-media.com / alicdn.com and are neither paced nor logged. Cached
pages never reach this module, so a repeat within the cache day is free.

Pacing: requests are let out one at a time, in arrival order, and each one
starts a random 0.5 to 3 s (uniform) after the one before it. The gap only
spaces requests out; it never retries or changes a request. Firecrawl
attempts leave from Firecrawl's servers, not this IP, so they are logged but
not paced.

Log: one JSON line per attempt in a small rotating file next to the fetch
cache: time (UTC), item id, URL, tier, seconds, seconds waited for the turn,
and the outcome: ``ok``, ``challenged`` (slider CAPTCHA or another bot
wall) or ``error``. ``summary()`` (MCP tool ``aliexpress_fetch_log``) counts
the last N days: outcomes, challenge rate, by tier, by day and by hour.

Environment:
    ALIEXPRESS_GAP_MIN_S / ALIEXPRESS_GAP_MAX_S: the random gap between two
        page requests, in seconds (default 0.5 and 3; 0 and 0 turn pacing off)
    ALIEXPRESS_LOG_PATH: the log file (default
        ~/.cache/searxncrawl/aliexpress-fetches.jsonl; "off" turns it off)
    ALIEXPRESS_LOG_MAX_BYTES: size before the log rotates (default 1 MB;
        five old files are kept)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import weakref
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)

DEFAULT_GAP_MIN_S = 0.5
DEFAULT_GAP_MAX_S = 3.0
DEFAULT_LOG_PATH = Path.home() / ".cache" / "searxncrawl" / "aliexpress-fetches.jsonl"
DEFAULT_LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 5

# Page hosts only: aliexpress.com and its country sites, not the media CDNs.
_HOST = re.compile(r"(^|\.)aliexpress\.(com|us|ru)$", re.IGNORECASE)
_ITEM = re.compile(r"/item/(\d+)\.html", re.IGNORECASE)

# Patchable in tests.
_clock = time.monotonic
_sleep = asyncio.sleep
_uniform = random.uniform


def paced(url: str) -> bool:
    """True for a page on an AliExpress page host."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return bool(_HOST.search(host))


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return max(0.0, float(raw)) if raw else default
    except ValueError:
        return default


def gap_bounds() -> tuple[float, float]:
    low = _env_float("ALIEXPRESS_GAP_MIN_S", DEFAULT_GAP_MIN_S)
    high = _env_float("ALIEXPRESS_GAP_MAX_S", DEFAULT_GAP_MAX_S)
    return low, max(low, high)


@dataclass
class _Turns:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_start: Optional[float] = None


# One queue per event loop: an asyncio.Lock belongs to the loop it runs on.
_turns: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Turns]" = (
    weakref.WeakKeyDictionary()
)


async def wait_turn(url: str) -> float:
    """Wait until ``url`` may go out; seconds waited (0 for other hosts)."""
    if not paced(url):
        return 0.0
    low, high = gap_bounds()
    if high <= 0:
        return 0.0
    loop = asyncio.get_running_loop()
    turns = _turns.get(loop)
    if turns is None:
        turns = _turns[loop] = _Turns()
    began = _clock()
    async with turns.lock:
        if turns.last_start is not None:
            due = turns.last_start + _uniform(low, high)
            delay = due - _clock()
            if delay > 0:
                await _sleep(delay)
        turns.last_start = _clock()
    return turns.last_start - began


# --- log --------------------------------------------------------------------

_log = logging.getLogger("searxncrawl.aliexpress_fetches")
_log.propagate = False
_log.setLevel(logging.INFO)
_log_path: Optional[Path] = None


def log_path() -> Optional[Path]:
    raw = os.getenv("ALIEXPRESS_LOG_PATH", "").strip()
    if raw.lower() == "off":
        return None
    return Path(raw).expanduser() if raw else DEFAULT_LOG_PATH


def _writer() -> Optional[logging.Logger]:
    """The log, (re)opened when the configured path changed."""
    global _log_path
    path = log_path()
    if path is None:
        return None
    if path != _log_path:
        for handler in list(_log.handlers):
            _log.removeHandler(handler)
            handler.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        size = int(_env_float("ALIEXPRESS_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES))
        handler = RotatingFileHandler(
            path,
            maxBytes=max(1024, size),
            backupCount=LOG_BACKUPS,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        _log.addHandler(handler)
        _log_path = path
    return _log


def item_id(url: str) -> Optional[str]:
    found = _ITEM.search(urlsplit(url).path or "")
    return found.group(1) if found else None


def record(
    url: str,
    tier: str,
    seconds: float,
    outcome: str,
    *,
    waited: float = 0.0,
    note: str = "",
) -> None:
    """One JSON line for one AliExpress page attempt; never raises."""
    if not paced(url):
        return
    entry = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "item": item_id(url),
        "url": url,
        "tier": tier,
        "seconds": round(seconds, 2),
        "waited": round(waited, 2),
        "outcome": outcome,
    }
    if note:
        entry["note"] = note[:200]
    try:
        writer = _writer()
        if writer is not None:
            writer.info(json.dumps(entry, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - a log problem must not fail a fetch
        LOGGER.debug("AliExpress fetch log failed: %s", exc)


# --- summary ----------------------------------------------------------------


def _log_files(path: Path) -> list[Path]:
    files = [path.with_name(f"{path.name}.{n}") for n in range(LOG_BACKUPS, 0, -1)]
    return [f for f in files + [path] if f.exists()]


def read_entries(path: Optional[Path] = None) -> list[dict]:
    """Every logged attempt, oldest first; unreadable lines are skipped."""
    path = path or log_path()
    if path is None:
        return []
    entries = []
    for file in _log_files(path):
        try:
            with open(file, encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and "time" in entry:
                entries.append(entry)
    return entries


def _rate(counts: Counter) -> str:
    total = sum(counts.values())
    if not total:
        return "-"
    return f"{100 * counts['challenged'] / total:.0f}%"


def summary(
    days: int = 7,
    entries: Optional[Iterable[dict]] = None,
    now: Optional[datetime] = None,
) -> str:
    """Markdown: AliExpress page attempts in the last ``days`` days."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    picked = []
    for entry in read_entries() if entries is None else entries:
        try:
            at = datetime.fromisoformat(entry["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at >= since:
            picked.append((at, entry))
    head = f"# AliExpress page fetches, last {days} days (UTC)"
    if not picked:
        return f"{head}\n\nNo AliExpress page fetches logged since {since:%Y-%m-%d %H:%M}.\n"

    outcomes = Counter(e.get("outcome", "?") for _, e in picked)
    by_tier: dict[str, Counter] = defaultdict(Counter)
    by_day: dict[str, Counter] = defaultdict(Counter)
    by_hour: dict[int, Counter] = defaultdict(Counter)
    for at, e in picked:
        outcome = e.get("outcome", "?")
        by_tier[e.get("tier", "?")][outcome] += 1
        by_day[at.strftime("%Y-%m-%d")][outcome] += 1
        by_hour[at.hour][outcome] += 1
    total = len(picked)
    waits = [float(e.get("waited") or 0) for _, e in picked]
    last_challenge = next(
        (at for at, e in reversed(picked) if e.get("outcome") == "challenged"), None
    )

    def table(title: str, rows: dict, key_name: str) -> list[str]:
        out = [
            f"## {title}",
            "",
            f"| {key_name} | Fetches | OK | Challenged | Error | Challenge rate |",
            "|---|---|---|---|---|---|",
        ]
        for key in sorted(rows):
            c = rows[key]
            label = f"{key:02d}:00" if isinstance(key, int) else key
            out.append(
                f"| {label} | {sum(c.values())} | {c['ok']} | {c['challenged']} "
                f"| {c['error']} | {_rate(c)} |"
            )
        return out + [""]

    lines = [
        head,
        "",
        f"- Fetches: {total} (ok {outcomes['ok']}, challenged "
        f"{outcomes['challenged']}, error {outcomes['error']})",
        f"- Challenge rate: {_rate(outcomes)}",
        f"- Last challenge: {last_challenge:%Y-%m-%d %H:%M}"
        if last_challenge
        else "- Last challenge: none in this window",
        f"- Wait for a turn: average {sum(waits) / total:.1f} s, longest {max(waits):.1f} s",
        "",
    ]
    lines += table("By tier", by_tier, "Tier")
    lines += table("By day", by_day, "Day")
    lines += table("By hour of day", by_hour, "Hour")
    return "\n".join(lines).rstrip() + "\n"
