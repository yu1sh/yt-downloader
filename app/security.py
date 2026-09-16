from __future__ import annotations

import re
import secrets
import unicodedata
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


PASSWORD_HASHER = PasswordHasher()
USERNAME_RE = re.compile(r"[^\s/\\]{3,32}", re.UNICODE)
TEMP_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


def hash_password(password: str) -> str:
    return PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def validate_username(username: str) -> str:
    normalized = unicodedata.normalize("NFKC", username).strip()
    if not USERNAME_RE.fullmatch(normalized):
        raise ValueError("ユーザー名は3〜32文字で、空白やスラッシュは使えません")
    return normalized


def validate_new_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("パスワードは12文字以上にしてください")
    if len(password) > 256:
        raise ValueError("パスワードが長すぎます")
    if any(ord(char) < 32 for char in password):
        raise ValueError("パスワードに制御文字は使えません")
    return password


def generate_temporary_password(length: int = 16) -> str:
    return "".join(secrets.choice(TEMP_PASSWORD_ALPHABET) for _ in range(length))


def generate_token(size: int = 32) -> str:
    return secrets.token_urlsafe(size)


def safe_filename(title: str, extension: str) -> str:
    normalized = unicodedata.normalize("NFKC", title or "youtube-video")
    normalized = re.sub(r"[\x00-\x1f\x7f/\\]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    normalized = normalized[:120].rstrip(" .") or "youtube-video"
    if normalized.upper() in {"CON", "PRN", "AUX", "NUL"}:
        normalized = f"{normalized}-file"
    extension = extension.lower().lstrip(".")
    if extension not in {"mp4", "webm", "mp3", "m4a", "opus"}:
        raise ValueError("unsupported file extension")
    return f"{normalized}.{extension}"


def is_path_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "時間不明"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_bytes(value: int | float | None) -> str:
    if value is None or value < 0:
        return "容量不明"
    number = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(number)} {unit}"
            return f"{number:.1f} {unit}"
        number /= 1024
    return "容量不明"

