from pydantic import BaseModel, EmailStr, field_validator

from shared.auth_core.config import settings

class SignupRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < settings.MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"Password must be at least {settings.MIN_PASSWORD_LENGTH} characters"
            )
        return v

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class UserResponse(BaseModel):
    id: int
    email: EmailStr
    is_admin: bool
