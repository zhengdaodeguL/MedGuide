"""Issue #6: protected APIs must require authentication in every mode.

In MEDGUIDE_MODE=test (and offline), requests without a valid session
cookie used to reach protected endpoints and even read other users'
sessions, while a request carrying an *invalid* cookie was rejected --
the check was exactly backwards. These tests pin the fixed behavior:
no credential -> 401, invalid credential -> 401, valid session -> 200,
another user's session -> 404.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from conftest import login_client

_ANON_CASES = [
    ("post", "/api/search", {"query": "布洛芬副作用", "intent": "drug"}),
    ("post", "/api/sessions", None),
    ("post", "/api/chat", {"message": "我咳嗽"}),
    ("post", "/api/query", {"text": "查询布洛芬库存"}),
    ("post", "/api/feedback", {"session_id": "missing", "message_id": "missing:turn:1", "rating": "up"}),
    ("get", "/api/metrics", None),
]


@pytest.mark.parametrize(("method", "path", "payload"), _ANON_CASES)
def test_anonymous_requests_are_rejected_in_test_mode(method: str, path: str, payload: dict | None) -> None:
    with TestClient(app, base_url="https://testserver") as client:
        response = client.request(method, path, json=payload)
    assert response.status_code == 401
    assert response.json()["detail"] == "请先登录"


@pytest.mark.parametrize("path", ["/api/search", "/api/sessions", "/api/metrics"])
def test_invalid_cookie_is_rejected_in_test_mode(path: str) -> None:
    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set("medguide_session", "invalidtoken123")
        if path == "/api/search":
            response = client.post(path, json={"query": "咳嗽", "intent": "disease"})
        elif path == "/api/sessions":
            response = client.post(path)
        else:
            response = client.get(path)
    assert response.status_code == 401
    assert response.json()["detail"] == "请先登录"


def test_logged_in_user_can_use_protected_apis_in_test_mode() -> None:
    with TestClient(app, base_url="https://testserver") as client:
        login_client(client, "anon_access_user")
        created = client.post("/api/sessions")
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        assert client.get(f"/api/sessions/{session_id}").status_code == 200
        answer = client.post(
            "/api/chat",
            json={"session_id": session_id, "message": "我咳嗽三天"},
        )
        assert answer.status_code == 200
        assert client.post(
            "/api/search", json={"query": "布洛芬副作用", "intent": "drug"}
        ).status_code == 200
        assert client.post("/api/query", json={"text": "查询布洛芬库存"}).status_code == 200
        metrics = client.get("/api/metrics")
        assert metrics.status_code == 200
        assert metrics.json()["requests"] >= 1


def test_anonymous_and_cross_user_session_reads_are_rejected() -> None:
    with TestClient(app, base_url="https://testserver") as owner:
        login_client(owner, "anon_owner_user")
        session_id = owner.post("/api/sessions").json()["session_id"]
    with TestClient(app, base_url="https://testserver") as other:
        login_client(other, "anon_other_user")
        assert other.get(f"/api/sessions/{session_id}").status_code == 404
    with TestClient(app, base_url="https://testserver") as anonymous:
        assert anonymous.get(f"/api/sessions/{session_id}").status_code == 401


def test_public_probes_stay_public_in_test_mode() -> None:
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/health").json()["status"] == "ok"
        assert client.get("/api/ready").status_code == 200
