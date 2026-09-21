"""Microsoft OneDrive (personal) integration: OAuth flow, file listing, and
report generation — mirrors google.py/dropbox_integration.py's shape so
generate_report()/render_report_text()/send_report_email() all return the
same fields across all three providers.

Uses MSAL (Microsoft's own auth library) for the OAuth flow, the same
library tenants_core's Microsoft 365 admin connector already uses — same
Azure AD app registration, different (much narrower) delegated scopes and
a different redirect URI. See MICROSOFT_* in integrations_core/config.py
and the .env comments for exact setup.
"""
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

import msal
import quickxorhash
import requests
from sqlalchemy.orm import Session

from shared.integrations_core import service as token_service
from shared.integrations_core.config import settings
from shared.integrations_core.email_utils import send_email

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _msal_app() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        client_id=settings.MICROSOFT_CLIENT_ID,
        client_credential=settings.MICROSOFT_CLIENT_SECRET,
        authority=f"https://login.microsoftonline.com/{settings.MICROSOFT_TENANT}",
    )


def get_authorization_url(state: str) -> str:
    return _msal_app().get_authorization_request_url(
        scopes=settings.microsoft_scopes_list,
        state=state,
        redirect_uri=settings.MICROSOFT_REDIRECT_URI,
    )


def exchange_code_for_token(code: str) -> dict:
    result = _msal_app().acquire_token_by_authorization_code(
        code=code,
        scopes=settings.microsoft_scopes_list,
        redirect_uri=settings.MICROSOFT_REDIRECT_URI,
    )
    if "error" in result:
        raise RuntimeError(f"{result.get('error')}: {result.get('error_description')}")
    return result


def _refresh(refresh_token: str) -> dict:
    result = _msal_app().acquire_token_by_refresh_token(refresh_token, scopes=settings.microsoft_scopes_list)
    if "error" in result:
        raise RuntimeError(f"{result.get('error')}: {result.get('error_description')}")
    return result


def get_access_token(db: Session, user_id: int) -> str | None:
    record = token_service.get_tokens(db, user_id, "microsoft")
    if not record or not record.access_token:
        return None

    if record.expires_at and record.expires_at <= datetime.now(timezone.utc) and record.refresh_token:
        result = _refresh(record.refresh_token)
        token_service.save_tokens(
            db, user_id, "microsoft",
            access_token=result["access_token"],
            refresh_token=result.get("refresh_token", record.refresh_token),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=result.get("expires_in", 3600)),
        )
        return result["access_token"]

    return record.access_token


def _graph_get(access_token: str, path: str) -> dict:
    resp = requests.get(f"{GRAPH_BASE}{path}", headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def format_file_size(size_bytes) -> str:
    if size_bytes is None:
        return "-"
    try:
        size = float(size_bytes)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"
    except (ValueError, TypeError):
        return "-"


def categorize_filename(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        return "PDF"
    if ext in [".doc", ".docx", ".odt", ".rtf", ".txt", ".md"]:
        return "Document"
    if ext in [".xls", ".xlsx", ".csv", ".tsv"]:
        return "Spreadsheet"
    if ext in [".ppt", ".pptx"]:
        return "Presentation"
    if ext in [".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".heic", ".bmp", ".tiff"]:
        return "Image"
    if ext in [".mp4", ".mov", ".avi", ".mkv", ".mp3", ".wav", ".m4a", ".flac"]:
        return "Media"
    if ext in [".zip", ".tar", ".gz", ".rar", ".7z"]:
        return "Archive"
    if ext in [".py", ".js", ".ts", ".html", ".css", ".json", ".xml", ".yaml", ".yml", ".sql", ".sh", ".c", ".cpp"]:
        return "Code"
    return "Other"


_ROOT_PATH_PREFIX = "/drive/root:"


def _list_all_items(access_token: str) -> list[dict]:
    """Flat listing of everything in the drive via Graph's `delta`
    endpoint — a single paginated stream covering the whole drive
    recursively, unlike walking `/children` folder by folder (which needs
    one request per folder). Confirmed live against a real ~1000-item
    drive: delta covers it in ~8s across 6 pages; the folder-by-folder
    walk took over 90s for the same drive — a real, user-visible
    difference, not a micro-optimization.

    Two things delta returns that a plain listing wouldn't, both filtered
    out here since this is a one-shot report rather than a sync consumer:
    a handful of drive-root bookkeeping entries (identifiable by a
    `parentReference` with no `path` — confirmed live) and, in general,
    tombstone entries for deleted items (a `deleted` facet).

    Sharing status comes from driveItem's `shared` facet, included right
    in this same $select — it's `null`/absent for a private item and a
    populated object (with a `scope` of "anonymous"/"organization"/"users")
    for a shared one. Earlier attempts got this wrong two different ways,
    both confirmed live against a real account: `$expand=permissions` on
    a children listing isn't supported by Graph (400 "notSupported"), and
    a per-item `/permissions` call "works" but always includes the
    owner's own grant — so `bool(permissions)` reads every item as
    shared. The `shared` facet is the actually-correct signal.
    """
    entries: list[dict] = []
    url = (
        f"{GRAPH_BASE}/me/drive/root/delta"
        "?$select=id,name,folder,file,size,lastModifiedDateTime,webUrl,shared,parentReference,deleted"
    )
    while url:
        resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("value", []):
            if item.get("deleted"):
                continue
            parent_path = item.get("parentReference", {}).get("path")
            if parent_path is None:
                continue  # the drive-root bookkeeping entry, not real content

            is_folder = "folder" in item
            shared_facet = item.get("shared")
            shared = shared_facet is not None
            anyone_with_link = shared and shared_facet.get("scope") == "anonymous"

            relative_folder = parent_path[len(_ROOT_PATH_PREFIX):].lstrip("/")
            relative_path = f"{relative_folder}/{item['name']}" if relative_folder else item["name"]

            entries.append(
                {
                    "name": item["name"],
                    "path": relative_path,
                    "is_folder": is_folder,
                    "category": "Folder" if is_folder else categorize_filename(item["name"]),
                    "formatted_size": "-" if is_folder else format_file_size(item.get("size")),
                    "formatted_date": (
                        item["lastModifiedDateTime"][:10] if item.get("lastModifiedDateTime") else "-"
                    ),
                    "anyone_with_link": anyone_with_link,
                    "shared": shared,
                    "sharing_label": (
                        "Anyone with link" if anyone_with_link else "Shared" if shared else "Private"
                    ),
                    "sharing_badge": (
                        "public" if anyone_with_link else "shared" if shared else "private"
                    ),
                    "webViewLink": item.get("webUrl"),
                }
            )
        url = data.get("@odata.nextLink")
    return entries


def generate_report(db: Session, user_id: int) -> dict:
    access_token = get_access_token(db, user_id)
    if not access_token:
        return {
            "connected": False,
            "error": "Microsoft OneDrive is not connected.",
            "user_email": None,
            "user_name": None,
            "storage_used": "-",
            "total_quota": "-",
            "storage_percent": 0,
            "total_files": 0,
            "total_folders": 0,
            "anyone_with_link_count": 0,
            "shared_count": 0,
            "private_count": 0,
            "categories": {},
            "files": [],
        }

    try:
        me = _graph_get(access_token, "/me")
        drive = _graph_get(access_token, "/me/drive")
        quota = drive.get("quota", {})
        used_bytes = quota.get("used")
        total_bytes = quota.get("total")

        files = _list_all_items(access_token)

        total_files = sum(1 for f in files if not f["is_folder"])
        total_folders = sum(1 for f in files if f["is_folder"])
        anyone_with_link_count = sum(1 for f in files if f["anyone_with_link"])
        shared_count = sum(1 for f in files if f["shared"] and not f["anyone_with_link"])
        private_count = sum(1 for f in files if not f["shared"])

        categories: dict[str, int] = {}
        for f in files:
            categories[f["category"]] = categories.get(f["category"], 0) + 1

        return {
            "connected": True,
            "user_email": me.get("mail") or me.get("userPrincipalName"),
            "user_name": me.get("displayName"),
            "storage_used": format_file_size(used_bytes),
            "total_quota": format_file_size(total_bytes) if total_bytes else "Unknown",
            "storage_percent": (
                min(round((used_bytes / total_bytes) * 100, 1), 100) if used_bytes and total_bytes else 0
            ),
            "total_files": total_files,
            "total_folders": total_folders,
            "anyone_with_link_count": anyone_with_link_count,
            "shared_count": shared_count,
            "private_count": private_count,
            "categories": categories,
            "files": files,
            "error": None,
        }
    except Exception as e:
        return {
            "connected": True,
            "error": f"Microsoft OneDrive error: {e}",
            "user_email": None,
            "user_name": None,
            "storage_used": "-",
            "total_quota": "-",
            "storage_percent": 0,
            "total_files": 0,
            "total_folders": 0,
            "anyone_with_link_count": 0,
            "shared_count": 0,
            "private_count": 0,
            "categories": {},
            "files": [],
        }


def render_report_text(data: dict) -> str:
    if not data.get("connected") or data.get("error"):
        return data.get("error", "Microsoft OneDrive is not connected.")

    lines = [
        "=== Microsoft OneDrive Report ===",
        f"Connected Account: {data.get('user_name')} ({data.get('user_email')})",
        f"Storage Usage: {data['storage_used']} / {data['total_quota']} ({data['storage_percent']}% used)",
        f"Total Files: {data['total_files']} | Total Folders: {data['total_folders']}",
        (
            f"Sharing Audit: {data['anyone_with_link_count']} Public, "
            f"{data['shared_count']} Shared, {data['private_count']} Private"
        ),
        "",
        "Category Breakdown:",
    ]
    for cat, count in sorted(data["categories"].items(), key=lambda x: x[1], reverse=True):
        lines.append(f"  - {cat}: {count}")

    lines.append("")
    lines.append("Files:")
    if not data["files"]:
        lines.append("No files found.")
    else:
        for f in data["files"]:
            lines.append(f"- [{f['sharing_label']}] {f['name']} | {f['category']} | {f['formatted_size']}")
    return "\n".join(lines)


def send_report_email(to_email: str, report_text: str) -> bool:
    return send_email(to_email, "Microsoft OneDrive Report", report_text)


# ---------------------------------------------------------
# MIGRATION SUPPORT: folder tree walking, download/upload, sharing
# ---------------------------------------------------------

ONEDRIVE_SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024  # Graph's documented safe limit for PUT .../content
ONEDRIVE_UPLOAD_CHUNK_SIZE = 10 * 1024 * 1024  # must be a multiple of 320 KiB per Graph docs


def compute_quickxorhash(data: bytes) -> str:
    """OneDrive's own upload-integrity hash — Microsoft's proprietary
    QuickXorHash algorithm (not in hashlib), returned base64-encoded to
    match the `file.hashes.quickXorHash` field Graph reports. Verified
    against a real downloaded file's Graph-reported hash before trusting
    this for migration integrity checks — see quickxorhash on PyPI."""
    h = quickxorhash.quickxorhash()
    h.update(data)
    return base64.b64encode(h.digest()).decode()


def resolve_path_to_item_id(access_token: str, path: str) -> str:
    if not path:
        return "root"
    resp = requests.get(
        f"{GRAPH_BASE}/me/drive/root:/{path}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def list_folder_tree(access_token: str, root_ref: str) -> list[dict]:
    """Flat listing of everything under root_ref (a driveItem id, or the
    literal "root"), each carrying a `relative_path` relative to that
    root — the migration engine's ProviderAdapter.list_tree() contract.

    Uses `delta` scoped to root_ref rather than a manual per-folder walk,
    for the same reason generate_report() does (see _list_all_items) —
    confirmed live to be an order of magnitude faster. Scoping delta to a
    non-root item still returns each child's `parentReference.path` as an
    ABSOLUTE path from the drive root, not relative to root_ref, so
    root_ref's own absolute path is fetched once up front and stripped
    from every entry.
    """
    if root_ref == "root":
        root_abs_path = _ROOT_PATH_PREFIX
    else:
        root_info = _graph_get(access_token, f"/me/drive/items/{root_ref}?$select=name,parentReference")
        parent_path = root_info.get("parentReference", {}).get("path", _ROOT_PATH_PREFIX)
        root_abs_path = f"{parent_path}/{root_info['name']}"

    entries: list[dict] = []
    url = (
        f"{GRAPH_BASE}/me/drive/items/{root_ref}/delta"
        "?$select=id,name,folder,file,size,parentReference,shared,deleted"
    )
    while url:
        resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("value", []):
            if item.get("deleted") or item["id"] == root_ref:
                continue
            parent_path = item.get("parentReference", {}).get("path")
            if parent_path is None:
                continue  # the drive-root bookkeeping entry, not real content

            is_folder = "folder" in item
            full_path = f"{parent_path}/{item['name']}"
            relative_path = full_path[len(root_abs_path):].lstrip("/")
            file_facet = item.get("file") or {}
            shared_facet = item.get("shared")

            entries.append(
                {
                    "relative_path": relative_path,
                    "is_folder": is_folder,
                    "size": int(item.get("size") or 0),
                    "native_hash": None if is_folder else file_facet.get("hashes", {}).get("quickXorHash"),
                    "mime_type": None if is_folder else file_facet.get("mimeType"),
                    "ref": item["id"],
                    "anyone_with_link": bool(shared_facet and shared_facet.get("scope") == "anonymous"),
                }
            )
        url = data.get("@odata.nextLink")
    return entries


def ensure_folder(access_token: str, parent_id: str, name: str) -> str:
    """Creates `name` under parent_id if it doesn't exist yet. Returns its
    id either way — a 409 conflict means it already exists, so that case
    just looks the existing folder up instead of raising."""
    resp = requests.post(
        f"{GRAPH_BASE}/me/drive/items/{parent_id}/children",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
        timeout=30,
    )
    if resp.status_code == 409:
        lookup = requests.get(
            f"{GRAPH_BASE}/me/drive/items/{parent_id}:/{name}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        lookup.raise_for_status()
        return lookup.json()["id"]
    resp.raise_for_status()
    return resp.json()["id"]


def download_file_bytes(access_token: str, item_id: str) -> bytes:
    resp = requests.get(
        f"{GRAPH_BASE}/me/drive/items/{item_id}/content",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.content


def _chunked_upload(access_token: str, parent_id: str, name: str, data: bytes) -> dict:
    session_resp = requests.post(
        f"{GRAPH_BASE}/me/drive/items/{parent_id}:/{name}:/createUploadSession",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        timeout=30,
    )
    session_resp.raise_for_status()
    upload_url = session_resp.json()["uploadUrl"]

    total = len(data)
    result = None
    for start in range(0, total, ONEDRIVE_UPLOAD_CHUNK_SIZE):
        end = min(start + ONEDRIVE_UPLOAD_CHUNK_SIZE, total)
        chunk = data[start:end]
        # The upload session URL is pre-authenticated (embedded token) —
        # no Authorization header, sending one can actually cause a 401.
        resp = requests.put(
            upload_url,
            headers={"Content-Length": str(len(chunk)), "Content-Range": f"bytes {start}-{end - 1}/{total}"},
            data=chunk,
            timeout=120,
        )
        resp.raise_for_status()
        if resp.status_code in (200, 201):
            result = resp.json()
    return result


def upload_file_bytes(access_token: str, parent_id: str, name: str, data: bytes) -> dict:
    """Returns {"ref", "hash"}. Uses a chunked upload session above
    Graph's documented 4MB simple-upload threshold, the same size
    boundary Dropbox's own adapter uses (150MB there) for the same
    reason — a large simple PUT is unreliable and Graph explicitly
    recommends upload sessions past this size."""
    if len(data) <= ONEDRIVE_SIMPLE_UPLOAD_LIMIT:
        resp = requests.put(
            f"{GRAPH_BASE}/me/drive/items/{parent_id}:/{name}:/content",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/octet-stream"},
            data=data,
            timeout=120,
        )
        resp.raise_for_status()
        result = resp.json()
    else:
        result = _chunked_upload(access_token, parent_id, name, data)

    file_facet = (result or {}).get("file") or {}
    return {"ref": result["id"], "hash": file_facet.get("hashes", {}).get("quickXorHash")}


def apply_public_sharing(access_token: str, item_id: str) -> str:
    resp = requests.post(
        f"{GRAPH_BASE}/me/drive/items/{item_id}/createLink",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"type": "view", "scope": "anonymous"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("link", {}).get("webUrl")


def apply_named_sharing(access_token: str, item_id: str, email: str) -> bool:
    resp = requests.post(
        f"{GRAPH_BASE}/me/drive/items/{item_id}/invite",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={
            "recipients": [{"email": email}],
            "requireSignIn": False,
            "sendInvitation": False,
            "roles": ["read"],
        },
        timeout=30,
    )
    return resp.status_code == 200
