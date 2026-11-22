"""crawl(images=true): page images as JPEG content blocks (offline)."""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from crawler import fetch_chain, mcp_server, page_images
from crawler.document import CrawledDocument


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, "PNG")
    return buffer.getvalue()


def _size(jpeg: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(jpeg)) as image:
        assert image.format == "JPEG"
        return image.size


def _doc(images: list, status: str = "success") -> CrawledDocument:
    return CrawledDocument(
        request_url="https://shop.example/item/1.html",
        final_url="https://shop.example/item/1.html",
        status=status,
        markdown="# item",
        metadata={"page_images": images},
    )


SOURCES = {
    "https://img.example/a.png": _png(2000, 2000),
    "https://img.example/b.png": _png(800, 600),
    "https://img.example/tall.png": _png(1000, 5000),
}


async def _fake_download(url, referer, client=None):
    assert referer == "https://shop.example/"
    return SOURCES.get(url)


def test_square_image_is_scaled_to_the_long_side():
    (jpeg,) = page_images.to_jpegs(_png(2000, 1500))
    assert _size(jpeg) == (1568, 1176)
    (small,) = page_images.to_jpegs(_png(800, 600))
    assert _size(small) == (800, 600)


def test_tall_banner_is_cut_into_tiles():
    tiles = page_images.to_jpegs(_png(1000, 5000))
    # 1500 px tiles (1.5 x the width) with a 40 px overlap: 0, 1460, 2920, 4380.
    assert [_size(t) for t in tiles] == [
        (1000, 1500),
        (1000, 1500),
        (1000, 1500),
        (1000, 620),
    ]


def test_wide_banner_is_cut_left_to_right():
    tiles = page_images.to_jpegs(_png(3000, 500))
    # 750 px tiles: 0, 710, 1420, 2130, 2840.
    assert len(tiles) == 5
    assert all(_size(t)[1] == 500 for t in tiles)


def test_page_images_reads_route_metadata():
    doc = _doc(["https://x/1.jpg", {"url": "https://x/2.jpg", "label": "two"}, {}])
    assert page_images.page_images(doc) == [
        {"url": "https://x/1.jpg", "label": "image 1"},
        {"url": "https://x/2.jpg", "label": "two"},
    ]


async def test_blocks_follow_page_order_with_labels():
    doc = _doc(
        [
            {"url": "https://img.example/a.png", "label": "description image 1"},
            {"url": "https://img.example/missing.png", "label": "description image 2"},
            {"url": "https://img.example/tall.png", "label": "description image 3"},
        ]
    )
    blocks, note = await page_images.image_blocks([doc], _fake_download)
    kinds = [b.type for b in blocks]
    # a: label + image; tall: 4 tiles, each label + image.
    assert kinds == ["text", "image"] * 5
    assert blocks[0].text == "[description image 1] https://img.example/a.png"
    assert blocks[2].text.startswith("[description image 3 (tile 1 of 4)]")
    assert blocks[1].mime_type == "image/jpeg"
    assert _size(base64.b64decode(blocks[1].data)) == (1568, 1568)
    assert note.startswith("Images: 2 of 3 page images attached below (5 image blocks")
    assert (
        "- description image 2: download failed - https://img.example/missing.png"
        in note
    )


async def test_block_cap_names_what_was_left_out(monkeypatch):
    monkeypatch.setenv("CRAWL_IMAGES_MAX", "2")
    doc = _doc(
        [
            {"url": "https://img.example/b.png", "label": "one"},
            {"url": "https://img.example/tall.png", "label": "two"},
            {"url": "https://img.example/a.png", "label": "three"},
        ]
    )
    blocks, note = await page_images.image_blocks([doc], _fake_download)
    # one (1 block) and three (1 block) fit; two needs 4 blocks.
    assert [b.text for b in blocks if b.type == "text"] == [
        "[one] https://img.example/b.png",
        "[three] https://img.example/a.png",
    ]
    assert "- two: over the image cap - https://img.example/tall.png" in note


async def test_failed_pages_and_pages_without_images_give_nothing():
    assert await page_images.image_blocks(
        [_doc([]), _doc(["https://x/1.jpg"], "failed")]
    ) == (
        [],
        "",
    )


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch):
    async def fetch_many(urls, **kwargs):
        return [
            _doc([{"url": "https://img.example/b.png", "label": "description image 1"}])
            for _ in urls
        ]

    monkeypatch.setattr(fetch_chain, "fetch_many", fetch_many)
    monkeypatch.setattr(page_images, "_download", _fake_download)


async def test_crawl_tool_returns_image_blocks_only_when_asked(served):
    from fastmcp import Client

    async with Client(mcp_server.mcp) as client:
        plain = await client.call_tool(
            "crawl", {"urls": ["https://shop.example/item/1.html"]}
        )
        assert [b.type for b in plain.content] == ["text"]
        assert "Images:" not in plain.content[0].text

        shown = await client.call_tool(
            "crawl", {"urls": ["https://shop.example/item/1.html"], "images": True}
        )
    assert [b.type for b in shown.content] == ["text", "text", "image"]
    assert shown.content[0].text.startswith(plain.content[0].text)
    assert "Images: 1 of 1 page images attached below" in shown.content[0].text
    assert shown.content[1].text == "[description image 1] https://img.example/b.png"
    assert shown.content[2].mime_type == "image/jpeg"


async def test_crawl_tool_images_on_a_page_without_any(monkeypatch):
    async def fetch_many(urls, **kwargs):
        return [_doc([]) for _ in urls]

    monkeypatch.setattr(fetch_chain, "fetch_many", fetch_many)
    result = await mcp_server.crawl(urls=["https://shop.example/"], images=True)
    assert len(result) == 1
    assert "Images: none" in result[0].text
