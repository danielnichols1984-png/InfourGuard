"""Dropbox integration: OAuth flow, file listing, and report generation."""
import hashlib
from pathlib import Path

import dropbox
from dropbox.files import CommitInfo, FolderMetadata, UploadSessionCursor, WriteMode
from sqlalchemy.orm import Session

from shared.integrations_core import service as token_service
from shared.integrations_core.config import settings
from shared.integrations_core.email_utils import send_email

DROPBOX_HASH_BLOCK_SIZE = 4 * 1024 * 1024
DROPBOX_UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
DROPBOX_SIMPLE_UPLOAD_LIMIT = 150 * 1024 * 1024


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


def compute_dropbox_content_hash(data: bytes) -> str:
    """Dropbox's own upload-integrity hash: SHA-256 of each 4MB block,
    concatenated, then SHA-256 of that. Documented at
    https://www.dropbox.com/developers/reference/content-hash — computing
    this locally lets us verify an upload against the `content_hash`
    Dropbox reports back, with no extra API call."""
    block_hashes = b"".join(
        hashlib.sha256(data[i : i + DROPBOX_HASH_BLOCK_SIZE]).digest()
        for i in range(0, len(data), DROPBOX_HASH_BLOCK_SIZE)
    )
    return hashlib.sha256(block_hashes).hexdigest()


# ---------------------------------------------------------
# MIGRATION SUPPORT: folder tree walking, download/upload, sharing
# ---------------------------------------------------------


def list_folder_tree(dbx: dropbox.Dropbox, root_path: str) -> list[dict]:
    """Flat list of every file/folder under root_path, each carrying a
    `relative_path` relative to that root."""
    root_path = "" if root_path in ("", "/") else "/" + root_path.strip("/")

    result = dbx.files_list_folder(root_path, recursive=True)
    entries = list(result.entries)
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)

    shared_link_paths = {
        link.path_lower for link in dbx.sharing_list_shared_links().links if link.path_lower
    }

    tree = []
    prefix_len = len(root_path)
    for entry in entries:
        is_folder = isinstance(entry, FolderMetadata)
        full_path = entry.path_display or entry.name
        relative_path = full_path[prefix_len:].lstrip("/")
        if is_folder and not relative_path:
            # Confirmed live: when root_path is a named subfolder (not the
            # account root ""), Dropbox's recursive listing can include an
            # entry for that root folder itself. A blank relative_path can
            # never be a real child to migrate, so skip it — otherwise the
            # migration engine tries to recreate the root folder as a
            # child of itself at the destination.
            continue
        has_shared_link = (entry.path_lower or "") in shared_link_paths
        has_sharing_info = bool(getattr(entry, "sharing_info", None))

        tree.append(
            {
                "name": entry.name,
                "path": full_path,
                "relative_path": relative_path,
                "is_folder": is_folder,
                "size": 0 if is_folder else getattr(entry, "size", 0),
                "content_hash": None if is_folder else getattr(entry, "content_hash", None),
                "shared": has_sharing_info or has_shared_link,
                "anyone_with_link": has_shared_link,
            }
        )
    return tree


def ensure_folder(dbx: dropbox.Dropbox, path: str) -> str:
    """Creates `path` (and any missing parents) if it doesn't exist yet.
    Returns the path. Dropbox's create_folder_v2 already no-ops sanely for
    intermediate segments, so this just swallows the "already exists" case."""
    try:
        dbx.files_create_folder_v2(path)
    except dropbox.exceptions.ApiError as e:
        if not (hasattr(e.error, "is_path") and e.error.is_path() and e.error.get_path().is_conflict()):
            raise
    return path


def download_file_bytes(dbx: dropbox.Dropbox, path: str) -> bytes:
    _, response = dbx.files_download(path)
    return response.content


def upload_file_bytes(dbx: dropbox.Dropbox, path: str, data: bytes) -> dict:
    """Uploads `data` to `path`, using a chunked upload session for files
    over Dropbox's 150MB simple-upload limit. Returns
    {"path", "content_hash", "name"}.

    mode="add" + autorename=True (not "overwrite"): a migration retry
    never re-uploads an already-copied item (service.py's _run_mapping
    skips anything already marked status=="copied" in our own bookkeeping
    before ever calling this again), so overwrite mode was never actually
    needed for that case — it only meant a destination folder that
    happens to already contain an unrelated same-named file would get
    silently destroyed. Auto-rename means that file survives untouched
    and the incoming one lands under a Dropbox-assigned alternate name
    instead — callers must use the returned "name" (not the requested
    `path`'s name) as the item's actual destination name."""
    if len(data) <= DROPBOX_SIMPLE_UPLOAD_LIMIT:
        metadata = dbx.files_upload(data, path, mode=WriteMode("add"), autorename=True)
    else:
        session_start = dbx.files_upload_session_start(data[:DROPBOX_UPLOAD_CHUNK_SIZE])
        cursor = UploadSessionCursor(
            session_id=session_start.session_id, offset=DROPBOX_UPLOAD_CHUNK_SIZE
        )
        offset = DROPBOX_UPLOAD_CHUNK_SIZE
        while len(data) - offset > DROPBOX_UPLOAD_CHUNK_SIZE:
            chunk = data[offset : offset + DROPBOX_UPLOAD_CHUNK_SIZE]
            dbx.files_upload_session_append_v2(chunk, cursor)
            cursor.offset += len(chunk)
            offset += len(chunk)
        commit = CommitInfo(path=path, mode=WriteMode("add"), autorename=True)
        metadata = dbx.files_upload_session_finish(data[offset:], cursor, commit)

    return {"path": metadata.path_display, "content_hash": metadata.content_hash, "name": metadata.name}


def apply_public_sharing(dbx: dropbox.Dropbox, path: str) -> str:
    link = dbx.sharing_create_shared_link_with_settings(path)
    return link.url


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


_NOT_CONNECTED = {
    "connected": False,
    "error": "Dropbox is not connected.",
    "user_email": None,
    "user_name": None,
    "used_bytes": None,
    "allocated_bytes": None,
    "total_files": 0,
    "total_folders": 0,
    "anyone_with_link_count": 0,
    "shared_count": 0,
    "private_count": 0,
    "categories": {},
    "files": [],
}


def _gather(db: Session, user_id: int) -> dict:
    """Does the actual Dropbox API work once; generate_report() (Overview)
    and the Security/Documents/Storage tab report functions below all
    build their (differently-shaped) output from this same call instead
    of each re-fetching from Dropbox."""
    dbx = get_client(db, user_id)
    if not dbx:
        return dict(_NOT_CONNECTED)

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
            "error": None,
            "user_email": account.email,
            "user_name": account.name.display_name,
            "used_bytes": used_bytes,
            "allocated_bytes": allocated_bytes,
            "total_files": total_files,
            "total_folders": total_folders,
            "anyone_with_link_count": anyone_with_link_count,
            "shared_count": shared_count,
            "private_count": private_count,
            "categories": categories,
            "files": files,
        }
    except Exception as e:
        data = dict(_NOT_CONNECTED)
        data["connected"] = True
        data["error"] = f"Dropbox error: {e}"
        return data


def generate_report(db: Session, user_id: int) -> dict:
    data = _gather(db, user_id)
    used_bytes, allocated_bytes = data.pop("used_bytes", None), data.pop("allocated_bytes", None)
    data["storage_used"] = format_file_size(used_bytes)
    data["total_quota"] = "-" if used_bytes is None else format_file_size(allocated_bytes) if allocated_bytes else "Unlimited"
    data["storage_percent"] = min(round((used_bytes / allocated_bytes) * 100, 1), 100) if used_bytes and allocated_bytes else 0
    return data


def generate_documents_report(db: Session, user_id: int) -> dict:
    data = _gather(db, user_id)
    return {
        "connected": data["connected"],
        "error": data["error"],
        "files": data["files"],
        "categories": data["categories"],
        "total_files": data["total_files"],
        "total_folders": data["total_folders"],
    }


def generate_security_report(db: Session, user_id: int) -> dict:
    data = _gather(db, user_id)
    anyone_with_link_files = [f for f in data["files"] if f.get("anyone_with_link")]
    return {
        "connected": data["connected"],
        "error": data["error"],
        "anyone_with_link_count": data["anyone_with_link_count"],
        "shared_count": data["shared_count"],
        "private_count": data["private_count"],
        "anyone_with_link_files": anyone_with_link_files,
        # Dropbox has no API exposing a personal account's own 2FA status
        # to third-party apps — not a gap in this code, a real platform
        # limitation. Don't fabricate a status.
        "mfa": {
            "available": False,
            "note": "Dropbox doesn't expose two-step verification status to third-party apps. Check it directly at dropbox.com/account/security.",
        },
    }


def generate_storage_report(db: Session, user_id: int) -> dict:
    data = _gather(db, user_id)
    used_bytes, allocated_bytes = data.pop("used_bytes", None), data.pop("allocated_bytes", None)
    if data["connected"] and not data["error"] and used_bytes is not None:
        token_service.record_storage_snapshot(db, user_id, "dropbox", used_bytes)
    return {
        "connected": data["connected"],
        "error": data["error"],
        "storage_used": format_file_size(used_bytes),
        "storage_total": "-" if used_bytes is None else format_file_size(allocated_bytes) if allocated_bytes else "Unlimited",
        "storage_percent": min(round((used_bytes / allocated_bytes) * 100, 1), 100) if used_bytes and allocated_bytes else 0,
        "storage_growth": token_service.compute_storage_growth(db, user_id, "dropbox"),
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
