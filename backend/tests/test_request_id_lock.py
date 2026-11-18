"""Issue #22: a plain chat after an idempotent turn must not 409.

Root cause: the persisted session state kept the previous turn's
``request_id``/``_request_fingerprint``, and a request without
``request_id`` inherited them in ``_state_for_request``. Both chat paths
then read the stale identifier from that state and the fingerprint check
compared the new message against the previous turn's identifier, so every
plain chat after an idempotent turn raised 409 and the session looked
permanently locked.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from conftest import login_client


def _new_session(client: TestClient) -> str:
    # Issue #6: protected APIs require a login in every mode, including
    # test mode, so create a session only after logging in.
    login_client(client, "request_id_lock_user")
    session_id = client.post("/api/sessions").json()["session_id"]
    assert session_id
    return session_id


def _chat(client: TestClient, session_id: str, message: str, request_id: str | None = None):
    payload = {"session_id": session_id, "message": message}
    if request_id is not None:
        payload["request_id"] = request_id
    return client.post("/api/chat", json=payload)


def test_plain_chat_after_idempotent_turn_returns_200() -> None:
    with TestClient(app) as client:
        session_id = _new_session(client)
        first = _chat(client, session_id, "我咳嗽三天了", request_id="repro-req-00001")
        assert first.status_code == 200

        # The exact reproduction from issue #22: the next plain chat used
        # to 409 with "请求标识已用于其他消息，不能重复使用".
        second = _chat(client, session_id, "我现在头痛")
        third = _chat(client, session_id, "还有点发烧")

        assert second.status_code == 200
        assert third.status_code == 200
        assert "请求标识已用于其他消息" not in second.text
        state = client.get(f"/api/sessions/{session_id}").json()
        assert state["turn_count"] == 3


def test_plain_stream_after_idempotent_turn_returns_final() -> None:
    with TestClient(app) as client:
        session_id = _new_session(client)
        first = _chat(client, session_id, "我咳嗽三天了", request_id="repro-req-00002")
        assert first.status_code == 200

        streamed = client.post(
            "/api/chat?stream=true",
            json={"session_id": session_id, "message": "我现在头痛"},
        )
        assert streamed.status_code == 200
        assert "event: error" not in streamed.text
        assert "event: final" in streamed.text


def test_idempotent_replay_still_works_after_plain_turns() -> None:
    with TestClient(app) as client:
        session_id = _new_session(client)
        request = {"session_id": session_id, "message": "我咳嗽三天了", "request_id": "replay-req-00001"}
        first = client.post("/api/chat", json=request)
        assert first.status_code == 200
        assert _chat(client, session_id, "我现在头痛").status_code == 200

        replay = client.post("/api/chat", json=request)
        recovered = client.get(f"/api/sessions/{session_id}/requests/replay-req-00001")
        state = client.get(f"/api/sessions/{session_id}").json()

        assert replay.status_code == 200
        assert replay.json()["response_id"] == first.json()["response_id"]
        assert recovered.status_code == 200
        assert recovered.json()["response_id"] == first.json()["response_id"]
        # The replay must not advance the turn counter.
        assert state["turn_count"] == 2


def test_same_request_id_different_message_still_conflicts() -> None:
    with TestClient(app) as client:
        session_id = _new_session(client)
        first = _chat(client, session_id, "我咳嗽", request_id="turn-collision-0001")
        assert first.status_code == 200
        # A plain turn in between must not disturb the fingerprint tombstone.
        assert _chat(client, session_id, "我现在头痛").status_code == 200

        conflict = _chat(client, session_id, "我腹痛", request_id="turn-collision-0001")
        assert conflict.status_code == 409


def test_alternating_idempotent_and_plain_turns() -> None:
    with TestClient(app) as client:
        session_id = _new_session(client)
        first = _chat(client, session_id, "我咳嗽三天了", request_id="alternating-00001")
        assert first.status_code == 200
        assert _chat(client, session_id, "我现在头痛").status_code == 200
        second = _chat(client, session_id, "还有点发烧", request_id="alternating-00002")
        assert second.status_code == 200
        assert _chat(client, session_id, "昨晚没睡好").status_code == 200

        # Replaying the first idempotent turn still returns its result.
        replay = _chat(client, session_id, "我咳嗽三天了", request_id="alternating-00001")
        assert replay.status_code == 200
        assert replay.json()["response_id"] == first.json()["response_id"]
        state = client.get(f"/api/sessions/{session_id}").json()
        assert state["turn_count"] == 4


def test_invalid_request_id_still_rejected() -> None:
    with TestClient(app) as client:
        session_id = _new_session(client)
        assert _chat(client, session_id, "我咳嗽", request_id="short").status_code == 422
        assert _chat(client, session_id, "我咳嗽", request_id="bad id!").status_code == 422
        # A rejected request must not poison the session.
        assert _chat(client, session_id, "我咳嗽").status_code == 200
