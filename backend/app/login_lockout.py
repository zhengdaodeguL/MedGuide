"""In-memory brute-force protection for ``POST /api/auth/login``.

The login endpoint used to accept an unbounded number of failed attempts
for any username: the same account could be tried forever and every
failure cost the attacker the same fixed PBKDF2 work, so a weak password
had no protection at all. This module caps that rate.

Design notes (issue #23):

- Storage is process memory, not Redis. The API runs as a single uvicorn
  worker (``backend/Dockerfile`` has no ``--workers``), so per-process
  state covers the whole deployment; Redis is optional in this codebase
  (``REDIS_URL`` may be unset and the session store falls back to memory),
  so tying login security to it would open a fail-open hole whenever
  Redis is down or unconfigured. The trade-off is that lockout state is
  lost on restart -- the acceptance criteria allow that -- and that a
  future multi-worker deployment would need a shared store.
- Two dimensions are counted independently: per normalized account and
  per client IP. The account dimension stops password guessing against
  one user; the IP dimension stops credential stuffing across many
  accounts from a single address.
- Unknown usernames are counted and locked exactly like known ones, and
  ``verify_credentials`` already runs the full PBKDF2 derivation against
  a dummy salt/hash for unknown users, so a failed login costs the same
  time whether or not the account exists. A locked attempt returns 429
  with the same generic message for both cases, so the lockout itself
  reveals nothing about account existence.
- Attempts made while already locked do not extend the lockout: an
  attacker hammering a locked account must not be able to keep the
  legitimate owner locked out forever. After the lockout expires the
  slate is clean.
- A successful login clears the account's failure history. It does not
  clear the IP bucket: on a shared address one user's success must not
  mask another address's attack traffic.
"""

from __future__ import annotations

import os
import threading
import time
import unicodedata
from collections import deque
from typing import Callable

# Defaults: 5 failed attempts per account (or 20 per client IP) inside a
# 15-minute window lock the key for 15 minutes. Each value can be tuned
# with an environment variable; out-of-range values are clamped instead
# of failing startup.
DEFAULT_MAX_FAILED_ATTEMPTS_PER_ACCOUNT = 5
DEFAULT_MAX_FAILED_ATTEMPTS_PER_IP = 20
DEFAULT_ATTEMPT_WINDOW_SECONDS = 900
DEFAULT_LOCKOUT_SECONDS = 900
# Bound the number of tracked keys so an attacker spraying random
# usernames cannot grow memory without bound; the oldest key is evicted.
DEFAULT_MAX_TRACKED_KEYS = 10_000


def _environment_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


MAX_FAILED_ATTEMPTS_PER_ACCOUNT = _environment_int(
    "MEDGUIDE_LOGIN_MAX_FAILED_ATTEMPTS",
    DEFAULT_MAX_FAILED_ATTEMPTS_PER_ACCOUNT,
    minimum=1,
    maximum=100,
)
MAX_FAILED_ATTEMPTS_PER_IP = _environment_int(
    "MEDGUIDE_LOGIN_MAX_FAILED_ATTEMPTS_PER_IP",
    DEFAULT_MAX_FAILED_ATTEMPTS_PER_IP,
    minimum=1,
    maximum=1_000,
)
ATTEMPT_WINDOW_SECONDS = _environment_int(
    "MEDGUIDE_LOGIN_ATTEMPT_WINDOW_SECONDS",
    DEFAULT_ATTEMPT_WINDOW_SECONDS,
    minimum=60,
    maximum=3_600,
)
LOCKOUT_SECONDS = _environment_int(
    "MEDGUIDE_LOGIN_LOCKOUT_SECONDS",
    DEFAULT_LOCKOUT_SECONDS,
    minimum=60,
    maximum=86_400,
)
MAX_TRACKED_KEYS = _environment_int(
    "MEDGUIDE_LOGIN_MAX_TRACKED_KEYS",
    DEFAULT_MAX_TRACKED_KEYS,
    minimum=100,
    maximum=1_000_000,
)


class LoginAttemptLimiter:
    """Thread-safe failed-login counter with temporary lockouts.

    All state lives in memory and is keyed by normalized username
    (``account:<name>``) and by client IP (``ip:<address>``). Pass a fake
    ``clock`` in tests to control lockout expiry without sleeping.
    """

    def __init__(
        self,
        *,
        max_failed_attempts_per_account: int = MAX_FAILED_ATTEMPTS_PER_ACCOUNT,
        max_failed_attempts_per_ip: int = MAX_FAILED_ATTEMPTS_PER_IP,
        attempt_window_seconds: int = ATTEMPT_WINDOW_SECONDS,
        lockout_seconds: int = LOCKOUT_SECONDS,
        max_tracked_keys: int = MAX_TRACKED_KEYS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_failed_attempts_per_account < 1 or max_failed_attempts_per_ip < 1:
            raise ValueError("login attempt limits must be at least 1")
        if attempt_window_seconds < 1 or lockout_seconds < 1 or max_tracked_keys < 1:
            raise ValueError("login lockout window, duration and key cap must be positive")
        self.max_failed_attempts_per_account = int(max_failed_attempts_per_account)
        self.max_failed_attempts_per_ip = int(max_failed_attempts_per_ip)
        self.attempt_window_seconds = int(attempt_window_seconds)
        self.lockout_seconds = int(lockout_seconds)
        self.max_tracked_keys = int(max_tracked_keys)
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}

    @staticmethod
    def _account_key(username: str) -> str:
        # Match verify_credentials' normalization closely enough that the
        # same account always lands in the same bucket, without raising
        # for malformed input: an invalid username is still rate-limited
        # (and still counts toward the IP bucket).
        return "account:" + unicodedata.normalize("NFKC", str(username)).strip()

    @staticmethod
    def _ip_key(client_ip: str) -> str:
        return "ip:" + (client_ip or "").strip()

    def _evict_oldest_if_needed(self) -> None:
        while len(self._failures) > self.max_tracked_keys:
            oldest = next(iter(self._failures))
            self._failures.pop(oldest, None)
            self._locked_until.pop(oldest, None)

    def _prune(self, key: str, now: float) -> None:
        failures = self._failures.get(key)
        if not failures:
            return
        cutoff = now - self.attempt_window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            self._failures.pop(key, None)

    def locked_for_seconds(self, username: str, client_ip: str) -> float | None:
        """Remaining lockout in seconds, or None when the attempt may proceed."""
        now = self._clock()
        with self._lock:
            for key in (self._account_key(username), self._ip_key(client_ip)):
                locked_until = self._locked_until.get(key)
                if locked_until is None:
                    continue
                if locked_until > now:
                    return locked_until - now
                self._locked_until.pop(key, None)
            return None

    def record_failure(self, username: str, client_ip: str) -> None:
        """Count one failed login for the account and the client IP.

        Keys that are already locked are left alone, so attempts made
        during a lockout do not extend it.
        """
        now = self._clock()
        with self._lock:
            for key, limit in (
                (self._account_key(username), self.max_failed_attempts_per_account),
                (self._ip_key(client_ip), self.max_failed_attempts_per_ip),
            ):
                if self._locked_until.get(key, 0.0) > now:
                    continue
                self._prune(key, now)
                failures = self._failures.setdefault(key, deque())
                failures.append(now)
                self._evict_oldest_if_needed()
                if len(failures) >= limit:
                    self._locked_until[key] = now + self.lockout_seconds
                    self._failures.pop(key, None)

    def record_success(self, username: str) -> None:
        """Clear an account's failure history after a successful login."""
        with self._lock:
            key = self._account_key(username)
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)
