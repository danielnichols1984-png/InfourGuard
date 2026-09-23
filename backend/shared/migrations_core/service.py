"""The migration engine: job/mapping CRUD, pre-stage (plan without
transferring bytes), full run (the actual copy), reports, and archiving.

Pre-stage and full run are meant to be invoked as FastAPI background tasks
(see routes.py) — each entry point here opens its own DB session rather
than accepting one from the caller, since a background task runs in a
worker thread after the triggering request (and its session) has already
completed.
"""
import mimetypes
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import PurePosixPath

from sqlalchemy.orm import Session

from shared.integrations_core.email_utils import send_email
from shared.migrations_core.adapters import get_adapter, recreate_sharing
from shared.migrations_core.config import settings
from shared.migrations_core.db import SessionLocal
from shared.migrations_core.models import MigrationItem, MigrationJob, MigrationUserMapping

VALID_PROVIDERS = {"google", "dropbox", "microsoft"}


# --- CRUD ------------------------------------------------------------------


def create_job(
    db: Session,
    created_by_user_id: int,
    source_provider: str,
    destination_provider: str,
    notify_email: str | None = None,
) -> MigrationJob:
    job = MigrationJob(
        created_by_user_id=created_by_user_id,
        source_provider=source_provider,
        destination_provider=destination_provider,
        notify_email=notify_email,
        status="draft",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_job(db: Session, job_id: int) -> MigrationJob | None:
    return db.query(MigrationJob).filter(MigrationJob.id == job_id).first()


def list_jobs(db: Session, user_id: int | None) -> list[MigrationJob]:
    query = db.query(MigrationJob)
    if user_id is not None:
        query = query.filter(MigrationJob.created_by_user_id == user_id)
    return query.order_by(MigrationJob.id.desc()).all()


def add_mapping(
    db: Session,
    job: MigrationJob,
    source_user_id: int,
    destination_user_id: int,
    source_root_path: str = "",
    destination_root_path: str = "",
    source_container_type: str | None = None,
    source_container_id: str | None = None,
    destination_container_type: str | None = None,
    destination_container_id: str | None = None,
    preserve_metadata: bool = True,
) -> MigrationUserMapping:
    mapping = MigrationUserMapping(
        job_id=job.id,
        source_user_id=source_user_id,
        destination_user_id=destination_user_id,
        source_root_path=source_root_path,
        destination_root_path=destination_root_path,
        source_container_type=source_container_type,
        source_container_id=source_container_id,
        destination_container_type=destination_container_type,
        destination_container_id=destination_container_id,
        preserve_metadata=preserve_metadata,
        status="pending",
        stats={},
    )
    db.add(mapping)
    if job.status == "draft":
        job.status = "mapped"
    db.commit()
    db.refresh(mapping)
    return mapping


def get_mapping(db: Session, mapping_id: int) -> MigrationUserMapping | None:
    return db.query(MigrationUserMapping).filter(MigrationUserMapping.id == mapping_id).first()


def list_mappings(db: Session, job_id: int) -> list[MigrationUserMapping]:
    return (
        db.query(MigrationUserMapping)
        .filter(MigrationUserMapping.job_id == job_id)
        .order_by(MigrationUserMapping.id)
        .all()
    )


def remove_mapping(db: Session, mapping: MigrationUserMapping) -> None:
    db.delete(mapping)
    db.commit()


def archive_job(db: Session, job: MigrationJob) -> MigrationJob:
    job.status = "archived"
    db.commit()
    return job


def delete_job(db: Session, job: MigrationJob) -> None:
    """Cascades to the job's mappings and their items via the ORM
    relationship's cascade="all, delete-orphan" — no manual cleanup needed."""
    db.delete(job)
    db.commit()


def list_items(db: Session, mapping_id: int) -> list[MigrationItem]:
    return (
        db.query(MigrationItem)
        .filter(MigrationItem.mapping_id == mapping_id)
        .order_by(MigrationItem.id)
        .all()
    )


# --- Pre-stage: plan without transferring bytes -----------------------------


def _parse_source_modified(value) -> datetime | None:
    """Each adapter normalizes its provider's own timestamp shape to an
    ISO string (or None) under "source_modified_at" — see list_tree() in
    each GoogleAdapter/DropboxAdapter/MicrosoftAdapter. Dropbox's
    client_modified is a naive (no tzinfo) UTC datetime per Dropbox's own
    API convention once isoformat()'d, so a value with no offset is
    treated as UTC rather than left ambiguous."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _walk_and_plan(db: Session, job: MigrationJob, mapping: MigrationUserMapping) -> None:
    """Builds (or, on a mapping that's already been run before, MERGES) the
    plan for this mapping from a fresh source scan. This merge is what
    makes pre-stage double as a second/delta pass — see the plan this was
    built from (individual-migrations wizard work) for the full rationale.
    Matches existing MigrationItem rows to the new scan by source_path:

    - existing "copied" item, hash unchanged -> left completely alone
      (zero writes; this is what makes a rescan of a huge, mostly-
      unchanged tree cheap instead of a full re-copy).
    - existing item (any status), hash changed or never successfully
      copied -> snapshot fields refreshed in place, status reset to
      "planned" for (re-)copying. destination_ref is deliberately left
      as-is (not cleared) when one was already set from a prior
      successful copy — that's the signal _copy_item uses to call
      replace() instead of upload() for a genuine content update rather
      than creating a second file.
    - source_path with no existing row -> new "planned" item, same as
      today's first-pass behavior.
    - existing "copied" item whose source_path is missing from the new
      scan (deleted at the source since last pass) -> status set to the
      terminal "source_removed". The destination copy is never touched —
      purely informational, matching the "nothing ever deletes" rule
      this whole engine follows for the source side; extended here to
      mean the engine never deletes at the destination either, even when
      the source side has shrunk.
    - existing item that was never successfully copied and is now
      missing -> the row is just removed; it never had any destination-
      side effect, so there's nothing to preserve or flag.
    """
    source_adapter = get_adapter(job.source_provider)
    source_client = source_adapter.get_client(
        mapping.source_user_id, mapping.source_container_type, mapping.source_container_id
    )
    if not source_client:
        raise RuntimeError(f"Source user {mapping.source_user_id} hasn't connected {job.source_provider}")

    source_root_ref = source_adapter.resolve_root(
        source_client, mapping.source_root_path, mapping.source_container_type, mapping.source_container_id
    )
    tree = source_adapter.list_tree(source_client, source_root_ref)
    scanned_paths = {entry["relative_path"] for entry in tree}

    existing_by_path = {
        item.source_path: item
        for item in db.query(MigrationItem).filter(MigrationItem.mapping_id == mapping.id).all()
    }

    total_files = total_folders = total_bytes = total_skipped = 0
    total_new = total_changed = total_removed = 0

    for entry in tree:
        sharing_snapshot = None
        if job.source_provider == "google":
            sharing_snapshot = {"permissions": entry.get("permissions") or []}
        elif entry.get("anyone_with_link"):
            sharing_snapshot = {"anyone_with_link": True}

        skip_reason = entry.get("skip_reason")
        new_hash = entry.get("native_hash")
        new_modified = _parse_source_modified(entry.get("source_modified_at"))
        existing = existing_by_path.get(entry["relative_path"])

        if existing is None:
            db.add(
                MigrationItem(
                    mapping_id=mapping.id,
                    source_path=entry["relative_path"],
                    destination_path=entry["relative_path"],
                    source_ref=entry["ref"],
                    mime_type=entry.get("mime_type"),
                    export_mime_type=entry.get("export_mime_type"),
                    is_folder=entry["is_folder"],
                    size_bytes=entry["size"],
                    source_hash=new_hash,
                    source_modified_at=new_modified,
                    sharing=sharing_snapshot,
                    status="skipped" if skip_reason else "planned",
                    error=skip_reason,
                )
            )
            if not skip_reason and not entry["is_folder"]:
                total_new += 1
        elif skip_reason:
            existing.status, existing.error = "skipped", skip_reason
        elif existing.status == "copied" and (entry["is_folder"] or existing.source_hash == new_hash):
            pass  # unchanged (or a folder, which has no hash to compare) and already copied — untouched
        else:
            was_copied = existing.status == "copied"
            existing.source_ref = entry["ref"]
            existing.mime_type = entry.get("mime_type")
            existing.export_mime_type = entry.get("export_mime_type")
            existing.size_bytes = entry["size"]
            existing.source_hash = new_hash
            existing.source_modified_at = new_modified
            existing.sharing = sharing_snapshot
            existing.status = "planned"
            existing.error = None
            existing.verified = False
            # destination_ref intentionally left as-is — _copy_item uses
            # it to tell an update-in-place (replace) apart from a fresh
            # create (upload) when this item is next copied.
            if was_copied and not entry["is_folder"]:
                total_changed += 1

        if skip_reason:
            total_skipped += 1
        elif entry["is_folder"]:
            total_folders += 1
        else:
            total_files += 1
            total_bytes += entry["size"]

    for path, item in existing_by_path.items():
        if path in scanned_paths:
            continue
        if item.status == "copied":
            item.status = "source_removed"
            total_removed += 1
        else:
            db.delete(item)

    mapping.stats = {
        "planned_files": total_files,
        "planned_folders": total_folders,
        "planned_bytes": total_bytes,
        "skipped_items": total_skipped,
        "new_since_last_scan": total_new,
        "changed_since_last_scan": total_changed,
        "removed_from_source_since_last_scan": total_removed,
    }
    db.commit()


def run_prestage(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = get_job(db, job_id)
        if not job:
            return

        for mapping in list_mappings(db, job.id):
            mapping.status = "prestaging"
            mapping.error = None
            db.commit()
            try:
                _walk_and_plan(db, job, mapping)
                mapping.status = "prestaged"
            except Exception as e:
                mapping.status = "failed"
                mapping.error = str(e)
            db.commit()

        any_failed = any(m.status == "failed" for m in list_mappings(db, job.id))
        job.status = "failed" if any_failed and all(
            m.status == "failed" for m in list_mappings(db, job.id)
        ) else "prestaged"
        db.commit()
    finally:
        db.close()


# --- Full run: the actual copy ----------------------------------------------


# Guards the "not in folder_cache yet" branch below. By the time the
# parallel file-copy phase runs, every folder should already be in
# folder_cache (created up front, single-threaded) — this lock only exists
# so a still-missing folder can't get created twice by two worker threads
# racing the same cache miss.
_folder_creation_lock = threading.Lock()


def _resolve_dest_parent(dest_adapter, dest_client, dest_root_ref, item: MigrationItem, folder_cache: dict) -> str:
    parent = str(PurePosixPath(item.destination_path).parent)
    if parent in (".", "/", ""):
        return dest_root_ref
    if parent in folder_cache:
        return folder_cache[parent]

    with _folder_creation_lock:
        if parent in folder_cache:
            return folder_cache[parent]
        ref = dest_root_ref
        accumulated = ""
        for part in parent.split("/"):
            accumulated = f"{accumulated}/{part}" if accumulated else part
            if accumulated in folder_cache:
                ref = folder_cache[accumulated]
            else:
                ref = dest_adapter.ensure_child_folder(dest_client, ref, part)
                folder_cache[accumulated] = ref
        return ref


def _copy_item(
    db: Session,
    job: MigrationJob,
    item: MigrationItem,
    source_client,
    dest_client,
    dest_root_ref: str,
    folder_cache: dict,
    preserve_metadata: bool = True,
) -> None:
    source_adapter = get_adapter(job.source_provider)
    dest_adapter = get_adapter(job.destination_provider)

    dest_parent_ref = _resolve_dest_parent(dest_adapter, dest_client, dest_root_ref, item, folder_cache)

    if item.is_folder:
        name = PurePosixPath(item.destination_path).name
        child_ref = dest_adapter.ensure_child_folder(dest_client, dest_parent_ref, name)
        folder_cache[item.destination_path] = child_ref
        item.destination_ref = child_ref
        item.status = "copied"
        db.commit()
        return

    try:
        data = source_adapter.download(source_client, item.source_ref, item.export_mime_type)
    except Exception as e:
        item.status, item.error = "failed", f"download failed: {e}"
        db.commit()
        return

    if item.source_hash and source_adapter.compute_hash(data) != item.source_hash:
        item.status, item.error = "failed", "source hash mismatch after download (possible corruption)"
        db.commit()
        return

    name = PurePosixPath(item.destination_path).name
    mime_type = item.mime_type or mimetypes.guess_type(name)[0]
    modified_at = item.source_modified_at.isoformat() if preserve_metadata and item.source_modified_at else None

    # A destination_ref already present means this is a delta-rescan's
    # positively-identified update to a file THIS engine copied
    # previously (see _walk_and_plan) — replace its content in place
    # rather than creating a second file under an auto-renamed name.
    is_update = bool(item.destination_ref)

    try:
        if is_update:
            result = dest_adapter.replace(dest_client, item.destination_ref, data, mime_type, modified_at)
        else:
            result = dest_adapter.upload(dest_client, dest_parent_ref, name, data, mime_type, modified_at)
    except Exception as e:
        item.status, item.error = "failed", f"{'update' if is_update else 'upload'} failed: {e}"
        db.commit()
        return

    dest_hash = result.get("hash")
    verified = bool(dest_hash) and dest_adapter.compute_hash(data) == dest_hash

    if not is_update:
        # A destination-side name collision with an unrelated pre-existing
        # file gets auto-renamed by the provider (never overwritten — see
        # dropbox_integration.upload_file_bytes / microsoft.upload_file_bytes)
        # rather than raising, so reflect whatever name it actually landed
        # under in our own records instead of the one we planned. Doesn't
        # apply to an update — replace() targets a known ref directly and
        # never renames.
        actual_name = result.get("name")
        if actual_name and actual_name != name:
            item.destination_path = str(PurePosixPath(item.destination_path).with_name(actual_name))

    item.destination_ref = result.get("ref")
    item.destination_hash = dest_hash
    item.verified = verified

    if not verified:
        item.status, item.error = "failed", "destination hash mismatch after upload (possible corruption)"
        db.commit()
        return

    try:
        item.share_recreated = recreate_sharing(
            job.source_provider, dest_adapter, dest_client, item.destination_ref, item.sharing
        )
    except Exception:
        item.share_recreated = False  # best-effort — never fail an otherwise-good copy over sharing

    item.status = "copied"
    db.commit()


_thread_local = threading.local()


def _get_thread_clients(source_adapter, dest_adapter, mapping_ctx: dict):
    """One provider client pair per worker thread, built lazily on first
    use and reused for every file that thread goes on to process —
    get_client() does its own small DB round trip, not worth repeating per
    file. Safe across separate _run_mapping() calls because each one opens
    (and fully tears down) its own ThreadPoolExecutor, so a worker thread
    is never reused across two different mappings/credentials."""
    if not hasattr(_thread_local, "clients"):
        source_client = source_adapter.get_client(
            mapping_ctx["source_user_id"], mapping_ctx["source_container_type"], mapping_ctx["source_container_id"]
        )
        dest_client = dest_adapter.get_client(
            mapping_ctx["destination_user_id"],
            mapping_ctx["destination_container_type"],
            mapping_ctx["destination_container_id"],
        )
        _thread_local.clients = (source_client, dest_client)
    return _thread_local.clients


def _copy_item_threadsafe(job_id: int, item_id: int, mapping_ctx: dict, dest_root_ref: str, folder_cache: dict) -> None:
    """Runs one file's copy on its own DB session — SQLAlchemy sessions
    aren't thread-safe, so this can't share the session _run_mapping used
    to build the item list. Any failure here, including setup itself and
    not just the copy, is caught and recorded on the item rather than
    raised, so one bad file can't take down the rest of the parallel
    batch."""
    db = SessionLocal()
    try:
        job = db.get(MigrationJob, job_id)
        item = db.get(MigrationItem, item_id)
        source_adapter = get_adapter(job.source_provider)
        dest_adapter = get_adapter(job.destination_provider)
        source_client, dest_client = _get_thread_clients(source_adapter, dest_adapter, mapping_ctx)
        _copy_item(
            db, job, item, source_client, dest_client, dest_root_ref, folder_cache, mapping_ctx["preserve_metadata"]
        )
    except Exception as e:
        db.rollback()
        item = db.get(MigrationItem, item_id)
        item.status, item.error = "failed", f"unexpected error: {e}"
        db.commit()
    finally:
        db.close()


def _run_mapping(db: Session, job: MigrationJob, mapping: MigrationUserMapping) -> None:
    source_adapter = get_adapter(job.source_provider)
    dest_adapter = get_adapter(job.destination_provider)

    source_client = source_adapter.get_client(
        mapping.source_user_id, mapping.source_container_type, mapping.source_container_id
    )
    if not source_client:
        raise RuntimeError(f"Source user no longer has {job.source_provider} connected")
    dest_client = dest_adapter.get_client(
        mapping.destination_user_id, mapping.destination_container_type, mapping.destination_container_id
    )
    if not dest_client:
        raise RuntimeError(f"Destination user no longer has {job.destination_provider} connected")

    dest_root_ref = dest_adapter.ensure_path(
        dest_client, mapping.destination_root_path, mapping.destination_container_type, mapping.destination_container_id
    )

    items = list_items(db, mapping.id)
    # Folders must exist before anything can be uploaded into them.
    items.sort(key=lambda i: (i.destination_path.count("/"), not i.is_folder))

    folder_cache: dict[str, str] = {}
    folders, files = [], []
    for item in items:
        if item.status == "copied":
            if item.is_folder:
                folder_cache[item.destination_path] = item.destination_ref
            continue
        if item.status in ("skipped", "source_removed"):
            # "skipped": e.g. a Google Form — no content to copy, not an
            # error. "source_removed": a rescan found this gone from the
            # source since it was copied — nothing to (re-)copy, and the
            # destination is deliberately never touched for this case.
            continue
        (folders if item.is_folder else files).append(item)

    # Phase 1: folders, sequential, in depth order (guaranteed by the sort
    # above) — every file's parent must exist before that file can be
    # uploaded into it, and creating two folders concurrently risks a
    # provider-side duplicate.
    for item in folders:
        _copy_item(db, job, item, source_client, dest_client, dest_root_ref, folder_cache, mapping.preserve_metadata)

    # Phase 2: files, in parallel — each is an independent, I/O-bound
    # download+upload with no dependency on any other file now that every
    # folder from Phase 1 is already in folder_cache.
    if files:
        mapping_ctx = {
            "source_user_id": mapping.source_user_id,
            "source_container_type": mapping.source_container_type,
            "source_container_id": mapping.source_container_id,
            "destination_user_id": mapping.destination_user_id,
            "destination_container_type": mapping.destination_container_type,
            "destination_container_id": mapping.destination_container_id,
            "preserve_metadata": mapping.preserve_metadata,
        }
        with ThreadPoolExecutor(max_workers=settings.MAX_PARALLEL_WORKERS) as executor:
            futures = [
                executor.submit(_copy_item_threadsafe, job.id, item.id, mapping_ctx, dest_root_ref, folder_cache)
                for item in files
            ]
            for future in as_completed(futures):
                # _copy_item_threadsafe catches everything itself and
                # records failures on the item — .result() here is just to
                # surface a truly unexpected bug loudly rather than
                # swallow it silently.
                future.result()

    # The parallel workers each committed on their own session — expire
    # this session's cached objects so the recount below reflects those
    # commits rather than whatever was already loaded before Phase 2 ran.
    db.expire_all()
    items = list_items(db, mapping.id)
    mapping.stats = {
        **(mapping.stats or {}),
        "copied_files": sum(1 for i in items if i.status == "copied" and not i.is_folder),
        "copied_folders": sum(1 for i in items if i.status == "copied" and i.is_folder),
        "failed_items": sum(1 for i in items if i.status == "failed"),
        "copied_bytes": sum(i.size_bytes or 0 for i in items if i.status == "copied" and not i.is_folder),
        "shares_recreated": sum(1 for i in items if i.share_recreated),
    }
    db.commit()


def run_full_migration(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = get_job(db, job_id)
        if not job:
            return

        job.status = "running"
        db.commit()

        any_errors = False
        for mapping in list_mappings(db, job.id):
            # Only a never-prestaged mapping has nothing to run yet. Anything
            # else (prestaged / failed / completed / completed_with_errors)
            # is safe to (re-)run — _run_mapping skips items already marked
            # "copied", so retrying after fixing a failure just resumes.
            if mapping.status in ("pending", "prestaging"):
                continue

            mapping.status = "running"
            mapping.error = None
            db.commit()

            try:
                _run_mapping(db, job, mapping)
            except Exception as e:
                mapping.status = "failed"
                mapping.error = str(e)
                db.commit()
                any_errors = True
                continue

            failed_count = (mapping.stats or {}).get("failed_items", 0)
            mapping.status = "completed_with_errors" if failed_count else "completed"
            any_errors = any_errors or bool(failed_count)
            db.commit()

        job.status = "completed_with_errors" if any_errors else "completed"
        db.commit()

        notify_job_complete(db, job)
    finally:
        db.close()


def rerun_share_recreation(job_id: int) -> None:
    """Re-attempts sharing recreation for already-copied files that don't
    have it yet — useful after fixing a permissions issue, without
    re-copying anything."""
    db = SessionLocal()
    try:
        job = get_job(db, job_id)
        if not job:
            return
        dest_adapter = get_adapter(job.destination_provider)

        for mapping in list_mappings(db, job.id):
            dest_client = dest_adapter.get_client(
                mapping.destination_user_id, mapping.destination_container_type, mapping.destination_container_id
            )
            if not dest_client:
                continue

            items = (
                db.query(MigrationItem)
                .filter(
                    MigrationItem.mapping_id == mapping.id,
                    MigrationItem.status == "copied",
                    MigrationItem.is_folder.is_(False),
                    MigrationItem.share_recreated.is_(False),
                    MigrationItem.destination_ref.isnot(None),
                )
                .all()
            )
            for item in items:
                try:
                    if recreate_sharing(
                        job.source_provider, dest_adapter, dest_client, item.destination_ref, item.sharing
                    ):
                        item.share_recreated = True
                except Exception:
                    pass
                db.commit()

            all_items = list_items(db, mapping.id)
            stats = mapping.stats or {}
            stats["shares_recreated"] = sum(1 for i in all_items if i.share_recreated)
            mapping.stats = stats
            db.commit()
    finally:
        db.close()


# --- Reports & notifications -------------------------------------------------


def render_job_report(db: Session, job: MigrationJob) -> str:
    lines = [
        f"=== Migration Report: Job #{job.id} ===",
        f"{job.source_provider} -> {job.destination_provider}",
        f"Status: {job.status}",
        "",
    ]

    for mapping in list_mappings(db, job.id):
        stats = mapping.stats or {}
        lines.append(
            f"User mapping #{mapping.id}: user {mapping.source_user_id} -> "
            f"user {mapping.destination_user_id} [{mapping.status}]"
        )
        lines.append(
            f"  Root: {mapping.source_root_path or '/'} -> {mapping.destination_root_path or '/'}"
        )
        lines.append(
            f"  Files: {stats.get('copied_files', 0)}/{stats.get('planned_files', 0)} copied | "
            f"Folders: {stats.get('copied_folders', 0)}/{stats.get('planned_folders', 0)}"
        )
        lines.append(f"  Bytes copied: {stats.get('copied_bytes', 0)}")
        lines.append(f"  Shares recreated: {stats.get('shares_recreated', 0)}")

        if stats.get("skipped_items"):
            lines.append(f"  Skipped items (no exportable content): {stats['skipped_items']}")
            skipped = (
                db.query(MigrationItem)
                .filter(MigrationItem.mapping_id == mapping.id, MigrationItem.status == "skipped")
                .all()
            )
            for it in skipped:
                lines.append(f"    - {it.source_path}: {it.error}")

        if stats.get("failed_items"):
            lines.append(f"  Failed items: {stats['failed_items']}")
            failed = (
                db.query(MigrationItem)
                .filter(MigrationItem.mapping_id == mapping.id, MigrationItem.status == "failed")
                .all()
            )
            for it in failed:
                lines.append(f"    - {it.source_path}: {it.error}")

        if mapping.error:
            lines.append(f"  Mapping error: {mapping.error}")
        lines.append("")

    return "\n".join(lines)


def notify_job_complete(db: Session, job: MigrationJob) -> bool:
    if not job.notify_email:
        return False
    return send_email(
        job.notify_email, f"Migration job #{job.id}: {job.status}", render_job_report(db, job)
    )
