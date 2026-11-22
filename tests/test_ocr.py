"""OCR helper: caps, skip reasons, and one real recognition (models are bundled)."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from crawler import ocr


def _run(coro):
    return asyncio.run(coro)


def _pool(n=2):
    return ThreadPoolExecutor(max_workers=n)


def test_read_images_cap_and_errors():
    async def download(url):
        if url.endswith("bad"):
            raise OSError("boom")
        if url.endswith("empty"):
            return None
        return url.encode()

    def recogniser(data):
        if data.endswith(b"broken"):
            raise ValueError("not an image")
        return [data.decode().upper()]

    result = _run(
        ocr.read_images(
            ["u1", "u2bad", "u3empty", "u4broken", "u5", "u6"],
            download,
            cap=5,
            budget=60,
            recogniser=recogniser,
            executor=_pool(),
        )
    )
    assert result.texts == {"u1": ["U1"], "u5": ["U5"]}
    assert result.skipped["u2bad"] == "download failed: OSError"
    assert result.skipped["u3empty"] == "download failed or image too large"
    assert result.skipped["u4broken"] == "OCR failed: ValueError"
    assert result.skipped["u6"] == "over the 5-image safety cap"
    # Results come back in page order, whatever order they finished in.
    assert list(result.skipped) == ["u2bad", "u3empty", "u4broken", "u6"]


def test_read_images_time_budget():
    async def download(url):
        return b"x"

    result = _run(
        ocr.read_images(
            ["a", "b"],
            download,
            cap=10,
            budget=0,
            recogniser=lambda d: ["t"],
            executor=_pool(),
        )
    )
    assert result.texts == {}
    assert result.skipped == {
        "a": "over the 0 s OCR time budget",
        "b": "over the 0 s OCR time budget",
    }


def test_downloads_run_at_once():
    """Each download waits until all three started: serial downloads would hang."""
    started: list[str] = []

    async def download(url):
        started.append(url)
        while len(started) < 3:
            await asyncio.sleep(0)
        return url.encode()

    async def go():
        return await asyncio.wait_for(
            ocr.read_images(
                ["a", "b", "c"],
                download,
                budget=60,
                recogniser=lambda d: [d.decode()],
                executor=_pool(),
            ),
            5,
        )

    result = _run(go())
    assert result.texts == {"a": ["a"], "b": ["b"], "c": ["c"]}


def test_recognitions_run_in_parallel():
    """Two recognitions meet at a barrier: one at a time would break it."""
    barrier = threading.Barrier(2, timeout=5)

    def recogniser(data):
        barrier.wait()
        return [data.decode()]

    async def download(url):
        return url.encode()

    result = _run(
        ocr.read_images(
            ["a", "b"], download, budget=60, recogniser=recogniser, executor=_pool(2)
        )
    )
    assert result.texts == {"a": ["a"], "b": ["b"]}


def test_budget_skips_the_tail_in_page_order():
    """Downloads finish last-first; recognition still starts in page order,
    so the image past the budget is the last one, not the first."""
    order: list[str] = []
    release = {u: asyncio.Event() for u in "abc"}

    async def download(url):
        # c arrives first, then b, then a.
        await release[url].wait()
        nxt = {"c": "b", "b": "a"}.get(url)
        if nxt:
            release[nxt].set()
        return url.encode()

    def recogniser(data):
        order.append(data.decode())
        if data == b"b":
            time.sleep(0.4)  # b runs past the 0.2 s budget; c never starts
        return [data.decode()]

    async def go():
        task = asyncio.ensure_future(
            ocr.read_images(
                ["a", "b", "c"],
                download,
                budget=0.2,
                recogniser=recogniser,
                executor=_pool(1),
            )
        )
        await asyncio.sleep(0)
        release["c"].set()
        return await task

    result = _run(go())
    assert order == ["a", "b"]
    # b was already running at the deadline, so it finishes and counts.
    assert result.texts == {"a": ["a"], "b": ["b"]}
    assert result.skipped == {"c": "over the 0.2 s OCR time budget"}


def test_env_caps(monkeypatch):
    monkeypatch.setenv("OCR_MAX_IMAGES", "3")
    monkeypatch.setenv("OCR_MAX_BYTES", "junk")
    assert ocr.max_images() == 3
    assert ocr.max_bytes() == ocr.DEFAULT_MAX_BYTES
    assert ocr.budget_seconds() == ocr.DEFAULT_BUDGET_S == 15


@pytest.mark.parametrize(
    "cpus, env, expected",
    [
        (16, {}, (4, 2)),  # 8 cores / 16 threads: 4 x 2 = the 8 cores
        (8, {}, (2, 2)),
        (4, {}, (1, 2)),
        (1, {}, (1, 1)),
        (32, {}, (4, 4)),
        (16, {"OCR_WORKERS": "2", "OCR_THREADS": "3"}, (2, 3)),
        (16, {"OCR_WORKERS": "0", "OCR_THREADS": "junk"}, (4, 2)),
    ],
)
def test_pool_size(monkeypatch, cpus, env, expected):
    monkeypatch.setattr(ocr.os, "cpu_count", lambda: cpus)
    for key in ("OCR_WORKERS", "OCR_THREADS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert (ocr.workers(), ocr.threads_per_worker()) == expected


@pytest.mark.skipif(not ocr.available(), reason="rapidocr-onnxruntime not installed")
def test_recognise_real_image():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    image = np.full((200, 900, 3), 255, np.uint8)
    cv2.putText(
        image,
        "Material: ABS plastic",
        (20, 110),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.6,
        (0, 0, 0),
        3,
    )
    ok, buf = cv2.imencode(".png", image)
    assert ok
    assert ocr.recognise(buf.tobytes()) == ["Material: ABS plastic"]


def test_recognise_rejects_non_image():
    if not ocr.available():
        pytest.skip("rapidocr-onnxruntime not installed")
    with pytest.raises(ValueError):
        ocr.recognise(b"not an image")


def test_rows_join_boxes_side_by_side():
    def box(x, y, w=80, h=20):
        return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]

    result = [
        [box(300, 12), "2.10 inch", 0.99],
        [box(10, 10), "Size:", 0.98],
        [box(10, 60), "Driver IC:", 0.97],
        [box(300, 58), "GC9B72", 0.96],
        [box(10, 110), "noise", 0.2],
        [box(10, 160), "十", 0.99],
        [box(10, 210), "+ .", 0.99],
        [box(10, 260), "100x", 0.99],
    ]
    assert ocr.rows(result) == ["Size: 2.10 inch", "Driver IC: GC9B72", "100x"]


def test_ocr_cache_round_trip(tmp_path):
    from crawler.fetch_cache import FetchCache

    cache = FetchCache(path=tmp_path / "c.sqlite", ttl=3600)
    assert cache.get_ocr("https://img/1.jpg") is None
    cache.put_ocr(
        "https://img/1.jpg", ["产品参数 Product parameters", "Size: 2.10 inch"]
    )
    assert cache.get_ocr("https://img/1.jpg") == [
        "产品参数 Product parameters",
        "Size: 2.10 inch",
    ]
    off = FetchCache(path=tmp_path / "d.sqlite", ttl=0)
    off.put_ocr("https://img/1.jpg", ["x"])
    assert off.get_ocr("https://img/1.jpg") is None


def test_clean_drops_stray_marks():
    assert ocr.clean(["十", "+", ".", "X", "10pcs", "产品参数", "NO Electricity"]) == [
        "10pcs",
        "产品参数",
        "NO Electricity",
    ]
