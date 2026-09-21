import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from shared.auth_core.dependencies import get_current_user, require_admin
from shared.integrations_core import dropbox_integration, google, microsoft, service
from shared.integrations_core.config import settings
from shared.integrations_core.db import get_db
from shared.integrations_core.schemas import EmailReportRequest, IntegrationStatus

router = APIRouter()


def _redirect_after_connect(target: str) -> HTMLResponse:
    """A rendered page, not an HTTP redirect, to land on after the OAuth
    callback.

    Chrome (and other browsers) treat the *entire* redirect chain as
    cross-site once any hop in it was cross-site — so an HTTP redirect
    straight from this callback to `target` would still get its SameSite=Lax
    session cookie dropped, even though that hop is same-origin, because the
    chain started at Google/Dropbox. Rendering a real page here ends the
    navigation; the follow-up request (triggered by this page, not by the
    provider's redirect) is a fresh same-site navigation and carries the
    cookie correctly.
    """
    return HTMLResponse(
        f'<!DOCTYPE html><html><head><meta http-equiv="refresh" content="0;url={target}">'
        f"</head><body>Connected — <a href=\"{target}\">continue</a></body></html>"
    )


def _require_google_configured() -> None:
    if not settings.google_configured:
        raise HTTPException(status_code=503, detail="Google integration is not configured")


def _require_dropbox_configured() -> None:
    if not settings.dropbox_configured:
        raise HTTPException(status_code=503, detail="Dropbox integration is not configured")


def _require_microsoft_configured() -> None:
    if not settings.microsoft_configured:
        raise HTTPException(status_code=503, detail="Microsoft integration is not configured")


@router.get("/status", response_model=list[IntegrationStatus])
def get_status(user=Depends(get_current_user), db: Session = Depends(get_db)):
    return [
        IntegrationStatus(provider="google", connected=service.is_connected(db, user.id, "google")),
        IntegrationStatus(provider="dropbox", connected=service.is_connected(db, user.id, "dropbox")),
        IntegrationStatus(provider="microsoft", connected=service.is_connected(db, user.id, "microsoft")),
    ]


# --- Google ------------------------------------------------------------


@router.get("/google")
def connect_google(user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_google_configured()
    # The user's identity has to travel in `state` itself, not the session
    # cookie — modern browsers increasingly drop cookies on the cross-site
    # redirect chain back from the provider (confirmed: Chrome sent zero
    # cookies on this exact callback in testing), so `get_current_user`
    # can't be relied on on the way back.
    csrf_token = secrets.token_urlsafe(24)
    state = f"{csrf_token}:{user.id}"
    flow = google.build_flow(state=state)
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        # No include_granted_scopes: we always request the same fixed scope
        # set, not incrementally growing one, and that flag causes Google to
        # union in whatever was granted under an older scope list (e.g. a
        # prior drive.readonly-only grant) — which then trips oauthlib's
        # "scope changed" check on the token response, since the granted
        # set no longer exactly matches what was requested.
    )
    service.save_oauth_state(db, user.id, "google", csrf_token, flow.code_verifier)
    return RedirectResponse(authorization_url)


@router.get("/google/callback")
def google_callback(
    request: Request,
    error: str | None = None,
    state: str | None = None,
    db: Session = Depends(get_db),
):
    _require_google_configured()
    if error:
        raise HTTPException(status_code=400, detail=f"Google OAuth error: {error}")

    if not state or ":" not in state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    csrf_token, _, user_id_str = state.partition(":")
    try:
        user_id = int(user_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    expected_csrf = service.get_oauth_state(db, user_id, "google")
    if not expected_csrf or expected_csrf != csrf_token:
        raise HTTPException(status_code=400, detail="Invalid OAuth state (CSRF check failed)")

    flow = google.build_flow(state=state)
    flow.code_verifier = service.get_code_verifier(db, user_id, "google")
    try:
        flow.fetch_token(authorization_response=str(request.url))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Google token exchange failed: {e}")
    creds = flow.credentials

    service.save_tokens(
        db,
        user_id,
        "google",
        access_token=creds.token,
        refresh_token=creds.refresh_token,
        expires_at=creds.expiry,
        # What Google actually granted, per the token response — not our
        # config's wishlist. This is what later scope-sufficiency checks
        # (e.g. before using this account as a migration destination) read.
        scope=" ".join(creds.scopes) if creds.scopes else None,
    )
    return _redirect_after_connect("/google/report")


@router.post("/google/disconnect")
def disconnect_google(user=Depends(get_current_user), db: Session = Depends(get_db)):
    service.delete_tokens(db, user.id, "google")
    return {"message": "Google disconnected"}


@router.get("/google/report")
def google_report(user=Depends(get_current_user), db: Session = Depends(get_db)):
    return google.generate_report(db, user.id)


@router.post("/google/report/email")
def email_google_report(
    body: EmailReportRequest, user=Depends(get_current_user), db: Session = Depends(get_db)
):
    data = google.generate_report(db, user.id)
    sent = google.send_report_email(body.email, google.render_report_text(data))
    return {"sent": sent}


# --- Dropbox -------------------------------------------------------------


@router.get("/dropbox")
def connect_dropbox(user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_dropbox_configured()
    from dropbox.oauth import DropboxOAuth2Flow

    session: dict = {}
    flow = DropboxOAuth2Flow(
        consumer_key=settings.DROPBOX_APP_KEY,
        consumer_secret=settings.DROPBOX_APP_SECRET,
        redirect_uri=settings.DROPBOX_REDIRECT_URI,
        session=session,
        csrf_token_session_key="dropbox-csrf",
        token_access_type="offline",
    )
    # url_state carries the user's id through Dropbox and back — same reason
    # as Google above: the session cookie isn't reliably sent on the
    # cross-site redirect back from the provider on modern browsers.
    authorize_url = flow.start(url_state=str(user.id))
    service.save_oauth_state(db, user.id, "dropbox", session.get("dropbox-csrf"), None)
    return RedirectResponse(authorize_url)


@router.get("/dropbox/callback")
def dropbox_callback(request: Request, db: Session = Depends(get_db)):
    _require_dropbox_configured()
    from dropbox.oauth import DropboxOAuth2Flow

    error = request.query_params.get("error")
    if error:
        raise HTTPException(status_code=400, detail=f"Dropbox OAuth error: {error}")

    raw_state = request.query_params.get("state") or ""
    _, _, url_state = raw_state.partition("|")
    try:
        user_id = int(url_state)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    expected_csrf = service.get_oauth_state(db, user_id, "dropbox")
    if not expected_csrf:
        raise HTTPException(status_code=400, detail="OAuth session expired or not found")

    session = {"dropbox-csrf": expected_csrf}
    flow = DropboxOAuth2Flow(
        consumer_key=settings.DROPBOX_APP_KEY,
        consumer_secret=settings.DROPBOX_APP_SECRET,
        redirect_uri=settings.DROPBOX_REDIRECT_URI,
        session=session,
        csrf_token_session_key="dropbox-csrf",
        token_access_type="offline",
    )
    try:
        result = flow.finish(dict(request.query_params))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Dropbox token exchange failed: {e}")

    service.save_tokens(
        db,
        user_id,
        "dropbox",
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_at=result.expires_at,
    )
    return _redirect_after_connect("/dropbox/report")


@router.post("/dropbox/disconnect")
def disconnect_dropbox(user=Depends(get_current_user), db: Session = Depends(get_db)):
    service.delete_tokens(db, user.id, "dropbox")
    return {"message": "Dropbox disconnected"}


@router.get("/dropbox/report")
def dropbox_report(user=Depends(get_current_user), db: Session = Depends(get_db)):
    return dropbox_integration.generate_report(db, user.id)


@router.post("/dropbox/report/email")
def email_dropbox_report(
    body: EmailReportRequest, user=Depends(get_current_user), db: Session = Depends(get_db)
):
    data = dropbox_integration.generate_report(db, user.id)
    sent = dropbox_integration.send_report_email(
        body.email, dropbox_integration.render_report_text(data)
    )
    return {"sent": sent}


# --- Microsoft (OneDrive) ---------------------------------------------


@router.get("/microsoft")
def connect_microsoft(user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_microsoft_configured()
    # Same reasoning as Google/Dropbox above: identity travels in `state`,
    # not the session cookie, since the callback is reached via a
    # cross-site redirect chain from Microsoft.
    csrf_token = secrets.token_urlsafe(24)
    state = f"{csrf_token}:{user.id}"
    authorization_url = microsoft.get_authorization_url(state)
    service.save_oauth_state(db, user.id, "microsoft", csrf_token, None)
    return RedirectResponse(authorization_url)


@router.get("/microsoft/callback")
def microsoft_callback(
    request: Request,
    error: str | None = None,
    state: str | None = None,
    code: str | None = None,
    db: Session = Depends(get_db),
):
    _require_microsoft_configured()
    if error:
        raise HTTPException(status_code=400, detail=f"Microsoft OAuth error: {error}")
    if not state or ":" not in state or not code:
        raise HTTPException(status_code=400, detail="Invalid OAuth response")

    csrf_token, _, user_id_str = state.partition(":")
    try:
        user_id = int(user_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    expected_csrf = service.get_oauth_state(db, user_id, "microsoft")
    if not expected_csrf or expected_csrf != csrf_token:
        raise HTTPException(status_code=400, detail="Invalid OAuth state (CSRF check failed)")

    try:
        result = microsoft.exchange_code_for_token(code)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Microsoft token exchange failed: {e}")

    from datetime import datetime, timedelta, timezone

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=result.get("expires_in", 3600))

    service.save_tokens(
        db,
        user_id,
        "microsoft",
        access_token=result["access_token"],
        refresh_token=result.get("refresh_token"),
        expires_at=expires_at,
        scope=" ".join(result.get("scope", [])) if isinstance(result.get("scope"), list) else result.get("scope"),
    )
    return _redirect_after_connect("/microsoft/report")


@router.post("/microsoft/disconnect")
def disconnect_microsoft(user=Depends(get_current_user), db: Session = Depends(get_db)):
    service.delete_tokens(db, user.id, "microsoft")
    return {"message": "Microsoft disconnected"}


@router.get("/microsoft/report")
def microsoft_report(user=Depends(get_current_user), db: Session = Depends(get_db)):
    return microsoft.generate_report(db, user.id)


@router.post("/microsoft/report/email")
def email_microsoft_report(
    body: EmailReportRequest, user=Depends(get_current_user), db: Session = Depends(get_db)
):
    data = microsoft.generate_report(db, user.id)
    sent = microsoft.send_report_email(body.email, microsoft.render_report_text(data))
    return {"sent": sent}


# --- Platform admin: send any user's report without impersonating ---------


@router.post("/admin/users/{user_id}/google/report/email")
def admin_email_google_report(
    user_id: int,
    body: EmailReportRequest,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    data = google.generate_report(db, user_id)
    sent = google.send_report_email(body.email, google.render_report_text(data))
    return {"sent": sent}


@router.post("/admin/users/{user_id}/dropbox/report/email")
def admin_email_dropbox_report(
    user_id: int,
    body: EmailReportRequest,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    data = dropbox_integration.generate_report(db, user_id)
    sent = dropbox_integration.send_report_email(
        body.email, dropbox_integration.render_report_text(data)
    )
    return {"sent": sent}


@router.post("/admin/users/{user_id}/microsoft/report/email")
def admin_email_microsoft_report(
    user_id: int,
    body: EmailReportRequest,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    data = microsoft.generate_report(db, user_id)
    sent = microsoft.send_report_email(body.email, microsoft.render_report_text(data))
    return {"sent": sent}
