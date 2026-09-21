from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from shared.auth_core.db import get_db as get_auth_db
from shared.auth_core.dependencies import get_current_user, require_admin
from shared.auth_core.models import User as AuthUser
from shared.auth_core.schemas import UserResponse
from shared.auth_core.security import create_access_token
from shared.auth_core.config import settings as auth_settings
from shared.auth_core.service import (
    create_user,
    end_impersonation_log,
    set_user_password,
    start_impersonation_log,
)
from shared.businesses_core.db import get_db
from shared.businesses_core.dependencies import require_business_admin
from shared.businesses_core.models import Business, BusinessMembership
from shared.businesses_core.schemas import (
    AdminBusinessSummary,
    AdminCreateBusinessRequest,
    BusinessResponse,
    CreateMemberRequest,
    EmailReportRequest,
    MemberResponse,
    SetMemberPasswordRequest,
)
from shared.businesses_core.service import (
    add_member,
    create_business,
    delete_business,
    get_membership,
    list_businesses,
    list_members,
    remove_member,
)
from shared.integrations_core.db import get_db as get_integrations_db
from shared.integrations_core import dropbox_integration, google, microsoft

router = APIRouter()


def _member_responses(db_auth: Session, memberships: list[BusinessMembership]) -> list[MemberResponse]:
    emails = {
        u.id: u.email
        for u in db_auth.query(AuthUser).filter(AuthUser.id.in_([m.user_id for m in memberships])).all()
    }
    return [
        MemberResponse(user_id=m.user_id, email=emails.get(m.user_id, "?"), role=m.role)
        for m in memberships
    ]


def require_business_member(
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    membership = get_membership(db, user.id)
    if not membership:
        raise HTTPException(status_code=404, detail="You're not part of a business")
    return user, membership


# --- Platform admin: manage all businesses ---------------------------------


@router.get("/admin/businesses", response_model=list[AdminBusinessSummary])
def admin_list_businesses(admin: AuthUser = Depends(require_admin), db: Session = Depends(get_db)):
    businesses = list_businesses(db)
    return [
        AdminBusinessSummary(id=b.id, name=b.name, member_count=len(list_members(db, b.id)))
        for b in businesses
    ]


@router.post("/admin/businesses", response_model=AdminBusinessSummary)
def admin_create_business(
    body: AdminCreateBusinessRequest,
    admin: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
    auth_db: Session = Depends(get_auth_db),
):
    target = auth_db.query(AuthUser).filter(AuthUser.email == body.admin_email).first()
    if not target:
        raise HTTPException(status_code=404, detail="No user found with that email")
    if get_membership(db, target.id):
        raise HTTPException(status_code=400, detail="That user is already part of a business")

    business = create_business(db, body.name, target.id)
    return AdminBusinessSummary(id=business.id, name=business.name, member_count=1)


@router.get("/admin/businesses/{business_id}/members", response_model=list[MemberResponse])
def admin_get_business_members(
    business_id: int,
    admin: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
    auth_db: Session = Depends(get_auth_db),
):
    business = db.query(Business).filter(Business.id == business_id).first()
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    return _member_responses(auth_db, list_members(db, business_id))


@router.delete("/admin/businesses/{business_id}")
def admin_delete_business(
    business_id: int, admin: AuthUser = Depends(require_admin), db: Session = Depends(get_db)
):
    error = delete_business(db, business_id)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return {"message": "Business deleted"}


# --- Business admin / member: manage own business ---------------------------


@router.get("/my-business", response_model=BusinessResponse)
def get_my_business(
    membership_dep=Depends(require_business_member),
    db: Session = Depends(get_db),
    auth_db: Session = Depends(get_auth_db),
):
    _, membership = membership_dep
    business = db.query(Business).filter(Business.id == membership.business_id).first()
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    members = _member_responses(auth_db, list_members(db, business.id))
    return BusinessResponse(id=business.id, name=business.name, members=members)


@router.post("/my-business/members", response_model=MemberResponse)
def create_business_member(
    body: CreateMemberRequest,
    admin_dep=Depends(require_business_admin),
    db: Session = Depends(get_db),
    auth_db: Session = Depends(get_auth_db),
):
    _, membership = admin_dep
    existing = auth_db.query(AuthUser).filter(AuthUser.email == body.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    new_user = create_user(auth_db, body.email, body.password)
    add_member(db, business_id=membership.business_id, user_id=new_user.id, role="member")
    return MemberResponse(user_id=new_user.id, email=new_user.email, role="member")


def _get_member_in_own_business(db: Session, membership: BusinessMembership, user_id: int) -> BusinessMembership:
    target = (
        db.query(BusinessMembership)
        .filter(
            BusinessMembership.business_id == membership.business_id,
            BusinessMembership.user_id == user_id,
        )
        .first()
    )
    if not target:
        raise HTTPException(status_code=404, detail="That user isn't part of your business")
    return target


@router.delete("/my-business/members/{user_id}")
def remove_business_member(
    user_id: int,
    admin_dep=Depends(require_business_admin),
    db: Session = Depends(get_db),
):
    admin, membership = admin_dep
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Can't remove yourself from the business")

    target = _get_member_in_own_business(db, membership, user_id)
    remove_member(db, target)
    return {"message": "Member removed from business"}


@router.put("/my-business/members/{user_id}/password")
def set_business_member_password(
    user_id: int,
    body: SetMemberPasswordRequest,
    admin_dep=Depends(require_business_admin),
    db: Session = Depends(get_db),
    auth_db: Session = Depends(get_auth_db),
):
    _, membership = admin_dep
    _get_member_in_own_business(db, membership, user_id)

    target_user = auth_db.query(AuthUser).filter(AuthUser.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    set_user_password(auth_db, target_user, body.new_password)
    return {"message": "Password updated"}


@router.post("/my-business/members/{user_id}/impersonate")
def impersonate_business_member(
    user_id: int,
    admin_dep=Depends(require_business_admin),
    db: Session = Depends(get_db),
    auth_db: Session = Depends(get_auth_db),
):
    admin, membership = admin_dep
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Can't view as yourself")

    target_membership = _get_member_in_own_business(db, membership, user_id)
    if target_membership.role == "business_admin":
        raise HTTPException(status_code=400, detail="Can't view as another business admin")

    target_user = auth_db.query(AuthUser).filter(AuthUser.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    start_impersonation_log(auth_db, admin_id=admin.id, target_id=target_user.id)

    token = create_access_token({"sub": str(target_user.id), "impersonator_id": str(admin.id)})
    response = JSONResponse(
        UserResponse(
            id=target_user.id,
            email=target_user.email,
            is_admin=target_user.is_admin,
            impersonating=True,
            impersonator_email=admin.email,
        ).model_dump()
    )
    response.set_cookie(
        key=auth_settings.COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=auth_settings.COOKIE_SECURE,
    )
    return response


@router.post("/my-business/members/{user_id}/reports/{provider}/email")
def email_member_report(
    user_id: int,
    provider: str,
    body: EmailReportRequest,
    admin_dep=Depends(require_business_admin),
    db: Session = Depends(get_db),
    integrations_db: Session = Depends(get_integrations_db),
):
    _, membership = admin_dep
    _get_member_in_own_business(db, membership, user_id)

    if provider == "google":
        data = google.generate_report(integrations_db, user_id)
        sent = google.send_report_email(body.email, google.render_report_text(data))
    elif provider == "dropbox":
        data = dropbox_integration.generate_report(integrations_db, user_id)
        sent = dropbox_integration.send_report_email(
            body.email, dropbox_integration.render_report_text(data)
        )
    elif provider == "microsoft":
        data = microsoft.generate_report(integrations_db, user_id)
        sent = microsoft.send_report_email(body.email, microsoft.render_report_text(data))
    else:
        raise HTTPException(status_code=404, detail="Unknown report provider")

    return {"sent": sent}
