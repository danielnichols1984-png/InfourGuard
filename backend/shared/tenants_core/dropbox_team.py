"""Dropbox Business (Team) tenant-wide admin metrics.

Reuses the same Dropbox app (DROPBOX_APP_KEY/SECRET from integrations_core)
with a separate redirect URI and team-scoped permissions, and the SDK's
`DropboxTeam` client (a distinct class from the personal `Dropbox` client
already used elsewhere in this codebase) for team/business API calls.

Storage totals specifically require Dropbox's "Team member file access"
app permission (to act as each member and read their space usage) — a
more sensitive permission Dropbox gates separately from basic team-info
read access. If that permission isn't granted on the app, storage figures
degrade to "unavailable" rather than failing the whole report — see
generate_tenant_report(). Like the other two providers in this module,
none of this is verified against a live Dropbox Business team.
"""
from sqlalchemy.orm import Session

from shared.integrations_core.config import settings as integrations_settings
from shared.tenants_core import service as token_service
from shared.tenants_core.config import settings

TEAM_SCOPES = ["members.read", "team_info.read", "team_data.member"]


def build_flow(session: dict):
    from dropbox.oauth import DropboxOAuth2Flow

    return DropboxOAuth2Flow(
        consumer_key=integrations_settings.DROPBOX_APP_KEY,
        consumer_secret=integrations_settings.DROPBOX_APP_SECRET,
        redirect_uri=settings.DROPBOX_BUSINESS_REDIRECT_URI,
        session=session,
        csrf_token_session_key="dropbox-business-csrf",
        token_access_type="offline",
        scope=TEAM_SCOPES,
    )


def get_team_client(db: Session, admin_user_id: int):
    import dropbox

    record = token_service.get_tokens(db, admin_user_id, "dropbox_business")
    if not record or not record.access_token:
        return None
    return dropbox.DropboxTeam(
        oauth2_access_token=record.access_token,
        oauth2_refresh_token=record.refresh_token,
        app_key=integrations_settings.DROPBOX_APP_KEY,
        app_secret=integrations_settings.DROPBOX_APP_SECRET,
    )


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
    import dropbox

    dbx_team = get_team_client(db, admin_user_id)
    if not dbx_team:
        return {"connected": False, "error": "Dropbox Business is not connected."}

    result = {"connected": True, "error": None, "warnings": []}

    try:
        info = dbx_team.team_get_info()
        result["tenant_name"] = info.name
        result["total_users"] = getattr(info, "num_provisioned_users", None) or getattr(
            info, "num_licensed_users", None
        )
    except dropbox.exceptions.ApiError as e:
        result["total_users"] = None
        result["warnings"].append(f"Couldn't fetch team info (needs a team admin token): {e}")

    try:
        members_result = dbx_team.team_members_list()
        members = list(members_result.members)
        while members_result.has_more:
            members_result = dbx_team.team_members_list_continue(members_result.cursor)
            members.extend(members_result.members)
        if result.get("total_users") is None:
            result["total_users"] = len(members)

        # Storage totals need "Team member file access" — act as each
        # member and read their own space usage, then sum. Best-effort:
        # if the app doesn't have that permission, this whole block fails
        # once and we report storage as unavailable rather than partial.
        try:
            total_used = 0
            total_allocated = 0
            has_allocation_data = False
            for member in members:
                member_id = member.profile.team_member_id
                member_client = dbx_team.as_user(member_id)
                usage = member_client.users_get_space_usage()
                total_used += usage.used
                if usage.allocation.is_individual():
                    total_allocated += usage.allocation.get_individual().allocated
                    has_allocation_data = True

            result["storage_used"] = format_bytes(total_used)
            result["storage_total"] = format_bytes(total_allocated) if has_allocation_data else "Unknown"
            result["storage_percent"] = (
                round((total_used / total_allocated) * 100, 1) if total_allocated else None
            )
        except dropbox.exceptions.ApiError as e:
            result["warnings"].append(
                f"Couldn't fetch per-member storage (needs 'Team member file access' app permission): {e}"
            )
    except dropbox.exceptions.ApiError as e:
        result["warnings"].append(f"Couldn't list team members: {e}")

    result["external_sharing_note"] = (
        "Dropbox's Team API doesn't expose a single tenant-wide "
        "'externally shared items' count either — same limitation noted "
        "for Microsoft. Would need per-member shared-link enumeration "
        "(dbx_team.as_user(...).sharing_list_shared_links() per member), "
        "not implemented here yet."
    )

    if not result["warnings"]:
        result.pop("warnings", None)

    return result
