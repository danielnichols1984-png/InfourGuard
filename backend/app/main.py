import asyncio

from fastapi import Depends, FastAPI, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from shared.auth_core.db import init_db as init_auth_db
from shared.auth_core.dependencies import get_optional_user
from shared.auth_core.routes import router as auth_router
from shared.businesses_core.db import get_db as get_businesses_db, init_db as init_businesses_db
from shared.businesses_core.routes import router as businesses_router
from shared.businesses_core.service import get_membership as get_business_membership
from shared.integrations_core import dropbox_integration, google as google_integration, microsoft as microsoft_integration
from shared.integrations_core import service as integrations_service
from shared.integrations_core.db import get_db as get_integrations_db, init_db as init_integrations_db
from shared.integrations_core.routes import dropbox_callback, google_callback
from shared.integrations_core.routes import router as integrations_router
from shared.migrations_core.db import init_db as init_migrations_db
from shared.migrations_core.routes import router as migrations_router
from shared.migrations_core.scheduler import scheduler_loop as migrations_scheduler_loop
from shared.subscriptions_core.db import get_db as get_subscriptions_db, init_db as init_subscriptions_db
from shared.subscriptions_core.routes import router as subscriptions_router
from shared.subscriptions_core.service import get_user_plan, seed_default_plans
from shared.tenants_core import dropbox_team, google_admin, microsoft_graph
from shared.tenants_core import service as tenants_service
from shared.tenants_core.db import get_db as get_tenants_db, init_db as init_tenants_db
from shared.tenants_core.routes import router as tenants_router

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


def _wants_html(request: Request) -> bool:
    """A real browser navigation (typed URL, clicked link, form submit,
    redirect target) always sends `Accept: text/html,...` explicitly.
    Bare `fetch()` calls — which is most of the JS in this app's Jinja
    pages — default to `Accept: */*` when no header is set, which must
    NOT match here, or every one of those pages' error handling (which
    expects JSON back) would silently break."""
    return "text/html" in request.headers.get("accept", "")


@app.exception_handler(StarletteHTTPException)
async def html_aware_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if _wants_html(request):
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": exc.status_code, "detail": exc.detail},
            status_code=exc.status_code,
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def html_aware_unhandled_exception_handler(request: Request, exc: Exception):
    if _wants_html(request):
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": 500, "detail": "Something went wrong."},
            status_code=500,
        )
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


app.include_router(auth_router, prefix="/auth")
app.include_router(subscriptions_router, prefix="/subscriptions")
app.include_router(integrations_router, prefix="/connect")
app.include_router(migrations_router, prefix="/migrations")
app.include_router(tenants_router, prefix="/tenants")
app.include_router(businesses_router, prefix="/businesses")

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


@app.on_event("startup")
def _init_migrations_db():
    init_migrations_db()


@app.on_event("startup")
async def _start_migrations_scheduler():
    asyncio.create_task(migrations_scheduler_loop())


@app.on_event("startup")
def _init_tenants_db():
    init_tenants_db()


@app.on_event("startup")
def _init_businesses_db():
    init_businesses_db()


@app.get("/login")
def login_page(request: Request, error: str = None):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": error}
    )


@app.get("/signup")
def signup_page(request: Request):
    return templates.TemplateResponse(request, "signup.html", {})


@app.get("/dashboard")
def dashboard_page(
    request: Request,
    user=Depends(get_optional_user),
    db: Session = Depends(get_subscriptions_db),
    businesses_db: Session = Depends(get_businesses_db),
):
    if not user:
        return RedirectResponse("/login")
    plan = get_user_plan(db, user.id)
    business_membership = get_business_membership(businesses_db, user.id)
    return templates.TemplateResponse(
        request, "dashboard.html", {"user": user, "plan": plan, "business": business_membership}
    )


@app.get("/plans")
def plans_page(request: Request, user=Depends(get_optional_user)):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "plans.html", {})


@app.get("/admin/plans")
def admin_plans_page(request: Request, user=Depends(get_optional_user)):
    if not user:
        return RedirectResponse("/login")
    if not user.is_admin:
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse(request, "admin.html", {})


@app.get("/admin/users")
def admin_users_page(request: Request, user=Depends(get_optional_user)):
    if not user:
        return RedirectResponse("/login")
    if not user.is_admin:
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse(request, "admin_users.html", {})


@app.get("/admin/businesses")
def admin_businesses_page(request: Request, user=Depends(get_optional_user)):
    if not user:
        return RedirectResponse("/login")
    if not user.is_admin:
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse(request, "admin_businesses.html", {})


@app.get("/business")
def business_page(
    request: Request,
    user=Depends(get_optional_user),
    businesses_db: Session = Depends(get_businesses_db),
):
    if not user:
        return RedirectResponse("/login")
    if not get_business_membership(businesses_db, user.id):
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse(request, "business.html", {})


@app.get("/integrations")
def integrations_page(request: Request, user=Depends(get_optional_user)):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "integrations.html", {})


@app.get("/migrations")
def migrations_page(request: Request, user=Depends(get_optional_user)):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "migrations.html", {"user": user})


@app.get("/tenants")
def tenants_page(request: Request, user=Depends(get_optional_user)):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "tenants.html", {"user": user})


@app.get("/organization/google-workspace", response_class=HTMLResponse)
def tenant_google_workspace_page(
    request: Request, user=Depends(get_optional_user), db: Session = Depends(get_tenants_db)
):
    if not user:
        return RedirectResponse("/login")
    data = google_admin.generate_tenant_report(db, user.id)
    return templates.TemplateResponse(request, "tenant_google_workspace.html", {"user": user, **data})


@app.post("/organization/google-workspace/email_report", response_class=HTMLResponse)
def tenant_google_workspace_email_report(
    request: Request,
    email: str = Form(...),
    user=Depends(get_optional_user),
    db: Session = Depends(get_tenants_db),
):
    if not user:
        return RedirectResponse("/login")
    data = google_admin.generate_tenant_report(db, user.id)
    text = tenants_service.render_tenant_report_text("Google Workspace", data)
    sent = tenants_service.send_tenant_report_email(email, "Google Workspace", text)
    return templates.TemplateResponse(
        request,
        "tenant_google_workspace.html",
        {
            "user": user,
            **data,
            "success": f"Report successfully emailed to {email}!" if sent else None,
            "error": data.get("error") or (None if sent else "Failed to send email. Check SMTP settings."),
        },
    )


@app.post("/organization/google-workspace/disconnect")
def tenant_google_workspace_disconnect(
    user=Depends(get_optional_user), db: Session = Depends(get_tenants_db)
):
    if not user:
        return RedirectResponse("/login")
    tenants_service.delete_tokens(db, user.id, "google_workspace")
    return RedirectResponse("/organization/google-workspace", status_code=302)


@app.get("/organization/microsoft365", response_class=HTMLResponse)
def tenant_microsoft365_page(
    request: Request, user=Depends(get_optional_user), db: Session = Depends(get_tenants_db)
):
    if not user:
        return RedirectResponse("/login")
    data = microsoft_graph.generate_tenant_report(db, user.id)
    return templates.TemplateResponse(request, "tenant_microsoft365.html", {"user": user, **data})


@app.post("/organization/microsoft365/email_report", response_class=HTMLResponse)
def tenant_microsoft365_email_report(
    request: Request,
    email: str = Form(...),
    user=Depends(get_optional_user),
    db: Session = Depends(get_tenants_db),
):
    if not user:
        return RedirectResponse("/login")
    data = microsoft_graph.generate_tenant_report(db, user.id)
    text = tenants_service.render_tenant_report_text("Microsoft 365", data)
    sent = tenants_service.send_tenant_report_email(email, "Microsoft 365", text)
    return templates.TemplateResponse(
        request,
        "tenant_microsoft365.html",
        {
            "user": user,
            **data,
            "success": f"Report successfully emailed to {email}!" if sent else None,
            "error": data.get("error") or (None if sent else "Failed to send email. Check SMTP settings."),
        },
    )


@app.post("/organization/microsoft365/disconnect")
def tenant_microsoft365_disconnect(
    user=Depends(get_optional_user), db: Session = Depends(get_tenants_db)
):
    if not user:
        return RedirectResponse("/login")
    tenants_service.delete_tokens(db, user.id, "microsoft365")
    return RedirectResponse("/organization/microsoft365", status_code=302)


@app.get("/organization/dropbox-business", response_class=HTMLResponse)
def tenant_dropbox_business_page(
    request: Request, user=Depends(get_optional_user), db: Session = Depends(get_tenants_db)
):
    if not user:
        return RedirectResponse("/login")
    data = dropbox_team.generate_tenant_report(db, user.id)
    return templates.TemplateResponse(request, "tenant_dropbox_business.html", {"user": user, **data})


@app.post("/organization/dropbox-business/email_report", response_class=HTMLResponse)
def tenant_dropbox_business_email_report(
    request: Request,
    email: str = Form(...),
    user=Depends(get_optional_user),
    db: Session = Depends(get_tenants_db),
):
    if not user:
        return RedirectResponse("/login")
    data = dropbox_team.generate_tenant_report(db, user.id)
    text = tenants_service.render_tenant_report_text("Dropbox Business", data)
    sent = tenants_service.send_tenant_report_email(email, "Dropbox Business", text)
    return templates.TemplateResponse(
        request,
        "tenant_dropbox_business.html",
        {
            "user": user,
            **data,
            "success": f"Report successfully emailed to {email}!" if sent else None,
            "error": data.get("error") or (None if sent else "Failed to send email. Check SMTP settings."),
        },
    )


@app.post("/organization/dropbox-business/disconnect")
def tenant_dropbox_business_disconnect(
    user=Depends(get_optional_user), db: Session = Depends(get_tenants_db)
):
    if not user:
        return RedirectResponse("/login")
    tenants_service.delete_tokens(db, user.id, "dropbox_business")
    return RedirectResponse("/organization/dropbox-business", status_code=302)


@app.get("/google/report", response_class=HTMLResponse)
def google_report_page(
    request: Request,
    user=Depends(get_optional_user),
    db: Session = Depends(get_integrations_db),
):
    if not user:
        return RedirectResponse("/login")
    data = google_integration.generate_report(db, user.id)
    context = {"user": user, "report": google_integration.render_report_text(data), **data}
    return templates.TemplateResponse(request, "google_report.html", context)


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
        "user": user,
        "report": google_integration.render_report_text(data),
        **data,
        "success": f"Report successfully emailed to {email}!" if sent else None,
        "error": data.get("error") or (None if sent else "Failed to send email. Check SMTP settings."),
    }
    return templates.TemplateResponse(request, "google_report.html", context)


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
        "user": user,
        "report": dropbox_integration.render_report_text(data),
        **data,
    }
    return templates.TemplateResponse(request, "dropbox_report.html", context)


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
        "user": user,
        "report": dropbox_integration.render_report_text(data),
        **data,
        "success": f"Report successfully emailed to {email}!" if sent else None,
        "error": data.get("error") or (None if sent else "Failed to send email. Check SMTP settings."),
    }
    return templates.TemplateResponse(request, "dropbox_report.html", context)


@app.post("/dropbox/disconnect")
def dropbox_disconnect_page(
    user=Depends(get_optional_user), db: Session = Depends(get_integrations_db)
):
    if not user:
        return RedirectResponse("/login")
    integrations_service.delete_tokens(db, user.id, "dropbox")
    return RedirectResponse("/dropbox/report", status_code=302)


@app.get("/microsoft/report", response_class=HTMLResponse)
def microsoft_report_page(
    request: Request,
    user=Depends(get_optional_user),
    db: Session = Depends(get_integrations_db),
):
    if not user:
        return RedirectResponse("/login")
    data = microsoft_integration.generate_report(db, user.id)
    context = {
        "user": user,
        "report": microsoft_integration.render_report_text(data),
        **data,
    }
    return templates.TemplateResponse(request, "microsoft_report.html", context)


@app.post("/microsoft/email_report", response_class=HTMLResponse)
def microsoft_email_report(
    request: Request,
    email: str = Form(...),
    user=Depends(get_optional_user),
    db: Session = Depends(get_integrations_db),
):
    if not user:
        return RedirectResponse("/login")
    data = microsoft_integration.generate_report(db, user.id)
    sent = microsoft_integration.send_report_email(email, microsoft_integration.render_report_text(data))
    context = {
        "user": user,
        "report": microsoft_integration.render_report_text(data),
        **data,
        "success": f"Report successfully emailed to {email}!" if sent else None,
        "error": data.get("error") or (None if sent else "Failed to send email. Check SMTP settings."),
    }
    return templates.TemplateResponse(request, "microsoft_report.html", context)


@app.post("/microsoft/disconnect")
def microsoft_disconnect_page(
    user=Depends(get_optional_user), db: Session = Depends(get_integrations_db)
):
    if not user:
        return RedirectResponse("/login")
    integrations_service.delete_tokens(db, user.id, "microsoft")
    return RedirectResponse("/microsoft/report", status_code=302)