from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from shared.integrations_core.email_utils import send_email
from shared.tenants_core.models import TenantConnection, TenantStorageSnapshot


def _get_record(db: Session, admin_user_id: int, provider: str) -> TenantConnection | None:
    return db.query(TenantConnection).filter_by(admin_user_id=admin_user_id, provider=provider).first()


def _get_or_create_record(db: Session, admin_user_id: int, provider: str) -> TenantConnection:
    record = _get_record(db, admin_user_id, provider)
    if not record:
        record = TenantConnection(admin_user_id=admin_user_id, provider=provider)
        db.add(record)
    return record


def save_oauth_state(
    db: Session, admin_user_id: int, provider: str, state: str, code_verifier: str | None
) -> None:
    record = _get_or_create_record(db, admin_user_id, provider)
    record.oauth_state = state
    record.code_verifier = code_verifier
    db.commit()


def get_oauth_state(db: Session, admin_user_id: int, provider: str) -> str | None:
    record = _get_record(db, admin_user_id, provider)
    return record.oauth_state if record else None


def get_code_verifier(db: Session, admin_user_id: int, provider: str) -> str | None:
    record = _get_record(db, admin_user_id, provider)
    return record.code_verifier if record else None


def save_tokens(
    db: Session,
    admin_user_id: int,
    provider: str,
    access_token: str | None,
    refresh_token: str | None = None,
    expires_at: datetime | None = None,
    scope: str | None = None,
    tenant_domain: str | None = None,
    tenant_name: str | None = None,
) -> TenantConnection:
    record = _get_or_create_record(db, admin_user_id, provider)
    if access_token is not None:
        record.access_token = access_token
    if refresh_token is not None:
        record.refresh_token = refresh_token
    if expires_at is not None:
        record.expires_at = expires_at
    if scope is not None:
        record.scope = scope
    if tenant_domain is not None:
        record.tenant_domain = tenant_domain
    if tenant_name is not None:
        record.tenant_name = tenant_name
    record.oauth_state = None
    record.code_verifier = None
    record.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(record)
    return record


def get_tokens(db: Session, admin_user_id: int, provider: str) -> TenantConnection | None:
    return _get_record(db, admin_user_id, provider)


def is_connected(db: Session, admin_user_id: int, provider: str) -> bool:
    record = _get_record(db, admin_user_id, provider)
    return bool(record and record.access_token)


def delete_tokens(db: Session, admin_user_id: int, provider: str) -> None:
    record = _get_record(db, admin_user_id, provider)
    if record:
        db.delete(record)
        db.commit()


def render_tenant_report_text(provider_label: str, data: dict) -> str:
    """Plain-text rendering of a tenant report dict, shared across all
    three providers instead of each having its own near-duplicate
    renderer — mirrors integrations_core's render_report_text."""
    if not data.get("connected"):
        return f"{provider_label} tenant report\n\nNot connected: {data.get('error', 'unknown reason')}"

    if data.get("error"):
        return f"{provider_label} tenant report\n\nError: {data['error']}"

    lines = [f"{provider_label} tenant report", ""]
    if data.get("tenant_name"):
        lines.append(f"Organization: {data['tenant_name']}")
    if data.get("tenant_domain"):
        lines.append(f"Domain: {data['tenant_domain']}")
    if data.get("total_users") is not None:
        lines.append(f"Total users: {data['total_users']}")
    if data.get("storage_used"):
        total = f" / {data['storage_total']}" if data.get("storage_total") else ""
        percent = f" ({data['storage_percent']}%)" if data.get("storage_percent") is not None else ""
        lines.append(f"Storage used: {data['storage_used']}{total}{percent}")
    if data.get("external_sharing_note"):
        lines.append(f"\nNote: {data['external_sharing_note']}")
    for warning in data.get("warnings") or []:
        lines.append(f"Warning: {warning}")
    return "\n".join(lines)


def send_tenant_report_email(to_email: str, provider_label: str, text: str) -> bool:
    """Reuses integrations_core's email sending — same SMTP settings this
    module already depends on (see README: tenants_core reads
    integrations_core's config for Google/Dropbox OAuth reuse)."""
    return send_email(to_email, f"{provider_label} tenant report", text)


def record_storage_snapshot(db: Session, admin_user_id: int, provider: str, storage_used_bytes: int) -> None:
    db.add(
        TenantStorageSnapshot(
            admin_user_id=admin_user_id, provider=provider, storage_used_bytes=storage_used_bytes
        )
    )
    db.commit()


def compute_storage_growth(db: Session, admin_user_id: int, provider: str) -> dict:
    """Graph has no "growth rate" endpoint, so this compares our own
    accumulated snapshots (see record_storage_snapshot) instead — the
    latest one against whichever prior snapshot lands closest to 30 days
    before it. Returns {"available": False, ...} until there's a
    snapshot old enough to compare against, rather than fabricating a
    rate from too little history."""
    snapshots = (
        db.query(TenantStorageSnapshot)
        .filter(TenantStorageSnapshot.admin_user_id == admin_user_id, TenantStorageSnapshot.provider == provider)
        .order_by(TenantStorageSnapshot.captured_at.desc())
        .all()
    )
    if len(snapshots) < 2:
        return {
            "available": False,
            "note": "Not enough snapshot history yet to compute a growth rate — check back after this report has been run a few times over at least a few weeks.",
        }

    latest = snapshots[0]
    target = latest.captured_at - timedelta(days=30)
    baseline = min(snapshots[1:], key=lambda s: abs((s.captured_at - target).total_seconds()))

    if abs((baseline.captured_at - target).days) > 15:
        return {
            "available": False,
            "note": "Not enough snapshot history yet to compute a growth rate — check back after this report has been run a few times over at least a few weeks.",
        }

    delta_bytes = latest.storage_used_bytes - baseline.storage_used_bytes
    period_days = max((latest.captured_at - baseline.captured_at).days, 1)
    return {
        "available": True,
        "delta_bytes": delta_bytes,
        "period_days": period_days,
        "monthly_rate_bytes": round(delta_bytes * 30 / period_days),
    }
