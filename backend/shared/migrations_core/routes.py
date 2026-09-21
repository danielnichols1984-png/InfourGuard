from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from shared.auth_core.dependencies import get_current_user
from shared.auth_core.models import User
from shared.integrations_core.email_utils import send_email
from shared.migrations_core import service
from shared.migrations_core.adapters import user_has_destination_write_access, user_has_provider_connected
from shared.migrations_core.db import get_db
from shared.migrations_core.schemas import (
    AddMappingRequest,
    CreateJobRequest,
    EmailReportRequest,
    ItemResponse,
    JobResponse,
    MappingResponse,
    ScheduleJobRequest,
)

router = APIRouter()


def _job_or_404(db: Session, job_id: int):
    job = service.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Migration job not found")
    return job


def _require_job_access(job, user: User) -> None:
    if not user.is_admin and job.created_by_user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your migration job")


def _mapping_or_404(db: Session, job_id: int, mapping_id: int):
    mapping = service.get_mapping(db, mapping_id)
    if not mapping or mapping.job_id != job_id:
        raise HTTPException(status_code=404, detail="Mapping not found")
    return mapping


@router.post("/jobs", response_model=JobResponse)
def create_job(body: CreateJobRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if body.source_provider not in service.VALID_PROVIDERS or body.destination_provider not in service.VALID_PROVIDERS:
        raise HTTPException(status_code=400, detail="provider must be 'google', 'dropbox', or 'microsoft'")
    if body.source_provider == body.destination_provider:
        raise HTTPException(status_code=400, detail="Source and destination providers must differ")

    return service.create_job(
        db, user.id, body.source_provider, body.destination_provider, body.notify_email
    )


@router.get("/jobs", response_model=list[JobResponse])
def list_jobs(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return service.list_jobs(db, user_id=None if user.is_admin else user.id)


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    job = _job_or_404(db, job_id)
    _require_job_access(job, user)
    return job


# A job that actually ran (successfully or not) is a record worth keeping —
# archive it instead. Only jobs that never produced a real result (still
# being set up, mid-run, or one whose pre-stage itself failed before
# copying anything) can be deleted outright.
_UNDELETABLE_STATUSES = {"running", "completed", "completed_with_errors", "archived"}


@router.delete("/jobs/{job_id}")
def delete_job(job_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    job = _job_or_404(db, job_id)
    _require_job_access(job, user)
    if job.status in _UNDELETABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Can't delete a job that's {job.status.replace('_', ' ')} — "
                "archive it instead to keep the record."
            ),
        )
    service.delete_job(db, job)
    return {"message": "Job deleted"}


@router.get("/jobs/{job_id}/mappings", response_model=list[MappingResponse])
def list_mappings(job_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    job = _job_or_404(db, job_id)
    _require_job_access(job, user)
    return service.list_mappings(db, job_id)


@router.post("/jobs/{job_id}/mappings", response_model=MappingResponse)
def add_mapping(
    job_id: int,
    body: AddMappingRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _job_or_404(db, job_id)
    _require_job_access(job, user)

    if job.status not in ("draft", "mapped"):
        raise HTTPException(status_code=400, detail="Can't add mappings after the job has been staged or run")

    if not user.is_admin and (body.source_user_id != user.id or body.destination_user_id != user.id):
        raise HTTPException(
            status_code=403, detail="Only an admin can create a migration mapping for another user"
        )

    if not user_has_provider_connected(body.source_user_id, job.source_provider):
        raise HTTPException(status_code=400, detail=f"Source user hasn't connected {job.source_provider}")
    if not user_has_provider_connected(body.destination_user_id, job.destination_provider):
        raise HTTPException(
            status_code=400, detail=f"Destination user hasn't connected {job.destination_provider}"
        )
    if not user_has_destination_write_access(body.destination_user_id, job.destination_provider):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Destination user's {job.destination_provider} connection doesn't have write "
                "access (it was likely connected before write access was required). "
                "Reconnect it under Connected Accounts, then try again."
            ),
        )

    return service.add_mapping(
        db,
        job,
        body.source_user_id,
        body.destination_user_id,
        body.source_root_path,
        body.destination_root_path,
    )


@router.delete("/jobs/{job_id}/mappings/{mapping_id}")
def remove_mapping(
    job_id: int, mapping_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    job = _job_or_404(db, job_id)
    _require_job_access(job, user)
    mapping = _mapping_or_404(db, job_id, mapping_id)
    service.remove_mapping(db, mapping)
    return {"message": "Mapping removed"}


@router.get("/jobs/{job_id}/mappings/{mapping_id}/items", response_model=list[ItemResponse])
def list_items(
    job_id: int, mapping_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    job = _job_or_404(db, job_id)
    _require_job_access(job, user)
    mapping = _mapping_or_404(db, job_id, mapping_id)
    return service.list_items(db, mapping.id)


@router.post("/jobs/{job_id}/prestage")
def prestage_job(
    job_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _job_or_404(db, job_id)
    _require_job_access(job, user)
    if not service.list_mappings(db, job_id):
        raise HTTPException(status_code=400, detail="Add at least one user mapping first")

    job.status = "prestaging"
    db.commit()
    background_tasks.add_task(service.run_prestage, job_id)
    return {"message": "Pre-stage started"}


@router.post("/jobs/{job_id}/run")
def run_job(
    job_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _job_or_404(db, job_id)
    _require_job_access(job, user)
    # completed/completed_with_errors/failed are all re-runnable: already-
    # copied items are skipped (see _run_mapping), so retrying after fixing
    # whatever caused a failure just picks up where it left off.
    if job.status not in ("prestaged", "completed", "completed_with_errors", "failed"):
        raise HTTPException(status_code=400, detail="Run pre-stage first")

    job.status = "running"
    db.commit()
    background_tasks.add_task(service.run_full_migration, job_id)
    return {"message": "Migration started"}


@router.post("/jobs/{job_id}/schedule", response_model=JobResponse)
def schedule_job(
    job_id: int,
    body: ScheduleJobRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _job_or_404(db, job_id)
    _require_job_access(job, user)
    if job.status != "prestaged":
        raise HTTPException(status_code=400, detail="Run pre-stage first")

    job.status = "scheduled"
    job.scheduled_at = body.scheduled_at
    db.commit()
    db.refresh(job)
    return job


@router.post("/jobs/{job_id}/unschedule", response_model=JobResponse)
def unschedule_job(job_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    job = _job_or_404(db, job_id)
    _require_job_access(job, user)
    if job.status != "scheduled":
        raise HTTPException(status_code=400, detail="Job isn't scheduled")

    job.status = "prestaged"
    job.scheduled_at = None
    db.commit()
    db.refresh(job)
    return job


@router.post("/jobs/{job_id}/recreate-share-links")
def recreate_share_links(
    job_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _job_or_404(db, job_id)
    _require_job_access(job, user)
    if job.status not in ("completed", "completed_with_errors"):
        raise HTTPException(status_code=400, detail="Job must have completed a run first")

    background_tasks.add_task(service.rerun_share_recreation, job_id)
    return {"message": "Recreating share links"}


@router.get("/jobs/{job_id}/report")
def get_report(job_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    job = _job_or_404(db, job_id)
    _require_job_access(job, user)
    return {"report_text": service.render_job_report(db, job)}


@router.post("/jobs/{job_id}/report/email")
def email_report(
    job_id: int,
    body: EmailReportRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _job_or_404(db, job_id)
    _require_job_access(job, user)
    sent = send_email(body.email, f"Migration job #{job.id} report", service.render_job_report(db, job))
    return {"sent": sent}


@router.post("/jobs/{job_id}/archive", response_model=JobResponse)
def archive_job(job_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    job = _job_or_404(db, job_id)
    _require_job_access(job, user)
    return service.archive_job(db, job)
