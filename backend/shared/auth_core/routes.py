from fastapi import APIRouter, Depends, HTTPException, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from shared.auth_core.config import settings
from shared.auth_core.rate_limit import enforce_rate_limit
from shared.auth_core.schemas import (
    AdminCreateUserRequest,
    AdminSetPasswordRequest,
    AdminUserResponse,
    LoginRequest,
    SignupRequest,
    UserResponse,
)
from shared.auth_core.security import create_access_token
from shared.auth_core.service import (
    authenticate_user,
    create_user,
    delete_user,
    end_impersonation_log,
    generate_login_token,
    list_users,
    set_admin_status,
    set_user_password,
    start_impersonation_log,
)
from shared.auth_core.dependencies import get_current_user, get_impersonator, get_token_payload, require_admin
from shared.auth_core.models import User
from shared.auth_core.db import get_db


router = APIRouter()


def _wants_json(request: Request) -> bool:
    """Browser <form> submits don't send this; a JS client (fetch/axios) does.

    Lets /login serve both the legacy server-rendered forms (redirect flow)
    and a JSON SPA client from the same endpoint.
    """
    return "application/json" in request.headers.get("accept", "")


@router.post("/signup")
def signup(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    enforce_rate_limit(
        request, "signup", settings.SIGNUP_RATE_LIMIT, settings.RATE_LIMIT_WINDOW_SECONDS
    )

    try:
        payload = SignupRequest(email=email, password=password)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors()[0]["msg"])

    existing = db.query(User).filter(User.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    create_user(db, payload.email, payload.password)

    # Same content-negotiation switch as /login: a JS client (e.g. the
    # signup page's plan-selection flow, which needs to keep going —
    # log in, then possibly checkout — without a page navigation in
    # between) gets JSON back; a plain <form> submit still redirects.
    if _wants_json(request):
        return JSONResponse({"message": "Account created"})
    return RedirectResponse("/login", status_code=302)


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    enforce_rate_limit(
        request, "login", settings.LOGIN_RATE_LIMIT, settings.RATE_LIMIT_WINDOW_SECONDS
    )

    wants_json = _wants_json(request)

    try:
        payload = LoginRequest(email=email, password=password)
    except ValidationError:
        if wants_json:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        return RedirectResponse("/login?error=1", status_code=302)

    user = authenticate_user(db, payload.email, payload.password)
    if not user:
        if wants_json:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        return RedirectResponse("/login?error=1", status_code=302)

    token = generate_login_token(user)

    if wants_json:
        response = JSONResponse(
            UserResponse(id=user.id, email=user.email, is_admin=user.is_admin).model_dump()
        )
    else:
        response = RedirectResponse("/dashboard", status_code=302)

    response.set_cookie(
        key=settings.COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
    )
    return response


@router.get("/me", response_model=UserResponse)
def me(
    user: User = Depends(get_current_user),
    payload: dict = Depends(get_token_payload),
    db: Session = Depends(get_db),
):
    impersonator = get_impersonator(payload, db)
    return UserResponse(
        id=user.id,
        email=user.email,
        is_admin=user.is_admin,
        impersonating=impersonator is not None,
        impersonator_email=impersonator.email if impersonator else None,
    )


@router.post("/logout")
def logout(request: Request):
    if _wants_json(request):
        response = JSONResponse({"message": "logged out"})
    else:
        response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(key=settings.COOKIE_NAME, samesite="lax")
    return response


# --- Admin: user management -------------------------------------------------


@router.get("/admin/users", response_model=list[AdminUserResponse])
def admin_list_users(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return list_users(db)


@router.post("/admin/users", response_model=AdminUserResponse)
def admin_create_user(
    body: AdminCreateUserRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    existing = db.query(User).filter(User.email == body.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    return create_user(db, body.email, body.password)


@router.delete("/admin/users/{user_id}")
def admin_delete_user(
    user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Can't delete your own account")

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.is_admin:
        raise HTTPException(
            status_code=400,
            detail="Can't delete another admin account through this — use direct DB access if that's really intended",
        )

    delete_user(db, target)
    return {"message": "User deleted"}


@router.put("/admin/users/{user_id}/admin-status", response_model=AdminUserResponse)
def admin_set_admin_status(
    user_id: int,
    is_admin: bool,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if user_id == admin.id and not is_admin:
        raise HTTPException(status_code=400, detail="Can't remove your own admin access")

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    return set_admin_status(db, target, is_admin)


@router.put("/admin/users/{user_id}/password")
def admin_set_user_password(
    user_id: int,
    body: AdminSetPasswordRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.is_admin and target.id != admin.id:
        raise HTTPException(
            status_code=400,
            detail="Can't reset another admin account's password through this — use direct DB access if that's really intended",
        )

    set_user_password(db, target, body.new_password)
    # Note: this doesn't revoke any token the user already has — our JWTs are
    # stateless with no revocation list, so an existing session stays valid
    # until it naturally expires (AUTH_ACCESS_TOKEN_EXPIRE_MINUTES).
    return {"message": "Password updated"}


# --- Admin: impersonation ("view as") ---------------------------------------


@router.post("/admin/users/{user_id}/impersonate")
def admin_impersonate_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # No separate "already impersonating" check needed: require_admin above
    # already resolves the *current effective identity* (the impersonated
    # member, while mid-impersonation), and that's never an admin — so a
    # nested impersonation attempt is rejected by the ordinary admin gate
    # before this body ever runs. Verified directly: attempting this while
    # impersonating returns 403, not a fallthrough to here.
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.is_admin:
        raise HTTPException(status_code=400, detail="Can't impersonate another admin")

    start_impersonation_log(db, admin_id=admin.id, target_id=target.id)

    token = create_access_token({"sub": str(target.id), "impersonator_id": str(admin.id)})
    response = JSONResponse(
        UserResponse(
            id=target.id,
            email=target.email,
            is_admin=target.is_admin,
            impersonating=True,
            impersonator_email=admin.email,
        ).model_dump()
    )
    response.set_cookie(
        key=settings.COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
    )
    return response


@router.post("/admin/stop-impersonating")
def admin_stop_impersonating(
    payload: dict = Depends(get_token_payload),
    db: Session = Depends(get_db),
):
    impersonator_id = payload.get("impersonator_id")
    if not impersonator_id:
        raise HTTPException(status_code=400, detail="Not currently impersonating")

    admin = db.query(User).filter(User.id == impersonator_id).first()
    if not admin:
        raise HTTPException(status_code=404, detail="Original admin account no longer exists")

    end_impersonation_log(db, admin_id=admin.id, target_id=int(payload.get("sub")))

    token = create_access_token({"sub": str(admin.id)})
    response = JSONResponse(
        UserResponse(id=admin.id, email=admin.email, is_admin=admin.is_admin).model_dump()
    )
    response.set_cookie(
        key=settings.COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
    )
    return response


