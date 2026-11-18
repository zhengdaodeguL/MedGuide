"""Issue #21: feedback comments must be stored and readable.

On main, a submitted comment was only ever used as a boolean: the text
was discarded, a same-rating resubmission carrying a new comment came
back `changed:false`, and no endpoint could read the text back. The fix
stores the latest comment per message (in-memory and Redis) and exposes
it through GET /api/feedback/comments with the same authentication as
POST /api/feedback.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.auth import AuthStore
from app.main import app
from app.metrics import MetricsStore
from app.store import SessionStore
from conftest import login_client

PASSWORD = "Correct-Horse-42"

# Issue #6: the feedback endpoints require an authenticated session, so
# tests that seed a session directly must log in as its owner first.
COMMENT_USER = "comment_user"


def _login(client) -> None:
    assert client.post(
        "/api/auth/register", json={"username": COMMENT_USER, "password": PASSWORD}
    ).status_code == 201


def _services(tmp_path):
    """Isolated offline services: fresh auth/store/metrics per test."""
    return SimpleNamespace(
        production=False,
        mode="offline",
        local=False,
        api_tokens=(),
        production_missing=(),
        auth=AuthStore(
            tmp_path / "auth.sqlite3",
            session_ttl_seconds=300,
            cookie_secure=False,
            password_iterations=100_000,
        ),
        store=SessionStore(mode="offline"),
        metrics=MetricsStore(),
    )


@pytest.fixture
def services(tmp_path, monkeypatch):
    current = _services(tmp_path)
    monkeypatch.setattr(main_module, "get_services", lambda: current)
    return current


def _client() -> TestClient:
    return TestClient(app, base_url="https://testserver")


def _seed_session(services, owner_id=None, response_ids=("resp-1", "resp-2")):
    state = services.store.create(owner_id=owner_id)
    state["response_ids"] = list(response_ids)
    return services.store.save(state)


def _post_feedback(client, session_id, message_id, rating, comment=None):
    payload = {"session_id": session_id, "message_id": message_id, "rating": rating}
    if comment is not None:
        payload["comment"] = comment
    return client.post("/api/feedback", json=payload)


def test_submit_comment_then_read_back(services) -> None:
    state = _seed_session(services, owner_id=f"user:{COMMENT_USER}")
    session_id = state["session_id"]
    with _client() as client:
        _login(client)
        posted = _post_feedback(client, session_id, "resp-1", "up", "回答很清楚，谢谢")
        assert posted.status_code == 200
        assert posted.json()["changed"] is True

        listed = client.get("/api/feedback/comments")
        assert listed.status_code == 200
        body = listed.json()
        assert body["count"] == 1
        assert body["comments"] == [
            {
                "session_id": session_id,
                "message_id": "resp-1",
                "rating": "up",
                "comment": "回答很清楚，谢谢",
                "seq": 1,
            }
        ]
        # The metrics snapshot keeps counting; the text lives only in the
        # comments endpoint, never in the anonymous /api/metrics snapshot.
        assert services.metrics.snapshot()["feedback"].get("commented") == 1
        assert "回答很清楚" not in str(services.metrics.snapshot())


def test_same_rating_new_comment_is_change_and_updates_text(services) -> None:
    state = _seed_session(services, owner_id=f"user:{COMMENT_USER}")
    session_id = state["session_id"]
    with _client() as client:
        _login(client)
        assert _post_feedback(client, session_id, "resp-1", "up", "第一版评价").json()["changed"] is True
        second = _post_feedback(client, session_id, "resp-1", "up", "补充一点：希望更简短")
        assert second.status_code == 200
        assert second.json()["changed"] is True

        listed = client.get("/api/feedback/comments").json()
        assert listed["count"] == 1
        assert listed["comments"][0]["comment"] == "补充一点：希望更简短"
        # One message ever carried a comment: still counted once.
        assert services.metrics.snapshot()["feedback"].get("commented") == 1


def test_same_rating_same_comment_is_not_change(services) -> None:
    state = _seed_session(services, owner_id=f"user:{COMMENT_USER}")
    session_id = state["session_id"]
    with _client() as client:
        _login(client)
        assert _post_feedback(client, session_id, "resp-1", "up", "很好").json()["changed"] is True
        again = _post_feedback(client, session_id, "resp-1", "up", "很好")
        assert again.json()["changed"] is False
        assert client.get("/api/feedback/comments").json()["count"] == 1


def test_rating_change_keeps_comment(services) -> None:
    state = _seed_session(services, owner_id=f"user:{COMMENT_USER}")
    session_id = state["session_id"]
    with _client() as client:
        _login(client)
        _post_feedback(client, session_id, "resp-1", "up", "一开始觉得有用")
        changed = _post_feedback(client, session_id, "resp-1", "down")
        assert changed.json()["changed"] is True
        listed = client.get("/api/feedback/comments").json()
        assert listed["count"] == 1
        assert listed["comments"][0]["comment"] == "一开始觉得有用"
        assert listed["comments"][0]["rating"] == "down"
        # The message once carried a comment: the count stays.
        assert services.metrics.snapshot()["feedback"].get("commented") == 1


def test_blank_comment_is_not_stored_or_counted(services) -> None:
    state = _seed_session(services, owner_id=f"user:{COMMENT_USER}")
    session_id = state["session_id"]
    with _client() as client:
        _login(client)
        for blank in ("", "   ", "\n\t "):
            response = _post_feedback(client, session_id, "resp-1", "up", blank)
            assert response.status_code == 200
        assert client.get("/api/feedback/comments").json() == {"comments": [], "count": 0}
        assert "commented" not in services.metrics.snapshot()["feedback"]

        # A blank resubmission must not clear a stored comment either.
        _post_feedback(client, session_id, "resp-2", "down", "确实有问题")
        assert _post_feedback(client, session_id, "resp-2", "down", "   ").json()["changed"] is False
        listed = client.get("/api/feedback/comments").json()
        assert listed["count"] == 1
        assert listed["comments"][0]["comment"] == "确实有问题"


def test_comment_over_500_chars_is_rejected(services) -> None:
    state = _seed_session(services, owner_id=f"user:{COMMENT_USER}")
    with _client() as client:
        _login(client)
        response = _post_feedback(client, state["session_id"], "resp-1", "up", "x" * 501)
        assert response.status_code == 422
        assert client.get("/api/feedback/comments").json() == {"comments": [], "count": 0}


def test_read_with_invalid_cookie_is_401(services) -> None:
    _seed_session(services)
    with _client() as client:
        client.cookies.set("medguide_session", "invalid-token")
        assert client.get("/api/feedback/comments").status_code == 401
        # Same authentication as the POST endpoint.
        assert client.post(
            "/api/feedback",
            json={"session_id": "nope", "message_id": "resp-1", "rating": "up"},
        ).status_code == 401


def test_comments_are_scoped_to_owner(services) -> None:
    alice = _client()
    bob = _client()
    try:
        assert alice.post(
            "/api/auth/register", json={"username": "alice_comment", "password": PASSWORD}
        ).status_code == 201
        assert bob.post(
            "/api/auth/register", json={"username": "bob_comment", "password": PASSWORD}
        ).status_code == 201

        created = alice.post("/api/sessions")
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        assert services.store.get(session_id)["owner_id"] == "user:alice_comment"

        state = services.store.get(session_id)
        state["response_ids"] = ["resp-1"]
        services.store.save(state)
        assert _post_feedback(alice, session_id, "resp-1", "up", "只给 Alice 看").json()["changed"] is True

        assert alice.get("/api/feedback/comments").json()["count"] == 1
        assert bob.get("/api/feedback/comments").json() == {"comments": [], "count": 0}
        # Scoping to one session: the owner sees it, anyone else gets 404.
        assert alice.get("/api/feedback/comments", params={"session_id": session_id}).json()["count"] == 1
        assert bob.get("/api/feedback/comments", params={"session_id": session_id}).status_code == 404
        assert alice.get("/api/feedback/comments", params={"session_id": "missing"}).status_code == 404
    finally:
        alice.close()
        bob.close()


def test_end_to_end_chat_feedback_comment_read_back() -> None:
    """Real app wiring: chat -> feedback with comment -> read it back."""
    with TestClient(app, base_url="https://testserver") as client:
        # Issue #6: session creation requires a login in every mode.
        login_client(client, "feedback_comments_e2e_user")
        created = client.post("/api/sessions")
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        response = client.post("/api/chat", json={"session_id": session_id, "message": "我32岁，咳嗽持续3天"})
        assert response.status_code == 200
        response_id = response.json()["response_id"]

        posted = client.post(
            "/api/feedback",
            json={"session_id": session_id, "message_id": response_id, "rating": "up", "comment": "清楚"},
        )
        assert posted.status_code == 200
        listed = client.get("/api/feedback/comments", params={"session_id": session_id})
        assert listed.status_code == 200
        assert listed.json()["count"] == 1
        assert listed.json()["comments"][0]["comment"] == "清楚"
        assert listed.json()["comments"][0]["message_id"] == response_id
