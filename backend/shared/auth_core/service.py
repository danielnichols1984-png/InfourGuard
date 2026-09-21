from datetime import datetime, timezone

from sqlalchemy.orm import Session
from shared.auth_core.models import ImpersonationLog, User
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


def list_users(db: Session) -> list[User]:
    return db.query(User).order_by(User.id).all()


def delete_user(db: Session, user: User) -> None:
    db.delete(user)
    db.commit()


def set_admin_status(db: Session, user: User, is_admin: bool) -> User:
    user.is_admin = is_admin
    db.commit()
    db.refresh(user)
    return user


def set_user_password(db: Session, user: User, new_password: str) -> User:
    user.hashed_password = hash_password(new_password)
    db.commit()
    db.refresh(user)
    return user


def start_impersonation_log(db: Session, admin_id: int, target_id: int) -> ImpersonationLog:
    log = ImpersonationLog(admin_user_id=admin_id, target_user_id=target_id)
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def end_impersonation_log(db: Session, admin_id: int, target_id: int) -> None:
    log = (
        db.query(ImpersonationLog)
        .filter(
            ImpersonationLog.admin_user_id == admin_id,
            ImpersonationLog.target_user_id == target_id,
            ImpersonationLog.ended_at.is_(None),
        )
        .order_by(ImpersonationLog.id.desc())
        .first()
    )
    if log:
        log.ended_at = datetime.now(timezone.utc)
        db.commit()

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
