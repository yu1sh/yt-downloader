from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat(timespec="seconds")


def iso_after(seconds: int) -> str:
    return (utcnow() + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(db_path), timeout=30, isolation_level=None, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    thumbnail TEXT NOT NULL DEFAULT '',
    duration INTEGER,
    mode TEXT NOT NULL CHECK (mode IN ('simple', 'detailed')),
    target TEXT NOT NULL CHECK (target IN ('video', 'audio')),
    video_format TEXT NOT NULL CHECK (video_format IN ('mp4', 'webm')),
    audio_format TEXT NOT NULL CHECK (audio_format IN ('mp3', 'm4a', 'opus')),
    max_height INTEGER,
    mp3_quality INTEGER,
    status TEXT NOT NULL DEFAULT 'waiting',
    progress INTEGER NOT NULL DEFAULT 0,
    stage TEXT NOT NULL DEFAULT 'waiting',
    error_code TEXT,
    error_message TEXT,
    error_detail TEXT,
    file_path TEXT,
    filename TEXT,
    file_size INTEGER,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    expires_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_user_created ON jobs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_expiry ON jobs(expires_at);
"""


def init_db(db_path: Path) -> None:
    connection = connect(db_path)
    try:
        connection.executescript(SCHEMA)
    finally:
        connection.close()


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None

