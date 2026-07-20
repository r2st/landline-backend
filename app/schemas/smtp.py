"""Pydantic schemas for the SMTP sender-account endpoints."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


# Common provider hosts to make the UI's job easier (purely informational).
COMMON_SMTP_HOSTS = {
    "gmail": {"host": "smtp.gmail.com", "port": 587, "use_tls": True, "use_ssl": False},
    "outlook": {"host": "smtp-mail.outlook.com", "port": 587, "use_tls": True, "use_ssl": False},
    "office365": {"host": "smtp.office365.com", "port": 587, "use_tls": True, "use_ssl": False},
    "yahoo": {"host": "smtp.mail.yahoo.com", "port": 465, "use_tls": False, "use_ssl": True},
    "zoho": {"host": "smtp.zoho.com", "port": 465, "use_tls": False, "use_ssl": True},
}


class SmtpAccountBase(BaseModel):
    email: EmailStr
    display_name: Optional[str] = None
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(587, ge=1, le=65535)
    username: str = Field(..., min_length=1, max_length=320)
    use_tls: bool = True
    use_ssl: bool = False
    # IMAP settings for the reply/bounce sweep. When ``imap_host`` is omitted it
    # is derived from ``host`` (smtp.→imap.). ``imap_enabled`` turns polling off
    # for send-only relays.
    imap_host: Optional[str] = Field(None, max_length=255)
    imap_port: int = Field(993, ge=1, le=65535)
    imap_use_ssl: bool = True
    imap_enabled: bool = True

    @field_validator("host")
    @classmethod
    def strip_host(cls, v: str) -> str:
        return v.strip()


class SmtpAccountCreate(SmtpAccountBase):
    """Payload to add a new SMTP sender account."""

    password: str = Field(..., min_length=1)


class SmtpAccountUpdate(BaseModel):
    """Partial update — only provided fields are changed. Password optional."""

    email: Optional[EmailStr] = None
    display_name: Optional[str] = None
    host: Optional[str] = Field(None, min_length=1, max_length=255)
    port: Optional[int] = Field(None, ge=1, le=65535)
    username: Optional[str] = Field(None, min_length=1, max_length=320)
    password: Optional[str] = Field(None, min_length=1)
    use_tls: Optional[bool] = None
    use_ssl: Optional[bool] = None
    imap_host: Optional[str] = Field(None, max_length=255)
    imap_port: Optional[int] = Field(None, ge=1, le=65535)
    imap_use_ssl: Optional[bool] = None
    imap_enabled: Optional[bool] = None


class SmtpAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: Optional[str] = None
    host: str
    port: int
    username: str
    use_tls: bool
    use_ssl: bool
    imap_host: Optional[str] = None
    imap_port: int = 993
    imap_use_ssl: bool = True
    imap_enabled: bool = True
    is_active: bool
    sending_enabled: bool
    status: str
    last_error: Optional[str] = None
    daily_send_count: int = 0
    connected_at: datetime
    last_used_at: Optional[datetime] = None
    # Discriminator so the UI can render Gmail + SMTP senders in one list.
    type: str = "smtp"


class ToggleSendingRequest(BaseModel):
    sending_enabled: bool


class TestSendRequest(BaseModel):
    """Send a test email to confirm an account can deliver. Defaults to self."""

    to_email: Optional[EmailStr] = None


class TestSendResult(BaseModel):
    status: str  # "sent" | "error"
    detail: Optional[str] = None
    sender: Optional[str] = None
    transport: Optional[str] = None
