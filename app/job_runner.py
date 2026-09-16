from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .db import connect, iso_after, iso_now, row_dict
from .security import is_path_inside, safe_filename
from .youtube import (
    YoutubeError,
    expected_extension,
    extract_video_info,
    format_selector,
    normalize_youtube_url,
    validate_selection,
)


TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted", "expired"}


def _job_dir(settings: Settings, job_id: str) -> Path:
    path = (settings.jobs_dir / job_id).resolve()
    if not is_path_inside(path, settings.jobs_dir):
        raise RuntimeError("invalid job path")
    return path


def _update(db: Any, job_id: str, **values: Any) -> None:
    if not values:
        return
    values["updated_at"] = iso_now()
    assignments = ", ".join(f"{key} = ?" for key in values)
    db.execute(
        f"UPDATE jobs SET {assignments} WHERE id = ?",
        (*values.values(), job_id),
    )


def _current_job(db: Any, job_id: str) -> dict[str, Any] | None:
    return row_dict(db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def _cancel_requested(db: Any, job_id: str) -> bool:
    row = db.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return bool(row and row[0])


def _format_progress(value: Any) -> int:
    try:
        return max(0, min(95, int(value)))
    except (TypeError, ValueError):
        return 0


def _make_yt_dlp_options(settings: Settings, job: dict[str, Any], job_dir: Path, db: Any) -> dict[str, Any]:
    target = str(job["target"])
    video_format = str(job["video_format"])
    audio_format = str(job["audio_format"])
    max_height = int(job["max_height"] or 720)
    last_update = 0.0

    def update_from_progress(data: dict[str, Any]) -> None:
        nonlocal last_update
        now = time.monotonic()
        status = data.get("status")
        if status == "finished":
            _update(db, job["id"], stage="processing", progress=92)
            return
        if status != "downloading" or now - last_update < 0.5:
            return
        downloaded = data.get("downloaded_bytes") or 0
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        percent = int(downloaded / total * 90) if total else 5
        _update(
            db,
            job["id"],
            stage="downloading",
            progress=_format_progress(percent),
        )
        last_update = now

    def update_from_postprocessor(data: dict[str, Any]) -> None:
        status = data.get("status")
        if status in {"started", "processing"}:
            _update(db, job["id"], stage="processing", progress=94)
        elif status == "finished":
            _update(db, job["id"], stage="processing", progress=98)

    options: dict[str, Any] = {
        "socket_timeout": settings.download_socket_timeout_seconds,
        "format": format_selector(
            target=target,
            video_format=video_format,
            audio_format=audio_format,
            max_height=max_height,
        ),
        "outtmpl": str(job_dir / "source.%(ext)s"),
        "paths": {"home": str(job_dir), "temp": str(job_dir / "tmp")},
        "restrictfilenames": True,
        "windowsfilenames": True,
        "continuedl": True,
        "overwrites": False,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 1,
        "max_filesize": settings.max_job_bytes,
        "progress_hooks": [update_from_progress],
        "postprocessor_hooks": [update_from_postprocessor],
    }
    if target == "video":
        options["merge_output_format"] = video_format
        if video_format == "mp4":
            options["postprocessors"] = [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ]
    else:
        preferred_quality: str | None = None
        if audio_format == "mp3":
            preferred_quality = str(job["mp3_quality"] or 192)
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                **({"preferredquality": preferred_quality} if preferred_quality else {}),
            }
        ]
    return options


def _find_output_file(job_dir: Path, expected: str) -> Path:
    candidates: list[Path] = []
    allowed = {f".{expected}"}
    for path in job_dir.rglob("*"):
        if not path.is_file() or path.name.endswith((".part", ".ytdl")):
            continue
        if path.suffix.lower() in allowed and path.parent.name != "tmp":
            candidates.append(path)
    if not candidates:
        raise RuntimeError("yt-dlp completed without producing a supported file")
    return max(candidates, key=lambda path: path.stat().st_mtime)


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


def run_job(job_id: str, settings: Settings) -> int:
    settings.ensure_paths()
    db = connect(settings.db_path)
    job_dir = _job_dir(settings, job_id)
    try:
        job = _current_job(db, job_id)
        if not job:
            return 2
        if job["status"] in TERMINAL_STATUSES:
            return 0 if job["status"] == "completed" else 1
        if _cancel_requested(db, job_id):
            _update(db, job_id, status="cancelled", stage="cancelled", progress=0)
            return 0

        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "tmp").mkdir(parents=True, exist_ok=True)
        normalized = normalize_youtube_url(str(job["source_url"]))
        info = extract_video_info(normalized, settings)
        duration = info.get("duration")
        if duration is not None and int(duration) > settings.max_duration_seconds:
            raise YoutubeError(
                "duration_limit",
                "動画が長すぎるため保存できません",
                f"duration={duration}",
            )
        validate_selection(
            info,
            target=str(job["target"]),
            video_format=str(job["video_format"]),
            audio_format=str(job["audio_format"]),
            max_height=int(job["max_height"] or 720),
        )
        _update(
            db,
            job_id,
            title=str(info.get("title") or job["title"] or "YouTube video")[:500],
            thumbnail=str(info.get("thumbnail") or job["thumbnail"] or "")[:1000],
            duration=int(duration) if duration is not None else job["duration"],
            stage="downloading",
            progress=1,
        )

        options = _make_yt_dlp_options(settings, job, job_dir, db)
        # extract_info(download=True) keeps the integration on yt-dlp's
        # supported Python API and avoids parsing human-readable CLI output.
        from .youtube import _extract

        _extract(normalized.url, settings, download=True, **options)
        if _cancel_requested(db, job_id):
            _update(db, job_id, status="cancelled", stage="cancelled", progress=0)
            shutil.rmtree(job_dir, ignore_errors=True)
            return 0

        expected = expected_extension(
            str(job["target"]), str(job["video_format"]), str(job["audio_format"])
        )
        source_file = _find_output_file(job_dir, expected)
        size = source_file.stat().st_size
        working_directory_size = _directory_size(job_dir)
        if size > settings.max_job_bytes or working_directory_size > settings.max_job_bytes:
            raise YoutubeError(
                "size_limit",
                "作業ファイルが容量上限を超えたため保存できません",
                f"file_size={size}, working_directory_size={working_directory_size}",
            )
        title = str(info.get("title") or job["title"] or "youtube-video")
        friendly_name = safe_filename(title, source_file.suffix.lstrip("."))
        final_path = job_dir / friendly_name
        if source_file.resolve() != final_path.resolve():
            source_file.replace(final_path)
        _update(
            db,
            job_id,
            status="completed",
            stage="completed",
            progress=100,
            title=title[:500],
            file_path=str(final_path),
            filename=final_path.name,
            file_size=final_path.stat().st_size,
            finished_at=iso_now(),
            expires_at=iso_after(settings.file_ttl_seconds),
            error_code=None,
            error_message=None,
            error_detail=None,
        )
        return 0
    except YoutubeError as exc:
        if _cancel_requested(db, job_id):
            _update(db, job_id, status="cancelled", stage="cancelled", progress=0)
            shutil.rmtree(job_dir, ignore_errors=True)
            return 0
        _update(
            db,
            job_id,
            status="failed",
            stage="failed",
            error_code=exc.code,
            error_message=exc.message,
            error_detail=exc.detail[:2000],
            finished_at=iso_now(),
        )
        shutil.rmtree(job_dir, ignore_errors=True)
        return 1
    except Exception as exc:
        if _cancel_requested(db, job_id):
            _update(db, job_id, status="cancelled", stage="cancelled", progress=0)
            shutil.rmtree(job_dir, ignore_errors=True)
            return 0
        _update(
            db,
            job_id,
            status="failed",
            stage="failed",
            error_code="worker_error",
            error_message="処理中に予期しないエラーが発生しました",
            error_detail=str(exc)[:2000],
            finished_at=iso_now(),
        )
        shutil.rmtree(job_dir, ignore_errors=True)
        return 1
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one YouTube download job")
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    raise SystemExit(run_job(args.job_id, Settings.from_env()))


if __name__ == "__main__":
    main()
