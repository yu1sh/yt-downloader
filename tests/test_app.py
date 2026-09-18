from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest
import fastapi.dependencies.utils
import fastapi.routing
from httpx import ASGITransport, AsyncClient

from app.auth import create_user
from app.cleanup import cleanup_once
from app.config import Settings
from app.db import connect, init_db, iso_now, utcnow
from app.job_runner import run_job
from app.main import app, lifespan
from app.worker import recover_active_jobs
from app.youtube import (
    YoutubeError,
    build_inspection,
    format_selector,
    normalize_youtube_url,
)


@pytest.fixture
def test_settings(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("MIN_FREE_BYTES", "0")
    monkeypatch.setenv("MAX_DURATION_SECONDS", "7200")
    settings = Settings.from_env()
    settings.ensure_paths()
    init_db(settings.db_path)
    db = connect(settings.db_path)
    create_user(db, "oishi", "correct horse battery", is_admin=True)
    db.close()
    return settings


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def async_client(test_settings, monkeypatch):
    async def direct_in_threadpool(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(fastapi.routing, "run_in_threadpool", direct_in_threadpool)
    monkeypatch.setattr(fastapi.dependencies.utils, "run_in_threadpool", direct_in_threadpool)
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            yield client


async def login(client: AsyncClient, username: str = "oishi", password: str = "correct horse battery") -> str:
    response = await client.post(
        "/login",
        data={"username": username, "password": password, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = await client.get("/")
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)
    assert match
    return match.group(1)


@pytest.mark.anyio
async def test_login_and_authenticated_page(async_client):
    response = await async_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")

    csrf = await login(async_client)
    page = await async_client.get("/")
    assert page.status_code == 200
    assert "簡単Youtubeダウンローダー" in page.text
    assert "あなた専用の保存場所" not in page.text
    assert "スマホにもパソコンにも保存できます。" not in page.text
    assert "動画を確認" in page.text
    assert '<button id="save-picker-button" class="button button-primary save-picker-button"' in page.text
    assert '<a id="download-link" class="button button-outline" href="#">この端末に保存</a>' in page.text
    assert page.text.index('id="save-picker-button"') < page.text.index('id="download-link"')
    assert "保存先を選んで保存" in page.text
    assert "MP3・320kbps" in page.text
    assert page.text.index('value="audio"') < page.text.index('value="video"')
    assert 'value="audio" checked' in page.text
    assert csrf


@pytest.mark.anyio
async def test_admin_cannot_reset_self(async_client):
    csrf = await login(async_client)
    response = await async_client.post(
        "/admin/users/1/reset",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "アカウント画面から変更" in response.text
    page = await async_client.get("/admin/users")
    assert 'action="/admin/users/1/reset"' not in page.text


@pytest.mark.anyio
async def test_invalid_url_is_rejected(async_client):
    await login(async_client)
    response = await async_client.post("/api/inspect", json={"url": "https://example.com/watch?v=abc"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_url"


@pytest.mark.anyio
async def test_api_requires_csrf_and_validates_ownership(async_client, test_settings, monkeypatch):
    csrf = await login(async_client)
    video_id = "dQw4w9WgXcQ"
    fake_info = {
        "title": "テスト動画 / title",
        "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        "duration": 120,
        "formats": [
            {"format_id": "v", "vcodec": "avc1.640028", "acodec": "none", "height": 720, "ext": "mp4", "filesize": 1000},
            {"format_id": "a", "vcodec": "none", "acodec": "mp4a.40.2", "height": None, "ext": "m4a", "filesize": 500},
        ],
    }
    monkeypatch.setattr("app.main.extract_video_info", lambda normalized, current_settings: fake_info)

    missing_csrf = await async_client.post("/api/jobs", json={"video_id": video_id})
    assert missing_csrf.status_code == 403

    response = await async_client.post(
        "/api/jobs",
        headers={"X-CSRF-Token": csrf},
        json={"video_id": video_id, "target": "video", "max_height": 720},
    )
    assert response.status_code == 201
    job = response.json()
    assert job["status"] == "waiting"
    assert job["title"] == fake_info["title"]

    listed = (await async_client.get("/api/jobs")).json()["jobs"]
    assert [item["id"] for item in listed] == [job["id"]]
    assert test_settings.db_path.exists()


@pytest.mark.anyio
async def test_simple_audio_always_uses_320_kbps(async_client, monkeypatch):
    fake_info = {
        "title": "音声設定テスト",
        "thumbnail": "",
        "duration": 12,
        "formats": [
            {"format_id": "v", "vcodec": "avc1.640028", "acodec": "none", "height": 720, "ext": "mp4", "filesize": 1000},
            {"format_id": "a", "vcodec": "none", "acodec": "mp4a.40.2", "height": None, "ext": "m4a", "filesize": 500},
        ],
    }
    monkeypatch.setattr("app.main.extract_video_info", lambda normalized, current_settings: fake_info)
    csrf = await login(async_client)

    response = await async_client.post(
        "/api/jobs",
        headers={"X-CSRF-Token": csrf},
        json={
            "video_id": "dQw4w9WgXcQ",
            "mode": "simple",
            "target": "audio",
            "audio_format": "mp3",
            "mp3_quality": 128,
        },
    )

    assert response.status_code == 201
    assert response.json()["mp3_quality"] == 320


@pytest.mark.anyio
async def test_job_runner_completes_and_sanitizes_filename(async_client, test_settings, monkeypatch):
    csrf = await login(async_client)
    fake_info = {
        "title": "家族の動画 / 春休み",
        "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        "duration": 120,
        "formats": [
            {"format_id": "v", "vcodec": "avc1.640028", "acodec": "none", "height": 720, "ext": "mp4"},
            {"format_id": "a", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a"},
        ],
    }
    monkeypatch.setattr("app.main.extract_video_info", lambda normalized, current_settings: fake_info)
    monkeypatch.setattr("app.job_runner.extract_video_info", lambda normalized, current_settings: fake_info)

    response = await async_client.post(
        "/api/jobs",
        headers={"X-CSRF-Token": csrf},
        json={"video_id": "dQw4w9WgXcQ", "target": "video", "max_height": 720},
    )
    assert response.status_code == 201
    job_id = response.json()["id"]

    def fake_extract(url, current_settings, *, download=False, **options):
        assert download is True
        output = current_settings.jobs_dir / job_id / "source.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"test mp4")
        return fake_info

    monkeypatch.setattr("app.youtube._extract", fake_extract)
    assert run_job(job_id, test_settings) == 0

    db = connect(test_settings.db_path)
    row = db.execute("SELECT status, filename, file_path FROM jobs WHERE id = ?", (job_id,)).fetchone()
    db.close()
    assert row["status"] == "completed"
    assert row["filename"] == "家族の動画 春休み.mp4"
    assert Path(row["file_path"]).is_file()


def test_worker_recovery_marks_active_jobs_interrupted(test_settings):
    db = connect(test_settings.db_path)
    db.execute(
        "INSERT INTO jobs(id, user_id, video_id, source_url, title, mode, target, video_format, audio_format, "
        "max_height, mp3_quality, status, progress, stage, created_at, updated_at) "
        "VALUES (?, 1, ?, ?, 'video', 'simple', 'video', 'mp4', 'mp3', 720, 192, 'downloading', 40, 'downloading', ?, ?)",
        (
            "9d8c6c2a-0f65-4d75-9a28-11d71b8f30d1",
            "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            iso_now(),
            iso_now(),
        ),
    )
    db.close()

    recover_active_jobs(test_settings)

    db = connect(test_settings.db_path)
    row = db.execute("SELECT status, error_code FROM jobs WHERE id = ?", ("9d8c6c2a-0f65-4d75-9a28-11d71b8f30d1",)).fetchone()
    db.close()
    assert row["status"] == "interrupted"
    assert row["error_code"] == "worker_restarted"


def test_url_normalization_and_restrictions():
    normalized = normalize_youtube_url("https://youtu.be/dQw4w9WgXcQ?list=ignored")
    assert normalized.video_id == "dQw4w9WgXcQ"
    assert normalized.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert normalize_youtube_url("https://www.youtube.com/shorts/dQw4w9WgXcQ").video_id == "dQw4w9WgXcQ"
    with pytest.raises(YoutubeError):
        normalize_youtube_url("https://www.youtube.com/playlist?list=PL1234567890")


def test_inspection_has_only_available_options():
    info = {
        "title": "title",
        "duration": 61,
        "formats": [
            {"vcodec": "avc1", "acodec": "none", "height": 360, "ext": "mp4", "filesize": 100},
            {"vcodec": "none", "acodec": "opus", "ext": "webm", "filesize": 20},
        ],
    }
    normalized = normalize_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    result = build_inspection(info, normalized)
    assert [item["height"] for item in result["video_options"]] == [360]
    assert result["audio_formats"] == {"mp3": True, "m4a": False, "opus": True}
    assert result["audio_options"][0]["estimated_size_label"] == "音質で容量が変わります"
    assert result["audio_options"][1]["estimated_size_label"] == "20 B"


def test_format_selector_keeps_height_cap():
    selector = format_selector(target="video", video_format="mp4", audio_format="mp3", max_height=720)
    assert "height<=720" in selector
    assert format_selector(target="audio", video_format="mp4", audio_format="mp3", max_height=720) == "ba/b"


def test_cleanup_expires_completed_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings = Settings.from_env()
    settings.ensure_paths()
    init_db(settings.db_path)
    db = connect(settings.db_path)
    create_user(db, "owner", "correct horse battery")
    owner = db.execute("SELECT id FROM users WHERE username = 'owner'").fetchone()[0]
    job_id = "9d8c6c2a-0f65-4d75-9a28-11d71b8f30d1"
    job_dir = settings.jobs_dir / job_id
    job_dir.mkdir()
    result_file = job_dir / "video.mp4"
    result_file.write_bytes(b"video")
    old_time = (utcnow() - timedelta(seconds=10)).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO jobs(id, user_id, video_id, source_url, title, mode, target, video_format, audio_format, "
        "max_height, mp3_quality, status, progress, stage, file_path, filename, file_size, created_at, updated_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, 'simple', 'video', 'mp4', 'mp3', 720, 192, 'completed', 100, 'completed', ?, ?, 5, ?, ?, ?)",
        (job_id, owner, "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "video", str(result_file), "video.mp4", old_time, old_time, old_time),
    )
    db.close()
    result = cleanup_once(settings)
    assert result["expired_files"] == 1
    assert not result_file.exists()
    db = connect(settings.db_path)
    row = db.execute("SELECT status, file_path FROM jobs WHERE id = ?", (job_id,)).fetchone()
    db.close()
    assert row["status"] == "expired"
    assert row["file_path"] is None
