"""Command-line account management for the archive.

There is no public sign-up — accounts are created here. Run inside the app
environment (or container):

    python -m app.manage_users add <username>
    python -m app.manage_users list
    python -m app.manage_users passwd <username>
    python -m app.manage_users delete <username>

`add` and `passwd` prompt for the password (hidden) so it never lands in shell
history.
"""
import argparse
import getpass
import sys

from sqlalchemy import select

from app.auth import hash_password
from app.database import SessionLocal
from app.models import Base, User
from app.database import engine


def _prompt_password() -> str:
    while True:
        pw = getpass.getpass("Password: ")
        if not pw:
            print("Password cannot be empty.")
            continue
        confirm = getpass.getpass("Confirm password: ")
        if pw != confirm:
            print("Passwords did not match. Try again.")
            continue
        return pw


def cmd_add(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.username == args.username)):
            print(f"User '{args.username}' already exists.", file=sys.stderr)
            return 1
        pw = _prompt_password()
        db.add(User(username=args.username, password_hash=hash_password(pw)))
        db.commit()
    print(f"User '{args.username}' created.")
    return 0


def cmd_passwd(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == args.username))
        if not user:
            print(f"User '{args.username}' not found.", file=sys.stderr)
            return 1
        user.password_hash = hash_password(_prompt_password())
        db.commit()
    print(f"Password updated for '{args.username}'.")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == args.username))
        if not user:
            print(f"User '{args.username}' not found.", file=sys.stderr)
            return 1
        db.delete(user)
        db.commit()
    print(f"User '{args.username}' deleted.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        users = db.scalars(select(User).order_by(User.username)).all()
    if not users:
        print("No users. Create one with: python -m app.manage_users add <username>")
        return 0
    for u in users:
        created = u.created_at.isoformat(sep=" ", timespec="minutes") if u.created_at else ""
        print(f"{u.username}\t{created}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Ensure the users table exists even if the web app hasn't started yet.
    Base.metadata.create_all(bind=engine)

    parser = argparse.ArgumentParser(prog="python -m app.manage_users")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Create a new user")
    p_add.add_argument("username")
    p_add.set_defaults(func=cmd_add)

    p_passwd = sub.add_parser("passwd", help="Reset a user's password")
    p_passwd.add_argument("username")
    p_passwd.set_defaults(func=cmd_passwd)

    p_delete = sub.add_parser("delete", help="Delete a user")
    p_delete.add_argument("username")
    p_delete.set_defaults(func=cmd_delete)

    p_list = sub.add_parser("list", help="List users")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
