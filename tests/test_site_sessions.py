"""In-memory site sessions: filtering, validation, sources. Fake cookies only."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from crawler import site_sessions
from crawler.site_sessions import SessionError

ALI = site_sessions.SITES["aliexpress"]
FAKE = "fake-not-a-secret"


def _state(*cookies: dict) -> dict:
    return {"cookies": list(cookies), "origins": [{"origin": "x", "localStorage": []}]}


def _c(name: str, domain: str, value: str = FAKE, expires: float = -1) -> dict:
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": expires}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("SITE_SESSIONS", raising=False)
    site_sessions._PUSHED.clear()
    yield
    site_sessions._PUSHED.clear()


def test_only_site_domains_are_kept():
    state = _state(
        _c("a", ".aliexpress.com"),
        _c("b", "www.aliexpress.com"),
        _c("c", ".aliexpress.us"),
        _c("d", ".google.com"),
        _c("e", "evilaliexpress.com"),
        _c("f", "aliexpress.com.evil.example"),
    )
    kept = site_sessions.site_cookies(state, ALI)
    assert [c["name"] for c in kept] == ["a", "b", "c"]


def test_bad_input_never_echoes_the_value():
    with pytest.raises(SessionError) as err:
        site_sessions.site_cookies({"cookies": [{"name": "x", "value": 5, "domain": ".aliexpress.com"}]}, ALI)
    assert FAKE not in str(err.value)
    with pytest.raises(SessionError):
        site_sessions.site_cookies([FAKE], ALI)
    with pytest.raises(SessionError, match="no AliExpress cookies"):
        site_sessions.site_cookies(_state(_c("a", ".google.com")), ALI)


def test_login_cookie_detection():
    future = time.time() + 3600
    assert site_sessions.has_login_cookie([_c("_hvn_login", ".aliexpress.com", "13", future)], ALI)
    assert site_sessions.has_login_cookie(
        [_c("xman_us_t", ".aliexpress.com", "x_lid=uk1&sign=0", future)], ALI
    )
    assert not site_sessions.has_login_cookie(
        [_c("xman_us_t", ".aliexpress.com", "sign=0", future)], ALI
    )
    assert not site_sessions.has_login_cookie(
        [_c("_hvn_login", ".aliexpress.com", "13", time.time() - 10)], ALI
    )


def test_push_keeps_cookies_in_memory_and_reports_no_values():
    body = json.dumps(_state(_c("_hvn_login", ".aliexpress.com", FAKE, 2_000_000_000), _c("z", ".x.com")))
    info = site_sessions.push("aliexpress", body.encode())
    assert info == {"site": "aliexpress", "cookies": 1, "login_cookie": True, "expires": "2033-05-18"}
    assert FAKE not in json.dumps(site_sessions.status())
    session, why = site_sessions.get("aliexpress")
    assert why is None and session.source == "pushed"
    assert session.cookies[0]["value"] == FAKE
    assert FAKE not in repr(session)
    assert site_sessions.forget("aliexpress")
    session, why = site_sessions.get("aliexpress")
    assert session is None and "no AliExpress session" in why


def test_push_rejects_junk():
    with pytest.raises(SessionError, match="not valid JSON"):
        site_sessions.push("aliexpress", b"{nope")
    with pytest.raises(SessionError, match="too large"):
        site_sessions.push("aliexpress", b" " * (site_sessions.MAX_STATE_BYTES + 1))
    with pytest.raises(SessionError, match="unknown site"):
        site_sessions.push("example", b"{}")


def test_configured_refs():
    refs = site_sessions.configured_refs("aliexpress=op://v/i/f, nope=op://a/b/c, x")
    assert refs == {"aliexpress": "op://v/i/f"}


def test_op_reference_is_read_into_memory(monkeypatch):
    monkeypatch.setenv("SITE_SESSIONS", "aliexpress=op://vault/item/storage_state")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        body = json.dumps(_state(_c("_hvn_login", ".aliexpress.com")))
        return subprocess.CompletedProcess(argv, 0, body.encode(), b"")

    monkeypatch.setattr(site_sessions.subprocess, "run", fake_run)
    session, why = site_sessions.get("aliexpress")
    assert why is None and session.source == "1password"
    argv, kwargs = calls[0]
    assert argv == ["op", "read", "--no-newline", "op://vault/item/storage_state"]
    assert kwargs.get("capture_output") is True


def test_failed_op_read_falls_back_to_a_pushed_session(monkeypatch):
    monkeypatch.setenv("SITE_SESSIONS", "aliexpress=op://vault/item/storage_state")

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, b"", b"[ERROR] not signed in\n")

    monkeypatch.setattr(site_sessions.subprocess, "run", fake_run)
    session, why = site_sessions.get("aliexpress")
    assert session is None and "not signed in" in why
    site_sessions.push("aliexpress", json.dumps(_state(_c("a", ".aliexpress.com"))).encode())
    session, why = site_sessions.get("aliexpress")
    assert session.source == "pushed"
