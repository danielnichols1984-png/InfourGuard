from datetime import datetime, timezone

from sqlalchemy.orm import Session

from shared.subscriptions_core.models import Payment, Plan, UserSubscription

# Deliberate cross-module import — confirming a business-tier plan needs to
# provision an organization, same precedent as tenants_core already
# importing integrations_core for shared OAuth app credentials. This module
# still works standalone for anyone not using businesses_core; the import
# only matters at the point a business-tier plan is actually confirmed.
from shared.businesses_core.db import SessionLocal as BusinessesSessionLocal
from shared.businesses_core.service import create_business, get_membership as get_business_membership

# Starter plans, inserted once into an empty `plans` table by seed_default_plans.
# Edit this list to match your own product — it only ever runs on a fresh DB.
SEED_PLANS = [
    {
        "name": "free",
        "display_name": "Free",
        "price_display": "$0/mo",
        "features": ["1 project", "Community support"],
        "is_default": True,
    },
    {
        "name": "pro",
        "display_name": "Pro",
        "price_display": "$19/mo",
        "features": ["Unlimited projects", "Priority support", "Advanced analytics"],
        "is_default": False,
    },
    {
        "name": "team",
        "display_name": "Team",
        "price_display": "$49/mo",
        "features": ["Everything in Pro", "5 team seats", "Shared workspaces"],
        "is_default": False,
        "is_business_plan": True,
    },
]


def seed_default_plans(db: Session) -> None:
    if db.query(Plan).first():
        return
    for data in SEED_PLANS:
        db.add(Plan(**data))
    db.commit()


def list_plans(db: Session) -> list[Plan]:
    return db.query(Plan).order_by(Plan.id).all()


def get_default_plan(db: Session) -> Plan | None:
    return db.query(Plan).filter(Plan.is_default.is_(True)).first()


def get_user_plan(db: Session, user_id: int) -> Plan | None:
    sub = db.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()
    if sub:
        plan = db.query(Plan).filter(Plan.id == sub.plan_id).first()
        if plan:
            return plan
    return get_default_plan(db)


def set_user_plan(db: Session, user_id: int, plan_id: int) -> UserSubscription:
    sub = db.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()
    if sub:
        sub.plan_id = plan_id
    else:
        sub = UserSubscription(user_id=user_id, plan_id=plan_id)
        db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _clear_other_defaults(db: Session, keep_plan_id: int) -> None:
    db.query(Plan).filter(Plan.id != keep_plan_id, Plan.is_default.is_(True)).update(
        {"is_default": False}
    )
    db.commit()


def create_plan(
    db: Session,
    name: str,
    display_name: str,
    price_display: str,
    features: list[str],
    is_default: bool,
    is_business_plan: bool = False,
) -> Plan:
    plan = Plan(
        name=name,
        display_name=display_name,
        price_display=price_display,
        features=features,
        is_default=is_default,
        is_business_plan=is_business_plan,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    if is_default:
        _clear_other_defaults(db, plan.id)
    return plan


def update_plan(db: Session, plan_id: int, updates: dict) -> Plan | None:
    plan = db.query(Plan).filter(Plan.id == plan_id).first()
    if not plan:
        return None
    for key, value in updates.items():
        setattr(plan, key, value)
    db.commit()
    db.refresh(plan)
    if updates.get("is_default"):
        _clear_other_defaults(db, plan.id)
    return plan


def delete_plan(db: Session, plan_id: int) -> str | None:
    """Returns None on success, or an error message describing why not."""
    plan = db.query(Plan).filter(Plan.id == plan_id).first()
    if not plan:
        return "Plan not found"

    in_use = db.query(UserSubscription).filter(UserSubscription.plan_id == plan_id).first()
    if in_use:
        return "Plan is assigned to at least one user; reassign them first"

    db.delete(plan)
    db.commit()
    return None


def create_payment(db: Session, user_id: int, plan_id: int, billing: dict) -> Payment:
    payment = Payment(user_id=user_id, plan_id=plan_id, method="cash", status="pending", **billing)
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


def list_user_payments(db: Session, user_id: int) -> list[Payment]:
    return (
        db.query(Payment)
        .filter(Payment.user_id == user_id)
        .order_by(Payment.id.desc())
        .all()
    )


def list_payments(db: Session, status: str | None = None) -> list[Payment]:
    query = db.query(Payment)
    if status:
        query = query.filter(Payment.status == status)
    return query.order_by(Payment.id.desc()).all()


def confirm_payment(db: Session, payment: Payment, admin_id: int) -> Payment:
    payment.status = "confirmed"
    payment.confirmed_at = datetime.now(timezone.utc)
    payment.confirmed_by_admin_id = admin_id
    db.commit()
    set_user_plan(db, payment.user_id, payment.plan_id)

    plan = db.query(Plan).filter(Plan.id == payment.plan_id).first()
    if plan and plan.is_business_plan:
        _provision_business_if_needed(payment)

    db.refresh(payment)
    return payment


def _provision_business_if_needed(payment: Payment) -> None:
    """A business-tier plan makes its buyer the admin of a new
    organization — but only if they aren't already part of one (e.g. a
    second business-plan purchase by an existing business admin shouldn't
    spawn a duplicate org). Own short-lived session since this module has
    no other reason to hold a businesses_core connection open."""
    businesses_db = BusinessesSessionLocal()
    try:
        if get_business_membership(businesses_db, payment.user_id):
            return
        name = payment.business_name or f"{payment.billing_email}'s Organization"
        create_business(businesses_db, name=name, admin_user_id=payment.user_id)
    finally:
        businesses_db.close()


def reject_payment(db: Session, payment: Payment, admin_id: int) -> Payment:
    payment.status = "rejected"
    payment.confirmed_at = datetime.now(timezone.utc)
    payment.confirmed_by_admin_id = admin_id
    db.commit()
    db.refresh(payment)
    return payment


def list_user_plans(users, db: Session) -> list[dict]:
    """users: iterable of objects with .id/.email (e.g. auth_core's User).

    Returns each user's current plan, defaulting to the default plan for
    anyone who hasn't explicitly switched.
    """
    default_plan = get_default_plan(db)
    plans_by_id = {p.id: p for p in list_plans(db)}
    plan_id_by_user = {
        sub.user_id: sub.plan_id for sub in db.query(UserSubscription).all()
    }

    result = []
    for user in users:
        plan_id = plan_id_by_user.get(user.id)
        plan = plans_by_id.get(plan_id) if plan_id else default_plan
        result.append({"user_id": user.id, "email": user.email, "plan": plan})
    return result
