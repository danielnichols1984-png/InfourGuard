from datetime import datetime, timezone

from sqlalchemy.orm import Session

from shared.integrations_core.models import UserIntegration


def _get_record(db: Session, user_id: int, provider: str) -> UserIntegration | None:
    return db.query(UserIntegration).filter_by(user_id=user_id, provider=provider).first()


def _get_or_create_record(db: Session, user_id: int, provider: str) -> UserIntegration:
    record = _get_record(db, user_id, provider)
    if not record:
        record = UserIntegration(user_id=user_id, provider=provider)
        db.add(record)
    return record


def save_oauth_state(
    db: Session, user_id: int, provider: str, state: str, code_verifier: str | None
) -> None:
    record = _get_or_create_record(db, user_id, provider)
    record.oauth_state = state
    record.code_verifier = code_verifier
    db.commit()


def get_oauth_state(db: Session, user_id: int, provider: str) -> str | None:
    record = _get_record(db, user_id, provider)
    return record.oauth_state if record else None


def get_code_verifier(db: Session, user_id: int, provider: str) -> str | None:
    record = _get_record(db, user_id, provider)
    return record.code_verifier if record else None


def save_tokens(
    db: Session,
    user_id: int,
    provider: str,
    access_token: str | None,
    refresh_token: str | None = None,
    expires_at: datetime | None = None,
    scope: str | None = None,
) -> UserIntegration:
    record = _get_or_create_record(db, user_id, provider)
    if access_token is not None:
        record.access_token = access_token
    if refresh_token is not None:
        record.refresh_token = refresh_token
    if expires_at is not None:
        record.expires_at = expires_at
    if scope is not None:
        record.scope = scope
    # The handshake is done once real tokens are in hand.
    record.oauth_state = None
    record.code_verifier = None
    record.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(record)
    return record


def get_tokens(db: Session, user_id: int, provider: str) -> UserIntegration | None:
    return _get_record(db, user_id, provider)


def is_connected(db: Session, user_id: int, provider: str) -> bool:
    record = _get_record(db, user_id, provider)
    return bool(record and record.access_token)


def delete_tokens(db: Session, user_id: int, provider: str) -> None:
    record = _get_record(db, user_id, provider)
    if record:
        db.delete(record)
        db.commit()
