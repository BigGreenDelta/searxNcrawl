"""Bearer-token gate for the HTTP transport."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from crawler.mcp_server import BearerTokenMiddleware


def _client() -> TestClient:
    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/mcp", ok, methods=["GET", "POST"]), Route("/healthz", ok)],
        middleware=[Middleware(BearerTokenMiddleware, token="s3cret")],
    )
    return TestClient(app)


def test_missing_or_wrong_token_is_rejected():
    c = _client()
    assert c.post("/mcp").status_code == 401
    assert c.post("/mcp", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_right_token_passes():
    c = _client()
    assert c.post("/mcp", headers={"Authorization": "Bearer s3cret"}).text == "ok"


def test_healthz_needs_no_token():
    assert _client().get("/healthz").status_code == 200
