"""Offline text recognition for images that carry text (product descriptions).

Uses RapidOCR (PaddleOCR models on onnxruntime). The models ship inside the
``rapidocr-onnxruntime`` wheel, so nothing is downloaded at run time.

All images of a page are downloaded at once. Recognition runs on a small pool
of worker threads shared by every crawl; each worker loads its own engine on
first use. onnxruntime gives each engine a few threads of its own, and
workers x threads stays at about the number of physical cores, so parallel
pages do not oversubscribe the CPU. Images start recognition in page order,
so when the time budget runs out the skipped images are the last ones.

Environment:
    OCR_BUDGET_S: wall-clock seconds per page for downloading and reading
        images; images not started by then are listed as skipped (default 15)
    OCR_MAX_IMAGES: safety cap on images read per page (default 60)
    OCR_MAX_BYTES: largest image downloaded, in bytes (default 4 MB)
    OCR_WORKERS: recognition threads (default: logical CPUs / 4, 1 to 4)
    OCR_THREADS: onnxruntime threads per worker (default: physical cores
        (logical / 2) divided by the workers, at least 1)
    OCR_DOWNLOADS: image downloads at once per page (default 8)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_IMAGES = 60
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_BUDGET_S = 15.0
DEFAULT_DOWNLOADS = 8
# Lines the recogniser is less sure of than this are dropped as noise.
MIN_SCORE = 0.6
# Long side of the image handed to the recogniser; larger images are scaled
# down (very tall description banners are cut into slices of this height).
_MAX_SIDE = 1600

_WORD = re.compile(r"\w")
# One engine per worker thread: a thread never shares its engine.
_local = threading.local()
_pool: Optional[ThreadPoolExecutor] = None
_pool_lock = threading.Lock()


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


def downloads() -> int:
    return max(1, int(_env_number("OCR_DOWNLOADS", DEFAULT_DOWNLOADS)))


def _cpus() -> int:
    return os.cpu_count() or 1


def workers() -> int:
    """Recognition threads: OCR_WORKERS, else a quarter of the logical CPUs."""
    found = int(_env_number("OCR_WORKERS", 0))
    return found if found >= 1 else max(1, min(4, _cpus() // 4))


def threads_per_worker() -> int:
    """onnxruntime threads per engine: OCR_THREADS, else cores / workers.

    os.cpu_count() counts logical CPUs; onnxruntime works best on physical
    cores, so half of them are shared out among the workers.
    """
    found = int(_env_number("OCR_THREADS", 0))
    if found >= 1:
        return min(found, _cpus())
    return max(1, (_cpus() // 2) // workers())


def available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def _get_engine():
    """This thread's engine, loaded on first use."""
    engine = getattr(_local, "engine", None)
    if engine is None:
        from rapidocr_onnxruntime import RapidOCR

        engine = RapidOCR(
            intra_op_num_threads=threads_per_worker(), inter_op_num_threads=1
        )
        _local.engine = engine
    return engine


def _get_pool() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=workers(), thread_name_prefix="ocr")
            LOGGER.info(
                "OCR pool: %d workers x %d onnxruntime threads",
                workers(),
                threads_per_worker(),
            )
        return _pool


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
    for part in _slices(image):
        result, _ = engine(part)
        for text in rows(result or []):
            # The slice overlap can read the line on a cut twice.
            if text in lines[-4:]:
                continue
            lines.append(text)
    return lines


def rows(result: list) -> list[str]:
    """RapidOCR boxes as text rows: boxes side by side join, left to right.

    Spec sheets in images are tables ("Size:" ... "2.10 inch"); the
    recogniser returns each cell on its own, which reads as a jumble.
    """
    boxes = []
    for box, text, score in result:
        text = str(text).strip()
        if not text or float(score) < MIN_SCORE:
            continue
        ys = [float(p[1]) for p in box]
        xs = [float(p[0]) for p in box]
        boxes.append((min(ys), max(ys), min(xs), text))
    boxes.sort(key=lambda b: (b[0] + b[1]) / 2)
    out: list[list[tuple]] = []
    for box in boxes:
        top, bottom = box[0], box[1]
        centre = (top + bottom) / 2
        if out:
            row = out[-1]
            r_top = sum(b[0] for b in row) / len(row)
            r_bottom = sum(b[1] for b in row) / len(row)
            # Same row: the centre lies inside the row's band.
            if r_top <= centre <= r_bottom:
                row.append(box)
                continue
        out.append([box])
    lines = [" ".join(b[3] for b in sorted(row, key=lambda b: b[2])) for row in out]
    return clean(lines)


def clean(lines: list[str]) -> list[str]:
    """Drop stray marks that product photos give ("+", ".", one CJK character).

    A line needs two letters or digits to count as text.
    """
    return [line for line in lines if len(_WORD.findall(line)) >= 2]


@dataclass
class OcrResult:
    """Text read from the images of one page, by image URL, in page order."""

    texts: dict[str, list[str]] = field(default_factory=dict)
    # URL -> why the image was not read (too large, over budget, error, ...).
    skipped: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0


Downloader = Callable[[str], Awaitable[Optional[bytes]]]


class _OverBudget(Exception):
    pass


async def read_images(
    urls: list[str],
    download: Downloader,
    *,
    cap: Optional[int] = None,
    budget: Optional[float] = None,
    recogniser: Callable[[bytes], list[str]] = recognise,
    executor: Optional[ThreadPoolExecutor] = None,
) -> OcrResult:
    """OCR ``urls`` within a wall-clock budget and a safety cap.

    Every image downloads at once (up to OCR_DOWNLOADS); recognition runs on
    the shared worker pool. Images are handed to the pool in page order, and
    an image whose recognition has not started by the deadline is skipped,
    so the skipped ones are the tail of the page. Recognitions already
    running at the deadline finish.
    """
    cap = max_images() if cap is None else cap
    budget = budget_seconds() if budget is None else budget
    start = time.monotonic()
    deadline = start + budget
    over = f"over the {budget:g} s OCR time budget"
    loop = asyncio.get_running_loop()
    pool = executor or _get_pool()
    gate = asyncio.Semaphore(downloads())
    batch = urls[:cap]
    # handed[i] is set once image i went to the pool (or was dropped), so
    # image i + 1 waits its turn and the pool queue keeps the page order.
    handed = [asyncio.Event() for _ in batch]
    texts: dict[str, list[str]] = {}
    skipped: dict[str, str] = {}

    def job(data: bytes) -> list[str]:
        if time.monotonic() > deadline:
            raise _OverBudget
        return recogniser(data)

    async def fetch(url: str) -> Optional[bytes]:
        left = deadline - time.monotonic()
        if left <= 0:
            raise _OverBudget
        async with gate:
            try:
                return await asyncio.wait_for(download(url), left)
            except asyncio.TimeoutError:
                raise _OverBudget from None

    async def one(index: int, url: str) -> None:
        work = None
        try:
            try:
                data = await fetch(url)
            except _OverBudget:
                skipped[url] = over
                return
            except Exception as exc:  # noqa: BLE001 - one bad image is not the page
                skipped[url] = f"download failed: {type(exc).__name__}"
                return
            if not data:
                skipped[url] = "download failed or image too large"
                return
            if index:
                await handed[index - 1].wait()
            work = loop.run_in_executor(pool, job, data)
        finally:
            handed[index].set()
        try:
            texts[url] = await work
        except _OverBudget:
            skipped[url] = over
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("OCR failed for %s: %s", url, exc)
            skipped[url] = f"OCR failed: {type(exc).__name__}"

    await asyncio.gather(*(one(i, u) for i, u in enumerate(batch)))
    for url in urls[cap:]:
        skipped[url] = f"over the {cap}-image safety cap"
    result = OcrResult(seconds=time.monotonic() - start)
    result.texts = {u: texts[u] for u in urls if u in texts}
    result.skipped = {u: skipped[u] for u in urls if u in skipped}
    return result
