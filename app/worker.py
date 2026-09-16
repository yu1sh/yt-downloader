from __future__ import annotations

import logging
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .db import connect, init_db, iso_now, row_dict
from .security import is_path_inside


LOGGER = logging.getLogger("yt_downloader.worker")
ACTIVE_STATUSES = ("downloading", "processing")


def recover_active_jobs(settings: Settings) -> None:
    db = connect(settings.db_path)
    try:
        now = iso_now()
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        db.execute(
            f"UPDATE jobs SET status = 'interrupted', stage = 'interrupted', "
            "error_code = 'worker_restarted', "
            "error_message = 'サーバーの処理が再起動で中断されました。再試行してください', "
            f"updated_at = ?, finished_at = ? WHERE status IN ({placeholders})",
            (now, now, *ACTIVE_STATUSES),
        )
    finally:
        db.close()


def claim_next_job(settings: Settings) -> dict[str, Any] | None:
    db = connect(settings.db_path)
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM jobs WHERE status = 'waiting' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if row is None:
            db.commit()
            return None
        now = iso_now()
        db.execute(
            "UPDATE jobs SET status = 'downloading', stage = 'downloading', progress = 1, "
            "started_at = ?, updated_at = ? WHERE id = ? AND status = 'waiting'",
            (now, now, row["id"]),
        )
        db.commit()
        return row_dict(db.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone())
    finally:
        db.close()


def _job_row(settings: Settings, job_id: str) -> dict[str, Any] | None:
    db = connect(settings.db_path)
    try:
        return row_dict(db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
    finally:
        db.close()


def _update_job(settings: Settings, job_id: str, **values: Any) -> None:
    if not values:
        return
    db = connect(settings.db_path)
    try:
        values["updated_at"] = iso_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        db.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*values.values(), job_id))
    finally:
        db.close()


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    if process.poll() is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def _cleanup_job_dir(settings: Settings, job_id: str) -> None:
    # The worker only touches a directory named by a database job id.
    path = (settings.jobs_dir / job_id).resolve()
    if is_path_inside(path, settings.jobs_dir) and path.name == job_id:
        shutil.rmtree(path, ignore_errors=True)


def _directory_size(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return total
    for candidate in path.rglob("*"):
        try:
            if candidate.is_file() and not candidate.is_symlink():
                total += candidate.stat().st_size
        except FileNotFoundError:
            continue
    return total


def run_claimed_job(job: dict[str, Any], settings: Settings) -> None:
    job_id = str(job["id"])
    module_root = str(Path(__file__).resolve().parent.parent)
    command = [sys.executable, "-m", "app.job_runner", "--job-id", job_id]
    process = subprocess.Popen(
        command,
        cwd=module_root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    started = time.monotonic()
    job_dir = (settings.jobs_dir / job_id).resolve()
    while process.poll() is None:
        current = _job_row(settings, job_id)
        if current is None:
            _terminate_process_group(process)
            return
        if current["cancel_requested"]:
            _terminate_process_group(process)
            _update_job(
                settings,
                job_id,
                status="cancelled",
                stage="cancelled",
                progress=0,
                finished_at=iso_now(),
            )
            _cleanup_job_dir(settings, job_id)
            return
        if _directory_size(job_dir) > settings.max_job_bytes:
            _terminate_process_group(process)
            _update_job(
                settings,
                job_id,
                status="failed",
                stage="failed",
                error_code="size_limit",
                error_message="作業ファイルが容量上限を超えたため保存できません",
                error_detail="job working directory exceeded the configured limit",
                finished_at=iso_now(),
            )
            _cleanup_job_dir(settings, job_id)
            return
        if shutil.disk_usage(settings.data_dir).free < settings.min_free_bytes:
            _terminate_process_group(process)
            _update_job(
                settings,
                job_id,
                status="failed",
                stage="failed",
                error_code="storage_low",
                error_message="サーバーの空き容量が少なくなったため処理を中止しました",
                error_detail="free space fell below the configured minimum",
                finished_at=iso_now(),
            )
            _cleanup_job_dir(settings, job_id)
            return
        if time.monotonic() - started > settings.job_timeout_seconds:
            _terminate_process_group(process)
            _update_job(
                settings,
                job_id,
                status="failed",
                stage="failed",
                error_code="timeout",
                error_message="処理時間の上限を超えました。時間を置いて再試行してください",
                error_detail="worker timeout",
                finished_at=iso_now(),
            )
            _cleanup_job_dir(settings, job_id)
            return
        time.sleep(1)

    current = _job_row(settings, job_id)
    if current and current["status"] in {"completed", "failed", "cancelled"}:
        return
    if process.returncode != 0:
        _update_job(
            settings,
            job_id,
            status="failed",
            stage="failed",
            error_code="worker_exit",
            error_message="処理が途中で終了しました。時間を置いて再試行してください",
            error_detail=f"job runner exit code {process.returncode}",
            finished_at=iso_now(),
        )
        _cleanup_job_dir(settings, job_id)


def run_loop(settings: Settings) -> None:
    settings.ensure_paths()
    init_db(settings.db_path)
    recover_active_jobs(settings)
    LOGGER.info("worker started")
    while True:
        try:
            job = claim_next_job(settings)
            if job is None:
                time.sleep(settings.worker_poll_seconds)
                continue
            run_claimed_job(job, settings)
        except KeyboardInterrupt:
            LOGGER.info("worker stopped")
            return
        except Exception:
            LOGGER.exception("worker loop failed")
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    run_loop(Settings.from_env())
