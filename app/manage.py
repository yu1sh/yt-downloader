from __future__ import annotations

import argparse
import getpass
import sys

from .auth import create_user
from .config import Settings
from .db import connect, init_db
from .security import generate_temporary_password, validate_new_password, validate_username


def create_admin(settings: Settings, username: str, password: str | None) -> str:
    username = validate_username(username)
    temporary = False
    if password:
        password = validate_new_password(password)
    else:
        password = generate_temporary_password()
        temporary = True
    init_db(settings.db_path)
    db = connect(settings.db_path)
    try:
        create_user(db, username, password, is_admin=True, must_change_password=temporary)
    finally:
        db.close()
    return password if temporary else ""


def main() -> None:
    parser = argparse.ArgumentParser(description="yt-downloader administration commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create-admin", help="create the first administrator")
    create_parser.add_argument("username")
    create_parser.add_argument("--password", help="use a password directly; omit to generate one")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command == "create-admin":
        password = args.password
        if password is None and sys.stdin.isatty():
            entered = getpass.getpass("新しい管理者パスワード（12文字以上、空欄で自動生成）: ")
            password = entered or None
        generated = create_admin(settings, args.username, password)
        if generated:
            print(f"仮パスワード: {generated}")
            print("ログイン後に必ずパスワードを変更してください。")
        else:
            print("管理者を作成しました。")


if __name__ == "__main__":
    main()

