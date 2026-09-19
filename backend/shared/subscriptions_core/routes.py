from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from shared.auth_core.db import get_db as get_auth_db
from shared.auth_core.dependencies import get_current_user, require_admin
from shared.auth_core.models import User as AuthUser
from shared.subscriptions_core.db import get_db
from shared.subscriptions_core.models import Plan
from shared.subscriptions_core.schemas import (
    AdminSetUserPlanRequest,
    AdminUserPlanResponse,
    ChangePlanRequest,
    PlanCreate,
    PlanResponse,
    PlanUpdate,
    SubscriptionResponse,
)
from shared.subscriptions_core.service import (
    create_plan,
    delete_plan,
    get_user_plan,
    list_plans,
    list_user_plans,
    set_user_plan,
    update_plan,
)

router = APIRouter()


@router.get("/plans", response_model=list[PlanResponse])
def get_plans(db: Session = Depends(get_db)):
    return list_plans(db)


@router.get("/me", response_model=SubscriptionResponse)
def get_my_subscription(user=Depends(get_current_user), db: Session = Depends(get_db)):
    plan = get_user_plan(db, user.id)
    if not plan:
        raise HTTPException(status_code=500, detail="No default plan configured")
    return SubscriptionResponse(plan=plan)


@router.post("/me", response_model=SubscriptionResponse)
def change_my_plan(
    body: ChangePlanRequest,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    plan = db.query(Plan).filter(Plan.id == body.plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    set_user_plan(db, user.id, plan.id)
    return SubscriptionResponse(plan=plan)


# --- Admin: manage plan definitions ---------------------------------------


@router.post("/admin/plans", response_model=PlanResponse)
def admin_create_plan(
    body: PlanCreate,
    admin: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    existing = db.query(Plan).filter(Plan.name == body.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="A plan with this name already exists")

    return create_plan(
        db, body.name, body.display_name, body.price_display, body.features, body.is_default
    )


@router.put("/admin/plans/{plan_id}", response_model=PlanResponse)
def admin_update_plan(
    plan_id: int,
    body: PlanUpdate,
    admin: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    plan = update_plan(db, plan_id, body.model_dump(exclude_unset=True))
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan


@router.delete("/admin/plans/{plan_id}")
def admin_delete_plan(
    plan_id: int,
    admin: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    error = delete_plan(db, plan_id)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return {"message": "Plan deleted"}


# --- Admin: manage which plan each user is on ------------------------------


@router.get("/admin/users", response_model=list[AdminUserPlanResponse])
def admin_list_users(
    admin: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
    auth_db: Session = Depends(get_auth_db),
):
    users = auth_db.query(AuthUser).order_by(AuthUser.id).all()
    return list_user_plans(users, db)


@router.put("/admin/users/{user_id}/plan", response_model=AdminUserPlanResponse)
def admin_set_user_plan(
    user_id: int,
    body: AdminSetUserPlanRequest,
    admin: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
    auth_db: Session = Depends(get_auth_db),
):
    plan = db.query(Plan).filter(Plan.id == body.plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    user = auth_db.query(AuthUser).filter(AuthUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    set_user_plan(db, user_id, plan.id)
    return AdminUserPlanResponse(user_id=user.id, email=user.email, plan=plan)
