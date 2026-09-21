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

TEAM_SCOPES = [
    "members.read",
    "team_info.read",
    "team_data.member",
    # Confirmed by introspecting the installed Dropbox SDK's own
    # docstring: team_team_folder_list() needs this specific scope, not
    # team_data.member. Read-only for now — the write-side scope needed
    # to actually migrate content INTO a team folder is still unverified,
    # to be confirmed once there's a connected Dropbox Business tenant to
    # test against.
    "team_data.content.read",
]


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
            result["_storage_used_bytes"] = total_used
        except dropbox.exceptions.ApiError as e:
            result["warnings"].append(
                f"Couldn't fetch per-member storage (needs 'Team member file access' app permission): {e}"
            )
    except dropbox.exceptions.ApiError as e:
        result["warnings"].append(f"Couldn't list team members: {e}")

    result["external_sharing_note"] = (
        "Dropbox's Team API doesn't expose a single tenant-wide "
        "'externally shared items' count directly — see generate_documents_report() "
        "for the real per-member shared-link listing this now supports."
    )

    if not result["warnings"]:
        result.pop("warnings", None)

    return result


def generate_storage_report(db: Session, admin_user_id: int) -> dict:
    """Storage totals + growth rate for the Storage & Growth tab. Reuses
    generate_tenant_report()'s existing per-member storage fetch rather
    than duplicating the as_user() loop, then records/reads a snapshot
    (shared TenantStorageSnapshot table) for the growth trend."""
    report = generate_tenant_report(db, admin_user_id)
    if not report.get("connected"):
        return report

    raw_bytes = report.pop("_storage_used_bytes", None)
    if raw_bytes:
        token_service.record_storage_snapshot(db, admin_user_id, "dropbox_business", raw_bytes)
    report["storage_growth"] = token_service.compute_storage_growth(db, admin_user_id, "dropbox_business")
    return report


def generate_security_report(db: Session, admin_user_id: int) -> dict:
    """Member status (active/suspended/invited) — a real signal available
    from the same team_members_list() call generate_tenant_report() already
    makes. Deliberately does NOT claim per-member 2FA status: unlike
    Google's isEnrolledIn2Sv or Microsoft's authentication methods
    endpoint, Dropbox's stable Team API doesn't expose a confirmed 2FA
    field on TeamMemberProfile — noted honestly rather than guessing at a
    field name that might not exist."""
    import dropbox

    dbx_team = get_team_client(db, admin_user_id)
    if not dbx_team:
        return {"connected": False, "error": "Dropbox Business is not connected."}

    result: dict = {"connected": True, "error": None, "warnings": []}

    try:
        members_result = dbx_team.team_members_list()
        members = list(members_result.members)
        while members_result.has_more:
            members_result = dbx_team.team_members_list_continue(members_result.cursor)
            members.extend(members_result.members)

        result["member_status"] = {
            "total_members": len(members),
            "active": sum(1 for m in members if m.profile.status.is_active()),
            "suspended": sum(1 for m in members if m.profile.status.is_suspended()),
            "invited_not_yet_active": sum(1 for m in members if m.profile.status.is_invited()),
        }
        result["mfa_coverage_note"] = (
            "Dropbox's Team API doesn't expose a confirmed, stable per-member 2FA status "
            "field the way Google/Microsoft's do — noted honestly rather than guessed at."
        )
    except dropbox.exceptions.ApiError as e:
        result["warnings"].append(f"Couldn't fetch team member status: {e}")

    if not result["warnings"]:
        result.pop("warnings", None)

    return result


def _link_visibility(link) -> str:
    """Defensive: Dropbox's ResolvedVisibility union shape is a real
    uncertainty point (unverified against a live team) — falls back to
    "unknown" rather than raising or guessing at a wrong tag."""
    try:
        visibility = link.link_permissions.resolved_visibility
        for tag in ("public", "team_only", "password", "team_and_password", "shared_folder_only"):
            if getattr(visibility, f"is_{tag}", lambda: False)():
                return tag
    except AttributeError:
        pass
    return "unknown"


def get_admin_team_member_id(dbx_team) -> str:
    """Whichever admin's token connected this Dropbox Business account —
    needed for as_admin() to reach team folders via namespace path root.
    Confirmed via the installed SDK's docstring: needs only
    team_info.read, already granted — no new scope for this specific
    call."""
    result = dbx_team.team_token_get_authenticated_admin()
    return result.admin_profile.team_member_id


def _list_team_folders(dbx_team) -> list[dict]:
    """Confirmed via the installed SDK's docstring that this needs
    team_data.content.read specifically (added to TEAM_SCOPES above),
    not team_data.member as originally assumed. Unverified against a
    live team with real team folders."""
    result = dbx_team.team_team_folder_list()
    folders = list(result.team_folders)
    while result.has_more:
        result = dbx_team.team_team_folder_list_continue(result.cursor)
        folders.extend(result.team_folders)
    return [{"team_folder_id": f.team_folder_id, "name": f.name} for f in folders if f.status.is_active()]


def _walk_team_folder(dbx_team, team_folder_id: str, folder_name: str) -> list[dict]:
    """Addresses a team folder's own namespace via as_admin() +
    with_path_root(namespace_id) (both confirmed to exist on the
    installed SDK), then the same files_list_folder call the personal
    Dropbox client already uses elsewhere in this codebase
    (integrations_core/dropbox_integration.py's list_folder_tree). The
    SDK methods are real; this exact namespace-addressing combination is
    unverified against a live team."""
    import dropbox
    from dropbox.common import PathRoot

    admin_id = get_admin_team_member_id(dbx_team)
    client = dbx_team.as_admin(admin_id).with_path_root(PathRoot.namespace_id(team_folder_id))

    result = client.files_list_folder("", recursive=True)
    entries = list(result.entries)
    while result.has_more:
        result = client.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)

    items: list[dict] = []
    for entry in entries:
        is_folder = isinstance(entry, dropbox.files.FolderMetadata)
        items.append(
            {
                "name": entry.name,
                "site": folder_name,
                "is_folder": is_folder,
                "size": 0 if is_folder else getattr(entry, "size", 0),
                "web_url": None,
                "shared": bool(getattr(entry, "sharing_info", None)),
                "scope": None,
            }
        )
    return items


def generate_documents_report(db: Session, admin_user_id: int) -> dict:
    """Two independent pieces: per-member shared-link visibility (via
    as_user() impersonation, already used for storage) and full team
    folder inventory (via as_admin() + namespace path root, new). Unlike
    Google Workspace, Dropbox Business's model gives both without a
    bigger trust escalation beyond the scope already added above."""
    import dropbox

    dbx_team = get_team_client(db, admin_user_id)
    if not dbx_team:
        return {"connected": False, "error": "Dropbox Business is not connected."}

    result: dict = {"connected": True, "error": None, "warnings": []}
    shared_items: list[dict] = []

    try:
        members_result = dbx_team.team_members_list()
        members = list(members_result.members)
        while members_result.has_more:
            members_result = dbx_team.team_members_list_continue(members_result.cursor)
            members.extend(members_result.members)

        for member in members:
            member_id = member.profile.team_member_id
            member_name = member.profile.name.display_name
            try:
                member_client = dbx_team.as_user(member_id)
                links_result = member_client.sharing_list_shared_links()
                for link in links_result.links:
                    shared_items.append(
                        {
                            "name": link.name,
                            "member": member_name,
                            "url": link.url,
                            "visibility": _link_visibility(link),
                        }
                    )
            except dropbox.exceptions.ApiError as e:
                result["warnings"].append(f"Couldn't list shared links for {member_name}: {e}")
    except dropbox.exceptions.ApiError as e:
        result["warnings"].append(f"Couldn't list team members: {e}")

    shared_items.sort(key=lambda i: i["visibility"] != "public")  # public (riskiest) first
    result["shared_items"] = shared_items
    result["total_shared"] = len(shared_items)
    result["anyone_with_link_count"] = sum(1 for i in shared_items if i["visibility"] == "public")

    all_items: list[dict] = []
    try:
        folders = _list_team_folders(dbx_team)
        for f in folders:
            try:
                all_items.extend(_walk_team_folder(dbx_team, f["team_folder_id"], f["name"]))
            except dropbox.exceptions.ApiError as e:
                result["warnings"].append(f"Couldn't walk team folder '{f['name']}': {e}")
    except dropbox.exceptions.ApiError as e:
        result["warnings"].append(f"Couldn't list team folders: {e}")

    result["all_items"] = all_items
    result["total_files"] = sum(1 for i in all_items if not i["is_folder"])
    result["total_folders"] = sum(1 for i in all_items if i["is_folder"])

    if not result["warnings"]:
        result.pop("warnings", None)

    return result
