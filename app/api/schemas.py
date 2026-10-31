from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

Role = Literal["user", "manager", "admin"]


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
    role: Role = "user"


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
    role: Role


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
