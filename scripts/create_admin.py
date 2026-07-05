"""Bootstrap the first admin user.

Run once to create an admin (the only role that can invite team members):

    .venv/bin/python -m scripts.create_admin admin@firm.com 'StrongPass123'

Idempotent: if the email already exists it just resets the password +
ensures the role is admin and status active.
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from app.core.db import SyncSessionLocal
from app.core.security import hash_password
from app.modules.auth.models import User


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python -m scripts.create_admin <email> <password> [full_name]")
        sys.exit(1)
    email = sys.argv[1].strip().lower()
    password = sys.argv[2]
    full_name = sys.argv[3].strip() if len(sys.argv) > 3 else None

    with SyncSessionLocal() as db:
        user = db.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()
        if user is None:
            user = User(
                email=email,
                full_name=full_name,
                role="admin",
                status="active",
                password_hash=hash_password(password),
            )
            db.add(user)
            print(f"Created admin: {email}")
        else:
            user.role = "admin"
            user.status = "active"
            user.password_hash = hash_password(password)
            if full_name:
                user.full_name = full_name
            print(f"Updated existing user to admin: {email}")
        db.commit()
        print(f"  id={user.id}  role={user.role}  status={user.status}")


if __name__ == "__main__":
    main()
