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
# AuditLog.Read.All was added for the (Premium-gated) MFA registration
# report and directory audit log events. Policy.Read.All and
# UserAuthenticationMethod.Read.All were added after confirming live that
# MFA visibility genuinely needs Azure AD Premium via the report endpoint
# — these two give a non-Premium alternative (Security Defaults status +
# per-user registered auth methods) instead. A connection made before any
# of these was added won't have them and needs to reconnect (Graph
# doesn't retroactively upgrade a token's granted scope).
GRAPH_SCOPES = [
    "Organization.Read.All",
    "User.Read.All",
    "Reports.Read.All",
    "AuditLog.Read.All",
    "Policy.Read.All",
    "UserAuthenticationMethod.Read.All",
    # Application.Read.All: lists service principals + their OAuth
    # consent grants (third-party app access). SharePointTenantSettings.
    # Read.All: the tenant-wide external-sharing policy setting — lives
    # on the Graph BETA endpoint, not v1.0, confirmed by a live 403 with
    # v1.0's URL rejected outright before this scope was added.
    "Application.Read.All",
    "SharePointTenantSettings.Read.All",
    # Sites.ReadWrite.All (replaces the earlier read-only Sites.Read.All —
    # a superset, so listed once) — this tenant admin connection is now
    # also the identity that performs SharePoint-site migrations
    # (migrations_core), not just site reporting. A deliberate choice:
    # one admin identity can migrate any site in the tenant, rather than
    # asking every individual employee to grant a broader personal scope.
    "Sites.ReadWrite.All",
]

GRAPH_BETA_BASE = "https://graph.microsoft.com/beta"


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
        try:
            result = _refresh(record.refresh_token)
        except RuntimeError:
            # Confirmed live: a refresh token can't silently pick up a
            # scope added after it was issued (e.g. GRAPH_SCOPES growing
            # this session) — Azure AD rejects it with invalid_grant
            # rather than partially honoring it. That's routine, expected
            # OAuth behavior whenever the requested scope changes, not a
            # bug — the fix is a fresh interactive connect, so this reads
            # as "not connected" (every caller already handles that
            # gracefully) rather than an unhandled 500.
            return None
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


def _get_security_defaults(access_token: str) -> dict:
    """Security Defaults is the free, tenant-wide "require MFA for
    everyone" toggle most small tenants without Conditional Access
    (a Premium P1 feature) rely on. A simple on/off, not per-user detail,
    but it's a real, accurate signal available on any licensing tier."""
    data = _graph_get(access_token, "/policies/identitySecurityDefaultsEnforcementPolicy")
    return {"enabled": bool(data.get("isEnabled"))}


_MFA_CAPABLE_METHOD_TYPES = {
    "#microsoft.graph.phoneAuthenticationMethod",
    "#microsoft.graph.microsoftAuthenticatorAuthenticationMethod",
    "#microsoft.graph.fido2AuthenticationMethod",
    "#microsoft.graph.windowsHelloForBusinessAuthenticationMethod",
    "#microsoft.graph.softwareOathAuthenticationMethod",
    "#microsoft.graph.temporaryAccessPassAuthenticationMethod",
}


def _get_mfa_coverage_per_user(access_token: str) -> dict:
    """Non-Premium alternative to _get_mfa_coverage(): lists each user's
    own registered authentication methods (/users/{id}/authentication/
    methods) rather than the aggregate report, and checks for any method
    beyond a plain password. This is a basic per-user identity lookup,
    not a "report," so it isn't gated behind Azure AD Premium the way
    userRegistrationDetails is — confirmed live against a Business Basic
    tenant with no Premium add-on."""
    users = _graph_get(access_token, "/users?$select=id,displayName,userPrincipalName").get("value", [])
    per_user = []
    registered = 0
    unknown = 0
    last_error: requests.HTTPError | None = None
    for u in users:
        try:
            methods = _graph_get(access_token, f"/users/{u['id']}/authentication/methods").get("value", [])
            has_mfa = any(m.get("@odata.type") in _MFA_CAPABLE_METHOD_TYPES for m in methods)
        except requests.HTTPError as e:
            has_mfa = None
            unknown += 1
            last_error = e
        if has_mfa:
            registered += 1
        per_user.append(
            {
                "name": u.get("displayName"),
                "upn": u.get("userPrincipalName"),
                "mfa_registered": has_mfa,
            }
        )

    total = len(users)
    # If every single per-user lookup failed, this is a scope/permission
    # problem (e.g. UserAuthenticationMethod.Read.All not actually
    # granted yet), not "confirmed 0% MFA coverage" — surface it as the
    # failure it is rather than reporting fabricated-looking certainty.
    if total and unknown == total and last_error is not None:
        raise last_error

    checkable = total - unknown
    return {
        "total_users": total,
        "mfa_registered": registered,
        "mfa_coverage_percent": round((registered / checkable) * 100, 1) if checkable else None,
        "unknown_count": unknown,
        "users": per_user,
        "source": "per-user registered authentication methods (works without Premium licensing)",
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


def _get_teams_activity(access_token: str) -> dict:
    """Same CSV-report pattern as the OneDrive usage report, pointed at
    Teams user activity instead — active users and message counts over
    the period, per Microsoft's documented column names. Column names
    unverified against a live tenant with real Teams usage (the test
    tenant this was built against has none yet)."""
    resp = requests.get(
        f"{GRAPH_BASE}/reports/getTeamsUserActivityUserDetail(period='D30')",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(resp.text)))

    active_users = sum(1 for r in rows if (r.get("Last Activity Date") or "").strip())
    total_channel_messages = sum(int(r.get("Team Chat Message Count", 0) or 0) for r in rows)
    total_calls = sum(int(r.get("Call Count", 0) or 0) for r in rows)

    return {
        "total_users": len(rows),
        "active_users_30d": active_users,
        "channel_messages_30d": total_channel_messages,
        "calls_30d": total_calls,
    }


def _get_mailbox_usage(access_token: str) -> dict:
    """Same CSV-report pattern again, for Exchange mailbox storage.
    Column names per Microsoft's documented schema, unverified against a
    live tenant the same way the OneDrive report's columns were confirmed
    (no licensed mailbox exists yet in the test tenant to check rows
    against, only that the header row itself matches)."""
    resp = requests.get(
        f"{GRAPH_BASE}/reports/getMailboxUsageDetail(period='D30')",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(resp.text)))

    used = sum(int(r.get("Storage Used (Byte)", 0) or 0) for r in rows)
    total_item_count = sum(int(r.get("Item Count", 0) or 0) for r in rows)

    return {
        "total_mailboxes": len(rows),
        "storage_used": format_bytes(used),
        "total_item_count": total_item_count,
    }


# Microsoft's own first-party service principals all share this
# well-known home tenant id — used to filter first-party Microsoft apps
# (Office, Teams, etc.) out of the "third-party app access" list below,
# since those aren't the kind of access grant this metric is meant to
# surface.
_MICROSOFT_FIRST_PARTY_TENANT_ID = "f8cdef31-a31e-4b4a-93e4-5f571e91255a"


def _get_app_grants(access_token: str) -> dict:
    """Which apps (third-party or otherwise) have been granted delegated
    OAuth access to this tenant's data, and what scopes. Needs
    Application.Read.All. Unverified against a live response with real
    third-party apps installed — this test tenant has none yet, so the
    grants list will legitimately be empty or Microsoft-only even when
    this works correctly."""
    grants = _graph_get(access_token, "/oauth2PermissionGrants?$top=200").get("value", [])

    sp_lookup: dict[str, dict] = {}
    for sp_id in {g["clientId"] for g in grants if g.get("clientId")}:
        try:
            sp_lookup[sp_id] = _graph_get(
                access_token, f"/servicePrincipals/{sp_id}?$select=id,displayName,appOwnerOrganizationId"
            )
        except requests.HTTPError:
            continue

    apps: dict[str, dict] = {}
    for g in grants:
        sp = sp_lookup.get(g.get("clientId"), {})
        if sp.get("appOwnerOrganizationId") == _MICROSOFT_FIRST_PARTY_TENANT_ID:
            continue  # Microsoft's own first-party apps, not third-party access
        name = sp.get("displayName") or g.get("clientId") or "Unknown app"
        entry = apps.setdefault(name, {"name": name, "scopes": set(), "consent_type": g.get("consentType"), "grant_count": 0})
        entry["scopes"].update((g.get("scope") or "").split())
        entry["grant_count"] += 1

    app_list = [{**a, "scopes": sorted(a["scopes"])} for a in apps.values()]
    app_list.sort(key=lambda a: -a["grant_count"])

    return {"total_apps": len(app_list), "apps": app_list}


def _get_sharing_policy(access_token: str) -> dict:
    """The tenant-wide SharePoint/OneDrive external-sharing SETTING (a
    single policy value — "is external sharing even allowed, and at what
    level"), not the per-file scan _walk_site_drive_sharing does. Lives
    on Graph's BETA endpoint, not v1.0 — field names per Microsoft's beta
    docs, unverified against a live response."""
    resp = requests.get(
        f"{GRAPH_BETA_BASE}/admin/sharepoint/settings",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "sharing_capability": data.get("sharingCapability"),
        "sharing_domain_restriction_mode": data.get("sharingDomainRestrictionMode"),
    }


def _list_sites(access_token: str) -> list[dict]:
    return _graph_get(access_token, "/sites?search=*").get("value", [])


def _walk_site_drive_sharing(
    access_token: str, site_id: str, site_name: str
) -> tuple[int, int, int, list[dict], list[dict]]:
    """Same delta + `shared` facet approach verified for the personal
    OneDrive connector (integrations_core/microsoft.py's
    _list_all_items), pointed at a SharePoint site's drive instead of
    /me/drive. Returns (total_items, anyone_with_link_count, shared_count,
    shared_items, all_items) — shared_items carries enough detail (name,
    link, scope) to list on the Shared Documents tab's "shared only" view;
    all_items is the complete inventory (every file/folder, shared or
    not) for the "all files" view and for feeding a real migration plan,
    not just a sharing tally."""
    total = anyone_with_link = shared_other = 0
    shared_items: list[dict] = []
    all_items: list[dict] = []
    url = (
        f"{GRAPH_BASE}/sites/{site_id}/drive/root/delta"
        "?$select=name,webUrl,shared,deleted,parentReference,folder,size"
    )
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
            scope = None
            if shared_facet:
                scope = shared_facet.get("scope") or "unknown"
                if scope == "anonymous":
                    anyone_with_link += 1
                else:
                    shared_other += 1
                shared_items.append(
                    {
                        "name": item.get("name"),
                        "site": site_name,
                        "web_url": item.get("webUrl"),
                        "scope": scope,
                    }
                )
            all_items.append(
                {
                    "name": item.get("name"),
                    "site": site_name,
                    "is_folder": "folder" in item,
                    "size": item.get("size") or 0,
                    "web_url": item.get("webUrl"),
                    "shared": bool(shared_facet),
                    "scope": scope,
                }
            )
        url = data.get("@odata.nextLink")
    return total, anyone_with_link, shared_other, shared_items, all_items


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
        total_files, anyone_with_link, shared_other, shared_items, all_items = _walk_site_drive_sharing(
            access_token, site["id"], site_name
        )
        result["total_files"] = total_files
        result["anyone_with_link_count"] = anyone_with_link
        result["shared_count"] = shared_other
        result["_shared_items"] = shared_items
        result["_all_items"] = all_items
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
            # Confirmed live: this specific report needs Azure AD Premium
            # regardless of scope. Falls back to the per-user method,
            # which works without it — see _get_mfa_coverage_per_user.
            try:
                result["mfa_coverage"] = _get_mfa_coverage_per_user(access_token)
            except requests.HTTPError as e2:
                result["warnings"].append(f"Couldn't fetch per-user MFA methods: {e2}")
        else:
            result["warnings"].append(f"Couldn't fetch MFA registration coverage: {e}")

    try:
        result["security_defaults"] = _get_security_defaults(access_token)
    except requests.HTTPError as e:
        result["warnings"].append(f"Couldn't fetch Security Defaults policy: {e}")

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

    try:
        result["teams_activity"] = _get_teams_activity(access_token)
    except (requests.HTTPError, ValueError, csv.Error) as e:
        result["warnings"].append(f"Couldn't fetch Teams activity: {e}")

    try:
        result["mailbox_usage"] = _get_mailbox_usage(access_token)
    except (requests.HTTPError, ValueError, csv.Error) as e:
        result["warnings"].append(f"Couldn't fetch mailbox usage: {e}")

    try:
        result["app_grants"] = _get_app_grants(access_token)
    except requests.HTTPError as e:
        result["warnings"].append(f"Couldn't fetch app access grants: {e}")

    try:
        result["sharing_policy"] = _get_sharing_policy(access_token)
    except requests.HTTPError as e:
        result["warnings"].append(f"Couldn't fetch tenant sharing policy: {e}")

    sites: list[dict] = []
    try:
        sites = _list_sites(access_token)
    except requests.HTTPError as e:
        result["warnings"].append(f"Couldn't list SharePoint sites: {e}")

    site_details = [_analyze_site(access_token, s) for s in sites]
    result["sites"] = [{k: v for k, v in s.items() if not k.startswith("_")} for s in site_details]
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


def generate_storage_report(db: Session, admin_user_id: int) -> dict:
    """Thin wrapper: generate_security_report() already computes per-site
    storage and the growth rate as part of its site walk, so this reuses
    it rather than duplicating that walk — kept as its own function
    purely for routing symmetry with the other two providers, whose
    storage report is a separate, cheaper call."""
    return generate_security_report(db, admin_user_id)


def generate_documents_report(db: Session, admin_user_id: int) -> dict:
    """Populates the Shared Documents tab: an actual item-level listing of
    externally-shared files across every SharePoint site, not just a
    tally. Separate from generate_security_report() even though both walk
    the same sites, since a page only needs one or the other and there's
    no reason to pay for both walks on every load."""
    access_token = get_access_token(db, admin_user_id)
    if not access_token:
        return {"connected": False, "error": "Microsoft 365 admin is not connected."}

    result: dict = {"connected": True, "error": None, "warnings": []}

    sites: list[dict] = []
    try:
        sites = _list_sites(access_token)
    except requests.HTTPError as e:
        result["warnings"].append(f"Couldn't list SharePoint sites: {e}")

    site_details = [_analyze_site(access_token, s) for s in sites]
    shared_items: list[dict] = []
    all_items: list[dict] = []
    for s in site_details:
        shared_items.extend(s.get("_shared_items") or [])
        all_items.extend(s.get("_all_items") or [])
        if s.get("sharing_error"):
            result["warnings"].append(f"Couldn't check sharing on site '{s['name']}': {s['sharing_error']}")

    shared_items.sort(key=lambda i: i["scope"] != "anonymous")  # anonymous (riskiest) first
    result["shared_items"] = shared_items
    result["total_shared"] = len(shared_items)
    result["anyone_with_link_count"] = sum(1 for i in shared_items if i["scope"] == "anonymous")

    # Complete inventory — every file/folder across every site, not just
    # the ones that are shared. This is what a real migration plan needs
    # (see migrations_core's business-storage support), not just a
    # sharing-exposure count.
    result["all_items"] = all_items
    result["total_files"] = sum(1 for i in all_items if not i["is_folder"])
    result["total_folders"] = sum(1 for i in all_items if i["is_folder"])
    result["total_bytes"] = sum(i.get("size") or 0 for i in all_items if not i["is_folder"])

    if not result["warnings"]:
        result.pop("warnings", None)

    return result
