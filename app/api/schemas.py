from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

Role = Literal["user", "manager", "admin", "super_admin"]
AssignableRole = Literal["user", "manager", "admin"]


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserCreate(Credentials):
    role: AssignableRole = "user"


class UserView(BaseModel):
    id: str
    email: EmailStr
    role: Role
    created_at: datetime


class UserPage(BaseModel):
    items: list[UserView]
    limit: int
    offset: int


class RoleUpdate(BaseModel):
    role: AssignableRole


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=12, max_length=128)
    role: AssignableRole | None = None

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        return value.strip().lower() if value is not None else None

    @model_validator(mode="after")
    def require_update(self):
        if self.email is None and self.password is None and self.role is None:
            raise ValueError("At least one non-null field is required")
        return self


class ApprovalDecision(BaseModel):
    status: Literal["approved", "rejected"]


class AnalysisResult(BaseModel):
    text: str = Field(description="Gemini's generated response to the system prompt")
    kind: Literal["image", "video"]
    model: str
    tokens: int | None = None


class JobResponse(BaseModel):
    job_id: str
    status: str
    cached: bool = False
    result: AnalysisResult | None = None
    error: str | None = None


class JobView(BaseModel):
    job_id: str
    status: str
    approval_status: Literal["pending", "approved", "rejected"] = "pending"
    submitter_email: EmailStr | None = None
    original_filename: str
    attempts: int = 0
    cached: bool = False
    error: str | None = None
    result: AnalysisResult | None = None
    filename: str
    media_kind: str
    media_url: str
    created_at: datetime


class JobPage(BaseModel):
    items: list[JobView]
    limit: int
    offset: int


class StatsResponse(BaseModel):
    ready: int
    processing: int
    deferred: int
    dead: int
    gemini_calls_today: int


class SchedulePostRequest(BaseModel):
    scheduled_at: datetime
    timezone: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_schedule(self):
        if self.scheduled_at.tzinfo is None or self.scheduled_at.utcoffset() is None:
            raise ValueError("scheduled_at must include a UTC offset")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("timezone must be a valid IANA timezone") from None
        return self


class ScheduledPostView(BaseModel):
    id: str
    job_id: str
    original_filename: str
    media_kind: str
    status: str
    scheduled_at: datetime
    timezone: str
    local_date: date
    content: str
    created_at: datetime


class ScheduledPostPage(BaseModel):
    items: list[ScheduledPostView]
    limit: int
    offset: int


class CalendarDay(BaseModel):
    date: date
    posts: list[ScheduledPostView]


class CalendarResponse(BaseModel):
    timezone: str
    days: list[CalendarDay]
