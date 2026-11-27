"""site-login: 1Password writes over stdin and the login wait. Fake data only."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from crawler import site_login, site_sessions
from crawler.site_sessions import SessionError, Site

FAKE = "fake-cookie-json-not-a-secret"


class FakeOp:
    """Stands in for ``op``: records argv and stdin, answers from a dict."""

    def __init__(self, items=None, item_json=None):
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.items = items or []
        self.item_json = item_json or {}

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs.get("input")))
        args = argv[1:]
        if args[:2] == ["item", "list"]:
            out = json.dumps(self.items)
        elif args[:2] == ["vault", "list"]:
            out = json.dumps([{"id": "v1", "name": "Sessions"}])
        elif args[:2] == ["item", "get"]:
            out = json.dumps(self.item_json)
        elif args[:2] == ["item", "create"]:
            out = json.dumps({"id": "newid"})
        else:
            out = "{}"
        return subprocess.CompletedProcess(argv, 0, out.encode(), b"")


def _no_secret_in_argv(fake: FakeOp) -> None:
    for argv, _ in fake.calls:
        assert all(FAKE not in a for a in argv)


def test_save_creates_a_concealed_field_over_stdin(monkeypatch):
    fake = FakeOp()
    monkeypatch.setattr(site_login.subprocess, "run", fake)
    ref = site_login.save("Sessions", "AliExpress session", "storage_state", FAKE, {"site": "aliexpress"})
    assert ref == "op://Sessions/AliExpress session/storage_state"
    argv, stdin = fake.calls[-1]
    assert argv[:3] == ["op", "item", "create"]
    template = json.loads(stdin)
    secret = [f for f in template["fields"] if f["label"] == "storage_state"]
    assert secret == [{"id": "storage_state", "label": "storage_state", "type": "CONCEALED", "value": FAKE}]
    _no_secret_in_argv(fake)


def test_save_updates_the_field_in_place(monkeypatch):
    existing = {
        "id": "abc",
        "title": "AliExpress session",
        "fields": [
            {"id": "notesPlain", "label": "notesPlain", "type": "STRING", "value": "keep me"},
            {"id": "storage_state", "label": "storage_state", "type": "CONCEALED", "value": "old"},
            {"id": "site", "label": "site", "type": "STRING", "value": "old"},
        ],
    }
    fake = FakeOp(items=[{"id": "abc", "title": "AliExpress session"}], item_json=existing)
    monkeypatch.setattr(site_login.subprocess, "run", fake)
    site_login.save("Sessions", "AliExpress session", "storage_state", FAKE, {"site": "aliexpress"})
    argv, stdin = fake.calls[-1]
    assert argv[:4] == ["op", "item", "edit", "abc"]
    fields = {f["label"]: f["value"] for f in json.loads(stdin)["fields"]}
    assert fields == {"notesPlain": "keep me", "storage_state": FAKE, "site": "aliexpress"}
    _no_secret_in_argv(fake)


def test_reference_uses_ids_for_names_op_cannot_parse(monkeypatch):
    fake = FakeOp()
    monkeypatch.setattr(site_login.subprocess, "run", fake)
    ref = site_login.save("Sessions", "AliExpress (main)", "storage_state", FAKE, {})
    assert ref == "op://Sessions/newid/storage_state"


class _LoginPage(BaseHTTPRequestHandler):
    """/login sets login cookies after a moment and moves to another host.

    /stay sets them and stays on the page. /away visits another origin first
    and comes back, then waits: a poll that read local storage would open a
    tab for that origin on every round.
    """

    def do_GET(self):
        port = self.server.server_address[1]
        if self.path == "/done":
            self.send_response(200)
            self.send_header("Set-Cookie", "_hvn_login=13; Max-Age=3600; Path=/")
            self.send_header("Set-Cookie", "other=1; Path=/")
            self.end_headers()
            return
        if self.path == "/home2":
            # The main domain's home page: the site carries the login over.
            self.send_response(200)
            self.send_header("Set-Cookie", "_hvn_login=13; Max-Age=3600; Path=/")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"home")
            return
        if self.path == "/sister":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<script>setTimeout(() => fetch('/done'), 300)</script>")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if self.path == "/stay":
            self.wfile.write(b"<script>setTimeout(() => fetch('/done'), 300)</script>")
            return
        if self.path == "/away":
            self.wfile.write(
                (
                    "<script>if (!sessionStorage.back) { sessionStorage.back = 1;"
                    f" location = 'http://localhost:{port}/back' }}"
                    " else setTimeout(() => fetch('/done'), 1500)</script>"
                ).encode()
            )
            return
        if self.path == "/back":
            self.wfile.write(
                f"<script>location = 'http://127.0.0.1:{port}/away'</script>".encode()
            )
            return
        if self.path == "/login":
            self.wfile.write(
                (
                    "<script>setTimeout(async () => { await fetch('/done');"
                    f" location = 'http://localhost:{port}/home' }}, 300)</script>"
                ).encode()
            )
        else:
            self.wfile.write(b"home")

    def log_message(self, *args):
        pass


@pytest.fixture
def login_site():
    server = HTTPServer(("127.0.0.1", 0), _LoginPage)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    yield Site(
        name="selftest",
        title="Selftest",
        login_url=f"http://127.0.0.1:{port}/login",
        domains=("127.0.0.1",),
        login_cookies=(("_hvn_login", ""),),
    )
    server.shutdown()


def test_capture_waits_for_the_login_cookie(login_site):
    cookies, how = asyncio.run(
        site_login.capture(login_site, headless=True, timeout_s=30, poll_s=0.2, settle_s=0.2)
    )
    assert how == "login seen"
    assert sorted(c["name"] for c in cookies) == ["_hvn_login", "other"]


def test_capture_does_not_need_to_leave_the_login_page(login_site):
    stay = login_site.login_url.replace("/login", "/stay")
    cookies, how = asyncio.run(
        site_login.capture(
            login_site, login_url=stay, headless=True, timeout_s=30, poll_s=0.2, settle_s=0.2
        )
    )
    assert how == "login seen"
    assert "_hvn_login" in {c["name"] for c in cookies}


def test_capture_polls_cookies_only(login_site, monkeypatch):
    """storage_state() opens a tab per visited origin: never call it while waiting."""
    from playwright.async_api import BrowserContext

    async def no_storage_state(self, *args, **kwargs):
        raise AssertionError("storage_state() opens tabs in the visible window")

    monkeypatch.setattr(BrowserContext, "storage_state", no_storage_state)
    away = login_site.login_url.replace("/login", "/away")
    cookies, how = asyncio.run(
        site_login.capture(
            login_site, login_url=away, headless=True, timeout_s=30, poll_s=0.2, settle_s=0.2
        )
    )
    assert how == "login seen"
    assert "_hvn_login" in {c["name"] for c in cookies}


def test_sister_domain_login_is_carried_to_the_main_domain(login_site):
    """A login that lands on a sister domain first is not enough on its own."""
    port = login_site.login_url.split(":")[2].split("/")[0]
    site = Site(
        **{
            **login_site.__dict__,
            "domains": ("127.0.0.1", "localhost"),
            "main_domain": "127.0.0.1",
            "home_url": f"http://127.0.0.1:{port}/home2",
        }
    )
    cookies, how = asyncio.run(
        site_login.capture(
            site,
            login_url=f"http://localhost:{port}/sister",
            headless=True,
            timeout_s=30,
            poll_s=0.2,
            settle_s=0.2,
            sync_s=0.5,
        )
    )
    assert how == "login seen"
    assert {(c["name"], c["domain"]) for c in cookies} >= {
        ("_hvn_login", "localhost"),
        ("_hvn_login", "127.0.0.1"),
    }


def test_capture_gives_up_without_a_login(login_site):
    never = Site(**{**login_site.__dict__, "login_cookies": (("nope", ""),)})
    with pytest.raises(SessionError, match="no login within"):
        asyncio.run(site_login.capture(never, headless=True, timeout_s=3, poll_s=0.2))


def test_no_terminal_needs_vault_and_item(monkeypatch, capsys):
    monkeypatch.setattr(site_login.sys.stdin, "isatty", lambda: False)
    assert site_login.main(["--site", "aliexpress"]) == 1
    assert "--vault and --item are needed" in capsys.readouterr().err
