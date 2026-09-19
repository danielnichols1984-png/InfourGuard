from datetime import datetime, timedelta
from passlib.context import CryptContext
import jwt

from shared.auth_core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Computed once at import so a lookup for a nonexistent email can still run
# a verify against *something* — keeps login response time independent of
# whether the email exists, closing a timing side-channel for enumeration.
DUMMY_HASH = pwd_context.hash("not-a-real-password-000000")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def create_access_token(data: dict, expires_minutes: int = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(
        minutes=expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

def decode_access_token(token: str) -> dict:
    """Verify signature/expiry and return the token's claims.

    No database access here, so any service holding the same
    AUTH_SECRET_KEY/AUTH_ALGORITHM can call this to trust a token issued
    by this module without depending on auth_core's DB layer.
    """
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
