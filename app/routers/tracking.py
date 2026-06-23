"""Email open and click tracking endpoints."""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from itsdangerous import URLSafeTimedSerializer

from app.config import get_settings
from app.database import get_db
from app.models.email_template import EmailSend
from app.models.contact import Contact

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/track", tags=["tracking"])

# Status hierarchy — higher number = more advanced state; never downgrade
_STATUS_RANK = {
    "sent": 0,
    "delivered": 1,
    "opened": 2,
    "clicked": 3,
    "replied": 4,
}

# 1x1 transparent GIF
PIXEL_GIF = bytes([
    0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x01, 0x00,
    0x01, 0x00, 0x80, 0x00, 0x00, 0xff, 0xff, 0xff,
    0x00, 0x00, 0x00, 0x21, 0xf9, 0x04, 0x01, 0x00,
    0x00, 0x00, 0x00, 0x2c, 0x00, 0x00, 0x00, 0x00,
    0x01, 0x00, 0x01, 0x00, 0x00, 0x02, 0x02, 0x44,
    0x01, 0x00, 0x3b
])


def _get_serializer():
    settings = get_settings()
    return URLSafeTimedSerializer(settings.secret_key or "landline-tracking-secret")


def _get_unsubscribe_serializer():
    settings = get_settings()
    return URLSafeTimedSerializer(settings.secret_key or "landline-unsubscribe-secret")


def generate_tracking_pixel_url(send_id: str) -> str:
    """Generate a tracking pixel URL for an email send."""
    serializer = _get_serializer()
    token = serializer.dumps(send_id, salt="open-track")
    return f"https://landline-api.apprendtechnologies.com:8443/api/v1/track/open/{token}"


def generate_click_url(send_id: str, original_url: str) -> str:
    """Wrap a URL for click tracking."""
    serializer = _get_serializer()
    token = serializer.dumps({"send_id": send_id, "url": original_url}, salt="click-track")
    return f"https://landline-api.apprendtechnologies.com:8443/api/v1/track/click/{token}"


@router.get("/open/{token}")
async def track_open(token: str, db: AsyncSession = Depends(get_db)):
    """Record an email open event and return a 1x1 transparent GIF."""
    try:
        serializer = _get_serializer()
        send_id = serializer.loads(token, salt="open-track", max_age=86400 * 90)  # 90 days

        # Fetch current record to check status before updating
        result = await db.execute(
            select(EmailSend).where(EmailSend.id == uuid.UUID(send_id))
        )
        send_record = result.scalar_one_or_none()

        if send_record:
            current_rank = _STATUS_RANK.get(send_record.status, 0)
            new_rank = _STATUS_RANK.get("opened", 2)

            updates = {}
            # Only set opened_at on first open
            if send_record.opened_at is None:
                updates["opened_at"] = datetime.now(timezone.utc)
            # Only upgrade status, never downgrade
            if new_rank > current_rank:
                updates["status"] = "opened"

            if updates:
                await db.execute(
                    update(EmailSend)
                    .where(EmailSend.id == uuid.UUID(send_id))
                    .values(**updates)
                )
                await db.commit()

        logger.info("Tracked email open for send %s", send_id)
    except Exception as e:
        logger.debug("Open tracking failed: %s", str(e))

    return Response(content=PIXEL_GIF, media_type="image/gif", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    })


@router.get("/click/{token}")
async def track_click(token: str, db: AsyncSession = Depends(get_db)):
    """Record a click event and redirect to the original URL."""
    try:
        serializer = _get_serializer()
        data = serializer.loads(token, salt="click-track", max_age=86400 * 90)
        send_id = data["send_id"]
        original_url = data["url"]

        # Fetch current record to check status before updating
        result = await db.execute(
            select(EmailSend).where(EmailSend.id == uuid.UUID(send_id))
        )
        send_record = result.scalar_one_or_none()

        if send_record:
            current_rank = _STATUS_RANK.get(send_record.status, 0)
            new_rank = _STATUS_RANK.get("clicked", 3)

            updates = {}
            # Always record clicked_at on first click
            if send_record.clicked_at is None:
                updates["clicked_at"] = datetime.now(timezone.utc)
            # Also set opened_at if not already set (click implies open)
            if send_record.opened_at is None:
                updates["opened_at"] = datetime.now(timezone.utc)
            # Only upgrade status, never downgrade
            if new_rank > current_rank:
                updates["status"] = "clicked"

            if updates:
                await db.execute(
                    update(EmailSend)
                    .where(EmailSend.id == uuid.UUID(send_id))
                    .values(**updates)
                )
                await db.commit()

        logger.info("Tracked click for send %s -> %s", send_id, original_url)
        return RedirectResponse(url=original_url, status_code=302)
    except Exception as e:
        logger.warning("Click tracking failed: %s", str(e))
        return RedirectResponse(url="https://apprendtechnologies.com", status_code=302)


@router.get("/unsubscribe")
async def unsubscribe(token: str, db: AsyncSession = Depends(get_db)):
    """Handle email unsubscribe requests."""
    try:
        serializer = _get_unsubscribe_serializer()
        email = serializer.loads(token, salt="unsubscribe", max_age=86400 * 365)  # 1 year

        if email:
            # Mark contact as unsubscribed
            result = await db.execute(
                select(Contact).where(Contact.email == email)
            )
            contact = result.scalar_one_or_none()
            if contact:
                contact.unsubscribed = True
                await db.commit()
                logger.info("Contact %s unsubscribed", email)

        return HTMLResponse(
            "<html><body style='font-family: sans-serif; text-align: center; padding: 50px;'>"
            "<h2>You have been unsubscribed.</h2>"
            "<p>You will no longer receive emails from us.</p>"
            "</body></html>"
        )
    except Exception as e:
        logger.warning("Unsubscribe failed: %s", str(e))
        return HTMLResponse(
            "<html><body style='font-family: sans-serif; text-align: center; padding: 50px;'>"
            "<h2>Unsubscribe link expired or invalid.</h2>"
            "<p>Please reply to any email with UNSUBSCRIBE to be removed.</p>"
            "</body></html>",
            status_code=400,
        )
