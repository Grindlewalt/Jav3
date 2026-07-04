"""Admin CLI: python -m backend.cli create-user <username>"""
import asyncio
import getpass
import sys

from .auth import hash_password
from .db import get_db, init_db


async def create_user(username: str, password: str) -> None:
    await init_db()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?) "
            "ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash",
            (username, hash_password(password)),
        )
        await db.commit()
    finally:
        await db.close()
    print(f"user '{username}' created/updated")


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "create-user":
        username = sys.argv[2]
        password = sys.argv[3] if len(sys.argv) > 3 else getpass.getpass("password: ")
        asyncio.run(create_user(username, password))
    else:
        print("usage: python -m backend.cli create-user <username> [password]")
        sys.exit(1)


if __name__ == "__main__":
    main()
