"""In-memory site sessions: filtering, validation, the op source. Fake cookies only."""

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


FAKE_TOKEN = "ops_fake-token-not-a-secret"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("SITE_SESSIONS", raising=False)
    monkeypatch.delenv("SITE_SESSIONS_OP_CREDENTIAL", raising=False)
    monkeypatch.delenv(site_sessions.TOKEN_ENV, raising=False)


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


def test_configured_refs():
    refs = site_sessions.configured_refs("aliexpress=op://v/i/f, nope=op://a/b/c, x")
    assert refs == {"aliexpress": "op://v/i/f"}


def _fake_op(monkeypatch, body: bytes = b"", code: int = 0, err: bytes = b""):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, code, body, err)

    monkeypatch.setattr(site_sessions.subprocess, "run", fake_run)
    return calls


def test_no_reference_means_no_session(monkeypatch):
    calls = _fake_op(monkeypatch)
    session, why = site_sessions.get("aliexpress")
    assert session is None and why == "no AliExpress session configured on this server"
    assert calls == []


def test_op_reference_is_read_into_memory(monkeypatch):
    monkeypatch.setenv("SITE_SESSIONS", "aliexpress=op://vault/item/storage_state")
    body = json.dumps(_state(_c("_hvn_login", ".aliexpress.com"), _c("z", ".x.com")))
    calls = _fake_op(monkeypatch, body.encode())
    monkeypatch.setattr(site_sessions, "_cred_read", lambda target: None)
    session, why = site_sessions.get("aliexpress")
    assert why is None and [c["name"] for c in session.cookies] == ["_hvn_login"]
    assert FAKE not in repr(session)
    argv, kwargs = calls[0]
    assert argv == ["op", "read", "--no-newline", "op://vault/item/storage_state"]
    assert kwargs.get("capture_output") is True
    assert kwargs.get("stdin") is subprocess.DEVNULL


def test_windows_token_goes_to_the_op_child_only(monkeypatch):
    monkeypatch.setenv("SITE_SESSIONS", "aliexpress=op://vault/item/storage_state")
    asked = []

    def cred_read(target):
        asked.append(target)
        return FAKE_TOKEN

    monkeypatch.setattr(site_sessions, "_cred_read", cred_read)
    body = json.dumps(_state(_c("_hvn_login", ".aliexpress.com")))
    calls = _fake_op(monkeypatch, body.encode())
    session, why = site_sessions.get("aliexpress")
    assert why is None
    assert asked == ["web-mcp/1password-service-account"]
    assert calls[0][1]["env"][site_sessions.TOKEN_ENV] == FAKE_TOKEN
    # Never in this process's own environment, so browsers cannot inherit it.
    assert site_sessions.TOKEN_ENV not in site_sessions.os.environ


def test_credential_name_can_be_changed(monkeypatch):
    monkeypatch.setenv("SITE_SESSIONS_OP_CREDENTIAL", "other/cred")
    asked = []
    monkeypatch.setattr(site_sessions, "_cred_read", lambda t: asked.append(t) or None)
    env = site_sessions.op_env()
    assert asked == ["other/cred"] and site_sessions.TOKEN_ENV not in env


def test_a_token_already_in_the_environment_wins(monkeypatch):
    monkeypatch.setenv(site_sessions.TOKEN_ENV, FAKE_TOKEN)

    def cred_read(target):
        raise AssertionError("Credential Manager must not be read")

    monkeypatch.setattr(site_sessions, "_cred_read", cred_read)
    assert site_sessions.op_env()[site_sessions.TOKEN_ENV] == FAKE_TOKEN
    assert site_sessions.token_source() == site_sessions.TOKEN_ENV


def test_credential_manager_is_windows_only():
    if site_sessions.os.name != "nt":
        assert site_sessions._cred_read("web-mcp/1password-service-account") is None


def test_failed_op_read_says_why_without_values(monkeypatch):
    monkeypatch.setenv("SITE_SESSIONS", "aliexpress=op://vault/item/storage_state")
    monkeypatch.setattr(site_sessions, "_cred_read", lambda target: FAKE_TOKEN)
    _fake_op(monkeypatch, code=1, err=b"[ERROR] not signed in\n")
    session, why = site_sessions.get("aliexpress")
    assert session is None and "not signed in" in why
    assert FAKE_TOKEN not in why


def test_credential_read_error_is_reported(monkeypatch):
    monkeypatch.setenv("SITE_SESSIONS", "aliexpress=op://vault/item/storage_state")

    def cred_read(target):
        raise SessionError(f"cannot read the Windows credential {target} (error 1312)")

    monkeypatch.setattr(site_sessions, "_cred_read", cred_read)
    calls = _fake_op(monkeypatch)
    session, why = site_sessions.get("aliexpress")
    assert session is None and "error 1312" in why
    assert calls == []


@pytest.mark.parametrize(
    "body, message",
    [
        (b"{nope", "does not hold a storage_state JSON"),
        (b'{"cookies": [{"name": "a", "value": "x", "domain": ".google.com"}]}', "no AliExpress cookies"),
    ],
)
def test_bad_item_contents(monkeypatch, body, message):
    monkeypatch.setenv("SITE_SESSIONS", "aliexpress=op://vault/item/storage_state")
    monkeypatch.setattr(site_sessions, "_cred_read", lambda target: None)
    _fake_op(monkeypatch, body)
    session, why = site_sessions.get("aliexpress")
    assert session is None and message in why


def test_status_holds_no_secret(monkeypatch):
    monkeypatch.setenv("SITE_SESSIONS", "aliexpress=op://vault/item/storage_state")
    monkeypatch.setenv(site_sessions.TOKEN_ENV, FAKE_TOKEN)
    rows = site_sessions.status()
    assert rows == [
        {
            "site": "aliexpress",
            "op_ref": "op://vault/item/storage_state",
            "op_sign_in": site_sessions.TOKEN_ENV,
        }
    ]
    assert FAKE_TOKEN not in json.dumps(rows)
