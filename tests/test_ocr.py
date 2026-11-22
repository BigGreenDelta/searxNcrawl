"""OCR helper: caps, skip reasons, and one real recognition (models are bundled)."""

from __future__ import annotations

import asyncio

import pytest

from crawler import ocr


def _run(coro):
    return asyncio.run(coro)


def test_read_images_limit_and_errors():
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
            limit=5,
            budget=60,
            recogniser=recogniser,
        )
    )
    assert result.texts == {"u1": ["U1"], "u5": ["U5"]}
    assert result.skipped["u2bad"] == "download failed: OSError"
    assert result.skipped["u3empty"] == "download failed or image too large"
    assert result.skipped["u4broken"] == "OCR failed: ValueError"
    assert result.skipped["u6"] == "over the 5-image limit"


def test_read_images_time_budget():
    async def download(url):
        return b"x"

    result = _run(
        ocr.read_images(
            ["a", "b"], download, limit=10, budget=-1, recogniser=lambda d: ["t"]
        )
    )
    assert result.texts == {}
    assert result.skipped == {
        "a": "over the -1 s OCR time budget",
        "b": "over the -1 s OCR time budget",
    }


def test_env_caps(monkeypatch):
    monkeypatch.setenv("OCR_MAX_IMAGES", "3")
    monkeypatch.setenv("OCR_MAX_BYTES", "junk")
    assert ocr.max_images() == 3
    assert ocr.max_bytes() == ocr.DEFAULT_MAX_BYTES


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
    ]
    assert ocr.rows(result) == ["Size: 2.10 inch", "Driver IC: GC9B72"]


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
