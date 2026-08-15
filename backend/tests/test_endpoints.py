"""HTTP-layer tests: auth middleware and endpoint contracts. These exercise
the real FastAPI app, not just the services.

The lifespan eagerly preloads Whisper, the LLM and TTS, so one module-scoped
TestClient keeps the suite fast. /events is a never-ending SSE stream, so its
positive path is covered at the middleware level against a stub app instead
of over the wire.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services.companion import CompanionUnavailable

TEST_TOKEN = "test-token-abc123"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setattr(settings, "companion_token", TEST_TOKEN)
    # The lifespan calls store.init() with whatever db_path says — before any
    # function-scoped fixture has run. Point it at a throwaway file so the
    # real backend/data/companion.db is never opened (or created) by tests.
    mp.setattr(settings, "db_path", str(tmp_path_factory.mktemp("db") / "test.db"))
    with TestClient(app) as c:
        yield c
    mp.undo()


@pytest.fixture
def authed_headers():
    return {"X-Companion-Token": TEST_TOKEN}


def _stub_app():
    """Minimal ASGI app so the token middleware can be exercised without
    touching the real (streaming) routes."""

    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def _middleware_client():
    from app.middleware import CompanionTokenMiddleware

    return TestClient(CompanionTokenMiddleware(_stub_app()))


def test_health_is_open(client):
    assert client.get("/health").status_code == 200


def test_cors_preflight_no_wildcards(client):
    """iOS 12 Safari rejects "*" in Access-Control-Allow-Methods/-Headers
    (wildcards landed in Safari 13.1), so every request carrying the token
    header would fail its preflight. Methods and headers must be explicit;
    the origin wildcard is fine and stays."""
    r = client.options(
        "/chat",
        headers={
            "Origin": "https://192.168.1.50:8443",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-companion-token,content-type",
        },
    )
    assert r.status_code == 200
    assert "x-companion-token" in r.headers["access-control-allow-headers"].lower()
    assert "content-type" in r.headers["access-control-allow-headers"].lower()
    assert "post" in r.headers["access-control-allow-methods"].lower()
    assert "*" not in r.headers["access-control-allow-headers"].lower()
    assert "*" not in r.headers["access-control-allow-methods"].lower()


def test_protected_route_requires_token(client):
    assert client.get("/reminders").status_code == 401


def test_protected_route_accepts_token(client, authed_headers):
    r = client.get("/reminders", headers=authed_headers)
    assert r.status_code == 200


def test_wrong_token_rejected(client):
    r = client.get("/reminders", headers={"X-Companion-Token": "wrong"})
    assert r.status_code == 401


def test_header_token_accepted():
    r = _middleware_client().get("/anything", headers={"X-Companion-Token": TEST_TOKEN})
    assert r.status_code == 200


def test_events_accepts_token_in_query():
    """/events is the documented exception: EventSource cannot set headers,
    so the token rides the query string on this one read-only endpoint."""
    r = _middleware_client().get(f"/events?token={TEST_TOKEN}")
    assert r.status_code == 200


def test_query_token_rejected_on_other_routes():
    """The query-token escape hatch exists for /events and nowhere else."""
    r = _middleware_client().get(f"/reminders?token={TEST_TOKEN}")
    assert r.status_code == 401


def test_events_rejects_without_token(client):
    assert client.get("/events").status_code == 401


def test_vision_503_when_ollama_unavailable(client, authed_headers, monkeypatch):
    """The vision contract test that would have caught the undefined
    one_line() NameError: the endpoint must translate a companion outage
    into a clean 503, never a 500."""
    from app import main

    async def boom(_image_base64):
        raise CompanionUnavailable("Ollama is unreachable")

    monkeypatch.setattr(main, "describe_image", boom)
    r = client.post(
        "/vision",
        files={"image": ("shot.png", b"fake-png-bytes", "image/png")},
        headers=authed_headers,
    )
    assert r.status_code == 503
    assert "Ollama" in r.json()["detail"]


def test_vision_200_happy_path(client, authed_headers, monkeypatch):
    from app import main

    async def describe(_image_base64):
        return "A cat on a sofa"

    monkeypatch.setattr(main, "describe_image", describe)
    r = client.post(
        "/vision",
        files={"image": ("shot.png", b"fake-png-bytes", "image/png")},
        headers=authed_headers,
    )
    assert r.status_code == 200
    assert r.json()["text"] == "A cat on a sofa"


def test_vision_rejects_empty_upload(client, authed_headers):
    r = client.post(
        "/vision",
        files={"image": ("shot.png", b"", "image/png")},
        headers=authed_headers,
    )
    assert r.status_code == 400


def test_facts_endpoints(client, authed_headers, fresh_store):
    from app.services.store import store

    store.add_fact("the user likes dark theme")
    r = client.get("/facts", headers=authed_headers)
    assert r.status_code == 200
    assert any(f["text"] == "the user likes dark theme" for f in r.json()["facts"])

    fact_id = r.json()["facts"][0]["id"]
    assert client.delete(f"/facts/{fact_id}", headers=authed_headers).status_code == 200
    assert client.get("/facts", headers=authed_headers).json()["facts"] == []


def test_conversation_endpoints(client, authed_headers, fresh_store):
    from app.services.store import store

    store.append_message("user", "hello")
    store.append_message("assistant", "hi there")
    r = client.get("/conversation", headers=authed_headers)
    assert r.status_code == 200
    assert len(r.json()["messages"]) == 2

    assert client.delete("/conversation", headers=authed_headers).status_code == 200
    assert client.get("/conversation", headers=authed_headers).json()["messages"] == []


# NOTE: the find_files PowerShell-injection case is deliberately NOT a test
# here — it needs a live recursive scan of the user's real folders, which is
# far too slow for a unit suite. The escaping fix was verified manually.