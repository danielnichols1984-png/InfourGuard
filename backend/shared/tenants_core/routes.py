"""Routes for connecting and reporting on organization-wide admin access.

Both hard lessons from integrations_core's OAuth callbacks are applied
here from the start, rather than waiting to hit them again:
1. The user's identity travels in the OAuth `state` param (or Dropbox's
   `url_state`), not the session cookie — modern browsers can drop
   cookies on the cross-site redirect chain back from a provider.
2. Each callback ends with a rendered HTML page (a "connected" landing
   page), not an HTTP redirect — an HTTP redirect immediately after a
   cross-site-originated request can still get its cookie dropped even
   though the redirect target is same-origin, since the browser treats
   the whole chain as cross-site.
"""
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, EmailStr

from shared.auth_core.db import get_db as get_auth_db
from shared.auth_core.dependencies import require_admin
from shared.auth_core.models import User as AuthUser
from shared.businesses_core.db import get_db as get_businesses_db
from shared.businesses_core.dependencies import require_org_access
from shared.businesses_core.service import user_has_org_access
from shared.tenants_core import dropbox_team, google_admin, microsoft_graph, recommendations, service
from shared.tenants_core.config import settings
from shared.tenants_core.db import get_db

router = APIRouter()


class EmailReportRequest(BaseModel):
    email: EmailStr


_PROVIDERS = {
    "google-workspace": ("Google Workspace", google_admin),
    "microsoft365": ("Microsoft 365", microsoft_graph),
    "dropbox-business": ("Dropbox Business", dropbox_team),
}


def _send_provider_report(db, provider: str, admin_user_id: int, to_email: str) -> dict:
    if provider not in _PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown tenant provider")
    label, module = _PROVIDERS[provider]
    data = module.generate_tenant_report(db, admin_user_id)
    text = service.render_tenant_report_text(label, data)
    sent = service.send_tenant_report_email(to_email, label, text)
    return {"sent": sent}


def _require_configured(provider: str, configured: bool) -> None:
    if not configured:
        raise HTTPException(status_code=503, detail=f"{provider} tenant connection is not configured")


_CONFIGURED_CHECKS = {
    "google-workspace": lambda: settings.google_admin_configured,
    "microsoft365": lambda: settings.microsoft_configured,
    "dropbox-business": lambda: settings.dropbox_business_configured,
}


def _require_provider_configured(provider: str) -> None:
    if provider not in _PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown tenant provider")
    label, _ = _PROVIDERS[provider]
    if not _CONFIGURED_CHECKS[provider]():
        raise HTTPException(status_code=503, detail=f"{label} tenant connection is not configured")


def _landing_page(target: str) -> HTMLResponse:
    return HTMLResponse(
        f'<!DOCTYPE html><html><head><meta http-equiv="refresh" content="0;url={target}">'
        f"</head><body>Connected — <a href=\"{target}\">continue</a></body></html>"
    )


def _require_callback_user_has_org_access(user_id: int, auth_db, businesses_db) -> None:
    """Defense in depth for the three OAuth callbacks below, which can't
    use Depends(require_org_access) since identity comes from the OAuth
    `state` param, not the session cookie (see module docstring). The
    connect route already checked this before the flow started — this
    just closes the gap for a state that's somehow replayed after the
    user's access changed mid-flow."""
    user = auth_db.query(AuthUser).filter(AuthUser.id == user_id).first()
    if not user or not user_has_org_access(businesses_db, user):
        raise HTTPException(status_code=403, detail="Organization admin access required")


@router.get("/status")
def get_status(org_access=Depends(require_org_access), db=Depends(get_db)):
    user, _ = org_access
    return [
        {"provider": "google_workspace", "connected": service.is_connected(db, user.id, "google_workspace")},
        {"provider": "microsoft365", "connected": service.is_connected(db, user.id, "microsoft365")},
        {"provider": "dropbox_business", "connected": service.is_connected(db, user.id, "dropbox_business")},
    ]


# --- Google Workspace --------------------------------------------------


@router.get("/google-workspace")
def connect_google_workspace(org_access=Depends(require_org_access), db=Depends(get_db)):
    user, _ = org_access
    _require_configured("Google Workspace", settings.google_admin_configured)
    csrf_token = secrets.token_urlsafe(24)
    state = f"{csrf_token}:{user.id}"
    flow = google_admin.build_flow(state=state)
    authorization_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    service.save_oauth_state(db, user.id, "google_workspace", csrf_token, flow.code_verifier)
    return RedirectResponse(authorization_url)


@router.get("/google-workspace/callback")
def google_workspace_callback(
    request: Request,
    error: str | None = None,
    state: str | None = None,
    db=Depends(get_db),
    auth_db=Depends(get_auth_db),
    businesses_db=Depends(get_businesses_db),
):
    _require_configured("Google Workspace", settings.google_admin_configured)
    if error:
        raise HTTPException(status_code=400, detail=f"Google OAuth error: {error}")
    if not state or ":" not in state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    csrf_token, _, user_id_str = state.partition(":")
    try:
        user_id = int(user_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    expected_csrf = service.get_oauth_state(db, user_id, "google_workspace")
    if not expected_csrf or expected_csrf != csrf_token:
        raise HTTPException(status_code=400, detail="Invalid OAuth state (CSRF check failed)")
    _require_callback_user_has_org_access(user_id, auth_db, businesses_db)

    flow = google_admin.build_flow(state=state)
    flow.code_verifier = service.get_code_verifier(db, user_id, "google_workspace")
    try:
        flow.fetch_token(authorization_response=str(request.url))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Google token exchange failed: {e}")
    creds = flow.credentials

    service.save_tokens(
        db, user_id, "google_workspace",
        access_token=creds.token,
        refresh_token=creds.refresh_token,
        expires_at=creds.expiry,
        scope=" ".join(creds.scopes) if creds.scopes else None,
    )
    return _landing_page("/organization/google-workspace")


@router.post("/google-workspace/disconnect")
def disconnect_google_workspace(org_access=Depends(require_org_access), db=Depends(get_db)):
    user, _ = org_access
    service.delete_tokens(db, user.id, "google_workspace")
    return {"message": "Google Workspace disconnected"}


@router.get("/google-workspace/report")
def google_workspace_report(org_access=Depends(require_org_access), db=Depends(get_db)):
    user, _ = org_access
    return google_admin.generate_tenant_report(db, user.id)


# --- Microsoft 365 -------------------------------------------------------


@router.get("/microsoft365")
def connect_microsoft365(org_access=Depends(require_org_access), db=Depends(get_db)):
    user, _ = org_access
    _require_configured("Microsoft 365", settings.microsoft_configured)
    csrf_token = secrets.token_urlsafe(24)
    state = f"{csrf_token}:{user.id}"
    service.save_oauth_state(db, user.id, "microsoft365", csrf_token, None)
    return RedirectResponse(microsoft_graph.get_authorization_url(state))


@router.get("/microsoft365/callback")
def microsoft365_callback(
    error: str | None = None,
    state: str | None = None,
    code: str | None = None,
    db=Depends(get_db),
    auth_db=Depends(get_auth_db),
    businesses_db=Depends(get_businesses_db),
):
    _require_configured("Microsoft 365", settings.microsoft_configured)
    if error:
        raise HTTPException(status_code=400, detail=f"Microsoft OAuth error: {error}")
    if not state or ":" not in state or not code:
        raise HTTPException(status_code=400, detail="Invalid OAuth response")

    csrf_token, _, user_id_str = state.partition(":")
    try:
        user_id = int(user_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    expected_csrf = service.get_oauth_state(db, user_id, "microsoft365")
    if not expected_csrf or expected_csrf != csrf_token:
        raise HTTPException(status_code=400, detail="Invalid OAuth state (CSRF check failed)")
    _require_callback_user_has_org_access(user_id, auth_db, businesses_db)

    try:
        result = microsoft_graph.exchange_code_for_token(code)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Microsoft token exchange failed: {e}")

    from datetime import datetime, timedelta, timezone

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=result.get("expires_in", 3600))
    id_claims = result.get("id_token_claims") or {}

    service.save_tokens(
        db, user_id, "microsoft365",
        access_token=result["access_token"],
        refresh_token=result.get("refresh_token"),
        expires_at=expires_at,
        scope=" ".join(result.get("scope", [])) if isinstance(result.get("scope"), list) else result.get("scope"),
        tenant_domain=id_claims.get("tid"),
    )
    return _landing_page("/organization/microsoft365")


@router.post("/microsoft365/disconnect")
def disconnect_microsoft365(org_access=Depends(require_org_access), db=Depends(get_db)):
    user, _ = org_access
    service.delete_tokens(db, user.id, "microsoft365")
    return {"message": "Microsoft 365 disconnected"}


@router.get("/microsoft365/report")
def microsoft365_report(org_access=Depends(require_org_access), db=Depends(get_db)):
    user, _ = org_access
    return microsoft_graph.generate_tenant_report(db, user.id)


# --- Dropbox Business ------------------------------------------------------


@router.get("/dropbox-business")
def connect_dropbox_business(org_access=Depends(require_org_access), db=Depends(get_db)):
    user, _ = org_access
    _require_configured("Dropbox Business", settings.dropbox_business_configured)
    session: dict = {}
    flow = dropbox_team.build_flow(session)
    authorize_url = flow.start(url_state=str(user.id))
    service.save_oauth_state(db, user.id, "dropbox_business", session.get("dropbox-business-csrf"), None)
    return RedirectResponse(authorize_url)


@router.get("/dropbox-business/callback")
def dropbox_business_callback(
    request: Request, db=Depends(get_db), auth_db=Depends(get_auth_db), businesses_db=Depends(get_businesses_db)
):
    _require_configured("Dropbox Business", settings.dropbox_business_configured)

    error = request.query_params.get("error")
    if error:
        raise HTTPException(status_code=400, detail=f"Dropbox OAuth error: {error}")

    raw_state = request.query_params.get("state") or ""
    _, _, url_state = raw_state.partition("|")
    try:
        user_id = int(url_state)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    expected_csrf = service.get_oauth_state(db, user_id, "dropbox_business")
    if not expected_csrf:
        raise HTTPException(status_code=400, detail="OAuth session expired or not found")
    _require_callback_user_has_org_access(user_id, auth_db, businesses_db)

    session = {"dropbox-business-csrf": expected_csrf}
    flow = dropbox_team.build_flow(session)
    try:
        result = flow.finish(dict(request.query_params))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Dropbox token exchange failed: {e}")

    service.save_tokens(
        db, user_id, "dropbox_business",
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_at=result.expires_at,
    )
    return _landing_page("/organization/dropbox-business")


@router.post("/dropbox-business/disconnect")
def disconnect_dropbox_business(org_access=Depends(require_org_access), db=Depends(get_db)):
    user, _ = org_access
    service.delete_tokens(db, user.id, "dropbox_business")
    return {"message": "Dropbox Business disconnected"}


@router.get("/dropbox-business/report")
def dropbox_business_report(org_access=Depends(require_org_access), db=Depends(get_db)):
    user, _ = org_access
    return dropbox_team.generate_tenant_report(db, user.id)


# --- Tab reports: same 4 endpoints for every provider ----------------------
# Each provider module implements generate_security_report/_documents_report/
# _storage_report/_recommendations with the same signature, so one route per
# tab works for all three rather than three near-duplicate routes each.


@router.get("/{provider}/security-report")
def provider_security_report(provider: str, org_access=Depends(require_org_access), db=Depends(get_db)):
    """Deliberately separate from /report — the underlying per-site/
    per-member walks are meaningfully slower, so this is its own
    on-demand endpoint rather than something every overview page load
    pays for."""
    user, _ = org_access
    _require_provider_configured(provider)
    _, module = _PROVIDERS[provider]
    return module.generate_security_report(db, user.id)


@router.get("/{provider}/documents-report")
def provider_documents_report(provider: str, org_access=Depends(require_org_access), db=Depends(get_db)):
    user, _ = org_access
    _require_provider_configured(provider)
    _, module = _PROVIDERS[provider]
    return module.generate_documents_report(db, user.id)


@router.get("/{provider}/storage-report")
def provider_storage_report(provider: str, org_access=Depends(require_org_access), db=Depends(get_db)):
    user, _ = org_access
    _require_provider_configured(provider)
    _, module = _PROVIDERS[provider]
    return module.generate_storage_report(db, user.id)


@router.get("/{provider}/recommendations-report")
def provider_recommendations_report(provider: str, org_access=Depends(require_org_access), db=Depends(get_db)):
    user, _ = org_access
    _require_provider_configured(provider)
    _, module = _PROVIDERS[provider]

    tenant_data = module.generate_tenant_report(db, user.id)
    if not tenant_data.get("connected"):
        return tenant_data

    security_data = module.generate_security_report(db, user.id)
    merged = {**tenant_data, **{k: v for k, v in security_data.items() if k not in ("connected", "error")}}
    return {"connected": True, "error": None, "recommendations": recommendations.generate_recommendations(merged)}


# --- Email a tenant report ---------------------------------------------


@router.post("/{provider}/report/email")
def email_tenant_report(
    provider: str,
    body: EmailReportRequest,
    org_access=Depends(require_org_access),
    db=Depends(get_db),
):
    user, _ = org_access
    return _send_provider_report(db, provider, user.id, body.email)


@router.post("/admin/users/{user_id}/{provider}/report/email")
def admin_email_tenant_report(
    user_id: int,
    provider: str,
    body: EmailReportRequest,
    admin=Depends(require_admin),
    db=Depends(get_db),
):
    return _send_provider_report(db, provider, user_id, body.email)
