from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
import jwt

from shared.auth_core.config import settings
from shared.auth_core.db import get_db
from shared.auth_core.models import User
from shared.auth_core.security import decode_access_token


def get_token_payload(request: Request) -> dict:
    """Verify the token from the cookie and return its claims.

    Doesn't touch the database, so other services that only need to know
    who the caller is (and share AUTH_SECRET_KEY) can depend on this
    directly instead of pulling in the DB-backed get_current_user.
    """
    token = request.cookies.get(settings.COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        return decode_access_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_current_user(
    payload: dict = Depends(get_token_payload),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == payload.get("sub")).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def get_optional_user(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(settings.COOKIE_NAME)
    if not token:
        return None

    try:
        payload = decode_access_token(token)
        return db.query(User).filter(User.id == payload.get("sub")).first()
    except jwt.PyJWTError:
        return None