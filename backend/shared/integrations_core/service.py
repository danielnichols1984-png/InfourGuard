from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from shared.integrations_core.models import IntegrationStorageSnapshot, UserIntegration


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


def record_storage_snapshot(db: Session, user_id: int, provider: str, storage_used_bytes: int) -> None:
    db.add(
        IntegrationStorageSnapshot(user_id=user_id, provider=provider, storage_used_bytes=storage_used_bytes)
    )
    db.commit()


def compute_storage_growth(db: Session, user_id: int, provider: str) -> dict:
    """No provider exposes a "growth rate" endpoint, so this compares our
    own accumulated snapshots (see record_storage_snapshot) instead — the
    latest one against whichever prior snapshot lands closest to 30 days
    before it. Returns {"available": False, ...} until there's a snapshot
    old enough to compare against, rather than fabricating a rate from too
    little history. Mirrors tenants_core.service.compute_storage_growth."""
    snapshots = (
        db.query(IntegrationStorageSnapshot)
        .filter(IntegrationStorageSnapshot.user_id == user_id, IntegrationStorageSnapshot.provider == provider)
        .order_by(IntegrationStorageSnapshot.captured_at.desc())
        .all()
    )
    if len(snapshots) < 2:
        return {
            "available": False,
            "note": "Not enough snapshot history yet to compute a growth rate — check back after this page has been viewed a few times over at least a few weeks.",
        }

    latest = snapshots[0]
    target = latest.captured_at - timedelta(days=30)
    baseline = min(snapshots[1:], key=lambda s: abs((s.captured_at - target).total_seconds()))

    if abs((baseline.captured_at - target).days) > 15:
        return {
            "available": False,
            "note": "Not enough snapshot history yet to compute a growth rate — check back after this page has been viewed a few times over at least a few weeks.",
        }

    delta_bytes = latest.storage_used_bytes - baseline.storage_used_bytes
    period_days = max((latest.captured_at - baseline.captured_at).days, 1)
    return {
        "available": True,
        "delta_bytes": delta_bytes,
        "period_days": period_days,
        "monthly_rate_bytes": round(delta_bytes * 30 / period_days),
    }
