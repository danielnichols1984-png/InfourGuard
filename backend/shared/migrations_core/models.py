from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship

from shared.migrations_core.db import Base


def _utcnow():
    return datetime.now(timezone.utc)


class MigrationJob(Base):
    __tablename__ = "migration_jobs"

    id = Column(Integer, primary_key=True, index=True)
    # Plain user id, not a cross-module FK — same pattern as subscriptions_core
    # and integrations_core.
    created_by_user_id = Column(Integer, nullable=False, index=True)
    source_provider = Column(String(20), nullable=False)
    destination_provider = Column(String(20), nullable=False)
    # draft -> mapped -> prestaging -> prestaged -> scheduled -> running ->
    # completed / completed_with_errors / failed -> archived
    status = Column(String(30), nullable=False, default="draft")
    notify_email = Column(String(255), nullable=True)
    scheduled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    mappings = relationship(
        "MigrationUserMapping", back_populates="job", cascade="all, delete-orphan"
    )


class MigrationUserMapping(Base):
    __tablename__ = "migration_user_mappings"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("migration_jobs.id"), nullable=False, index=True)
    # For a personal-account mapping (the original, still-default case),
    # source_user_id/destination_user_id are the migrating individual's
    # own id. For a business-storage mapping (source/destination
    # container_type set), they're instead the ADMIN whose tenant
    # connection performs the work — see adapters.py's get_client().
    source_user_id = Column(Integer, nullable=False, index=True)
    destination_user_id = Column(Integer, nullable=False, index=True)
    source_root_path = Column(String(1024), nullable=False, default="")
    destination_root_path = Column(String(1024), nullable=False, default="")
    # None (default) = personal account, today's original behavior.
    # "shared_drive" | "site" | "team_folder" = business storage — the
    # matching *_container_id is that container's provider-native id
    # (a Google Shared Drive id, a SharePoint site id, or a Dropbox team
    # folder id), and root paths above are then relative to that
    # container's own root instead of a personal account's root.
    source_container_type = Column(String(20), nullable=True)
    source_container_id = Column(String(255), nullable=True)
    destination_container_type = Column(String(20), nullable=True)
    destination_container_id = Column(String(255), nullable=True)
    # Whether to carry the source's last-modified timestamp through to the
    # destination on copy (see _copy_item/adapters.py's upload()/replace()).
    # Off means every migrated file lands with today's date, same as
    # before this was added.
    preserve_metadata = Column(Boolean, nullable=False, default=True)
    # pending -> prestaging -> prestaged -> running ->
    # completed / completed_with_errors / failed
    status = Column(String(30), nullable=False, default="pending")
    stats = Column(JSON, nullable=False, default=dict)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    job = relationship("MigrationJob", back_populates="mappings")
    items = relationship("MigrationItem", back_populates="mapping", cascade="all, delete-orphan")


class MigrationItem(Base):
    """One planned file or folder within a user mapping.

    Populated by pre-stage as a snapshot (paths, native provider refs, the
    source's own hash, sharing info) — the full run executes purely off
    these rows rather than re-listing the source, which also means a
    partial failure can be retried by re-running against whatever's left
    in "planned"/"failed" status. Re-running pre-stage on a mapping that's
    already been run merges a fresh source scan into these same rows
    (matched by source_path) rather than replacing them wholesale, so an
    already-"copied" item with unchanged content is left untouched — see
    _walk_and_plan. That merge is what makes pre-stage double as a second
    (delta) pass.
    """

    __tablename__ = "migration_items"

    id = Column(Integer, primary_key=True, index=True)
    mapping_id = Column(
        Integer, ForeignKey("migration_user_mappings.id"), nullable=False, index=True
    )

    source_path = Column(Text, nullable=False)
    destination_path = Column(Text, nullable=False)
    # Provider-native reference: a Google Drive file id, or a Dropbox path.
    source_ref = Column(Text, nullable=False)
    destination_ref = Column(Text, nullable=True)

    is_folder = Column(Boolean, nullable=False, default=False)
    size_bytes = Column(Integer, nullable=True)
    # The source's own last-modified timestamp at prestage/rescan time —
    # captured regardless of preserve_metadata (cheap, comes back from the
    # same list call), only actually applied at copy time when it's on.
    # Also what a delta rescan compares against source_hash to detect a
    # real edit vs. an untouched file.
    source_modified_at = Column(DateTime(timezone=True), nullable=True)
    # Upload-time mime type. For a native Google Docs/Sheets/etc. source
    # file, this is the *export target* type (e.g. .docx), not the
    # original application/vnd.google-apps.* type.
    mime_type = Column(String(255), nullable=True)
    # Set only for a native Google Docs/Sheets/etc. source file: which
    # format to export it to at download time (get_media() doesn't work on
    # these — see integrations_core.google.export_file_bytes). None for
    # every ordinary binary file, and for Dropbox sources.
    export_mime_type = Column(String(255), nullable=True)
    # Snapshot of source sharing info at prestage time, shape depends on
    # source_provider — see migrations_core.adapters.recreate_sharing.
    sharing = Column(JSON, nullable=True)

    # planned -> copied / skipped / failed / source_removed (terminal —
    # set on a rescan when this item's source_path is gone from a fresh
    # scan but it was already "copied"; the destination copy is never
    # touched, this is purely informational — see _walk_and_plan)
    status = Column(String(20), nullable=False, default="planned")
    source_hash = Column(String(128), nullable=True)
    destination_hash = Column(String(128), nullable=True)
    verified = Column(Boolean, nullable=False, default=False)
    share_recreated = Column(Boolean, nullable=False, default=False)
    error = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    mapping = relationship("MigrationUserMapping", back_populates="items")
