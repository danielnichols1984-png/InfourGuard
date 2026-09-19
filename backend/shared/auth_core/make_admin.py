"""One-off CLI to bootstrap the first admin account.

There's no API endpoint for this deliberately — self-serve admin promotion
would be a privilege-escalation hole. Run this once against the database
directly, from the host app's backend directory with its venv active:

    python -m shared.auth_core.make_admin someone@example.com
"""
import sys

from shared.auth_core.db import SessionLocal
from shared.auth_core.models import User


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m shared.auth_core.make_admin <email>")
        raise SystemExit(1)

    email = sys.argv[1]
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            print(f"No user found with email {email}")
            raise SystemExit(1)
        user.is_admin = True
        db.commit()
        print(f"{email} is now an admin.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
