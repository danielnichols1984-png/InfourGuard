"""Google Workspace tenant-wide admin metrics.

Uses a regular OAuth consent flow (same mechanics as integrations_core's
personal Google Drive connection) requesting Admin SDK scopes, rather than
domain-wide delegation / a service account. Google enforces server-side
that these scopes only actually work for a Workspace **super admin** — a
non-admin who connects will get a clean permission error from Google
itself when the report is generated, not a false result.

Caveat, stated plainly: the exact Admin SDK Reports API parameter names
requested below (`_USAGE_PARAMETERS`) are based on Google's documented
schema but are UNVERIFIED against a live Workspace tenant — I have no way
to test this without one. If a parameter name is wrong, Google returns a
4xx for that specific report call, which is caught and surfaced as
"unavailable" for that metric rather than failing the whole report — see
generate_tenant_report(). Verify field names against
https://developers.google.com/admin-sdk/reports/v1/reference when you
have a real tenant to test against, and treat this module as a first
draft, not a finished, verified integration, until then.
"""
import os

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session

from shared.integrations_core.config import settings as integrations_settings
from shared.tenants_core import service as token_service

if integrations_settings.GOOGLE_REDIRECT_URI and integrations_settings.GOOGLE_REDIRECT_URI.startswith("http://"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

ADMIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/admin.directory.user.readonly",
    "https://www.googleapis.com/auth/admin.directory.domain.readonly",
    "https://www.googleapis.com/auth/admin.reports.usage.readonly",
]


def build_flow(state: str | None = None) -> Flow:
    from shared.tenants_core.config import settings

    client_config = {
        "web": {
            "client_id": integrations_settings.GOOGLE_CLIENT_ID,
            "client_secret": integrations_settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.GOOGLE_ADMIN_REDIRECT_URI],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=ADMIN_SCOPES, state=state)
    flow.redirect_uri = settings.GOOGLE_ADMIN_REDIRECT_URI
    return flow


def _credentials_from_record(record) -> Credentials | None:
    if not record or not record.access_token:
        return None
    return Credentials(
        token=record.access_token,
        refresh_token=record.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=integrations_settings.GOOGLE_CLIENT_ID,
        client_secret=integrations_settings.GOOGLE_CLIENT_SECRET,
        scopes=ADMIN_SCOPES,
    )


def get_credentials(db: Session, admin_user_id: int) -> Credentials | None:
    record = token_service.get_tokens(db, admin_user_id, "google_workspace")
    creds = _credentials_from_record(record)
    if not creds:
        return None
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        token_service.save_tokens(
            db, admin_user_id, "google_workspace",
            access_token=creds.token, refresh_token=creds.refresh_token, expires_at=creds.expiry,
        )
    return creds


def format_bytes(n) -> str:
    if n is None:
        return "-"
    try:
        size = float(n)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"
    except (ValueError, TypeError):
        return "-"


def generate_tenant_report(db: Session, admin_user_id: int) -> dict:
    creds = get_credentials(db, admin_user_id)
    if not creds:
        return {"connected": False, "error": "Google Workspace admin is not connected."}

    result = {"connected": True, "error": None, "warnings": []}

    # --- User count + domain, via the Admin SDK Directory API ---
    try:
        directory = build("admin", "directory_v1", credentials=creds)
        total_users = 0
        domain = None
        page_token = None
        while True:
            response = (
                directory.users()
                .list(customer="my_customer", maxResults=500, pageToken=page_token, fields="nextPageToken,users(primaryEmail)")
                .execute()
            )
            users = response.get("users", [])
            total_users += len(users)
            if users and not domain:
                domain = users[0]["primaryEmail"].split("@")[-1]
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        result["total_users"] = total_users
        result["tenant_domain"] = domain
    except HttpError as e:
        result["total_users"] = None
        result["warnings"].append(f"Couldn't fetch user count (needs super admin): {e}")

    # --- Storage/sharing metrics, via the Admin SDK Reports API ---
    # UNVERIFIED parameter names — see module docstring.
    try:
        reports = build("admin", "reports_v1", credentials=creds)
        usage = (
            reports.customerUsageReports()
            .get(
                date=_yesterday_iso(),
                parameters=(
                    "accounts:drive_used_quota_in_mb,"
                    "accounts:drive_total_quota_in_mb,"
                    "accounts:num_items_in_trash"
                ),
            )
            .execute()
        )
        parsed = _parse_customer_usage(usage)
        result.update(parsed)
    except HttpError as e:
        result["warnings"].append(f"Couldn't fetch storage/usage report: {e}")
        result.setdefault("storage_used", None)
        result.setdefault("storage_total", None)

    if not result.get("warnings"):
        result.pop("warnings", None)

    return result


def _yesterday_iso() -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _parse_customer_usage(usage: dict) -> dict:
    """Reports API returns a flat list of {name, parameters: [...]} per
    date. Pull out whatever we recognize; unrecognized/missing parameters
    just don't populate that key, handled gracefully by the caller."""
    values: dict[str, float] = {}
    for report in usage.get("usageReports", []):
        for param in report.get("parameters", []):
            name = param.get("name")
            if "intValue" in param:
                values[name] = float(param["intValue"])
            elif "msgValue" in param:
                values[name] = param["msgValue"]

    used_mb = values.get("accounts:drive_used_quota_in_mb")
    total_mb = values.get("accounts:drive_total_quota_in_mb")

    return {
        "storage_used": format_bytes(used_mb * 1024 * 1024) if used_mb is not None else None,
        "storage_total": format_bytes(total_mb * 1024 * 1024) if total_mb is not None else None,
        "storage_percent": round((used_mb / total_mb) * 100, 1) if used_mb and total_mb else None,
    }
