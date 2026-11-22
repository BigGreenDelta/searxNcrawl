"""Offline text recognition for images that carry text (product descriptions).

Uses RapidOCR (PaddleOCR models on onnxruntime). The models ship inside the
``rapidocr-onnxruntime`` wheel, so nothing is downloaded at run time. The
engine is loaded once, on first use, and recognition runs in a worker thread
so the server's event loop keeps serving other requests.

Environment:
    OCR_MAX_IMAGES: images read per page (default 12)
    OCR_MAX_BYTES: largest image downloaded, in bytes (default 4 MB)
    OCR_BUDGET_S: seconds of recognition per page; images past it are
        listed as skipped (default 40)
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_IMAGES = 12
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_BUDGET_S = 40.0
# Lines the recogniser is less sure of than this are dropped as noise.
MIN_SCORE = 0.6
# Long side of the image handed to the recogniser; larger images are scaled
# down (very tall description banners are cut into slices of this height).
_MAX_SIDE = 1600

_engine: Any = None
_engine_lock = threading.Lock()
_run_lock = threading.Lock()


def _env_number(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return max(0.0, float(raw)) if raw else default
    except ValueError:
        return default


def max_images() -> int:
    return int(_env_number("OCR_MAX_IMAGES", DEFAULT_MAX_IMAGES))


def max_bytes() -> int:
    return int(_env_number("OCR_MAX_BYTES", DEFAULT_MAX_BYTES))


def budget_seconds() -> float:
    return _env_number("OCR_BUDGET_S", DEFAULT_BUDGET_S)


def available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def _get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR

            _engine = RapidOCR()
        return _engine


def _slices(image):
    """The image scaled to at most _MAX_SIDE wide, cut into tall-side slices."""
    import cv2

    height, width = image.shape[:2]
    if width > _MAX_SIDE:
        scale = _MAX_SIDE / width
        image = cv2.resize(image, (_MAX_SIDE, max(1, int(height * scale))))
        height, width = image.shape[:2]
    step = _MAX_SIDE
    # A little overlap so a line on a cut is whole in one of the two slices.
    overlap = 60
    top = 0
    while top < height:
        yield image[top : min(height, top + step)]
        if top + step >= height:
            break
        top += step - overlap


def recognise(data: bytes) -> list[str]:
    """The text lines in one image, top to bottom. Blocking: call in a thread."""
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("not a readable image")
    engine = _get_engine()
    lines: list[str] = []
    # One recognition at a time: the engine is shared by all threads.
    with _run_lock:
        for part in _slices(image):
            result, _ = engine(part)
            for _box, text, score in result or []:
                text = str(text).strip()
                if not text or float(score) < MIN_SCORE:
                    continue
                # The slice overlap can read the line on a cut twice.
                if text in lines[-4:]:
                    continue
                lines.append(text)
    return lines


@dataclass
class OcrResult:
    """Text read from the images of one page, by image URL."""

    texts: dict[str, list[str]] = field(default_factory=dict)
    # URL -> why the image was not read (too large, over budget, error, ...).
    skipped: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0


Downloader = Callable[[str], Awaitable[Optional[bytes]]]


async def read_images(
    urls: list[str],
    download: Downloader,
    *,
    limit: Optional[int] = None,
    budget: Optional[float] = None,
    recogniser: Callable[[bytes], list[str]] = recognise,
) -> OcrResult:
    """OCR ``urls`` in order, within the image count and time caps."""
    limit = max_images() if limit is None else limit
    budget = budget_seconds() if budget is None else budget
    result = OcrResult()
    start = time.monotonic()
    for index, url in enumerate(urls):
        if index >= limit:
            result.skipped[url] = f"over the {limit}-image limit"
            continue
        if time.monotonic() - start > budget:
            result.skipped[url] = f"over the {budget:.0f} s OCR time budget"
            continue
        try:
            data = await download(url)
        except Exception as exc:  # noqa: BLE001 - one bad image is not the page
            result.skipped[url] = f"download failed: {type(exc).__name__}"
            continue
        if not data:
            result.skipped[url] = "download failed or image too large"
            continue
        try:
            result.texts[url] = await asyncio.to_thread(recogniser, data)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("OCR failed for %s: %s", url, exc)
            result.skipped[url] = f"OCR failed: {type(exc).__name__}"
    result.seconds = time.monotonic() - start
    return result
