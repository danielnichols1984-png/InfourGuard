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
    # For per-user third-party app OAuth grants (generate_security_report's
    # app_grants) — Google's Directory API Tokens resource.
    "https://www.googleapis.com/auth/admin.directory.user.security",
    # Full domain-wide Drive access, used with useDomainAdminAccess=True —
    # a deliberate, broad grant (confirmed with the user, who chose this
    # over the no-new-scope alternative of using each employee's own
    # personal Drive connection) so this tenant admin connection can list
    # and migrate ANY Shared Drive in the org, not just ones this admin
    # personally belongs to. Does NOT reach individual employees' personal
    # My Drive content — that would need actual domain-wide delegation
    # (a service-account auth model), a different and bigger step not
    # taken here. See tenants_core/README.md.
    "https://www.googleapis.com/auth/drive",
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
        # Raw byte count for snapshot/growth tracking (generate_storage_report)
        # — kept alongside the formatted string rather than replacing it,
        # same "_"-prefixed internal-field convention microsoft_graph.py uses.
        "_storage_used_bytes": int(used_mb * 1024 * 1024) if used_mb is not None else None,
    }


def generate_storage_report(db: Session, admin_user_id: int) -> dict:
    """Storage totals + growth rate for the Storage & Growth tab. Reuses
    generate_tenant_report()'s existing storage fetch rather than
    duplicating the Reports API call, then records/reads a snapshot
    (shared TenantStorageSnapshot table, same one Microsoft's report
    uses) for the growth trend."""
    report = generate_tenant_report(db, admin_user_id)
    if not report.get("connected"):
        return report

    raw_bytes = report.pop("_storage_used_bytes", None)
    if raw_bytes:
        token_service.record_storage_snapshot(db, admin_user_id, "google_workspace", raw_bytes)
    report["storage_growth"] = token_service.compute_storage_growth(db, admin_user_id, "google_workspace")
    return report


def generate_security_report(db: Session, admin_user_id: int) -> dict:
    """2FA enrollment and inactive/suspended accounts — both come from
    fields already included in the Directory API's user resource
    (isEnrolledIn2Sv, isEnforcedIn2Sv, suspended, lastLoginTime), needing
    no scope beyond admin.directory.user.readonly already granted for
    generate_tenant_report()'s user count. UNVERIFIED against a live
    Workspace tenant — see module docstring; field names are per Google's
    documented Users resource schema.

    mfa_coverage/inactive_accounts are shaped to match Microsoft's same
    keys (recommendations.py reads these generically across providers)."""
    creds = get_credentials(db, admin_user_id)
    if not creds:
        return {"connected": False, "error": "Google Workspace admin is not connected."}

    result: dict = {"connected": True, "error": None, "warnings": []}

    try:
        directory = build("admin", "directory_v1", credentials=creds)
        users: list[dict] = []
        page_token = None
        while True:
            response = (
                directory.users()
                .list(
                    customer="my_customer",
                    maxResults=500,
                    pageToken=page_token,
                    fields="nextPageToken,users(primaryEmail,isEnrolledIn2Sv,isEnforcedIn2Sv,suspended,lastLoginTime)",
                )
                .execute()
            )
            users.extend(response.get("users", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        total = len(users)
        mfa_registered = sum(1 for u in users if u.get("isEnrolledIn2Sv"))
        result["mfa_coverage"] = {
            "total_users": total,
            "mfa_registered": mfa_registered,
            "mfa_coverage_percent": round((mfa_registered / total) * 100, 1) if total else None,
            "source": "Directory API 2-step verification enrollment per user",
        }

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        buckets = {"0-30": 0, "31-60": 0, "61-90": 0, "90+": 0, "unknown": 0}
        suspended_count = 0
        for u in users:
            if u.get("suspended"):
                suspended_count += 1
            last_login = u.get("lastLoginTime") or ""
            # Google returns the epoch as a sentinel for "never logged in".
            if not last_login or last_login.startswith("1970-01-01"):
                buckets["unknown"] += 1
                continue
            try:
                last_active = datetime.strptime(last_login[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                buckets["unknown"] += 1
                continue
            days = (now - last_active).days
            if days <= 30:
                buckets["0-30"] += 1
            elif days <= 60:
                buckets["31-60"] += 1
            elif days <= 90:
                buckets["61-90"] += 1
            else:
                buckets["90+"] += 1

        result["inactive_accounts"] = {
            "total_accounts": total,
            "days_since_last_login": buckets,
            "suspended_accounts": suspended_count,
        }

        try:
            result["app_grants"] = _get_app_grants(directory, users)
        except HttpError as e:
            result["warnings"].append(f"Couldn't fetch per-user app access grants: {e}")
    except HttpError as e:
        result["warnings"].append(f"Couldn't fetch user security details (needs super admin): {e}")

    if not result["warnings"]:
        result.pop("warnings", None)

    return result


def _get_app_grants(directory, users: list[dict]) -> dict:
    """Which third-party apps have been granted OAuth access to each
    user's Google account data — Google's documented Tokens resource
    (Admin SDK Directory API), needing the newly-added
    admin.directory.user.security scope. A basic per-user lookup, not a
    "report," matching the same non-Premium-equivalent reasoning behind
    Microsoft's app-grants metric. Unverified against a live Workspace
    tenant with real third-party app grants installed."""
    apps: dict[str, dict] = {}
    for u in users:
        email = u.get("primaryEmail")
        if not email:
            continue
        try:
            tokens = directory.tokens().list(userKey=email).execute().get("items", [])
        except HttpError:
            continue
        for t in tokens:
            if t.get("nativeApp"):
                continue  # Google's own installed-app tokens, not third-party access
            name = t.get("displayText") or t.get("clientId") or "Unknown app"
            entry = apps.setdefault(name, {"name": name, "scopes": set(), "user_count": 0})
            entry["scopes"].update(t.get("scopes") or [])
            entry["user_count"] += 1

    app_list = [{**a, "scopes": sorted(a["scopes"])} for a in apps.values()]
    app_list.sort(key=lambda a: -a["user_count"])
    return {"total_apps": len(app_list), "apps": app_list}


_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def _list_shared_drives(creds) -> list[dict]:
    """Needs useDomainAdminAccess=True plus the domain-wide `drive` scope
    added to this admin connection specifically for this — without both,
    drives.list() only returns drives the connecting admin personally
    belongs to, not every Shared Drive in the org."""
    service = build("drive", "v3", credentials=creds)
    drives: list[dict] = []
    page_token = None
    while True:
        response = (
            service.drives()
            .list(useDomainAdminAccess=True, pageSize=100, pageToken=page_token, fields="nextPageToken,drives(id,name)")
            .execute()
        )
        drives.extend(response.get("drives", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return drives


def _walk_shared_drive(creds, drive_id: str, drive_name: str) -> list[dict]:
    """Full file/folder inventory of one Shared Drive — same
    domain-admin-backed access as _list_shared_drives, scoped to a single
    drive via corpora=drive/driveId rather than the personal report's
    corpora=user (integrations_core/google.py's _list_drive_files)."""
    service = build("drive", "v3", credentials=creds)
    items: list[dict] = []
    page_token = None
    while True:
        response = (
            service.files()
            .list(
                corpora="drive",
                driveId=drive_id,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                pageSize=1000,
                pageToken=page_token,
                fields="nextPageToken,files(id,name,mimeType,size,webViewLink,shared,permissions(type))",
            )
            .execute()
        )
        for f in response.get("files", []):
            is_folder = f.get("mimeType") == _FOLDER_MIME_TYPE
            permissions = f.get("permissions") or []
            anyone_with_link = any(p.get("type") == "anyone" for p in permissions)
            shared = f.get("shared", False) or anyone_with_link
            items.append(
                {
                    "name": f.get("name"),
                    "site": drive_name,
                    "is_folder": is_folder,
                    "size": int(f.get("size") or 0),
                    "web_url": f.get("webViewLink"),
                    "shared": shared,
                    "scope": "anyone" if anyone_with_link else ("shared" if shared else None),
                }
            )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return items


def generate_documents_report(db: Session, admin_user_id: int) -> dict:
    """Real for Shared Drives now, thanks to the domain-wide Drive scope
    added to this admin connection specifically for this. Still does NOT
    reach individual employees' personal My Drive content — that needs
    actual domain-wide delegation (a service-account auth model), a
    separate, bigger step not taken here — see tenants_core/README.md.
    Unverified against a live Workspace tenant with real Shared Drives."""
    creds = get_credentials(db, admin_user_id)
    if not creds:
        return {"connected": False, "error": "Google Workspace admin is not connected."}

    result: dict = {"connected": True, "error": None, "warnings": []}

    try:
        drives = _list_shared_drives(creds)
    except HttpError as e:
        return {"connected": True, "error": f"Couldn't list Shared Drives: {e}"}

    all_items: list[dict] = []
    for d in drives:
        try:
            all_items.extend(_walk_shared_drive(creds, d["id"], d.get("name", d["id"])))
        except HttpError as e:
            result["warnings"].append(f"Couldn't walk Shared Drive '{d.get('name', d['id'])}': {e}")

    shared_items = [i for i in all_items if i["shared"]]
    result["shared_items"] = shared_items
    result["all_items"] = all_items
    result["total_shared"] = len(shared_items)
    result["anyone_with_link_count"] = sum(1 for i in shared_items if i["scope"] == "anyone")
    result["total_files"] = sum(1 for i in all_items if not i["is_folder"])
    result["total_folders"] = sum(1 for i in all_items if i["is_folder"])
    result["shared_drives_count"] = len(drives)
    result["personal_drive_note"] = (
        "This covers Shared Drives only — individual employees' personal My Drive content "
        "isn't reachable without domain-wide delegation, a separate, bigger step not taken here."
    )

    if not result["warnings"]:
        result.pop("warnings", None)

    return result
