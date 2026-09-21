"""Google Drive integration: OAuth flow, file listing, and report generation."""
import hashlib
import io
import os
from pathlib import Path

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from sqlalchemy.orm import Session

from shared.integrations_core import service as token_service
from shared.integrations_core.config import settings
from shared.integrations_core.email_utils import send_email

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

# oauthlib refuses to complete a token exchange over plain HTTP. Only
# relax that for an http:// redirect URI (i.e. local dev) — a real
# https:// GOOGLE_REDIRECT_URI in production leaves the check intact.
if settings.GOOGLE_REDIRECT_URI and settings.GOOGLE_REDIRECT_URI.startswith("http://"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

# Safety net: Google can still grant a superset of the requested scope in
# some circumstances (e.g. a prior grant under a different scope list), and
# oauthlib's default behavior is to hard-fail the token exchange rather than
# just proceed with what was actually granted. We read the real granted
# scope back from the token response ourselves (see has_write_scope) and
# store that, so relaxing this check doesn't hide anything — it just stops
# a superset grant from blocking the exchange outright.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def build_flow(state: str | None = None) -> Flow:
    client_config = {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=settings.google_scopes_list, state=state)
    flow.redirect_uri = settings.GOOGLE_REDIRECT_URI
    return flow


def _credentials_from_record(record) -> Credentials | None:
    if not record or not record.access_token:
        return None
    return Credentials(
        token=record.access_token,
        refresh_token=record.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=settings.google_scopes_list,
    )


def get_credentials(db: Session, user_id: int) -> Credentials | None:
    record = token_service.get_tokens(db, user_id, "google")
    creds = _credentials_from_record(record)
    if not creds:
        return None

    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        token_service.save_tokens(
            db,
            user_id,
            "google",
            access_token=creds.token,
            refresh_token=creds.refresh_token,
            expires_at=creds.expiry,
        )
    return creds


WRITE_SCOPE = "https://www.googleapis.com/auth/drive"


def has_write_scope(db: Session, user_id: int) -> bool:
    """Whether the user's *actually granted* scope (recorded at connect
    time, not our current config's wishlist) includes full read/write
    Drive access. Anything short of that (drive.readonly, drive.file, or no
    stored scope at all — e.g. connected before scope tracking existed)
    can't be used as a migration destination or have sharing set on it."""
    record = token_service.get_tokens(db, user_id, "google")
    if not record or not record.scope:
        return False
    return WRITE_SCOPE in record.scope.split()


def build_drive_service(creds: Credentials):
    return build("drive", "v3", credentials=creds)


def compute_md5(data: bytes) -> str:
    """Google's own upload-integrity hash — plain MD5 of the file bytes."""
    return hashlib.md5(data).hexdigest()


# ---------------------------------------------------------
# MIGRATION SUPPORT: folder tree walking, download/upload, sharing
# ---------------------------------------------------------


def resolve_path_to_folder_id(service, path: str, drive_id: str | None = None) -> str:
    """Resolves a human path like "Team/Archive" to a Drive folder id.

    Drive has no real paths (files are attached to parent ids, and names
    aren't unique), so this walks one path segment at a time from "root",
    taking the first matching folder at each level. Good enough for a
    migration root path chosen by an admin/user who knows their own
    folder structure; not a general path-resolution guarantee.

    drive_id, when set, scopes every lookup to one Shared Drive (via
    corpora="drive"/supportsAllDrives) instead of the caller's own My
    Drive — see migrations_core's GoogleAdapter for how this gets
    threaded through for a business-storage migration. "root" in that
    case starts from the Shared Drive's own root, which Drive treats the
    same as a personal My Drive root for `parents` purposes.
    """
    parent_id = drive_id if drive_id else "root"
    segments = [s for s in path.strip("/").split("/") if s]
    for segment in segments:
        escaped = segment.replace("'", "\\'")
        query = (
            f"name = '{escaped}' and mimeType = '{FOLDER_MIME_TYPE}' "
            f"and '{parent_id}' in parents and trashed = false"
        )
        list_kwargs = {"q": query, "fields": "files(id, name)", "pageSize": 1}
        if drive_id:
            list_kwargs.update(corpora="drive", driveId=drive_id, includeItemsFromAllDrives=True, supportsAllDrives=True)
        response = service.files().list(**list_kwargs).execute()
        matches = response.get("files", [])
        if not matches:
            raise FileNotFoundError(f"Folder not found: {path!r} (missing segment {segment!r})")
        parent_id = matches[0]["id"]
    return parent_id


def list_folder_tree(service, root_folder_id: str, drive_id: str | None = None) -> list[dict]:
    """Flat list of every file/folder under root_folder_id, each carrying a
    `relative_path` computed from its position in the tree (BFS). See
    resolve_path_to_folder_id for what drive_id does."""
    tree: list[dict] = []
    queue: list[tuple[str, str]] = [(root_folder_id, "")]

    while queue:
        folder_id, prefix = queue.pop(0)
        page_token = None
        while True:
            list_kwargs = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": (
                    "nextPageToken, files(id, name, mimeType, size, md5Checksum, "
                    "modifiedTime, permissions(type, role, emailAddress))"
                ),
                "pageSize": 1000,
                "pageToken": page_token,
            }
            if drive_id:
                list_kwargs.update(corpora="drive", driveId=drive_id, includeItemsFromAllDrives=True, supportsAllDrives=True)
            response = service.files().list(**list_kwargs).execute()
            for f in response.get("files", []):
                is_folder = f.get("mimeType") == FOLDER_MIME_TYPE
                relative_path = f"{prefix}/{f['name']}" if prefix else f["name"]
                f["is_folder"] = is_folder
                f["relative_path"] = relative_path
                tree.append(f)
                if is_folder:
                    queue.append((f["id"], relative_path))
            page_token = response.get("nextPageToken")
            if not page_token:
                break

    return tree


def ensure_folder(service, parent_id: str, name: str, drive_id: str | None = None) -> str:
    """Returns the id of a child folder named `name` under parent_id,
    creating it if it doesn't already exist. See resolve_path_to_folder_id
    for what drive_id does — also required on the create() call itself
    when the parent lives inside a Shared Drive, or Drive rejects it."""
    escaped = name.replace("'", "\\'")
    query = (
        f"name = '{escaped}' and mimeType = '{FOLDER_MIME_TYPE}' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    list_kwargs = {"q": query, "fields": "files(id)", "pageSize": 1}
    if drive_id:
        list_kwargs.update(corpora="drive", driveId=drive_id, includeItemsFromAllDrives=True, supportsAllDrives=True)
    response = service.files().list(**list_kwargs).execute()
    matches = response.get("files", [])
    if matches:
        return matches[0]["id"]

    create_kwargs = {
        "body": {"name": name, "mimeType": FOLDER_MIME_TYPE, "parents": [parent_id]},
        "fields": "id",
    }
    if drive_id:
        create_kwargs["supportsAllDrives"] = True
    created = service.files().create(**create_kwargs).execute()
    return created["id"]


def download_file_bytes(service, file_id: str, drive_id: str | None = None) -> bytes:
    kwargs = {"fileId": file_id}
    if drive_id:
        kwargs["supportsAllDrives"] = True
    request = service.files().get_media(**kwargs)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


# Native Google Docs/Sheets/etc. have no fixed byte representation (hence no
# md5Checksum, and get_media() doesn't work on them) — they must be
# exported to a real format instead. Mapped to editable formats, not PDF,
# since a migration is meant to produce a working file, not a snapshot.
GOOGLE_NATIVE_EXPORT_TARGETS: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}

# Native types with no exportable content at all — these get skipped
# outright rather than attempted and failed.
GOOGLE_NATIVE_UNEXPORTABLE_TYPES = {
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.site",
    "application/vnd.google-apps.script",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.fusiontable",
    "application/vnd.google-apps.jam",
}


def is_google_native_type(mime_type: str | None) -> bool:
    return bool(mime_type) and mime_type.startswith("application/vnd.google-apps.") and mime_type != FOLDER_MIME_TYPE


def get_export_target(mime_type: str) -> tuple[str, str] | None:
    """Returns (export_mime_type, file_extension) for a Google-native type
    that can be exported, or None if it can't be (Forms, Sites, Apps
    Script, ...) — those should be skipped, not attempted."""
    return GOOGLE_NATIVE_EXPORT_TARGETS.get(mime_type)


def export_file_bytes(service, file_id: str, export_mime_type: str) -> bytes:
    """Like download_file_bytes, but for a native Google Docs/Sheets/etc.
    file — converts it to `export_mime_type` server-side. Subject to
    Drive's 10MB export size limit."""
    request = service.files().export_media(fileId=file_id, mimeType=export_mime_type)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


def upload_file_bytes(service, parent_id: str, name: str, data: bytes, mime_type: str, drive_id: str | None = None) -> dict:
    """Uploads `data` as a new file under parent_id. Returns {"id", "md5Checksum"}.
    supportsAllDrives is required on create() when parent_id lives inside
    a Shared Drive — see resolve_path_to_folder_id for drive_id."""
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type or "application/octet-stream")
    create_kwargs = {
        "body": {"name": name, "parents": [parent_id]},
        "media_body": media,
        "fields": "id, md5Checksum",
    }
    if drive_id:
        create_kwargs["supportsAllDrives"] = True
    created = service.files().create(**create_kwargs).execute()
    return created


def apply_public_sharing(service, file_id: str, drive_id: str | None = None) -> None:
    kwargs = {"fileId": file_id, "body": {"type": "anyone", "role": "reader"}}
    if drive_id:
        kwargs["supportsAllDrives"] = True
    service.permissions().create(**kwargs).execute()


def apply_named_sharing(service, file_id: str, email: str, role: str = "reader", drive_id: str | None = None) -> None:
    kwargs = {
        "fileId": file_id,
        "body": {"type": "user", "role": role, "emailAddress": email},
        "sendNotificationEmail": False,
    }
    if drive_id:
        kwargs["supportsAllDrives"] = True
    service.permissions().create(**kwargs).execute()


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


def categorize_mime_type(mime_type: str, name: str) -> str:
    mime = (mime_type or "").lower()
    ext = Path(name).suffix.lower() if name else ""

    if "folder" in mime:
        return "Folder"
    if "pdf" in mime or ext == ".pdf":
        return "PDF"
    if "document" in mime or "word" in mime or ext in [".doc", ".docx", ".odt", ".rtf", ".txt", ".md"]:
        return "Document"
    if "spreadsheet" in mime or "sheet" in mime or ext in [".xls", ".xlsx", ".csv", ".tsv"]:
        return "Spreadsheet"
    if "presentation" in mime or "powerpoint" in mime or ext in [".ppt", ".pptx", ".key"]:
        return "Presentation"
    if mime.startswith("image/") or ext in [".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".heic"]:
        return "Image"
    if mime.startswith("video/") or mime.startswith("audio/") or ext in [
        ".mp4", ".mov", ".avi", ".mp3", ".wav", ".m4a",
    ]:
        return "Media"
    if "zip" in mime or "compressed" in mime or "tar" in mime or ext in [".zip", ".tar", ".gz", ".rar", ".7z"]:
        return "Archive"
    if ext in [".py", ".js", ".ts", ".html", ".css", ".json", ".xml", ".yaml", ".yml", ".sql", ".sh"]:
        return "Code"
    return "Other"


def _get_drive_about(service) -> dict:
    try:
        return (
            service.about()
            .get(fields="user(displayName, emailAddress), storageQuota(limit, usage, usageInDrive)")
            .execute()
        )
    except Exception:
        return {}


def _list_drive_files(service) -> list[dict]:
    files = []
    page_token = None
    while True:
        response = (
            service.files()
            .list(
                q="trashed = false",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                fields=(
                    "nextPageToken, files(id, name, mimeType, size, modifiedTime, "
                    "webViewLink, shared, permissions(type))"
                ),
                pageSize=1000,
                pageToken=page_token,
            )
            .execute()
        )

        for file in response.get("files", []):
            name = file.get("name", "Untitled")
            mime = file.get("mimeType", "")
            is_folder = "folder" in mime.lower()
            permissions = file.get("permissions") or []
            anyone_with_link = any(p.get("type") == "anyone" for p in permissions)
            shared = file.get("shared", False) or anyone_with_link

            file["is_folder"] = is_folder
            file["category"] = "Folder" if is_folder else categorize_mime_type(mime, name)
            file["anyone_with_link"] = anyone_with_link
            file["shared"] = shared
            file["sharing_label"] = (
                "Anyone with link" if anyone_with_link else "Shared" if shared else "Private"
            )
            file["sharing_badge"] = "public" if anyone_with_link else "shared" if shared else "private"
            file["formatted_size"] = "-" if is_folder else format_file_size(file.get("size"))
            file["formatted_date"] = (file.get("modifiedTime") or "")[:10] or "-"
            files.append(file)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return files


def generate_report(db: Session, user_id: int) -> dict:
    creds = get_credentials(db, user_id)
    if not creds:
        return {
            "connected": False,
            "error": "Google Drive is not connected.",
            "user_email": None,
            "user_name": None,
            "drive_usage": "-",
            "total_quota": "-",
            "storage_percent": 0,
            "total_files": 0,
            "total_folders": 0,
            "anyone_with_link_count": 0,
            "shared_count": 0,
            "private_count": 0,
            "categories": {},
            "files": [],
            "has_drive_scope": True,
        }

    try:
        service = build_drive_service(creds)
        about = _get_drive_about(service)
        user_info = about.get("user", {})
        quota = about.get("storageQuota", {})

        files = _list_drive_files(service)

        usage_bytes = int(quota.get("usageInDrive") or quota.get("usage") or 0)
        limit_bytes = int(quota["limit"]) if quota.get("limit") else None

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
            "user_email": user_info.get("emailAddress"),
            "user_name": user_info.get("displayName"),
            "drive_usage": format_file_size(usage_bytes),
            "total_quota": format_file_size(limit_bytes) if limit_bytes else "Unlimited",
            "storage_percent": (
                min(round((usage_bytes / limit_bytes) * 100, 1), 100) if limit_bytes else 0
            ),
            "total_files": total_files,
            "total_folders": total_folders,
            "anyone_with_link_count": anyone_with_link_count,
            "shared_count": shared_count,
            "private_count": private_count,
            "categories": categories,
            "files": files,
            "has_drive_scope": has_write_scope(db, user_id),
            "error": None,
        }
    except Exception as e:
        return {
            "connected": True,
            "error": f"Google Drive error: {e}",
            "user_email": None,
            "user_name": None,
            "drive_usage": "-",
            "total_quota": "-",
            "storage_percent": 0,
            "total_files": 0,
            "total_folders": 0,
            "anyone_with_link_count": 0,
            "shared_count": 0,
            "private_count": 0,
            "categories": {},
            "files": [],
            "has_drive_scope": has_write_scope(db, user_id),
        }


def render_report_text(data: dict) -> str:
    if not data.get("connected") or data.get("error"):
        return data.get("error", "Google Drive is not connected.")

    lines = [
        "=== Google Drive Report ===",
        f"Connected Account: {data.get('user_name') or ''} ({data.get('user_email')})",
        f"Storage Usage: {data['drive_usage']} / {data['total_quota']} ({data['storage_percent']}% used)",
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
            lines.append(
                f"- [{f['sharing_label']}] {f['name']} | {f['category']} | "
                f"{f['formatted_size']} | modified {f['formatted_date']}"
            )
    return "\n".join(lines)


def send_report_email(to_email: str, report_text: str) -> bool:
    return send_email(to_email, "Google Drive Report", report_text)
