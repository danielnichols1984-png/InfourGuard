from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint

from shared.businesses_core.db import Base


class Business(Base):
    __tablename__ = "businesses"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class BusinessMembership(Base):
    __tablename__ = "business_memberships"
    __table_args__ = (UniqueConstraint("user_id", name="uq_business_memberships_user_id"),)

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    # Plain user id, not a cross-module FK — same pattern as
    # subscriptions_core.UserSubscription.user_id. A user belongs to at
    # most one business (the unique constraint above), matching the
    # "individual vs business" framing rather than allowing multi-business
    # membership, which nothing here currently needs.
    user_id = Column(Integer, nullable=False, index=True)
    role = Column(String, nullable=False, default="member")  # "business_admin" | "member"
