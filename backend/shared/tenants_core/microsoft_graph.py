"""Microsoft 365 tenant-wide admin metrics via Microsoft Graph.

Verified against a real Microsoft 365 business tenant (see
tenants_core/README.md) — the OAuth flow, /organization, /users/$count,
/subscribedSkus, and the getOneDriveUsageAccountDetail CSV column names
all confirmed correct against live responses. Report periods and exact
behavior for a larger/older tenant (multiple users, actual OneDrive
usage data) are still only lightly exercised — the tenant this was
verified against had a single, unlicensed user.

Every metric here is fetched independently and degrades to `None` with a
warning on failure, rather than one bad field name taking down the whole
report — see generate_tenant_report().
"""
import csv
import io
from datetime import datetime, timezone

import msal
import requests
from sqlalchemy.orm import Session

from shared.integrations_core.config import settings as integrations_settings
from shared.tenants_core import service as token_service
from shared.tenants_core.config import settings

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Delegated permissions — the signing-in user must be a Global Admin (or
# have been granted admin-consent rights) for these to actually resolve;
# a non-admin gets a clean 403 from Graph itself, not a false result.
# AuditLog.Read.All was added for MFA registration coverage and directory
# audit log events — a connection made before this was added won't have
# it and needs to reconnect (Graph doesn't retroactively upgrade a token's
# granted scope, same as every other OAuth scope change in this codebase).
GRAPH_SCOPES = [
    "Organization.Read.All",
    "User.Read.All",
    "Reports.Read.All",
    "Sites.Read.All",
    "AuditLog.Read.All",
]


def _msal_app() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        client_id=integrations_settings.MICROSOFT_CLIENT_ID,
        client_credential=integrations_settings.MICROSOFT_CLIENT_SECRET,
        authority=f"https://login.microsoftonline.com/{integrations_settings.MICROSOFT_TENANT}",
    )


def get_authorization_url(state: str) -> str:
    return _msal_app().get_authorization_request_url(
        scopes=GRAPH_SCOPES,
        state=state,
        redirect_uri=settings.MICROSOFT_ADMIN_REDIRECT_URI,
    )


def exchange_code_for_token(code: str) -> dict:
    """Returns the raw MSAL token result dict — has access_token,
    refresh_token, expires_in, and (usually) id_token_claims."""
    result = _msal_app().acquire_token_by_authorization_code(
        code=code,
        scopes=GRAPH_SCOPES,
        redirect_uri=settings.MICROSOFT_ADMIN_REDIRECT_URI,
    )
    if "error" in result:
        raise RuntimeError(f"{result.get('error')}: {result.get('error_description')}")
    return result


def _refresh(refresh_token: str) -> dict:
    result = _msal_app().acquire_token_by_refresh_token(refresh_token, scopes=GRAPH_SCOPES)
    if "error" in result:
        raise RuntimeError(f"{result.get('error')}: {result.get('error_description')}")
    return result


def get_access_token(db: Session, admin_user_id: int) -> str | None:
    record = token_service.get_tokens(db, admin_user_id, "microsoft365")
    if not record or not record.access_token:
        return None

    if record.expires_at and record.expires_at <= datetime.now(timezone.utc) and record.refresh_token:
        result = _refresh(record.refresh_token)
        token_service.save_tokens(
            db, admin_user_id, "microsoft365",
            access_token=result["access_token"],
            refresh_token=result.get("refresh_token", record.refresh_token),
            expires_at=datetime.now(timezone.utc).fromtimestamp(
                datetime.now(timezone.utc).timestamp() + result.get("expires_in", 3600), tz=timezone.utc
            ),
        )
        return result["access_token"]

    return record.access_token


def _graph_get(access_token: str, path: str, plain_text: bool = False):
    resp = requests.get(
        f"{GRAPH_BASE}{path}",
        headers={"Authorization": f"Bearer {access_token}", "ConsistencyLevel": "eventual"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text if plain_text else resp.json()


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
    access_token = get_access_token(db, admin_user_id)
    if not access_token:
        return {"connected": False, "error": "Microsoft 365 admin is not connected."}

    result = {"connected": True, "error": None, "warnings": []}

    try:
        orgs = _graph_get(access_token, "/organization").get("value", [])
        if orgs:
            result["tenant_name"] = orgs[0].get("displayName")
            domains = orgs[0].get("verifiedDomains", [])
            default_domain = next((d["name"] for d in domains if d.get("isDefault")), None)
            result["tenant_domain"] = default_domain or (domains[0]["name"] if domains else None)
            result["tenant_created"] = orgs[0].get("createdDateTime")
    except requests.HTTPError as e:
        result["warnings"].append(f"Couldn't fetch organization info (needs admin consent): {e}")

    try:
        # $count needs the ConsistencyLevel header (already sent) and
        # returns a plain integer as text, not JSON.
        count_text = _graph_get(access_token, "/users/$count", plain_text=True)
        result["total_users"] = int(count_text)
    except (requests.HTTPError, ValueError) as e:
        result["total_users"] = None
        result["warnings"].append(f"Couldn't fetch user count: {e}")

    # License seat utilization — doesn't depend on the usage-report
    # pipeline below (which needs a licensed user and can lag 24-48h), so
    # this is real, immediately-available "is this tenant actually being
    # used" signal even for a brand-new tenant with no licensed users yet.
    try:
        skus = _graph_get(access_token, "/subscribedSkus").get("value", [])
        result["total_licenses"] = sum(s.get("prepaidUnits", {}).get("enabled", 0) for s in skus)
        result["licenses_used"] = sum(s.get("consumedUnits", 0) for s in skus)
        result["license_skus"] = [
            {
                "name": s.get("skuPartNumber"),
                "enabled": s.get("prepaidUnits", {}).get("enabled", 0),
                "consumed": s.get("consumedUnits", 0),
            }
            for s in skus
        ]
    except requests.HTTPError as e:
        result["warnings"].append(f"Couldn't fetch license info: {e}")

    # OneDrive storage usage — Graph's usage-report endpoints return CSV,
    # not JSON, for most tenants. Column names below were UNVERIFIED
    # against a live tenant until now — confirmed correct against a real
    # response (empty rows, since no user in the test tenant had a
    # license yet, but the header row matched exactly).
    try:
        csv_text = requests.get(
            f"{GRAPH_BASE}/reports/getOneDriveUsageAccountDetail(period='D7')",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        csv_text.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(csv_text.text)))
        used = sum(int(r.get("Storage Used (Byte)", 0) or 0) for r in rows)
        allocated = sum(int(r.get("Storage Allocated (Byte)", 0) or 0) for r in rows)
        result["onedrive_account_count"] = len(rows)
        result["storage_used"] = format_bytes(used)
        result["storage_total"] = format_bytes(allocated) if allocated else "Unknown"
        result["storage_percent"] = round((used / allocated) * 100, 1) if allocated else None
        if not rows:
            result["warnings"].append(
                "No OneDrive accounts found in the usage report — this is expected if no "
                "user has been assigned a license yet (OneDrive isn't provisioned until "
                "then), or if a user was just licensed within the last 24-48h (Microsoft's "
                "usage reports lag behind real-time)."
            )
    except (requests.HTTPError, ValueError, csv.Error) as e:
        result["warnings"].append(f"Couldn't fetch OneDrive usage report: {e}")

    # The usage report above lags real-time by 24-48h, so a just-licensed
    # user's storage won't show up there for a while even once OneDrive
    # is provisioned. /users/{id}/drive is real-time but only works once
    # provisioning actually completes — confirmed live: it 403s with
    # "provisioningNotAllowed" (not a permissions error) for a licensed
    # user whose OneDrive isn't ready yet. Only used to OVERRIDE the
    # report above when it actually returns real data for at least one
    # user; otherwise the report's own (already-explained) zero stands.
    try:
        licensed_users = [
            u for u in _graph_get(access_token, "/users?$select=id,assignedLicenses").get("value", [])
            if u.get("assignedLicenses")
        ]
        live_used = live_total = live_count = 0
        for u in licensed_users:
            try:
                quota = _graph_get(access_token, f"/users/{u['id']}/drive").get("quota", {})
            except requests.HTTPError:
                continue
            if quota.get("used") is not None:
                live_used += quota["used"]
                live_total += quota.get("total") or 0
                live_count += 1
        if live_count:
            result["onedrive_account_count"] = live_count
            result["storage_used"] = format_bytes(live_used)
            result["storage_total"] = format_bytes(live_total) if live_total else "Unknown"
            result["storage_percent"] = round((live_used / live_total) * 100, 1) if live_total else None
            result["storage_source"] = "live per-user query (not the lagged usage report)"
    except requests.HTTPError as e:
        result["warnings"].append(f"Couldn't fetch live per-user drive data: {e}")

    # External sharing exposure has no simple aggregate endpoint in Graph
    # the way file/storage counts do — a real per-site walk is available,
    # but it's slow (see generate_security_report), so it's kept out of
    # this fast, page-load report deliberately rather than making every
    # tenant page load pay for it.
    result["external_sharing_note"] = (
        "Microsoft Graph doesn't expose a simple tenant-wide "
        "'externally shared items' count the way file/storage totals are "
        "available — load the Security & Governance report separately "
        "for a real (but slower) per-site sharing breakdown."
    )

    if not result["warnings"]:
        result.pop("warnings", None)

    return result


# ---------------------------------------------------------
# SECURITY & GOVERNANCE REPORT — deliberately separate from
# generate_tenant_report() above. That one is fast (a handful of cheap
# calls) and used on page load; everything below walks every SharePoint
# site's drive, which is exactly the kind of per-item work that made the
# personal OneDrive report take 90+ seconds before switching to `delta` —
# so this is fetched on demand via its own endpoint/button, never
# automatically. Every metric is independently try/excepted, same as
# generate_tenant_report(): a wrong field name shows up as one warning,
# not a broken page.
# ---------------------------------------------------------


def _get_mfa_coverage(access_token: str) -> dict:
    """Needs the AuditLog.Read.All scope added alongside this feature.
    Confirmed live against a real tenant: this report ALSO needs Azure AD
    Premium P1/P2 regardless of scope — Graph returns a distinct error
    (`Authentication_RequestFromNonPremiumTenantOrB2CTenant`) rather than
    a plain permissions 403, which generate_security_report() specifically
    detects to give an accurate "needs Premium licensing" message instead
    of a generic failure. Field names below (isMfaRegistered etc.) are
    still unverified against a real premium-tenant response — this
    codebase's own test tenant doesn't have that licensing."""
    details = _graph_get(access_token, "/reports/authenticationMethods/userRegistrationDetails").get("value", [])
    total = len(details)
    mfa_registered = sum(1 for d in details if d.get("isMfaRegistered"))
    return {
        "total_users": total,
        "mfa_registered": mfa_registered,
        "mfa_coverage_percent": round((mfa_registered / total) * 100, 1) if total else None,
    }


def _get_inactive_accounts(access_token: str) -> dict:
    """Approximates inactivity from OneDrive usage-report "Last Activity
    Date" — true sign-in-based inactivity needs the `signInActivity`
    field on /users, which 403s without Azure AD Premium P1. This is a
    real but weaker signal: a user active in email/Teams but not OneDrive
    would show up here as inactive."""
    resp = requests.get(
        f"{GRAPH_BASE}/reports/getOneDriveUsageAccountDetail(period='D180')",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(resp.text)))

    now = datetime.now(timezone.utc)
    buckets = {"0-30": 0, "31-60": 0, "61-90": 0, "90+": 0, "unknown": 0}
    for row in rows:
        raw_date = row.get("Last Activity Date")
        try:
            last_active = datetime.strptime(raw_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
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

    return {
        "total_accounts": len(rows),
        "days_since_last_onedrive_activity": buckets,
        "approximation_note": (
            "Based on OneDrive activity only, not true sign-in data (that needs Azure AD "
            "Premium P1, not included in your current plan) — a user active in email or "
            "Teams but not OneDrive would still show up here as inactive."
        ),
    }


def _guest_domain(guest: dict) -> str:
    email = guest.get("mail")
    if email and "@" in email:
        return email.split("@")[-1].lower()
    upn = guest.get("userPrincipalName") or ""
    if "#EXT#" in upn:
        local = upn.split("#EXT#")[0]
        if "_" in local:
            return local.rsplit("_", 1)[-1].lower()
    return "unknown"


def _get_guest_accounts(access_token: str) -> dict:
    resp = requests.get(
        f"{GRAPH_BASE}/users?$filter=userType eq 'Guest'&$select=id,displayName,mail,userPrincipalName",
        headers={"Authorization": f"Bearer {access_token}", "ConsistencyLevel": "eventual"},
        timeout=30,
    )
    resp.raise_for_status()
    guests = resp.json().get("value", [])

    by_domain: dict[str, int] = {}
    for g in guests:
        domain = _guest_domain(g)
        by_domain[domain] = by_domain.get(domain, 0) + 1

    return {"total_guests": len(guests), "by_external_domain": by_domain}


def _get_audit_events(access_token: str) -> dict:
    """Directory/admin actions only (user, role, app, group changes) —
    file-level sharing and deletion events live in the separate Office
    365 Management Activity API (its own OAuth resource, not part of
    Microsoft Graph), not covered here."""
    resp = requests.get(
        f"{GRAPH_BASE}/auditLogs/directoryAudits?$top=25&$orderby=activityDateTime desc",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    events = resp.json().get("value", [])

    def _actor(e: dict) -> str | None:
        initiated_by = e.get("initiatedBy") or {}
        user = (initiated_by.get("user") or {}).get("userPrincipalName")
        if user:
            return user
        return (initiated_by.get("app") or {}).get("displayName")

    return {
        "recent_events": [
            {
                "activity": e.get("activityDisplayName"),
                "date": e.get("activityDateTime"),
                "actor": _actor(e),
                "result": e.get("result"),
            }
            for e in events
        ],
        "note": (
            "Directory/admin actions only (user, role, app changes) — file-level sharing "
            "and deletion events aren't included; those live in the separate Office 365 "
            "Management Activity API."
        ),
    }


def _list_sites(access_token: str) -> list[dict]:
    return _graph_get(access_token, "/sites?search=*").get("value", [])


def _walk_site_drive_sharing(access_token: str, site_id: str) -> tuple[int, int, int]:
    """Same delta + `shared` facet approach verified for the personal
    OneDrive connector (integrations_core/microsoft.py's
    _list_all_items), pointed at a SharePoint site's drive instead of
    /me/drive. Returns (total_items, anyone_with_link_count, shared_count)."""
    total = anyone_with_link = shared_other = 0
    url = f"{GRAPH_BASE}/sites/{site_id}/drive/root/delta?$select=shared,deleted,parentReference"
    while url:
        resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("value", []):
            if item.get("deleted"):
                continue
            if item.get("parentReference", {}).get("path") is None:
                continue  # the drive-root bookkeeping entry, not real content
            total += 1
            shared_facet = item.get("shared")
            if shared_facet:
                if shared_facet.get("scope") == "anonymous":
                    anyone_with_link += 1
                else:
                    shared_other += 1
        url = data.get("@odata.nextLink")
    return total, anyone_with_link, shared_other


def _analyze_site(access_token: str, site: dict) -> dict:
    site_name = site.get("displayName") or site.get("name") or site["id"]
    result: dict = {"name": site_name, "site_id": site["id"]}

    try:
        drive = _graph_get(access_token, f"/sites/{site['id']}/drive")
        quota = drive.get("quota", {})
        used, total = quota.get("used"), quota.get("total")
        result["_storage_used_bytes"] = used or 0
        result["storage_used"] = format_bytes(used)
        result["storage_total"] = format_bytes(total) if total else "Unknown"
        result["storage_percent"] = round((used / total) * 100, 1) if used and total else None
    except requests.HTTPError as e:
        result["storage_error"] = str(e)

    try:
        total_files, anyone_with_link, shared_other = _walk_site_drive_sharing(access_token, site["id"])
        result["total_files"] = total_files
        result["anyone_with_link_count"] = anyone_with_link
        result["shared_count"] = shared_other
    except requests.HTTPError as e:
        result["sharing_error"] = str(e)

    return result


def generate_security_report(db: Session, admin_user_id: int) -> dict:
    access_token = get_access_token(db, admin_user_id)
    if not access_token:
        return {"connected": False, "error": "Microsoft 365 admin is not connected."}

    result: dict = {"connected": True, "error": None, "warnings": []}

    try:
        result["mfa_coverage"] = _get_mfa_coverage(access_token)
    except requests.HTTPError as e:
        if e.response is not None and "RequestFromNonPremiumTenantOrB2CTenant" in e.response.text:
            result["warnings"].append(
                "MFA registration coverage requires Azure AD Premium P1 or P2 licensing — "
                "confirmed live that this isn't available on your current plan (not a bug, "
                "a licensing wall)."
            )
        else:
            result["warnings"].append(f"Couldn't fetch MFA registration coverage: {e}")

    try:
        result["inactive_accounts"] = _get_inactive_accounts(access_token)
    except (requests.HTTPError, ValueError, csv.Error) as e:
        result["warnings"].append(f"Couldn't fetch inactive-account data: {e}")

    try:
        result["guest_accounts"] = _get_guest_accounts(access_token)
    except requests.HTTPError as e:
        result["warnings"].append(f"Couldn't fetch guest account data: {e}")

    try:
        result["audit_events"] = _get_audit_events(access_token)
    except requests.HTTPError as e:
        result["warnings"].append(f"Couldn't fetch audit log events: {e}")

    sites: list[dict] = []
    try:
        sites = _list_sites(access_token)
    except requests.HTTPError as e:
        result["warnings"].append(f"Couldn't list SharePoint sites: {e}")

    site_details = [_analyze_site(access_token, s) for s in sites]
    result["sites"] = [{k: v for k, v in s.items() if k != "_storage_used_bytes"} for s in site_details]
    result["external_sharing"] = {
        "total_files_scanned": sum(s.get("total_files", 0) for s in site_details),
        "anyone_with_link_count": sum(s.get("anyone_with_link_count", 0) for s in site_details),
        "shared_count": sum(s.get("shared_count", 0) for s in site_details),
    }
    total_storage_bytes = sum(s.get("_storage_used_bytes") or 0 for s in site_details)
    result["total_storage_used"] = format_bytes(total_storage_bytes) if total_storage_bytes else "Unknown"
    if total_storage_bytes:
        token_service.record_storage_snapshot(db, admin_user_id, "microsoft365", total_storage_bytes)
    result["storage_growth"] = token_service.compute_storage_growth(db, admin_user_id, "microsoft365")

    if not result["warnings"]:
        result.pop("warnings", None)

    return result
