from sqlalchemy import Column, ForeignKey, Integer, String, JSON, Boolean, DateTime, func

from shared.subscriptions_core.db import Base


class Plan(Base):
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)  # "free", "pro", "team"
    display_name = Column(String, nullable=False)  # "Free", "Pro", "Team"
    price_display = Column(String, nullable=False)  # "$0/mo" — no real billing wired up yet
    features = Column(JSON, nullable=False, default=list)
    is_default = Column(Boolean, nullable=False, default=False)


class UserSubscription(Base):
    __tablename__ = "user_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    # Plain user id, not a cross-module FK — this module only needs to know
    # which user id (from whatever auth system) a plan belongs to.
    user_id = Column(Integer, nullable=False, unique=True, index=True)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
