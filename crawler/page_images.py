"""Page images as MCP image content blocks, for ``crawl(images=true)``.

A site route lists the images worth seeing in ``metadata["page_images"]``
(``[{"url": ..., "label": ...}]``, in page order; the AliExpress route lists
its description images). This module downloads them, scales them down and
encodes them as JPEG, so a vision-capable caller can read them itself.

Very tall or very wide images (long banners) are cut into tiles along the
long side first (top to bottom, or left to right): squeezing a 1000 x 5000 banner to 1568 px tall would make
its text too small to read.

Environment:
    CRAWL_IMAGES_MAX: image blocks per call, tiles included (default 30)
    CRAWL_IMAGES_MAX_BYTES: JPEG bytes per call, all blocks (default 12 MB)
    CRAWL_IMAGES_SIDE: long side of each block in pixels (default 1568)
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

import mcp.types as mcp_types

from .document import CrawledDocument

LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_IMAGES = 30
DEFAULT_MAX_BYTES = 12 * 1024 * 1024
DEFAULT_SIDE = 1568
# Largest source image downloaded.
_MAX_DOWNLOAD = 15 * 1024 * 1024
# A side more than this many times the other is cut into tiles.
_TILE_RATIO = 2.0
# Each tile covers this many times the short side, plus a small overlap so
# a line of text on a cut is whole in one of the two tiles.
_TILE_LENGTH = 1.5
_TILE_OVERLAP = 40
_JPEG_QUALITY = 82
_DOWNLOADS = 8
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if value >= 1 else default


def max_images() -> int:
    return _env_int("CRAWL_IMAGES_MAX", DEFAULT_MAX_IMAGES)


def max_bytes() -> int:
    return _env_int("CRAWL_IMAGES_MAX_BYTES", DEFAULT_MAX_BYTES)


def side() -> int:
    return _env_int("CRAWL_IMAGES_SIDE", DEFAULT_SIDE)


def page_images(doc: CrawledDocument) -> list[dict]:
    """The images a route listed for ``doc``, each with a url and a label."""
    found = (doc.metadata or {}).get("page_images") or []
    out = []
    for index, entry in enumerate(found, 1):
        if isinstance(entry, str):
            entry = {"url": entry}
        if isinstance(entry, dict) and str(entry.get("url", "")).startswith("http"):
            out.append(
                {"url": entry["url"], "label": entry.get("label") or f"image {index}"}
            )
    return out


def to_jpegs(data: bytes, long_side: int = DEFAULT_SIDE) -> list[bytes]:
    """One image as JPEG tiles, each at most ``long_side`` px on its long side."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as opened:
        opened.seek(0)  # first frame of an animation
        image = opened.convert("RGB")
    width, height = image.size
    tiles = []
    if max(width, height) > _TILE_RATIO * min(width, height):
        short = min(width, height)
        step = max(1, int(short * _TILE_LENGTH))
        total = max(width, height)
        start = 0
        while start < total:
            end = min(total, start + step)
            box = (0, start, width, end) if height > width else (start, 0, end, height)
            tiles.append(image.crop(box))
            if end >= total:
                break
            start = end - _TILE_OVERLAP
    else:
        tiles.append(image)
    out = []
    for tile in tiles:
        tile.thumbnail((long_side, long_side), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        tile.save(buffer, "JPEG", quality=_JPEG_QUALITY, optimize=True)
        out.append(buffer.getvalue())
    return out


Downloader = Callable[[str, str], Awaitable[Optional[bytes]]]


async def _download(url: str, referer: str, client: Any = None) -> Optional[bytes]:
    headers = {"user-agent": _UA, "accept": "image/*,*/*;q=0.5"}
    if referer:
        headers["referer"] = referer
    async with client.stream("GET", url, headers=headers) as resp:
        if resp.status_code != 200:
            return None
        data = bytearray()
        async for chunk in resp.aiter_bytes():
            data += chunk
            if len(data) > _MAX_DOWNLOAD:
                return None
        return bytes(data)


@dataclass
class _Shot:
    doc: int
    label: str
    url: str
    jpegs: list[bytes] = field(default_factory=list)
    error: str = ""


def _referer(doc: CrawledDocument) -> str:
    parts = urlsplit(doc.final_url or doc.request_url or "")
    return f"{parts.scheme}://{parts.netloc}/" if parts.netloc else ""


async def image_blocks(
    docs: list[CrawledDocument], download: Optional[Downloader] = None
) -> tuple[list[Any], str]:
    """Content blocks for every page image of ``docs``, and a note on them.

    Each image is a text block naming it (label and URL), then its image
    block(s). Images past the block or byte cap, or that fail, are named in
    the note so the caller can open their URLs.
    """
    wanted = [
        _Shot(n, img["label"], img["url"])
        for n, doc in enumerate(docs)
        if doc.status == "success"
        for img in page_images(doc)
    ]
    if not wanted:
        return [], ""
    gate = asyncio.Semaphore(_DOWNLOADS)
    long_side = side()

    async def fetch(shot: _Shot, client: Any) -> None:
        referer = _referer(docs[shot.doc])
        try:
            async with gate:
                if download is not None:
                    data = await download(shot.url, referer)
                else:
                    data = await _download(shot.url, referer, client)
            if not data:
                shot.error = "download failed"
                return
            shot.jpegs = await asyncio.to_thread(to_jpegs, data, long_side)
        except Exception as exc:  # noqa: BLE001 - one bad image is not the page
            LOGGER.debug("page image %s failed: %s", shot.url, exc)
            shot.error = f"failed: {type(exc).__name__}"

    if download is None:
        import httpx

        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            await asyncio.gather(*(fetch(s, client) for s in wanted))
    else:
        await asyncio.gather(*(fetch(s, None) for s in wanted))

    blocks: list[Any] = []
    left_out: list[str] = []
    count, size = 0, 0
    cap_blocks, cap_bytes = max_images(), max_bytes()
    multi = len(docs) > 1
    for shot in wanted:
        name = f"{shot.label}" + (f" (page {shot.doc + 1})" if multi else "")
        if shot.error:
            left_out.append(f"{name}: {shot.error} - {shot.url}")
            continue
        need = sum(len(j) for j in shot.jpegs)
        if count + len(shot.jpegs) > cap_blocks or size + need > cap_bytes:
            left_out.append(f"{name}: over the image cap - {shot.url}")
            continue
        count += len(shot.jpegs)
        size += need
        tiles = len(shot.jpegs)
        for part, jpeg in enumerate(shot.jpegs, 1):
            piece = f" (tile {part} of {tiles})" if tiles > 1 else ""
            blocks.append(
                mcp_types.TextContent(type="text", text=f"[{name}{piece}] {shot.url}")
            )
            blocks.append(
                mcp_types.ImageContent(
                    type="image",
                    data=base64.b64encode(jpeg).decode("ascii"),
                    mime_type="image/jpeg",
                )
            )
    attached = len(wanted) - len(left_out)
    note = f"Images: {attached} of {len(wanted)} page images attached below"
    note += f" ({count} image blocks, JPEG, long side up to {long_side} px)."
    if left_out:
        note += " Not attached:\n" + "\n".join(f"- {line}" for line in left_out)
    return blocks, note
