"""The "web" skill served over MCP (Skills extension, SEP-2640)."""

from __future__ import annotations

import hashlib

import pytest
from fastmcp import Client
from fastmcp.skills import SkillsClientExtension

from crawler import mcp_server

SKILL_URI = "skill://web/SKILL.md"
SKILL_FILE = mcp_server.SKILLS_DIR / "web" / "SKILL.md"
LONG_DASHES = ("\u2014", "\u2013")


def _client() -> Client:
    return Client(mcp_server.mcp, extensions=[SkillsClientExtension()])


async def test_skill_is_listed():
    async with _client() as client:
        skills = await client.list_skills()

    by_uri = {skill.uri: skill for skill in skills}
    assert SKILL_URI in by_uri
    skill = by_uri[SKILL_URI]
    assert skill.frontmatter.name == "web"
    assert "crawl" in skill.frontmatter.description


async def test_skill_get_and_read_match_the_file():
    raw = SKILL_FILE.read_bytes()
    async with _client() as client:
        skill = await client.get_skill(SKILL_URI)
        contents = await client.read_resource(SKILL_URI)

    manifest = {entry.uri: entry for entry in skill.resources}
    entry = manifest[SKILL_URI]
    assert entry.size == len(raw)
    assert entry.digest.removeprefix("sha256:") == hashlib.sha256(raw).hexdigest()
    assert contents[0].text.encode("utf-8") == raw


async def test_unknown_skill_is_rejected():
    from mcp.shared.exceptions import MCPError

    async with _client() as client:
        with pytest.raises(MCPError):
            await client.get_skill("skill://nope/SKILL.md")


def test_skill_covers_the_caller_rules():
    text = SKILL_FILE.read_text(encoding="utf-8")
    for must in (
        "reddit_posts",
        "reddit_thread",
        "action=raw",
        "pmc-oa-opendata",
        "fresh=true",
        "Blocked:",
        "images=true",
        "aliexpress_fetch_log",
        "suspected block",
    ):
        assert must in text, must


@pytest.mark.parametrize(
    "text",
    [SKILL_FILE.read_text(encoding="utf-8"), mcp_server.SERVER_INSTRUCTIONS],
    ids=["skill", "instructions"],
)
def test_no_long_dashes(text: str):
    assert not any(dash in text for dash in LONG_DASHES)


def test_instructions_name_every_tool_and_the_skill():
    text = mcp_server.SERVER_INSTRUCTIONS
    assert "You.com" not in text
    for name in (
        "crawl",
        "crawl_site",
        "search",
        "reddit_posts",
        "reddit_thread",
        "aliexpress_fetch_log",
        SKILL_URI,
    ):
        assert name in text, name


LOGIN_URI = "skill://login/SKILL.md"
LOGIN_FILE = mcp_server.SKILLS_DIR / "login" / "SKILL.md"


async def test_login_skill_is_listed():
    async with _client() as client:
        skills = await client.list_skills()
    by_uri = {skill.uri: skill for skill in skills}
    assert LOGIN_URI in by_uri
    assert by_uri[LOGIN_URI].frontmatter.name == "login"


def test_login_skill_keeps_the_secret_away_from_the_model():
    text = LOGIN_FILE.read_text(encoding="utf-8")
    assert not any(dash in text for dash in LONG_DASHES)
    for must in ("AskUserQuestion", "Never read the session", "site_sessions", "--accept-unconfirmed"):
        assert must in text, must
