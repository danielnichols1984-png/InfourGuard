"""Integrity: does the engine actually detect a corrupted copy, and does
real completed data show real, verified hashes (not just a hardcoded
True)?

The corruption-detection tests call the real `_copy_item` production
function directly with fake adapters standing in for Dropbox/Google, so
they can deliberately inject a mismatch without needing a real provider to
misbehave on cue.
"""
import hashlib

import pytest


@pytest.fixture
def throwaway_item():
    """A disposable job/mapping/item row, isolated under a fake user id
    that doesn't correspond to any real account, cleaned up afterward."""
    from shared.migrations_core.db import SessionLocal
    from shared.migrations_core.models import MigrationItem, MigrationJob, MigrationUserMapping

    db = SessionLocal()
    job = MigrationJob(
        created_by_user_id=999999,
        source_provider="dropbox",
        destination_provider="google",
        status="running",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    mapping = MigrationUserMapping(
        job_id=job.id,
        source_user_id=999999,
        destination_user_id=999999,
        status="running",
        stats={},
    )
    db.add(mapping)
    db.commit()
    db.refresh(mapping)

    item = MigrationItem(
        mapping_id=mapping.id,
        source_path="test.txt",
        destination_path="test.txt",
        source_ref="fake-source-ref",
        is_folder=False,
        size_bytes=11,
        status="planned",
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    yield db, job, item

    db.delete(job)  # cascades to mapping + item
    db.commit()
    db.close()


class _FakeAdapter:
    """Stands in for a real ProviderAdapter. Always uses plain SHA-256 so
    the test controls exactly what "matches" means, independent of which
    real provider algorithm is in play."""

    def __init__(self, download_bytes=None, upload_hash=None):
        self._download_bytes = download_bytes
        self._upload_hash = upload_hash

    def get_client(self, user_id):
        return object()

    def download(self, client, ref, export_mime_type=None):
        return self._download_bytes

    def ensure_child_folder(self, client, parent_ref, name):
        return "fake-folder-ref"

    def upload(self, client, parent_ref, name, data, mime_type):
        return {"ref": "fake-dest-ref", "hash": self._upload_hash}

    def compute_hash(self, data):
        return hashlib.sha256(data).hexdigest()

    def apply_public_sharing(self, client, ref):
        pass

    def apply_named_sharing(self, client, ref, email):
        return False


def test_download_hash_mismatch_marks_item_failed_not_copied(monkeypatch, throwaway_item):
    db, job, item = throwaway_item
    real_bytes = b"hello world"
    item.source_hash = hashlib.sha256(b"something else entirely").hexdigest()  # deliberately wrong
    db.commit()

    from shared.migrations_core import service

    fake = _FakeAdapter(download_bytes=real_bytes)
    monkeypatch.setattr(service, "get_adapter", lambda provider: fake)

    service._copy_item(db, job, item, source_client=object(), dest_client=object(), dest_root_ref="root", folder_cache={})

    db.refresh(item)
    assert item.status == "failed"
    assert "source hash mismatch" in item.error
    assert item.verified is False


def test_upload_hash_mismatch_marks_item_failed_not_copied(monkeypatch, throwaway_item):
    db, job, item = throwaway_item
    real_bytes = b"hello world"
    item.source_hash = hashlib.sha256(real_bytes).hexdigest()  # correct — download leg passes
    db.commit()

    from shared.migrations_core import service

    fake = _FakeAdapter(download_bytes=real_bytes, upload_hash="0" * 64)  # destination lies about what it stored
    monkeypatch.setattr(service, "get_adapter", lambda provider: fake)

    service._copy_item(db, job, item, source_client=object(), dest_client=object(), dest_root_ref="root", folder_cache={})

    db.refresh(item)
    assert item.status == "failed"
    assert "destination hash mismatch" in item.error
    assert item.verified is False


def test_matching_hashes_both_legs_marks_item_copied_and_verified(monkeypatch, throwaway_item):
    db, job, item = throwaway_item
    real_bytes = b"hello world"
    correct_hash = hashlib.sha256(real_bytes).hexdigest()
    item.source_hash = correct_hash
    db.commit()

    from shared.migrations_core import service

    fake = _FakeAdapter(download_bytes=real_bytes, upload_hash=correct_hash)
    monkeypatch.setattr(service, "get_adapter", lambda provider: fake)

    service._copy_item(db, job, item, source_client=object(), dest_client=object(), dest_root_ref="root", folder_cache={})

    db.refresh(item)
    assert item.status == "copied"
    assert item.verified is True


def test_no_source_hash_available_skips_download_check_but_still_verifies_upload(monkeypatch, throwaway_item):
    """Mirrors a native Google Doc export: no native hash to compare on the
    way in, but the upload leg must still be checked."""
    db, job, item = throwaway_item
    real_bytes = b"exported docx bytes"
    item.source_hash = None
    db.commit()

    from shared.migrations_core import service

    fake = _FakeAdapter(download_bytes=real_bytes, upload_hash="0" * 64)  # wrong on upload
    monkeypatch.setattr(service, "get_adapter", lambda provider: fake)

    service._copy_item(db, job, item, source_client=object(), dest_client=object(), dest_root_ref="root", folder_cache={})

    db.refresh(item)
    assert item.status == "failed"
    assert "destination hash mismatch" in item.error


def test_any_real_completed_job_has_fully_verified_files():
    """Spot-check against whatever real completed migration already exists
    in this environment (not one this suite creates) — skips cleanly if
    none exists yet."""
    from shared.migrations_core.db import SessionLocal
    from shared.migrations_core.models import MigrationItem, MigrationJob, MigrationUserMapping

    db = SessionLocal()
    try:
        job = (
            db.query(MigrationJob)
            .filter(MigrationJob.status == "completed")
            .order_by(MigrationJob.id.desc())
            .first()
        )
        if not job:
            pytest.skip("no completed migration job in this environment yet")

        mappings = db.query(MigrationUserMapping).filter(MigrationUserMapping.job_id == job.id).all()
        files = []
        for m in mappings:
            files += (
                db.query(MigrationItem)
                .filter(MigrationItem.mapping_id == m.id, MigrationItem.is_folder.is_(False))
                .all()
            )

        assert files, f"job {job.id} is 'completed' but has no file items"
        for f in files:
            assert f.status == "copied", f"{f.source_path} is {f.status}, not copied"
            assert f.verified is True, f"{f.source_path} was not verified"
            assert f.source_hash, f"{f.source_path} has no recorded source_hash"
            assert f.destination_hash, f"{f.source_path} has no recorded destination_hash"
    finally:
        db.close()
