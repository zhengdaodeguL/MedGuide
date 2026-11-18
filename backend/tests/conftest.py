from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("MEDGUIDE_MODE", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def login_client(client, username: str, password: str = "Correct-Horse-42"):
    """Log a test-mode API client in through the real register/login flow.

    Issue #6: protected APIs require a valid session cookie in every mode,
    including test mode, so tests that used to call them anonymously must
    log in first. The shared test auth database persists between runs, so a
    re-run registers nothing new: a 409 falls back to login with the same
    password.
    """
    registered = client.post(
        "/api/auth/register", json={"username": username, "password": password}
    )
    assert registered.status_code in {201, 409}, registered.text
    if registered.status_code == 409:
        logged_in = client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        assert logged_in.status_code == 200, logged_in.text
    assert client.get("/api/auth/me").status_code == 200
    return client
