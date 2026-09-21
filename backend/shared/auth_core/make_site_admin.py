"""One-off CLI to grant an account the Site Admin permission (edits the
marketing homepage via content_core) — deliberately separate from
make_admin.py's is_admin flag, which controls plan management and
impersonation. A user can hold either, both, or neither.

There's no API endpoint for this deliberately — self-serve promotion
would be a privilege-escalation hole. Run this once against the database
directly, from the host app's backend directory with its venv active:

    python -m shared.auth_core.make_site_admin someone@example.com
"""
import sys

from shared.auth_core.db import SessionLocal
from shared.auth_core.models import User


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m shared.auth_core.make_site_admin <email>")
        raise SystemExit(1)

    email = sys.argv[1]
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            print(f"No user found with email {email}")
            raise SystemExit(1)
        user.is_site_admin = True
        db.commit()
        print(f"{email} is now a site admin.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
