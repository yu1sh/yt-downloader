from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .config import Settings
from .security import format_bytes, format_duration


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
STANDARD_HEIGHTS = (360, 480, 720, 1080, 1440, 2160)


class YoutubeError(Exception):
    def __init__(self, code: str, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class NormalizedUrl:
    video_id: str
    url: str


def normalize_youtube_url(raw_url: str) -> NormalizedUrl:
    if not isinstance(raw_url, str) or len(raw_url.strip()) > 2048:
        raise YoutubeError("invalid_url", "YouTubeの動画リンクを入力してください")
    value = raw_url.strip()
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise YoutubeError("invalid_url", "リンクの形式を確認してください") from exc
    if parsed.scheme.lower() not in {"http", "https"} or host not in YOUTUBE_HOSTS:
        raise YoutubeError("invalid_url", "YouTubeの動画リンクを入力してください")
    try:
        port = parsed.port
    except ValueError as exc:
        raise YoutubeError("invalid_url", "このリンク形式には対応していません") from exc
    if parsed.username or parsed.password or port:
        raise YoutubeError("invalid_url", "このリンク形式には対応していません")

    video_id = ""
    if host == "youtu.be":
        parts = [part for part in parsed.path.split("/") if part]
        if parts:
            video_id = parts[0]
    elif parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    else:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 2 and parts[0] in {"shorts", "embed"}:
            video_id = parts[1]

    if not VIDEO_ID_RE.fullmatch(video_id):
        raise YoutubeError("invalid_url", "動画1本分のYouTubeリンクを入力してください")
    return NormalizedUrl(video_id=video_id, url=f"https://www.youtube.com/watch?v={video_id}")


def _yt_dlp_options(settings: Settings) -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": settings.inspect_timeout_seconds,
        "js_runtimes": {"deno": {}},
    }


def _extract(url: str, settings: Settings, *, download: bool = False, **options: Any) -> dict[str, Any]:
    try:
        import yt_dlp
    except ImportError as exc:
        raise YoutubeError(
            "dependency_missing",
            "ダウンロード機能の準備が整っていません。管理者に連絡してください",
        ) from exc

    params = _yt_dlp_options(settings)
    params.update(options)
    try:
        with yt_dlp.YoutubeDL(params) as ydl:
            info = ydl.extract_info(url, download=download)
    except Exception as exc:  # yt-dlp exposes several extractor-specific exception types.
        detail = str(exc).strip()[:2000]
        lowered = detail.lower()
        if "private video" in lowered or "非公開" in detail:
            message = "この動画は非公開のため保存できません"
            code = "private_video"
        elif "sign in" in lowered or "login" in lowered:
            message = "この動画はログインが必要なため保存できません"
            code = "login_required"
        elif "not available" in lowered or "video unavailable" in lowered:
            message = "この動画は現在利用できません"
            code = "unavailable"
        elif "429" in lowered or "too many requests" in lowered:
            message = "アクセスが集中しているため、少し時間を置いて再試行してください"
            code = "rate_limited"
        else:
            message = "YouTubeから動画情報を取得できませんでした"
            code = "extract_failed"
        raise YoutubeError(code, message, detail) from exc

    if not isinstance(info, dict) or info.get("_type") == "playlist":
        raise YoutubeError("playlist_not_supported", "プレイリストではなく動画1本のリンクを指定してください")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise YoutubeError("live_not_supported", "ライブ配信の保存には対応していません")
    return info


def extract_video_info(normalized: NormalizedUrl, settings: Settings) -> dict[str, Any]:
    return _extract(normalized.url, settings)


def _number(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _format_size(item: dict[str, Any]) -> int | None:
    return _number(item.get("filesize") or item.get("filesize_approx"))


def _has_video(item: dict[str, Any]) -> bool:
    return bool(item.get("vcodec")) and item.get("vcodec") != "none"


def _has_audio(item: dict[str, Any]) -> bool:
    return bool(item.get("acodec")) and item.get("acodec") != "none"


def _audio_formats(info: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in info.get("formats", [])
        if isinstance(item, dict) and _has_audio(item) and not _has_video(item)
    ]


def _video_heights(info: dict[str, Any]) -> list[int]:
    heights = {
        height
        for item in info.get("formats", [])
        if isinstance(item, dict)
        and _has_video(item)
        and (height := _number(item.get("height"))) is not None
        and 0 < height <= 2160
    }
    return sorted(heights)


def available_height_caps(info: dict[str, Any]) -> list[int]:
    heights = _video_heights(info)
    if not heights:
        return []
    maximum = max(heights)
    return [
        cap
        for cap in STANDARD_HEIGHTS
        if cap <= maximum and any(height <= cap for height in heights)
    ]


def available_audio_formats(info: dict[str, Any]) -> dict[str, bool]:
    formats = _audio_formats(info)
    return {
        "mp3": bool(formats) or any(
            isinstance(item, dict) and _has_audio(item) for item in info.get("formats", [])
        ),
        "m4a": any(
            str(item.get("ext", "")).lower() in {"m4a", "mp4"} for item in formats
        ),
        "opus": any(
            "opus" in str(item.get("acodec", "")).lower() for item in formats
        ),
    }


def _thumbnail(info: dict[str, Any]) -> str:
    value = str(info.get("thumbnail") or "")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return ""
    if parsed.scheme == "https" and (host.endswith(".ytimg.com") or host == "ytimg.com"):
        return value
    return ""


def build_inspection(info: dict[str, Any], normalized: NormalizedUrl) -> dict[str, Any]:
    duration = _number(info.get("duration"))
    formats = info.get("formats", [])
    video_options: list[dict[str, Any]] = []
    for height in available_height_caps(info):
        candidates = [
            item
            for item in formats
            if isinstance(item, dict)
            and _has_video(item)
            and (_number(item.get("height")) or 0) <= height
        ]
        sizes = [_format_size(item) for item in candidates]
        sizes = [size for size in sizes if size is not None]
        video_options.append(
            {
                "height": height,
                "label": f"最大{height}p",
                "estimated_size": max(sizes) if sizes else None,
                "estimated_size_label": format_bytes(max(sizes)) if sizes else "容量不明",
            }
        )
    audio_formats = available_audio_formats(info)
    audio_options: list[dict[str, Any]] = []
    for audio_format, label in (
        ("mp3", "MP3（幅広い機器で再生）"),
        ("m4a", "M4A（再圧縮なし）"),
        ("opus", "Opus（再圧縮なし）"),
    ):
        if not audio_formats[audio_format]:
            continue
        source_formats = [item for item in _audio_formats(info) if isinstance(item, dict)]
        if audio_format == "m4a":
            source_formats = [
                item for item in source_formats
                if str(item.get("ext", "")).lower() in {"m4a", "mp4"}
            ]
        elif audio_format == "opus":
            source_formats = [
                item for item in source_formats
                if "opus" in str(item.get("acodec", "")).lower()
            ]
        sizes = [_format_size(item) for item in source_formats]
        sizes = [size for size in sizes if size is not None]
        if audio_format == "mp3":
            size_label = "音質で容量が変わります"
            estimated_size = None
        else:
            estimated_size = max(sizes) if sizes else None
            size_label = format_bytes(estimated_size) if estimated_size is not None else "容量不明"
        audio_options.append(
            {
                "format": audio_format,
                "label": label,
                "estimated_size": estimated_size,
                "estimated_size_label": size_label,
            }
        )
    return {
        "video_id": normalized.video_id,
        "canonical_url": normalized.url,
        "title": str(info.get("title") or "YouTube video")[:500],
        "uploader": str(info.get("uploader") or info.get("channel") or "")[:200],
        "thumbnail": _thumbnail(info),
        "duration": duration,
        "duration_label": format_duration(duration),
        "video_options": video_options,
        "audio_formats": audio_formats,
        "audio_options": audio_options,
        "audio_format_labels": {
            "mp3": "MP3（幅広い機器で再生）",
            "m4a": "M4A（再圧縮なし）",
            "opus": "Opus（再圧縮なし）",
        },
    }


def validate_selection(
    info: dict[str, Any],
    *,
    target: str,
    video_format: str,
    audio_format: str,
    max_height: int | None,
) -> None:
    if target not in {"video", "audio"}:
        raise YoutubeError("invalid_option", "保存する種類を選んでください")
    if target == "video":
        if video_format not in {"mp4", "webm"}:
            raise YoutubeError("invalid_option", "動画形式を選び直してください")
        if max_height not in available_height_caps(info):
            raise YoutubeError("unsupported_option", "指定した画質はこの動画では利用できません")
        if not any(
            isinstance(item, dict) and _has_audio(item)
            for item in info.get("formats", [])
        ):
            raise YoutubeError("unsupported_option", "映像と音声をそろえて取得できない動画です")
        return
    formats = available_audio_formats(info)
    if audio_format not in formats or not formats[audio_format]:
        raise YoutubeError("unsupported_option", "指定した音声形式はこの動画では利用できません")


def format_selector(*, target: str, video_format: str, audio_format: str, max_height: int) -> str:
    if target == "video" and video_format == "mp4":
        return (
            f"bv*[height<={max_height}][ext=mp4][vcodec^=avc1]+ba[ext=m4a]/"
            f"bv*[height<={max_height}][vcodec^=avc1]+ba/"
            f"b[height<={max_height}][ext=mp4]/b[height<={max_height}]"
        )
    if target == "video":
        return (
            f"bv*[height<={max_height}][ext=webm]+ba[ext=webm]/"
            f"bv*[height<={max_height}]+ba/b[height<={max_height}]"
        )
    if audio_format == "m4a":
        return "ba[ext=m4a]/ba[ext=mp4]"
    if audio_format == "opus":
        return "ba[acodec^=opus]"
    return "ba/b"


def expected_extension(target: str, video_format: str, audio_format: str) -> str:
    return video_format if target == "video" else audio_format
