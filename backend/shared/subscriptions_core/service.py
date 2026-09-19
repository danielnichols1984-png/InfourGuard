from sqlalchemy.orm import Session

from shared.subscriptions_core.models import Plan, UserSubscription

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
) -> Plan:
    plan = Plan(
        name=name,
        display_name=display_name,
        price_display=price_display,
        features=features,
        is_default=is_default,
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
