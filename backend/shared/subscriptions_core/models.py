from datetime import datetime, timezone

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
    # When true, a user confirmed onto this plan gets an organization
    # auto-provisioned for them (they become its business admin) — see
    # confirm_payment(). Free/individual plans leave this false.
    is_business_plan = Column(Boolean, nullable=False, default=False)


class UserSubscription(Base):
    __tablename__ = "user_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    # Plain user id, not a cross-module FK — this module only needs to know
    # which user id (from whatever auth system) a plan belongs to.
    user_id = Column(Integer, nullable=False, unique=True, index=True)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Payment(Base):
    """A checkout attempt for a paid plan. `method` is a plain string
    ("cash" today) rather than an enum so adding "card" later, once a real
    gateway is wired up, needs no migration. `status` starts "pending" and
    only a platform admin confirming/rejecting it changes that — the plan
    itself isn't switched until confirmed (see subscriptions_core README)."""

    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    method = Column(String, nullable=False, default="cash")
    status = Column(String, nullable=False, default="pending")  # pending | confirmed | rejected

    billing_name = Column(String, nullable=False)
    billing_email = Column(String, nullable=False)
    billing_phone = Column(String, nullable=True)
    billing_address_line1 = Column(String, nullable=False)
    billing_address_line2 = Column(String, nullable=True)
    billing_city = Column(String, nullable=False)
    billing_state = Column(String, nullable=True)
    billing_zip = Column(String, nullable=True)
    billing_country = Column(String, nullable=False)
    company_name = Column(String, nullable=True)
    notes = Column(String, nullable=True)
    # Optional org name for a business-plan checkout — captured here since
    # the plan doesn't actually take effect (and the organization doesn't
    # get created) until an admin confirms the payment.
    business_name = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    confirmed_by_admin_id = Column(Integer, nullable=True)
