from __future__ import annotations

import logging
import shutil
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .db import connect, init_db, iso_now, row_dict, utcnow
from .security import is_path_inside


LOGGER = logging.getLogger("yt_downloader.cleanup")


def _remove_job_directory(settings: Settings, job_id: str) -> None:
    path = (settings.jobs_dir / job_id).resolve()
    if is_path_inside(path, settings.jobs_dir) and path.name == job_id:
        shutil.rmtree(path, ignore_errors=True)


def cleanup_once(settings: Settings) -> dict[str, int]:
    settings.ensure_paths()
    init_db(settings.db_path)
    db = connect(settings.db_path)
    counters = {"expired_files": 0, "removed_history": 0, "removed_sessions": 0}
    try:
        now = iso_now()
        removed_sessions = db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        counters["removed_sessions"] = removed_sessions.rowcount

        expired = db.execute(
            "SELECT id, file_path FROM jobs WHERE status = 'completed' AND expires_at IS NOT NULL "
            "AND expires_at <= ?",
            (now,),
        ).fetchall()
        for row in expired:
            _remove_job_directory(settings, str(row["id"]))
            db.execute(
                "UPDATE jobs SET status = 'expired', stage = 'expired', progress = 0, "
                "file_path = NULL, file_size = NULL, updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
        counters["expired_files"] = len(expired)

        old_cutoff = (utcnow() - timedelta(seconds=settings.history_ttl_seconds)).isoformat(
            timespec="seconds"
        )
        old_jobs = db.execute(
            "SELECT id FROM jobs WHERE created_at <= ? AND status IN "
            "('failed', 'cancelled', 'interrupted', 'expired')",
            (old_cutoff,),
        ).fetchall()
        for row in old_jobs:
            _remove_job_directory(settings, str(row["id"]))
        removed_history = db.execute(
            "DELETE FROM jobs WHERE created_at <= ? AND status IN "
            "('failed', 'cancelled', 'interrupted', 'expired')",
            (old_cutoff,),
        )
        counters["removed_history"] = removed_history.rowcount
    finally:
        db.close()
    return counters


def run_loop(settings: Settings) -> None:
    while True:
        try:
            result = cleanup_once(settings)
            if any(result.values()):
                LOGGER.info("cleanup: %s", result)
        except KeyboardInterrupt:
            return
        except Exception:
            LOGGER.exception("cleanup failed")
        time.sleep(settings.cleanup_interval_seconds)


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    run_loop(Settings.from_env())

