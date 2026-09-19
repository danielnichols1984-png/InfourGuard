from pydantic import BaseModel, EmailStr


class IntegrationStatus(BaseModel):
    provider: str
    connected: bool


class EmailReportRequest(BaseModel):
    email: EmailStr
