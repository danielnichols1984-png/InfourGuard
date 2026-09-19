"""Dropbox integration: OAuth flow, file listing, and report generation."""
from pathlib import Path

import dropbox
from dropbox.files import FolderMetadata
from sqlalchemy.orm import Session

from shared.integrations_core import service as token_service
from shared.integrations_core.config import settings
from shared.integrations_core.email_utils import send_email


def get_client(db: Session, user_id: int) -> dropbox.Dropbox | None:
    record = token_service.get_tokens(db, user_id, "dropbox")
    if not record or not record.access_token:
        return None

    return dropbox.Dropbox(
        oauth2_access_token=record.access_token,
        oauth2_refresh_token=record.refresh_token,
        app_key=settings.DROPBOX_APP_KEY,
        app_secret=settings.DROPBOX_APP_SECRET,
    )


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
    if ext in [".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".pages"]:
        return "Document"
    if ext in [".xls", ".xlsx", ".csv", ".tsv", ".numbers"]:
        return "Spreadsheet"
    if ext in [".ppt", ".pptx", ".key"]:
        return "Presentation"
    if ext in [".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".heic", ".bmp", ".tiff"]:
        return "Image"
    if ext in [".mp4", ".mov", ".avi", ".mkv", ".mp3", ".wav", ".m4a", ".flac"]:
        return "Media"
    if ext in [".zip", ".tar", ".gz", ".rar", ".7z", ".pkg"]:
        return "Archive"
    if ext in [".py", ".js", ".ts", ".html", ".css", ".json", ".xml", ".yaml", ".yml", ".sql", ".sh", ".c", ".cpp"]:
        return "Code"
    return "Other"


def generate_report(db: Session, user_id: int) -> dict:
    dbx = get_client(db, user_id)
    if not dbx:
        return {
            "connected": False,
            "error": "Dropbox is not connected.",
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
        account = dbx.users_get_current_account()
        space = dbx.users_get_space_usage()
        used_bytes = space.used
        if space.allocation.is_individual():
            allocated_bytes = space.allocation.get_individual().allocated
        elif space.allocation.is_team():
            allocated_bytes = space.allocation.get_team().allocated
        else:
            allocated_bytes = None

        shared_link_paths = {
            link.path_lower for link in dbx.sharing_list_shared_links().links if link.path_lower
        }

        result = dbx.files_list_folder("", recursive=True)
        entries = list(result.entries)
        while result.has_more:
            result = dbx.files_list_folder_continue(result.cursor)
            entries.extend(result.entries)

        files = []
        for entry in entries:
            is_folder = isinstance(entry, FolderMetadata)
            path_lower = entry.path_lower or ""
            has_shared_link = path_lower in shared_link_paths
            has_sharing_info = bool(getattr(entry, "sharing_info", None))
            shared = has_sharing_info or has_shared_link
            size_bytes = 0 if is_folder else getattr(entry, "size", 0)

            files.append(
                {
                    "name": entry.name,
                    "path": getattr(entry, "path_display", entry.name),
                    "is_folder": is_folder,
                    "category": "Folder" if is_folder else categorize_filename(entry.name),
                    "formatted_size": "-" if is_folder else format_file_size(size_bytes),
                    "formatted_date": (
                        entry.server_modified.strftime("%Y-%m-%d")
                        if getattr(entry, "server_modified", None)
                        else "-"
                    ),
                    "anyone_with_link": has_shared_link,
                    "shared": shared,
                    "sharing_label": (
                        "Anyone with link" if has_shared_link else "Shared" if shared else "Private"
                    ),
                    "sharing_badge": (
                        "public" if has_shared_link else "shared" if shared else "private"
                    ),
                    "webViewLink": f"https://www.dropbox.com/home{getattr(entry, 'path_display', '')}",
                }
            )

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
            "user_email": account.email,
            "user_name": account.name.display_name,
            "storage_used": format_file_size(used_bytes),
            "total_quota": format_file_size(allocated_bytes) if allocated_bytes else "Unlimited",
            "storage_percent": (
                min(round((used_bytes / allocated_bytes) * 100, 1), 100) if allocated_bytes else 0
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
            "error": f"Dropbox error: {e}",
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
        return data.get("error", "Dropbox is not connected.")

    lines = [
        "=== Dropbox Report ===",
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
    return send_email(to_email, "Dropbox Report", report_text)
