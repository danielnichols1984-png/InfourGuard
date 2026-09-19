"""Google Drive integration: OAuth flow, file listing, and report generation."""
import os
from pathlib import Path

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from shared.integrations_core import service as token_service
from shared.integrations_core.config import settings
from shared.integrations_core.email_utils import send_email

# oauthlib refuses to complete a token exchange over plain HTTP. Only
# relax that for an http:// redirect URI (i.e. local dev) — a real
# https:// GOOGLE_REDIRECT_URI in production leaves the check intact.
if settings.GOOGLE_REDIRECT_URI and settings.GOOGLE_REDIRECT_URI.startswith("http://"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


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
        service = build("drive", "v3", credentials=creds)
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

        granted_scopes = getattr(creds, "scopes", None) or []
        has_drive_scope = any("drive" in s.lower() for s in granted_scopes) if granted_scopes else True

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
            "has_drive_scope": has_drive_scope,
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
            "has_drive_scope": True,
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
