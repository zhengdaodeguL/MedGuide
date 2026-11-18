"""Session history (issue #3): transcript persistence and listing.

Covers: multi-turn messages stored and restored via GET, the session list
being newest-first and owner-scoped, cross-user reads returning 404,
expired sessions excluded from the list, and idempotent replay not
duplicating transcript entries.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import AppServices, app
from app.store import SessionStore


@pytest.fixture()
def services(tmp_path, monkeypatch):
    """A fresh, isolated AppServices (fresh store + fresh auth DB)."""
    monkeypatch.setenv("MEDGUIDE_AUTH_DB_PATH", str(tmp_path / "auth.sqlite3"))
    current = AppServices()
    monkeypatch.setattr(main_module, "get_services", lambda: current)
    yield current
    current.close()


@pytest.fixture()
def client(services):
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


def _register(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "Correct-Horse-42"},
    )
    assert response.status_code == 201


def _chat(client: TestClient, session_id: str, message: str, request_id: str):
    response = client.post(
        "/api/chat",
        json={"session_id": session_id, "message": message, "request_id": request_id},
    )
    assert response.status_code == 200
    return response.json()


def test_multi_turn_messages_persist_and_restore_via_get(client: TestClient) -> None:
    # Issue #6 requires a session for /api/sessions in every mode.
    _register(client, "history_multi_turn")
    session_id = client.post("/api/sessions").json()["session_id"]
    first = _chat(client, session_id, "我32岁，咳嗽持续3天", "turn-history-0001")
    second = _chat(client, session_id, "还伴随发热38度", "turn-history-0002")

    state = client.get(f"/api/sessions/{session_id}").json()
    assert state["turn_count"] == 2
    messages = state["messages"]
    assert [item["role"] for item in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[0]["content"] == "我32岁，咳嗽持续3天"
    assert messages[2]["content"] == "还伴随发热38度"
    assert messages[1]["content"] == first["answer"]
    assert messages[3]["content"] == second["answer"]
    assert messages[1]["response_id"] == first["response_id"]
    assert messages[3]["response_id"] == second["response_id"]
    assert all(item.get("timestamp") for item in messages)


def test_session_list_is_newest_first_and_owner_scoped(client: TestClient) -> None:
    alice = client
    _register(alice, "history_alice")
    first_id = alice.post("/api/sessions").json()["session_id"]
    _chat(alice, first_id, "我咳嗽三天", "turn-list-a-0001")
    second_id = alice.post("/api/sessions").json()["session_id"]
    _chat(alice, second_id, "我想查布洛芬库存", "turn-list-a-0002")

    bob = TestClient(app, base_url="https://testserver")
    try:
        _register(bob, "history_bob")
        other_id = bob.post("/api/sessions").json()["session_id"]
        _chat(bob, other_id, "我头痛", "turn-list-b-0001")

        listed = alice.get("/api/sessions").json()
        assert listed["complete"] is True
        sessions = listed["sessions"]
        assert [item["session_id"] for item in sessions] == [second_id, first_id]
        assert sessions[0]["title"] == "我想查布洛芬库存"
        assert sessions[1]["title"] == "我咳嗽三天"
        assert sessions[0]["turn_count"] == 1
        assert all(item["session_id"] != other_id for item in sessions)

        bob_listed = bob.get("/api/sessions").json()
        assert [item["session_id"] for item in bob_listed["sessions"]] == [other_id]
    finally:
        bob.close()


def test_cross_user_session_read_returns_404(client: TestClient) -> None:
    _register(client, "history_owner")
    session_id = client.post("/api/sessions").json()["session_id"]
    _chat(client, session_id, "我咳嗽三天", "turn-cross-0001")

    stranger = TestClient(app, base_url="https://testserver")
    try:
        _register(stranger, "history_stranger")
        assert stranger.get(f"/api/sessions/{session_id}").status_code == 404
        assert stranger.get("/api/sessions").json()["sessions"] == []
    finally:
        stranger.close()


def test_idempotent_replay_does_not_duplicate_messages(client: TestClient) -> None:
    # Issue #6 requires a session for /api/sessions in every mode.
    _register(client, "history_replay")
    session_id = client.post("/api/sessions").json()["session_id"]
    request = {
        "session_id": session_id,
        "message": "我32岁，咳嗽持续3天",
        "request_id": "turn-replay-0001",
    }
    first = client.post("/api/chat", json=request)
    replay = client.post("/api/chat", json=request)
    assert first.status_code == replay.status_code == 200
    assert replay.json()["response_id"] == first.json()["response_id"]

    state = client.get(f"/api/sessions/{session_id}").json()
    assert state["turn_count"] == 1
    assert len(state["messages"]) == 2
    assert [item["role"] for item in state["messages"]] == ["user", "assistant"]


def test_new_session_public_state_has_empty_messages() -> None:
    store = SessionStore(mode="test")
    state = store.create(owner_id="user:someone")
    assert store.public(state)["messages"] == []
    assert store.list_sessions("user:someone")[0]["title"] == "新的健康咨询"


def test_expired_sessions_are_excluded_from_list() -> None:
    now = [1_000.0]
    store = SessionStore(mode="test", ttl_seconds=60, clock=lambda: now[0])
    old_id = store.create(owner_id="user:alice")["session_id"]
    now[0] = 1_050.0
    new_id = store.create(owner_id="user:alice")["session_id"]
    now[0] = 1_070.0
    assert store.get(old_id) is None
    listed = store.list_sessions("user:alice")
    assert [item["session_id"] for item in listed] == [new_id]


def test_redis_backend_list_is_empty_and_marked_incomplete() -> None:
    store = SessionStore(backend=SimpleNamespace(), mode="production")
    assert store.listing_complete is False
    assert store.list_sessions("user:alice") == []
