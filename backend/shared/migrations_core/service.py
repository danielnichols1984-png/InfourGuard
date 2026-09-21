"""The migration engine: job/mapping CRUD, pre-stage (plan without
transferring bytes), full run (the actual copy), reports, and archiving.

Pre-stage and full run are meant to be invoked as FastAPI background tasks
(see routes.py) — each entry point here opens its own DB session rather
than accepting one from the caller, since a background task runs in a
worker thread after the triggering request (and its session) has already
completed.
"""
import mimetypes
from pathlib import PurePosixPath

from sqlalchemy.orm import Session

from shared.integrations_core.email_utils import send_email
from shared.migrations_core.adapters import get_adapter, recreate_sharing
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


def _walk_and_plan(db: Session, job: MigrationJob, mapping: MigrationUserMapping) -> None:
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

    # Re-running prestage replaces the previous plan rather than duplicating it.
    db.query(MigrationItem).filter(MigrationItem.mapping_id == mapping.id).delete()

    total_files = total_folders = total_bytes = total_skipped = 0
    for entry in tree:
        sharing_snapshot = None
        if job.source_provider == "google":
            sharing_snapshot = {"permissions": entry.get("permissions") or []}
        elif entry.get("anyone_with_link"):
            sharing_snapshot = {"anyone_with_link": True}

        skip_reason = entry.get("skip_reason")
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
                source_hash=entry.get("native_hash"),
                sharing=sharing_snapshot,
                status="skipped" if skip_reason else "planned",
                error=skip_reason,
            )
        )
        if skip_reason:
            total_skipped += 1
        elif entry["is_folder"]:
            total_folders += 1
        else:
            total_files += 1
            total_bytes += entry["size"]

    mapping.stats = {
        "planned_files": total_files,
        "planned_folders": total_folders,
        "planned_bytes": total_bytes,
        "skipped_items": total_skipped,
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


def _resolve_dest_parent(dest_adapter, dest_client, dest_root_ref, item: MigrationItem, folder_cache: dict) -> str:
    parent = str(PurePosixPath(item.destination_path).parent)
    if parent in (".", "/", ""):
        return dest_root_ref
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

    try:
        result = dest_adapter.upload(dest_client, dest_parent_ref, name, data, mime_type)
    except Exception as e:
        item.status, item.error = "failed", f"upload failed: {e}"
        db.commit()
        return

    dest_hash = result.get("hash")
    verified = bool(dest_hash) and dest_adapter.compute_hash(data) == dest_hash

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
    for item in items:
        if item.status == "copied":
            if item.is_folder:
                folder_cache[item.destination_path] = item.destination_ref
            continue
        if item.status == "skipped":
            continue  # e.g. a Google Form — no content to copy, not an error
        _copy_item(db, job, item, source_client, dest_client, dest_root_ref, folder_cache)

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
