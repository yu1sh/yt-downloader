from __future__ import annotations

import shutil
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .auth import (
    clear_failed_logins,
    create_session,
    create_user,
    login_is_allowed,
    record_failed_login,
    remove_session,
)
from .config import Settings
from .db import connect, init_db, iso_after, iso_now, row_dict
from .security import (
    format_bytes,
    format_duration,
    generate_temporary_password,
    hash_password,
    is_path_inside,
    safe_filename,
    validate_new_password,
    validate_username,
    verify_password,
)
from .youtube import (
    YoutubeError,
    build_inspection,
    extract_video_info,
    normalize_youtube_url,
    validate_selection,
)


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
SESSION_COOKIE = "yt_session"
ACTIVE_STATUSES = {"waiting", "downloading", "processing"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted", "expired"}
STATUS_LABELS = {
    "waiting": "待機中",
    "downloading": "取得中",
    "processing": "変換中",
    "completed": "保存できます",
    "failed": "失敗",
    "cancelled": "中止しました",
    "interrupted": "中断",
    "expired": "期限切れ",
}
STAGE_LABELS = {
    "waiting": "順番を待っています",
    "downloading": "YouTubeから取得しています",
    "processing": "ファイルを整えています",
    "completed": "保存の準備ができました",
    "failed": "処理に失敗しました",
    "cancelled": "処理を中止しました",
    "interrupted": "処理が中断されました",
    "expired": "保存期限が切れました",
}
MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".opus": "audio/ogg",
}


class InspectRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class JobCreateRequest(BaseModel):
    video_id: str = Field(min_length=11, max_length=11, pattern=r"^[A-Za-z0-9_-]{11}$")
    mode: Literal["simple", "detailed"] = "simple"
    target: Literal["video", "audio"] = "audio"
    video_format: Literal["mp4", "webm"] = "mp4"
    audio_format: Literal["mp3", "m4a", "opus"] = "mp3"
    max_height: Literal[360, 480, 720, 1080, 1440, 2160] = 720
    mp3_quality: Literal[128, 192, 320] = 192


def _get_settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        settings = Settings.from_env()
        settings.ensure_paths()
    return settings


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = Settings.from_env()
    settings.ensure_paths()
    init_db(settings.db_path)
    application.state.settings = settings
    yield


app = FastAPI(
    title="そら保存",
    description="家族・知人向けYouTube動画・音声保存サイト",
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


async def get_db(request: Request):
    db = connect(_get_settings(request).db_path)
    try:
        yield db
    finally:
        db.close()


DB = Annotated[sqlite3.Connection, Depends(get_db)]


def _find_session_user(request: Request, db: sqlite3.Connection) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None, None
    row = db.execute(
        "SELECT u.*, s.token AS session_token, s.csrf_token, s.expires_at AS session_expires_at "
        "FROM sessions AS s JOIN users AS u ON u.id = s.user_id "
        "WHERE s.token = ? AND s.expires_at > ?",
        (token, iso_now()),
    ).fetchone()
    if row is None:
        return None, None
    db.execute("UPDATE sessions SET last_seen_at = ? WHERE token = ?", (iso_now(), token))
    values = dict(row)
    session = {
        "token": values.pop("session_token"),
        "csrf_token": values.pop("csrf_token"),
        "expires_at": values.pop("session_expires_at"),
    }
    return values, session


def _page_user(request: Request, db: sqlite3.Connection) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    user, session = _find_session_user(request, db)
    if user and not user["is_active"]:
        return None, None
    return user, session


def _login_redirect(request: Request) -> RedirectResponse:
    next_url = quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
    return RedirectResponse(f"/login?next={next_url}", status_code=303)


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def require_api_user(request: Request, db: DB) -> dict[str, Any]:
    user, session = _find_session_user(request, db)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "ログインしてください"},
        )
    if not user["is_active"]:
        raise HTTPException(
            status_code=403,
            detail={"code": "account_disabled", "message": "このアカウントは停止されています"},
        )
    if user["must_change_password"]:
        raise HTTPException(
            status_code=403,
            detail={"code": "password_change_required", "message": "先にパスワードを変更してください"},
        )
    request.state.session = session
    return user


USER = Annotated[dict[str, Any], Depends(require_api_user)]


def _require_csrf(request: Request, session: dict[str, Any], provided: str | None = None) -> None:
    supplied = provided or request.headers.get("X-CSRF-Token")
    if not supplied or supplied != session["csrf_token"]:
        raise HTTPException(
            status_code=403,
            detail={"code": "csrf_failed", "message": "ページを再読み込みしてから、もう一度お試しください"},
        )


def _youtube_http_error(exc: YoutubeError) -> HTTPException:
    status = 422 if exc.code in {
        "invalid_url",
        "invalid_option",
        "unsupported_option",
        "duration_limit",
        "playlist_not_supported",
        "live_not_supported",
    } else 502
    return HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})


def _job_dir(settings: Settings, job_id: str) -> Path:
    try:
        UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "処理が見つかりません"}) from exc
    path = (settings.jobs_dir / job_id).resolve()
    if not is_path_inside(path, settings.jobs_dir):
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "処理が見つかりません"})
    return path


def _remove_job_files(settings: Settings, job_id: str) -> None:
    path = _job_dir(settings, job_id)
    if path.name == job_id:
        shutil.rmtree(path, ignore_errors=True)


def _date_label(value: str | None) -> str:
    if not value:
        return "—"
    try:
        date = datetime.fromisoformat(value).astimezone()
        return date.strftime("%Y/%m/%d %H:%M")
    except ValueError:
        return value[:16].replace("T", " ")


def serialize_job(row: sqlite3.Row | dict[str, Any], settings: Settings) -> dict[str, Any]:
    job = dict(row)
    status = str(job["status"])
    job["status_label"] = STATUS_LABELS.get(status, status)
    job["stage_label"] = STAGE_LABELS.get(job.get("stage"), STAGE_LABELS.get(status, "処理中"))
    job["progress"] = max(0, min(100, int(job.get("progress") or 0)))
    job["duration_label"] = format_duration(job.get("duration"))
    job["file_size_label"] = format_bytes(job.get("file_size"))
    job["created_at_label"] = _date_label(job.get("created_at"))
    job["expires_at_label"] = _date_label(job.get("expires_at"))
    job["can_cancel"] = status in ACTIVE_STATUSES
    job["can_download"] = status == "completed" and bool(job.get("file_path"))
    job["download_url"] = f"/api/jobs/{job['id']}/download" if job["can_download"] else None
    job["message"] = job.get("error_message") or job["stage_label"]
    if job.get("thumbnail"):
        job["thumbnail"] = str(job["thumbnail"])
    return job


def _render(request: Request, template_name: str, context: dict[str, Any], *, status_code: int = 200):
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context=context,
        status_code=status_code,
    )


def _page_context(request: Request, user: dict[str, Any], session: dict[str, Any], **extra: Any) -> dict[str, Any]:
    context = {
        "request": request,
        "user": user,
        "csrf_token": session["csrf_token"],
        "status_labels": STATUS_LABELS,
    }
    context.update(extra)
    return context


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=422,
            content={"code": "invalid_request", "message": "入力内容を確認してください"},
        )
    return JSONResponse(status_code=422, content={"message": "入力内容を確認してください"})


@app.get("/healthz")
def healthz(request: Request, db: DB):
    db.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: DB, next_url: str | None = Query(default=None, alias="next")):
    user, _ = _page_user(request, db)
    if user:
        return RedirectResponse("/account/password" if user["must_change_password"] else "/", status_code=303)
    return _render(request, "login.html", {"request": request, "next_url": _safe_next(next_url)})


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    db: DB,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next_url: Annotated[str, Form(alias="next")] = "/",
):
    settings = _get_settings(request)
    client_host = request.client.host if request.client else "unknown"
    key = f"{client_host}:{username[:64].casefold()}"
    if not login_is_allowed(key, settings):
        return _render(
            request,
            "login.html",
            {"request": request, "next_url": _safe_next(next_url), "error": "試行回数が多すぎます。15分後にもう一度お試しください"},
            status_code=429,
        )
    row = db.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    valid = bool(row and row["is_active"] and verify_password(row["password_hash"], password))
    if not valid:
        record_failed_login(key, settings)
        return _render(
            request,
            "login.html",
            {"request": request, "next_url": _safe_next(next_url), "error": "ユーザー名またはパスワードが違います"},
            status_code=401,
        )
    clear_failed_logins(key)
    token, _ = create_session(db, int(row["id"]), settings)
    destination = "/account/password" if row["must_change_password"] else _safe_next(next_url)
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/logout")
def logout(request: Request, db: DB, csrf: Annotated[str | None, Form()] = None):
    _, session = _find_session_user(request, db)
    if session:
        _require_csrf(request, session, csrf)
        remove_session(db, session["token"])
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/account/password", response_class=HTMLResponse)
def password_page(request: Request, db: DB):
    user, session = _page_user(request, db)
    if user is None or session is None:
        return _login_redirect(request)
    return _render(
        request,
        "password.html",
        _page_context(request, user, session, forced=bool(user["must_change_password"])),
    )


@app.post("/account/password", response_class=HTMLResponse)
def change_password(
    request: Request,
    db: DB,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    new_password_confirm: Annotated[str, Form()],
    csrf: Annotated[str | None, Form()] = None,
):
    user, session = _page_user(request, db)
    if user is None or session is None:
        return _login_redirect(request)
    _require_csrf(request, session, csrf)
    error: str | None = None
    if not verify_password(user["password_hash"], current_password):
        error = "現在のパスワードが違います"
    elif new_password != new_password_confirm:
        error = "新しいパスワードが一致しません"
    else:
        try:
            validate_new_password(new_password)
        except ValueError as exc:
            error = str(exc)
    if error:
        return _render(
            request,
            "password.html",
            _page_context(request, user, session, forced=bool(user["must_change_password"]), error=error),
            status_code=422,
        )
    now = iso_now()
    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0, updated_at = ? WHERE id = ?",
        (hash_password(new_password), now, user["id"]),
    )
    db.execute("DELETE FROM sessions WHERE user_id = ? AND token != ?", (user["id"], session["token"]))
    return RedirectResponse("/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: DB):
    user, session = _page_user(request, db)
    if user is None or session is None:
        return _login_redirect(request)
    rows = db.execute(
        "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT 5", (user["id"],)
    ).fetchall()
    settings = _get_settings(request)
    return _render(
        request,
        "index.html",
        _page_context(
            request,
            user,
            session,
            recent_jobs=[serialize_job(row, settings) for row in rows],
        ),
    )


@app.get("/history", response_class=HTMLResponse)
def history(request: Request, db: DB):
    user, session = _page_user(request, db)
    if user is None or session is None:
        return _login_redirect(request)
    settings = _get_settings(request)
    rows = db.execute(
        "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT 50", (user["id"],)
    ).fetchall()
    return _render(
        request,
        "history.html",
        _page_context(request, user, session, jobs=[serialize_job(row, settings) for row in rows]),
    )


@app.post("/history/jobs/{job_id}/delete")
def delete_history_page(request: Request, job_id: str, db: DB, csrf: Annotated[str | None, Form()] = None):
    user, session = _page_user(request, db)
    if user is None or session is None:
        return _login_redirect(request)
    _require_csrf(request, session, csrf)
    row = db.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user["id"])).fetchone()
    if row and row["status"] not in ACTIVE_STATUSES:
        db.execute("DELETE FROM jobs WHERE id = ? AND user_id = ?", (job_id, user["id"]))
        _remove_job_files(_get_settings(request), job_id)
    return RedirectResponse("/history", status_code=303)


@app.post("/api/inspect")
def inspect_video(payload: InspectRequest, request: Request, user: USER):
    settings = _get_settings(request)
    try:
        normalized = normalize_youtube_url(payload.url)
        info = extract_video_info(normalized, settings)
        duration = info.get("duration")
        if duration is not None and int(duration) > settings.max_duration_seconds:
            raise YoutubeError("duration_limit", "動画が長すぎるため保存できません")
        return build_inspection(info, normalized)
    except YoutubeError as exc:
        raise _youtube_http_error(exc) from exc


@app.post("/api/jobs", status_code=201)
def create_job(payload: JobCreateRequest, request: Request, db: DB, user: USER):
    session = request.state.session
    _require_csrf(request, session)
    settings = _get_settings(request)
    try:
        normalized = normalize_youtube_url(f"https://www.youtube.com/watch?v={payload.video_id}")
        info = extract_video_info(normalized, settings)
        duration = info.get("duration")
        if duration is not None and int(duration) > settings.max_duration_seconds:
            raise YoutubeError("duration_limit", "動画が長すぎるため保存できません")
        validate_selection(
            info,
            target=payload.target,
            video_format=payload.video_format,
            audio_format=payload.audio_format,
            max_height=payload.max_height,
        )
    except YoutubeError as exc:
        raise _youtube_http_error(exc) from exc

    pending_statuses = tuple(ACTIVE_STATUSES)
    placeholders = ", ".join("?" for _ in pending_statuses)
    user_pending = db.execute(
        f"SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status IN ({placeholders})",
        (user["id"], *pending_statuses),
    ).fetchone()[0]
    total_pending = db.execute(
        f"SELECT COUNT(*) FROM jobs WHERE status IN ({placeholders})", pending_statuses
    ).fetchone()[0]
    if user_pending >= settings.max_pending_per_user:
        raise HTTPException(status_code=429, detail={"code": "user_queue_full", "message": "同時に待てる処理は3件までです"})
    if total_pending >= settings.max_pending_total:
        raise HTTPException(status_code=429, detail={"code": "queue_full", "message": "現在混み合っています。少し待ってからお試しください"})
    if shutil.disk_usage(settings.data_dir).free < settings.min_free_bytes:
        raise HTTPException(status_code=507, detail={"code": "storage_low", "message": "サーバーの空き容量が少ないため、現在保存できません"})

    mp3_quality = 320 if payload.mode == "simple" and payload.target == "audio" else payload.mp3_quality
    job_id = str(uuid4())
    now = iso_now()
    title = str(info.get("title") or "YouTube video")[:500]
    thumbnail = str(info.get("thumbnail") or "")[:1000]
    db.execute(
        "INSERT INTO jobs(id, user_id, video_id, source_url, title, thumbnail, duration, mode, target, "
        "video_format, audio_format, max_height, mp3_quality, status, progress, stage, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting', 0, 'waiting', ?, ?)",
        (
            job_id,
            user["id"],
            normalized.video_id,
            normalized.url,
            title,
            thumbnail,
            int(duration) if duration is not None else None,
            payload.mode,
            payload.target,
            payload.video_format,
            payload.audio_format,
            payload.max_height,
            mp3_quality,
            now,
            now,
        ),
    )
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return serialize_job(row, settings)


@app.get("/api/jobs")
def list_jobs(request: Request, db: DB, user: USER, limit: int = Query(default=20, ge=1, le=50)):
    settings = _get_settings(request)
    rows = db.execute(
        "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user["id"], limit)
    ).fetchall()
    return {"jobs": [serialize_job(row, settings) for row in rows]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request, db: DB, user: USER):
    settings = _get_settings(request)
    row = db.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "処理が見つかりません"})
    return serialize_job(row, settings)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request, db: DB, user: USER):
    _require_csrf(request, request.state.session)
    row = db.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "処理が見つかりません"})
    if row["status"] not in ACTIVE_STATUSES:
        return serialize_job(row, _get_settings(request))
    if row["status"] == "waiting":
        db.execute(
            "UPDATE jobs SET status = 'cancelled', stage = 'cancelled', progress = 0, "
            "finished_at = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (iso_now(), iso_now(), job_id, user["id"]),
        )
    else:
        db.execute(
            "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ? AND user_id = ?",
            (iso_now(), job_id, user["id"]),
        )
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return serialize_job(row, _get_settings(request))


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str, request: Request, db: DB, user: USER):
    settings = _get_settings(request)
    row = db.execute(
        "SELECT * FROM jobs WHERE id = ? AND user_id = ? AND status = 'completed'",
        (job_id, user["id"]),
    ).fetchone()
    if row is None or not row["file_path"]:
        raise HTTPException(status_code=404, detail={"code": "not_ready", "message": "保存できるファイルがありません"})
    path = Path(row["file_path"]).resolve()
    if not is_path_inside(path, settings.jobs_dir) or not path.is_file():
        raise HTTPException(status_code=404, detail={"code": "expired", "message": "ファイルの保存期限が切れています"})
    extension = path.suffix.lower()
    media_type = MEDIA_TYPES.get(extension, "application/octet-stream")
    filename = safe_filename(str(row["title"]), extension.lstrip("."))
    response = FileResponse(str(path), media_type=media_type, filename=filename)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str, request: Request, db: DB, user: USER):
    _require_csrf(request, request.state.session)
    row = db.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "処理が見つかりません"})
    if row["status"] in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail={"code": "job_active", "message": "処理中の項目は先に中止してください"})
    db.execute("DELETE FROM jobs WHERE id = ? AND user_id = ?", (job_id, user["id"]))
    _remove_job_files(_get_settings(request), job_id)
    return None


def _admin_page_auth(request: Request, db: sqlite3.Connection) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    user, session = _page_user(request, db)
    if user and user["must_change_password"]:
        return user, session
    if user and not user["is_admin"]:
        raise HTTPException(status_code=403, detail="管理者だけが利用できます")
    return user, session


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, db: DB):
    user, session = _admin_page_auth(request, db)
    if user is None or session is None:
        return _login_redirect(request)
    if user["must_change_password"]:
        return RedirectResponse("/account/password", status_code=303)
    rows = db.execute("SELECT id, username, is_admin, is_active, must_change_password, created_at FROM users ORDER BY id").fetchall()
    return _render(
        request,
        "admin_users.html",
        _page_context(request, user, session, users=[dict(row) for row in rows]),
    )


@app.post("/admin/users")
def admin_create_user(
    request: Request,
    db: DB,
    username: Annotated[str, Form()],
    initial_password: Annotated[str, Form()] = "",
    csrf: Annotated[str | None, Form()] = None,
):
    user, session = _admin_page_auth(request, db)
    if user is None or session is None:
        return _login_redirect(request)
    _require_csrf(request, session, csrf)
    if user["must_change_password"]:
        return RedirectResponse("/account/password", status_code=303)
    settings = _get_settings(request)
    try:
        clean_username = validate_username(username)
        generated_password = not bool(initial_password)
        password = initial_password or generate_temporary_password()
        validate_new_password(password)
        create_user(
            db,
            clean_username,
            password,
            must_change_password=generated_password,
        )
    except sqlite3.IntegrityError:
        generated_password = False
        password = ""
        error = "そのユーザー名はすでに使われています"
        rows = db.execute("SELECT id, username, is_admin, is_active, must_change_password, created_at FROM users ORDER BY id").fetchall()
        return _render(request, "admin_users.html", _page_context(request, user, session, users=[dict(row) for row in rows], error=error), status_code=422)
    except ValueError as exc:
        rows = db.execute("SELECT id, username, is_admin, is_active, must_change_password, created_at FROM users ORDER BY id").fetchall()
        return _render(request, "admin_users.html", _page_context(request, user, session, users=[dict(row) for row in rows], error=str(exc)), status_code=422)
    context = {"request": request, "user": user, "csrf_token": session["csrf_token"], "created_password": password if generated_password else None}
    rows = db.execute("SELECT id, username, is_admin, is_active, must_change_password, created_at FROM users ORDER BY id").fetchall()
    context["users"] = [dict(row) for row in rows]
    return _render(request, "admin_users.html", context)


@app.post("/admin/users/{user_id}/toggle")
def admin_toggle_user(request: Request, user_id: int, db: DB, csrf: Annotated[str | None, Form()] = None):
    user, session = _admin_page_auth(request, db)
    if user is None or session is None:
        return _login_redirect(request)
    _require_csrf(request, session, csrf)
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="自分のアカウントは停止できません")
    row = db.execute("SELECT is_active FROM users WHERE id = ?", (user_id,)).fetchone()
    if row:
        db.execute("UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?", (0 if row[0] else 1, iso_now(), user_id))
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{user_id}/reset")
def admin_reset_user(request: Request, user_id: int, db: DB, csrf: Annotated[str | None, Form()] = None):
    user, session = _admin_page_auth(request, db)
    if user is None or session is None:
        return _login_redirect(request)
    _require_csrf(request, session, csrf)
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="自分のパスワードはアカウント画面から変更してください")
    row = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
    password = generate_temporary_password()
    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 1, updated_at = ? WHERE id = ?",
        (hash_password(password), iso_now(), user_id),
    )
    db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    rows = db.execute("SELECT id, username, is_admin, is_active, must_change_password, created_at FROM users ORDER BY id").fetchall()
    return _render(
        request,
        "admin_users.html",
        _page_context(request, user, session, users=[dict(row) for row in rows], created_password=password),
    )


@app.get("/admin/jobs", response_class=HTMLResponse)
def admin_jobs(request: Request, db: DB):
    user, session = _admin_page_auth(request, db)
    if user is None or session is None:
        return _login_redirect(request)
    if user["must_change_password"]:
        return RedirectResponse("/account/password", status_code=303)
    rows = db.execute(
        "SELECT jobs.*, users.username FROM jobs JOIN users ON users.id = jobs.user_id "
        "ORDER BY jobs.created_at DESC LIMIT 100"
    ).fetchall()
    settings = _get_settings(request)
    return _render(
        request,
        "admin_jobs.html",
        _page_context(request, user, session, jobs=[serialize_job(row, settings) for row in rows]),
    )
