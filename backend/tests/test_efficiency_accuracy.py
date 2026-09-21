"""Efficiency and accuracy, run against whatever real migration data already
exists in this environment. These skip cleanly (not fail) when that data
isn't present, since they're spot-checks against real state, not tests that
create new real provider activity themselves.
"""
import time

import pytest


def _latest_job(session, status):
    r = session.get("/migrations/jobs")
    assert r.status_code == 200
    for job in r.json():
        if job["status"] == status:
            return job
    return None


def test_prestage_efficiency_on_a_real_prestaged_job(admin_user):
    """Re-runs pre-stage (read-only, idempotent) on whatever real prestaged
    job exists, and times it — a genuine throughput number, not a guess."""
    job = _latest_job(admin_user, "prestaged")
    if not job:
        pytest.skip("no real prestaged job in this environment")

    r = admin_user.post(f"/migrations/jobs/{job['id']}/prestage")
    assert r.status_code == 200

    start = time.time()
    mappings = []
    while time.time() - start < 60:
        r = admin_user.get(f"/migrations/jobs/{job['id']}/mappings")
        mappings = r.json()
        if mappings and mappings[0]["status"] in ("prestaged", "failed"):
            break
        time.sleep(1)
    else:
        pytest.fail("pre-stage did not finish within 60s")

    elapsed = time.time() - start
    stats = mappings[0]["stats"]
    total_items = (
        stats.get("planned_files", 0) + stats.get("planned_folders", 0) + stats.get("skipped_items", 0)
    )
    rate = total_items / elapsed if elapsed > 0 else float("inf")
    print(f"\nPre-stage: {total_items} items in {elapsed:.1f}s ({rate:.1f} items/sec) for job {job['id']}")

    assert elapsed < 60
    assert total_items > 0


def test_skipped_items_are_not_counted_as_failures(admin_user):
    job = _latest_job(admin_user, "prestaged")
    if not job:
        pytest.skip("no real prestaged job in this environment")

    mappings = admin_user.get(f"/migrations/jobs/{job['id']}/mappings").json()
    stats = mappings[0]["stats"]

    if not stats.get("skipped_items"):
        pytest.skip("this job's real data has no skipped (unexportable) items to check")

    items = admin_user.get(f"/migrations/jobs/{job['id']}/mappings/{mappings[0]['id']}/items").json()
    skipped = [i for i in items if i["status"] == "skipped"]
    assert len(skipped) == stats["skipped_items"]
    for s in skipped:
        assert s["error"], f"{s['source_path']} is skipped but has no reason recorded"


def test_native_google_docs_get_correct_export_extension(admin_user):
    job = _latest_job(admin_user, "prestaged") or _latest_job(admin_user, "completed")
    if not job:
        pytest.skip("no real job with Google-source items in this environment")

    mappings = admin_user.get(f"/migrations/jobs/{job['id']}/mappings").json()
    items = admin_user.get(f"/migrations/jobs/{job['id']}/mappings/{mappings[0]['id']}/items").json()

    exported = [i for i in items if i["destination_path"].endswith((".docx", ".xlsx", ".pptx"))]
    if not exported:
        pytest.skip("no native Google Docs/Sheets/Slides in this job's real data")

    for i in exported:
        assert i["status"] != "failed", f"{i['source_path']} failed: {i.get('error')}"
        assert i["source_path"] == i["destination_path"], "extension should be added consistently to both"


def test_folder_structure_is_mirrored_exactly(admin_user):
    job = _latest_job(admin_user, "prestaged") or _latest_job(admin_user, "completed")
    if not job:
        pytest.skip("no real job in this environment")

    mappings = admin_user.get(f"/migrations/jobs/{job['id']}/mappings").json()
    items = admin_user.get(f"/migrations/jobs/{job['id']}/mappings/{mappings[0]['id']}/items").json()

    nested = [i for i in items if "/" in i["source_path"]]
    if not nested:
        pytest.skip("this job's real data has no nested folder structure to check")

    for i in nested:
        assert i["source_path"] == i["destination_path"], (
            f"{i['source_path']} should mirror to the same relative destination path, "
            f"got {i['destination_path']}"
        )


def test_completed_job_byte_count_matches_sum_of_its_files(admin_user):
    job = _latest_job(admin_user, "completed")
    if not job:
        pytest.skip("no real completed job in this environment")

    mappings = admin_user.get(f"/migrations/jobs/{job['id']}/mappings").json()
    mapping = mappings[0]
    items = admin_user.get(f"/migrations/jobs/{job['id']}/mappings/{mapping['id']}/items").json()

    copied_files = [i for i in items if i["status"] == "copied" and not i["is_folder"]]
    actual_total = sum(i["size_bytes"] or 0 for i in copied_files)

    assert mapping["stats"]["copied_bytes"] == actual_total
    assert mapping["stats"]["copied_files"] == len(copied_files)
