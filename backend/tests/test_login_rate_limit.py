from __future__ import annotations

import ipaddress
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import app.main as main_module
from app.auth import AuthStore
from app.login_lockout import LoginAttemptLimiter
from app.main import app
from app.store import SessionStore

LOCKOUT_DETAIL = "登录尝试过于频繁，请稍后再试"


def _limiter(*, now, account_limit=3, ip_limit=100, lockout=60):
    return LoginAttemptLimiter(
        max_failed_attempts_per_account=account_limit,
        max_failed_attempts_per_ip=ip_limit,
        attempt_window_seconds=900,
        lockout_seconds=lockout,
        clock=lambda: now[0],
    )


def _runtime(tmp_path, *, limiter=None, trusted_proxy_networks=()):
    auth = AuthStore(
        tmp_path / "runtime" / "auth.sqlite3",
        session_ttl_seconds=300,
        cookie_secure=True,
        password_iterations=100_000,
    )
    return SimpleNamespace(
        production=True,
        mode="production",
        api_tokens=("service-client-token",),
        production_missing=(),
        auth=auth,
        store=SessionStore(mode="production"),
        login_limiter=limiter if limiter is not None else LoginAttemptLimiter(),
        trusted_proxy_networks=tuple(trusted_proxy_networks),
    )


def _client() -> TestClient:
    return TestClient(app, base_url="https://testserver")


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _register(client, username, password="Correct-Horse-42"):
    response = client.post("/api/auth/register", json={"username": username, "password": password})
    assert response.status_code == 201, response.text
    return response


# --- Limiter unit behaviour -------------------------------------------------


def test_limiter_locks_account_and_expires_with_clock() -> None:
    now = [1_000.0]
    limiter = _limiter(now=now, account_limit=3, lockout=60)

    limiter.record_failure("alice", "203.0.113.1")
    limiter.record_failure("alice", "203.0.113.1")
    assert limiter.locked_for_seconds("alice", "203.0.113.1") is None
    limiter.record_failure("alice", "203.0.113.1")
    remaining = limiter.locked_for_seconds("alice", "203.0.113.1")
    assert remaining is not None and 0 < remaining <= 60

    now[0] += 59
    assert limiter.locked_for_seconds("alice", "203.0.113.1") is not None
    now[0] += 2
    assert limiter.locked_for_seconds("alice", "203.0.113.1") is None


def test_limiter_failures_during_lockout_do_not_extend_it() -> None:
    now = [1_000.0]
    limiter = _limiter(now=now, account_limit=2, lockout=60)
    limiter.record_failure("alice", "203.0.113.1")
    limiter.record_failure("alice", "203.0.113.1")
    assert limiter.locked_for_seconds("alice", "203.0.113.1") is not None

    now[0] += 50
    limiter.record_failure("alice", "203.0.113.1")
    now[0] += 11
    assert limiter.locked_for_seconds("alice", "203.0.113.1") is None


def test_limiter_success_clears_account_failures_but_not_ip_failures() -> None:
    now = [1_000.0]
    limiter = _limiter(now=now, account_limit=3, ip_limit=4, lockout=60)
    limiter.record_failure("alice", "203.0.113.1")
    limiter.record_failure("alice", "203.0.113.1")
    limiter.record_success("alice")
    limiter.record_failure("alice", "203.0.113.1")
    limiter.record_failure("alice", "203.0.113.1")
    # Account bucket was cleared, so two more failures do not lock it
    # (checked from a different IP, so the IP bucket cannot interfere)...
    assert limiter.locked_for_seconds("alice", "198.51.100.7") is None
    # ...but the IP bucket kept all four failures and is now locked.
    assert limiter.locked_for_seconds("bob", "203.0.113.1") is not None


def test_limiter_caps_tracked_keys() -> None:
    now = [1_000.0]
    limiter = LoginAttemptLimiter(
        max_failed_attempts_per_account=5,
        max_failed_attempts_per_ip=100,
        max_tracked_keys=3,
        clock=lambda: now[0],
    )
    for name in ("user_a", "user_b", "user_c", "user_d"):
        limiter.record_failure(name, "203.0.113.1")
    assert len(limiter._failures) <= 3


def test_limiter_tolerates_malformed_usernames() -> None:
    limiter = LoginAttemptLimiter()
    limiter.record_failure("  ", "203.0.113.1")
    limiter.record_failure("", "")
    assert limiter.locked_for_seconds("  ", "203.0.113.1") is None


# --- HTTP behaviour --------------------------------------------------------


def test_login_locks_account_after_repeated_failures(tmp_path, monkeypatch) -> None:
    now = [1_000.0]
    current = _runtime(tmp_path, limiter=_limiter(now=now))
    monkeypatch.setattr(main_module, "get_services", lambda: current)
    client = _client()
    try:
        _register(client, "patient_001")
        client.post("/api/auth/logout")
        for _ in range(3):
            assert _login(client, "patient_001", "Wrong-Password-42").status_code == 401

        locked = _login(client, "patient_001", "Wrong-Password-42")
        assert locked.status_code == 429
        assert locked.json()["detail"] == LOCKOUT_DETAIL
        assert locked.headers["Retry-After"].isdigit()
        assert int(locked.headers["Retry-After"]) > 0
        # The correct password is refused while locked, too.
        assert _login(client, "patient_001", "Correct-Horse-42").status_code == 429
    finally:
        client.close()


def test_login_lockout_expires_and_allows_retry(tmp_path, monkeypatch) -> None:
    now = [1_000.0]
    current = _runtime(tmp_path, limiter=_limiter(now=now))
    monkeypatch.setattr(main_module, "get_services", lambda: current)
    client = _client()
    try:
        _register(client, "patient_001")
        client.post("/api/auth/logout")
        for _ in range(4):
            _login(client, "patient_001", "Wrong-Password-42")
        assert _login(client, "patient_001", "Correct-Horse-42").status_code == 429

        now[0] += 61
        assert _login(client, "patient_001", "Correct-Horse-42").status_code == 200
    finally:
        client.close()


def test_successful_login_resets_failure_count(tmp_path, monkeypatch) -> None:
    now = [1_000.0]
    current = _runtime(tmp_path, limiter=_limiter(now=now))
    monkeypatch.setattr(main_module, "get_services", lambda: current)
    client = _client()
    try:
        _register(client, "patient_001")
        client.post("/api/auth/logout")
        assert _login(client, "patient_001", "Wrong-Password-42").status_code == 401
        assert _login(client, "patient_001", "Wrong-Password-42").status_code == 401
        assert _login(client, "patient_001", "Correct-Horse-42").status_code == 200
        client.post("/api/auth/logout")
        assert _login(client, "patient_001", "Wrong-Password-42").status_code == 401
        assert _login(client, "patient_001", "Wrong-Password-42").status_code == 401
        # Two failures after the reset still leave room for one more...
        assert _login(client, "patient_001", "Wrong-Password-42").status_code == 401
        # ...and the next attempt is refused because the account locked.
        assert _login(client, "patient_001", "Wrong-Password-42").status_code == 429
    finally:
        client.close()


def test_unknown_username_is_locked_the_same_way(tmp_path, monkeypatch) -> None:
    now = [1_000.0]
    current = _runtime(tmp_path, limiter=_limiter(now=now))
    monkeypatch.setattr(main_module, "get_services", lambda: current)
    client = _client()
    try:
        _register(client, "patient_001")
        client.post("/api/auth/logout")
        for _ in range(3):
            assert _login(client, "ghost_user", "Wrong-Password-42").status_code == 401
        locked = _login(client, "ghost_user", "Wrong-Password-42")
        assert locked.status_code == 429
        assert locked.json()["detail"] == LOCKOUT_DETAIL
    finally:
        client.close()


def test_lockout_is_per_account(tmp_path, monkeypatch) -> None:
    now = [1_000.0]
    current = _runtime(tmp_path, limiter=_limiter(now=now))
    monkeypatch.setattr(main_module, "get_services", lambda: current)
    client = _client()
    try:
        _register(client, "patient_alice")
        _register(client, "patient_bob")
        client.post("/api/auth/logout")
        for _ in range(4):
            _login(client, "patient_alice", "Wrong-Password-42")
        assert _login(client, "patient_alice", "Correct-Horse-42").status_code == 429
        assert _login(client, "patient_bob", "Correct-Horse-42").status_code == 200
    finally:
        client.close()


def test_ip_lockout_blocks_many_accounts_from_one_address(tmp_path, monkeypatch) -> None:
    now = [1_000.0]
    current = _runtime(tmp_path, limiter=_limiter(now=now, ip_limit=5))
    monkeypatch.setattr(main_module, "get_services", lambda: current)
    client = _client()
    try:
        _register(client, "patient_001")
        client.post("/api/auth/logout")
        # Five failures spread over five different usernames: no single
        # account hits its limit, but the shared client IP does.
        for index in range(5):
            assert _login(client, f"target_{index}", "Wrong-Password-42").status_code == 401
        blocked = _login(client, "patient_001", "Correct-Horse-42")
        assert blocked.status_code == 429
        assert blocked.json()["detail"] == LOCKOUT_DETAIL
    finally:
        client.close()


def test_failed_login_timing_does_not_reveal_account_existence(tmp_path, monkeypatch) -> None:
    now = [1_000.0]
    current = _runtime(tmp_path, limiter=_limiter(now=now, ip_limit=1000))
    monkeypatch.setattr(main_module, "get_services", lambda: current)
    client = _client()
    try:
        _register(client, "patient_001")
        client.post("/api/auth/logout")

        started = time.perf_counter()
        known = _login(client, "patient_001", "Wrong-Password-42")
        known_seconds = time.perf_counter() - started
        started = time.perf_counter()
        unknown = _login(client, "ghost_user", "Wrong-Password-42")
        unknown_seconds = time.perf_counter() - started

        assert known.status_code == unknown.status_code == 401
        assert known.json() == unknown.json()
        # Both failures run the full PBKDF2 derivation (dummy hash for the
        # unknown user), so neither is instant and the two are close.
        assert known_seconds >= 0.005
        assert unknown_seconds >= 0.005
        assert max(known_seconds, unknown_seconds) / min(known_seconds, unknown_seconds) < 3
    finally:
        client.close()


# --- Client IP resolution ----------------------------------------------------


def _request(client_host: str, headers: dict[str, str]) -> Request:
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/auth/login",
        "headers": raw_headers,
        "client": (client_host, 1234),
        "server": ("testserver", 80),
    }
    return Request(scope)


def test_login_client_ip_uses_forwarded_for_only_from_trusted_proxy(tmp_path) -> None:
    current = _runtime(
        tmp_path,
        trusted_proxy_networks=(ipaddress.ip_network("10.0.0.0/8"),),
    )
    trusted = _request("10.0.0.5", {"X-Forwarded-For": "198.51.100.9, 10.0.0.5"})
    assert main_module._login_client_ip(current, trusted) == "198.51.100.9"

    untrusted = _request("203.0.113.7", {"X-Forwarded-For": "198.51.100.9"})
    assert main_module._login_client_ip(current, untrusted) == "203.0.113.7"

    direct = _request("203.0.113.7", {})
    assert main_module._login_client_ip(current, direct) == "203.0.113.7"

    malformed = _request("10.0.0.5", {"X-Forwarded-For": "not-an-ip"})
    assert main_module._login_client_ip(current, malformed) == "10.0.0.5"
