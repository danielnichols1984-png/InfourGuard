from fastapi import APIRouter, Depends, HTTPException, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from shared.auth_core.config import settings
from shared.auth_core.rate_limit import enforce_rate_limit
from shared.auth_core.schemas import LoginRequest, SignupRequest, UserResponse
from shared.auth_core.service import create_user, authenticate_user, generate_login_token
from shared.auth_core.dependencies import get_current_user
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

    # Redirect to login page after successful signup
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
def me(user = Depends(get_current_user)):
    return UserResponse(id=user.id, email=user.email, is_admin=user.is_admin)


@router.post("/logout")
def logout(request: Request):
    if _wants_json(request):
        response = JSONResponse({"message": "logged out"})
    else:
        response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(key=settings.COOKIE_NAME, samesite="lax")
    return response


