"""Issue #19: a recorded vote can be changed and revoked.

Change-vote already worked on the backend (``add_feedback`` upserts per
message); the frontend blocked it. Revoking had no backend semantics at
all, so ``DELETE /api/feedback`` is new here. The frontend change is
covered by ``frontend/tests/feedback-vote.test.mjs``.
"""
from fastapi.testclient import TestClient

from app.main import app
from app.metrics import MetricsStore
from conftest import login_client


def _answer(client: TestClient, request_id: str) -> tuple[str, str]:
    # Issue #6: /api/sessions requires a session in every mode.
    login_client(client, "feedback_change_vote_user")
    session_id = client.post("/api/sessions").json()["session_id"]
    answer = client.post(
        "/api/chat",
        json={"session_id": session_id, "message": "我咳嗽", "request_id": request_id},
    ).json()
    return session_id, answer["response_id"]


def _vote(client: TestClient, session_id: str, message_id: str, rating: str) -> dict:
    response = client.post(
        "/api/feedback",
        json={"session_id": session_id, "message_id": message_id, "rating": rating},
    )
    assert response.status_code == 200
    return response.json()


def _revoke(client: TestClient, session_id: str, message_id: str):
    return client.delete(
        "/api/feedback", params={"session_id": session_id, "message_id": message_id}
    )


def _feedback(client: TestClient) -> dict:
    return client.get("/api/metrics").json()["feedback"]


def test_change_vote_moves_counts_instead_of_adding_a_second_vote() -> None:
    with TestClient(app) as client:
        session_id, message_id = _answer(client, "turn-vote-change-0001")
        first = _vote(client, session_id, message_id, "up")
        changed = _vote(client, session_id, message_id, "down")
        metrics = client.get("/api/metrics").json()

        assert first["changed"] is True
        assert changed["changed"] is True
        assert _feedback(client) == {"down": 1}
        assert metrics["feedback_by_message"] == {f"{session_id}:{message_id}": "down"}


def test_repeat_same_rating_does_not_count_twice() -> None:
    with TestClient(app) as client:
        session_id, message_id = _answer(client, "turn-vote-repeat-0001")
        first = _vote(client, session_id, message_id, "up")
        duplicate = _vote(client, session_id, message_id, "up")

        assert first["changed"] is True
        assert duplicate["changed"] is False
        assert _feedback(client) == {"up": 1}


def test_revoke_removes_the_vote_and_a_second_revoke_changes_nothing() -> None:
    with TestClient(app) as client:
        session_id, message_id = _answer(client, "turn-vote-revoke-0001")
        _vote(client, session_id, message_id, "up")
        first = _revoke(client, session_id, message_id)
        second = _revoke(client, session_id, message_id)
        metrics = client.get("/api/metrics").json()

        assert first.status_code == 200
        assert first.json()["changed"] is True
        assert second.status_code == 200
        assert second.json()["changed"] is False
        assert _feedback(client) == {}
        assert metrics["feedback_by_message"] == {}


def test_revoke_without_a_vote_changes_nothing() -> None:
    with TestClient(app) as client:
        session_id, message_id = _answer(client, "turn-vote-revoke-0002")
        response = _revoke(client, session_id, message_id)

        assert response.status_code == 200
        assert response.json()["changed"] is False
        assert _feedback(client) == {}


def test_vote_again_after_revoke_counts_as_a_new_vote() -> None:
    with TestClient(app) as client:
        session_id, message_id = _answer(client, "turn-vote-revoke-0003")
        _vote(client, session_id, message_id, "up")
        _revoke(client, session_id, message_id)
        again = _vote(client, session_id, message_id, "down")

        assert again["changed"] is True
        assert _feedback(client) == {"down": 1}


def test_revoke_rejects_unknown_session_and_foreign_message() -> None:
    with TestClient(app) as client:
        session_id, message_id = _answer(client, "turn-vote-revoke-0004")
        unknown_session = _revoke(client, "missing-session", message_id)
        foreign_message = _revoke(client, session_id, "other-session:turn:1")
        missing_params = client.delete("/api/feedback", params={"session_id": session_id})

        assert unknown_session.status_code == 404
        assert foreign_message.status_code == 422
        assert missing_params.status_code == 422


def test_remove_feedback_unit_in_memory() -> None:
    store = MetricsStore()
    assert store.remove_feedback("message-1", session_id="session-1") is False
    assert store.add_feedback("up", None, "message-1", session_id="session-1") is True
    assert store.add_feedback("up", "写得很清楚", "message-2", session_id="session-1") is True
    assert store.snapshot()["feedback"] == {"up": 2, "commented": 1}

    assert store.remove_feedback("message-1", session_id="session-1") is True
    assert store.snapshot()["feedback"] == {"up": 1, "commented": 1}
    # A revoke removes the rating only; "commented" counts messages that
    # once carried a comment, and comment semantics belong to issue #21.
    assert store.remove_feedback("message-1", session_id="session-1") is False
    assert store.remove_feedback("message-2", session_id="other-session") is False
