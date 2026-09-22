from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text, UniqueConstraint

from shared.integrations_core.crypto import EncryptedText
from shared.integrations_core.db import Base


class UserIntegration(Base):
    __tablename__ = "user_integrations"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_user_integrations_user_provider"),
    )

    id = Column(Integer, primary_key=True, index=True)
    # Plain user id, not a cross-module FK — same pattern as subscriptions_core.
    user_id = Column(Integer, nullable=False, index=True)
    provider = Column(String(20), nullable=False, index=True)

    # Encrypted at rest (see crypto.py) — these are the actual live OAuth
    # credentials, the most sensitive thing this module stores.
    access_token = Column(EncryptedText, nullable=True)
    refresh_token = Column(EncryptedText, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    # Space-separated scopes actually granted at connect time (from the
    # provider's own token response) — the ground truth for what this
    # specific token can do. Never re-derive "what scope does this token
    # have" from the host app's current config wishlist; that only reflects
    # what we'd ask for today, not what a user already consented to.
    scope = Column(Text, nullable=True)

    # Transient OAuth handshake state (CSRF token + PKCE verifier), cleared
    # once save_tokens() is called after a successful callback. Also
    # encrypted — widened from String(255) to Text (via EncryptedText)
    # since ciphertext for a full-length PKCE verifier exceeds 255 chars.
    oauth_state = Column(EncryptedText, nullable=True)
    code_verifier = Column(EncryptedText, nullable=True)

    updated_at = Column(DateTime(timezone=True), nullable=True)


class IntegrationStorageSnapshot(Base):
    """One point-in-time reading of a user's storage used for a given
    provider. No provider exposes a "growth rate" endpoint — this is our
    own accumulated history, written once per successful storage-report
    fetch, so a growth rate can be computed by comparing snapshots over
    time instead. Same shape as tenants_core.models.TenantStorageSnapshot,
    kept as a separate table since admin_user_id there means "the tenant
    admin," not "the individual account owner" here."""

    __tablename__ = "integration_storage_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    provider = Column(String(20), nullable=False, index=True)
    captured_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    storage_used_bytes = Column(BigInteger, nullable=False)
