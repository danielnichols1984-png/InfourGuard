import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from shared.auth_core.dependencies import get_current_user
from shared.integrations_core import dropbox_integration, google, service
from shared.integrations_core.config import settings
from shared.integrations_core.db import get_db
from shared.integrations_core.schemas import EmailReportRequest, IntegrationStatus

router = APIRouter()


def _require_google_configured() -> None:
    if not settings.google_configured:
        raise HTTPException(status_code=503, detail="Google integration is not configured")


def _require_dropbox_configured() -> None:
    if not settings.dropbox_configured:
        raise HTTPException(status_code=503, detail="Dropbox integration is not configured")


@router.get("/status", response_model=list[IntegrationStatus])
def get_status(user=Depends(get_current_user), db: Session = Depends(get_db)):
    return [
        IntegrationStatus(provider="google", connected=service.is_connected(db, user.id, "google")),
        IntegrationStatus(provider="dropbox", connected=service.is_connected(db, user.id, "dropbox")),
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
        include_granted_scopes="true",
        prompt="consent",
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
    )
    return RedirectResponse("/google/report")


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
    return RedirectResponse("/dropbox/report")


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
