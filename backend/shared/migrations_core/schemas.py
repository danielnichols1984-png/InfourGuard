from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr


class CreateJobRequest(BaseModel):
    source_provider: str
    destination_provider: str
    notify_email: EmailStr | None = None


class AddMappingRequest(BaseModel):
    source_user_id: int
    destination_user_id: int
    source_root_path: str = ""
    destination_root_path: str = ""


class ScheduleJobRequest(BaseModel):
    scheduled_at: datetime


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_by_user_id: int
    source_provider: str
    destination_provider: str
    status: str
    notify_email: str | None
    scheduled_at: datetime | None


class MappingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    source_user_id: int
    destination_user_id: int
    source_root_path: str
    destination_root_path: str
    status: str
    stats: dict
    error: str | None


class ItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_path: str
    destination_path: str
    is_folder: bool
    size_bytes: int | None
    status: str
    verified: bool
    share_recreated: bool
    error: str | None


class EmailReportRequest(BaseModel):
    email: EmailStr
