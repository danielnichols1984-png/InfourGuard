from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from shared.auth_core.db import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    is_admin = Column(Boolean, nullable=False, default=False)


class ImpersonationLog(Base):
    """Audit trail for admin impersonation — every session start/stop, so
    "who did this" is always answerable. Never deleted or editable via the
    API."""

    __tablename__ = "impersonation_log"

    id = Column(Integer, primary_key=True, index=True)
    admin_user_id = Column(Integer, nullable=False, index=True)
    target_user_id = Column(Integer, nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    ended_at = Column(DateTime(timezone=True), nullable=True)
