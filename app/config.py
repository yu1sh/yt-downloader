from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str
    secret_key: str
    data_dir: Path
    db_path: Path
    cookie_secure: bool
    session_ttl_seconds: int
    max_duration_seconds: int
    max_job_bytes: int
    max_pending_per_user: int
    max_pending_total: int
    min_free_bytes: int
    file_ttl_seconds: int
    history_ttl_seconds: int
    inspect_timeout_seconds: int
    download_socket_timeout_seconds: int
    job_timeout_seconds: int
    cleanup_interval_seconds: int
    worker_poll_seconds: int
    login_window_seconds: int
    login_max_failures: int

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("DATA_DIR", "/data")).expanduser()
        db_path = Path(os.getenv("DB_PATH", str(data_dir / "app.db"))).expanduser()
        app_env = os.getenv("APP_ENV", "development").strip().lower()
        return cls(
            app_env=app_env,
            secret_key=os.getenv(
                "APP_SECRET_KEY",
                "development-only-secret-change-this-before-production",
            ),
            data_dir=data_dir,
            db_path=db_path,
            cookie_secure=_env_bool("COOKIE_SECURE", app_env == "production"),
            session_ttl_seconds=_env_int("SESSION_TTL_SECONDS", 7 * 24 * 60 * 60),
            max_duration_seconds=_env_int("MAX_DURATION_SECONDS", 2 * 60 * 60),
            max_job_bytes=_env_int("MAX_JOB_BYTES", 4 * 1024 * 1024 * 1024),
            max_pending_per_user=_env_int("MAX_PENDING_PER_USER", 3),
            max_pending_total=_env_int("MAX_PENDING_TOTAL", 20),
            min_free_bytes=_env_int("MIN_FREE_BYTES", 5 * 1024 * 1024 * 1024),
            file_ttl_seconds=_env_int("FILE_TTL_SECONDS", 24 * 60 * 60),
            history_ttl_seconds=_env_int("HISTORY_TTL_SECONDS", 7 * 24 * 60 * 60),
            inspect_timeout_seconds=_env_int("INSPECT_TIMEOUT_SECONDS", 120),
            download_socket_timeout_seconds=_env_int("DOWNLOAD_SOCKET_TIMEOUT_SECONDS", 30),
            job_timeout_seconds=_env_int("JOB_TIMEOUT_SECONDS", 2 * 60 * 60),
            cleanup_interval_seconds=_env_int("CLEANUP_INTERVAL_SECONDS", 5 * 60),
            worker_poll_seconds=_env_int("WORKER_POLL_SECONDS", 2),
            login_window_seconds=_env_int("LOGIN_WINDOW_SECONDS", 15 * 60),
            login_max_failures=_env_int("LOGIN_MAX_FAILURES", 5),
        )

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    def ensure_paths(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

