from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text, UniqueConstraint

from shared.tenants_core.crypto import EncryptedText
from shared.tenants_core.db import Base


class TenantConnection(Base):
    """One organization-wide admin connection: our user (the admin who
    authorized it) + provider + the tokens for calling that provider's
    tenant-level admin/reporting APIs. Separate from integrations_core's
    UserIntegration, which is a per-user, personal-account connection with
    a much narrower scope."""

    __tablename__ = "tenant_connections"
    __table_args__ = (
        UniqueConstraint("admin_user_id", "provider", name="uq_tenant_connections_admin_provider"),
    )

    id = Column(Integer, primary_key=True, index=True)
    admin_user_id = Column(Integer, nullable=False, index=True)
    # "google_workspace" | "microsoft365" | "dropbox_business"
    provider = Column(String(30), nullable=False, index=True)

    # Encrypted at rest (see crypto.py) — these are the actual live OAuth
    # credentials, the most sensitive thing this module stores.
    access_token = Column(EncryptedText, nullable=True)
    refresh_token = Column(EncryptedText, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    scope = Column(Text, nullable=True)

    tenant_domain = Column(String(255), nullable=True)
    tenant_name = Column(String(255), nullable=True)

    # Transient OAuth handshake state, also encrypted — widened from
    # String(255) to Text (via EncryptedText) since ciphertext for a
    # full-length PKCE verifier exceeds 255 chars.
    oauth_state = Column(EncryptedText, nullable=True)
    code_verifier = Column(EncryptedText, nullable=True)

    updated_at = Column(DateTime(timezone=True), nullable=True)


class TenantStorageSnapshot(Base):
    """One point-in-time reading of total tenant storage used. Graph has
    no "growth rate" endpoint — this is our own accumulated history,
    written once per successful security-report fetch, so a growth rate
    can be computed by comparing snapshots over time instead."""

    __tablename__ = "tenant_storage_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    admin_user_id = Column(Integer, nullable=False, index=True)
    provider = Column(String(30), nullable=False, index=True)
    captured_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    storage_used_bytes = Column(BigInteger, nullable=False)
