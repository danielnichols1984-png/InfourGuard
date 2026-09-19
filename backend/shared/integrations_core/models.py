from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint

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

    access_token = Column(Text, nullable=True)
    refresh_token = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    # Transient OAuth handshake state (CSRF token + PKCE verifier), cleared
    # once save_tokens() is called after a successful callback.
    oauth_state = Column(String(255), nullable=True)
    code_verifier = Column(String(255), nullable=True)

    updated_at = Column(DateTime(timezone=True), nullable=True)
