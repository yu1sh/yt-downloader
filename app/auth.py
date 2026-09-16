from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any

from .config import Settings
from .db import iso_after, iso_now
from .security import generate_token, hash_password


_failed_logins: dict[str, deque[float]] = defaultdict(deque)


def login_is_allowed(key: str, settings: Settings) -> bool:
    now = time.monotonic()
    attempts = _failed_logins[key]
    while attempts and now - attempts[0] > settings.login_window_seconds:
        attempts.popleft()
    return len(attempts) < settings.login_max_failures


def record_failed_login(key: str, settings: Settings) -> None:
    now = time.monotonic()
    attempts = _failed_logins[key]
    while attempts and now - attempts[0] > settings.login_window_seconds:
        attempts.popleft()
    attempts.append(now)


def clear_failed_logins(key: str) -> None:
    _failed_logins.pop(key, None)


def create_session(db: Any, user_id: int, settings: Settings) -> tuple[str, str]:
    token = generate_token()
    csrf_token = generate_token(24)
    now = iso_now()
    db.execute(
        "INSERT INTO sessions(token, user_id, csrf_token, created_at, last_seen_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (token, user_id, csrf_token, now, now, iso_after(settings.session_ttl_seconds)),
    )
    return token, csrf_token


def remove_session(db: Any, token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token = ?", (token,))


def create_user(
    db: Any,
    username: str,
    password: str,
    *,
    is_admin: bool = False,
    must_change_password: bool = False,
) -> int:
    now = iso_now()
    cursor = db.execute(
        "INSERT INTO users(username, password_hash, is_admin, is_active, must_change_password, "
        "created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)",
        (
            username,
            hash_password(password),
            int(is_admin),
            int(must_change_password),
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)

