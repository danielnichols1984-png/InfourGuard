from pydantic import BaseModel, ConfigDict


class PlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    display_name: str
    price_display: str
    features: list[str]
    is_default: bool


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


class PlanUpdate(BaseModel):
    display_name: str | None = None
    price_display: str | None = None
    features: list[str] | None = None
    is_default: bool | None = None


class AdminUserPlanResponse(BaseModel):
    user_id: int
    email: str
    plan: PlanResponse


class AdminSetUserPlanRequest(BaseModel):
    plan_id: int
