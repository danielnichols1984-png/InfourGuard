from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr


class PlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    display_name: str
    price_display: str
    features: list[str]
    is_default: bool
    is_business_plan: bool


class SubscriptionResponse(BaseModel):
    plan: PlanResponse


class ChangePlanRequest(BaseModel):
    plan_id: int


class PlanCreate(BaseModel):
    name: str
    display_name: str
    price_display: str
    features: list[str] = []
    is_default: bool = False
    is_business_plan: bool = False


class PlanUpdate(BaseModel):
    display_name: str | None = None
    price_display: str | None = None
    features: list[str] | None = None
    is_default: bool | None = None
    is_business_plan: bool | None = None


class AdminUserPlanResponse(BaseModel):
    user_id: int
    email: str
    plan: PlanResponse


class AdminSetUserPlanRequest(BaseModel):
    plan_id: int


class CheckoutRequest(BaseModel):
    plan_id: int
    method: str  # only "cash" is accepted today
    billing_name: str
    billing_email: EmailStr
    billing_phone: str | None = None
    billing_address_line1: str
    billing_address_line2: str | None = None
    billing_city: str
    billing_state: str | None = None
    billing_zip: str | None = None
    billing_country: str
    company_name: str | None = None
    notes: str | None = None
    business_name: str | None = None


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    plan_id: int
    method: str
    status: str
    billing_name: str
    billing_email: str
    billing_phone: str | None
    billing_address_line1: str
    billing_address_line2: str | None
    billing_city: str
    billing_state: str | None
    billing_zip: str | None
    billing_country: str
    company_name: str | None
    notes: str | None
    business_name: str | None
    created_at: datetime | None
    confirmed_at: datetime | None


class AdminPaymentResponse(PaymentResponse):
    email: str
    plan: PlanResponse
