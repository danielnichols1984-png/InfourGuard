from fastapi import Depends, FastAPI, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from shared.auth_core.db import init_db as init_auth_db
from shared.auth_core.dependencies import get_optional_user
from shared.auth_core.routes import router as auth_router
from shared.integrations_core import dropbox_integration, google as google_integration
from shared.integrations_core import service as integrations_service
from shared.integrations_core.db import get_db as get_integrations_db, init_db as init_integrations_db
from shared.integrations_core.routes import dropbox_callback, google_callback
from shared.integrations_core.routes import router as integrations_router
from shared.subscriptions_core.db import get_db as get_subscriptions_db, init_db as init_subscriptions_db
from shared.subscriptions_core.routes import router as subscriptions_router
from shared.subscriptions_core.service import get_user_plan, seed_default_plans

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="app/web/templates")
templates.env.cache = None
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")

app.include_router(auth_router, prefix="/auth")
app.include_router(subscriptions_router, prefix="/subscriptions")
app.include_router(integrations_router, prefix="/connect")

# Aliases for whatever redirect URI is actually registered with each
# provider's OAuth app (see GOOGLE_REDIRECT_URI/DROPBOX_REDIRECT_URI in
# .env) — same handlers as /connect/google/callback and
# /connect/dropbox/callback, just reachable at a second, exact path too.
app.add_api_route("/google/oauth/callback", google_callback, methods=["GET"])
app.add_api_route("/dropbox/oauth/callback", dropbox_callback, methods=["GET"])


@app.on_event("startup")
def _init_auth_db():
    init_auth_db()


@app.on_event("startup")
def _init_subscriptions_db():
    init_subscriptions_db()
    db = next(get_subscriptions_db())
    try:
        seed_default_plans(db)
    finally:
        db.close()


@app.on_event("startup")
def _init_integrations_db():
    init_integrations_db()


@app.get("/login")
def login_page(request: Request, error: str = None):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": error}
    )


@app.get("/signup")
def signup_page(request: Request):
    return templates.TemplateResponse("signup.html", {"request": request})


@app.get("/dashboard")
def dashboard_page(
    request: Request,
    user=Depends(get_optional_user),
    db: Session = Depends(get_subscriptions_db),
):
    if not user:
        return RedirectResponse("/login")
    plan = get_user_plan(db, user.id)
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "user": user, "plan": plan}
    )


@app.get("/plans")
def plans_page(request: Request, user=Depends(get_optional_user)):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse("plans.html", {"request": request})


@app.get("/admin/plans")
def admin_plans_page(request: Request, user=Depends(get_optional_user)):
    if not user:
        return RedirectResponse("/login")
    if not user.is_admin:
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse("admin.html", {"request": request})


@app.get("/integrations")
def integrations_page(request: Request, user=Depends(get_optional_user)):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse("integrations.html", {"request": request})


@app.get("/google/report", response_class=HTMLResponse)
def google_report_page(
    request: Request,
    user=Depends(get_optional_user),
    db: Session = Depends(get_integrations_db),
):
    if not user:
        return RedirectResponse("/login")
    data = google_integration.generate_report(db, user.id)
    context = {"request": request, "user": user, "report": google_integration.render_report_text(data), **data}
    return templates.TemplateResponse("google_report.html", context)


@app.post("/google/email_report", response_class=HTMLResponse)
def google_email_report(
    request: Request,
    email: str = Form(...),
    user=Depends(get_optional_user),
    db: Session = Depends(get_integrations_db),
):
    if not user:
        return RedirectResponse("/login")
    data = google_integration.generate_report(db, user.id)
    sent = google_integration.send_report_email(email, google_integration.render_report_text(data))
    context = {
        "request": request,
        "user": user,
        "report": google_integration.render_report_text(data),
        **data,
        "success": f"Report successfully emailed to {email}!" if sent else None,
        "error": data.get("error") or (None if sent else "Failed to send email. Check SMTP settings."),
    }
    return templates.TemplateResponse("google_report.html", context)


@app.post("/google/disconnect")
def google_disconnect_page(
    user=Depends(get_optional_user), db: Session = Depends(get_integrations_db)
):
    if not user:
        return RedirectResponse("/login")
    integrations_service.delete_tokens(db, user.id, "google")
    return RedirectResponse("/google/report", status_code=302)


@app.get("/dropbox/report", response_class=HTMLResponse)
def dropbox_report_page(
    request: Request,
    user=Depends(get_optional_user),
    db: Session = Depends(get_integrations_db),
):
    if not user:
        return RedirectResponse("/login")
    data = dropbox_integration.generate_report(db, user.id)
    context = {
        "request": request,
        "user": user,
        "report": dropbox_integration.render_report_text(data),
        **data,
    }
    return templates.TemplateResponse("dropbox_report.html", context)


@app.post("/dropbox/email_report", response_class=HTMLResponse)
def dropbox_email_report(
    request: Request,
    email: str = Form(...),
    user=Depends(get_optional_user),
    db: Session = Depends(get_integrations_db),
):
    if not user:
        return RedirectResponse("/login")
    data = dropbox_integration.generate_report(db, user.id)
    sent = dropbox_integration.send_report_email(email, dropbox_integration.render_report_text(data))
    context = {
        "request": request,
        "user": user,
        "report": dropbox_integration.render_report_text(data),
        **data,
        "success": f"Report successfully emailed to {email}!" if sent else None,
        "error": data.get("error") or (None if sent else "Failed to send email. Check SMTP settings."),
    }
    return templates.TemplateResponse("dropbox_report.html", context)


@app.post("/dropbox/disconnect")
def dropbox_disconnect_page(
    user=Depends(get_optional_user), db: Session = Depends(get_integrations_db)
):
    if not user:
        return RedirectResponse("/login")
    integrations_service.delete_tokens(db, user.id, "dropbox")
    return RedirectResponse("/dropbox/report", status_code=302)