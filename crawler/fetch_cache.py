"""SQLite cache of successfully fetched pages, keyed by URL.

A research run often fetches the same page more than once (search with
``include_content``, then ``crawl`` on the same hit). The cache returns the
stored Markdown instead of fetching again, which matters most for the paid
Firecrawl tier.

Environment:
    FETCH_CACHE_PATH: SQLite file (default ``~/.cache/searxncrawl/fetch.sqlite``)
    FETCH_CACHE_TTL: Seconds a cached page stays valid (default 86400; 0 disables)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from .document import CrawledDocument

LOGGER = logging.getLogger(__name__)

DEFAULT_TTL = 86400
DEFAULT_PATH = Path.home() / ".cache" / "searxncrawl" / "fetch.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    final_url TEXT NOT NULL,
    markdown TEXT NOT NULL,
    metadata TEXT NOT NULL,
    fetched_at REAL NOT NULL
)
"""


def cache_ttl() -> int:
    raw = os.getenv("FETCH_CACHE_TTL", "").strip()
    if not raw:
        return DEFAULT_TTL
    try:
        return max(0, int(raw))
    except ValueError:
        LOGGER.warning("Ignoring invalid FETCH_CACHE_TTL=%r", raw)
        return DEFAULT_TTL


def cache_path() -> Path:
    raw = os.getenv("FETCH_CACHE_PATH", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_PATH


def normalize_url(url: str) -> str:
    """Cache key: scheme and host lower-cased, fragment dropped."""
    parts = urlsplit(url.strip())
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


class FetchCache:
    def __init__(self, path: Optional[Path] = None, ttl: Optional[int] = None):
        self.ttl = cache_ttl() if ttl is None else ttl
        self.path = path or cache_path()
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def enabled(self) -> bool:
        return self.ttl > 0

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.execute(_SCHEMA)
        return self._conn

    def get(self, url: str) -> Optional[CrawledDocument]:
        if not self.enabled:
            return None
        row = (
            self._db()
            .execute(
                "SELECT final_url, markdown, metadata, fetched_at FROM pages WHERE url = ?",
                (normalize_url(url),),
            )
            .fetchone()
        )
        if row is None:
            return None
        final_url, markdown, metadata, fetched_at = row
        age = time.time() - fetched_at
        if age > self.ttl:
            return None
        meta = json.loads(metadata)
        meta["cache_age_seconds"] = int(age)
        return CrawledDocument(
            request_url=url,
            final_url=final_url,
            status="success",
            markdown=markdown,
            metadata=meta,
        )

    def put(self, doc: CrawledDocument) -> None:
        if not self.enabled or doc.status == "failed" or not doc.markdown:
            return
        meta = {k: v for k, v in doc.metadata.items() if k != "cache_age_seconds"}
        db = self._db()
        db.execute(
            "INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?, ?)",
            (
                normalize_url(doc.request_url),
                doc.final_url,
                doc.markdown,
                json.dumps(meta, default=str),
                time.time(),
            ),
        )
        db.commit()
