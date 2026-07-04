"""Pydantic schemas for authentication and user management."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.user import UserRole

# Minimum length for any password the app sets or accepts.
MIN_PASSWORD_LENGTH = 8


def _validate_role(value: str) -> str:
    if value not in UserRole.values():
        raise ValueError(f"role must be one of {UserRole.values()}")
    return value


# ─── Auth ────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    # Signals the frontend to route to the forced password-change screen.
    must_change_password: bool = False


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=128)


class MessageResponse(BaseModel):
    detail: str


# ─── Users ───────────────────────────────────────────────────────────────────

class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    name: str
    role: str
    is_active: bool
    must_change_password: bool
    created_by: Optional[uuid.UUID] = None
    created_at: datetime
    updated_at: datetime


class UserCreate(BaseModel):
    """Admin creates a user with an initial password."""

    email: EmailStr
    name: str = Field(min_length=1, max_length=255)
    role: str = UserRole.AGENT.value
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=128)
    # New accounts are forced to change their password on first login by default.
    must_change_password: bool = True

    _check_role = field_validator("role")(_validate_role)


class UserUpdate(BaseModel):
    """Admin updates a user's role / active status / name."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    role: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("role")
    @classmethod
    def _role(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        return _validate_role(value)
