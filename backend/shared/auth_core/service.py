from sqlalchemy.orm import Session
from shared.auth_core.models import User
from shared.auth_core.security import (
    DUMMY_HASH,
    hash_password,
    verify_password,
    create_access_token,
)

def create_user(db: Session, email: str, password: str):
    user = User(email=email, hashed_password=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user

def authenticate_user(db: Session, email: str, password: str):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        # Burn roughly the same time a real verify would take, so response
        # time doesn't reveal whether the email is registered.
        verify_password(password, DUMMY_HASH)
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user

def generate_login_token(user: User):
    return create_access_token({"sub": str(user.id)})
