from pydantic import BaseModel, EmailStr, field_validator

from shared.auth_core.config import settings as auth_settings


class MemberResponse(BaseModel):
    user_id: int
    email: str
    role: str


class BusinessResponse(BaseModel):
    id: int
    name: str
    members: list[MemberResponse] = []


class AdminBusinessSummary(BaseModel):
    id: int
    name: str
    member_count: int


class AdminCreateBusinessRequest(BaseModel):
    name: str
    admin_email: EmailStr


class CreateMemberRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < auth_settings.MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"Password must be at least {auth_settings.MIN_PASSWORD_LENGTH} characters"
            )
        return v


class SetMemberPasswordRequest(BaseModel):
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < auth_settings.MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"Password must be at least {auth_settings.MIN_PASSWORD_LENGTH} characters"
            )
        return v


class EmailReportRequest(BaseModel):
    email: EmailStr
