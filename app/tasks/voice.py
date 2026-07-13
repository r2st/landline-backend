"""Celery task for asynchronous call-recording processing (feature 3.5).

Used for URL-only recordings that need Whisper transcription (slow / network),
so the upload endpoint can return immediately and let a worker do the work.
"""

import asyncio
import logging
import uuid

from sqlalchemy import select

from app.database_celery import celery_session
from app.models.call_recording import CallRecording
from app.services.voice_intelligence import get_voice_intelligence
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.voice.process_call_recording")
def process_call_recording(recording_id: str, context: str = "", use_ai: bool = True):
    """Transcribe (if needed) and analyze a stored call recording."""
    async def _run():
        vi = get_voice_intelligence()
        async with celery_session() as db:
            recording = (
                await db.execute(
                    select(CallRecording).where(CallRecording.id == uuid.UUID(str(recording_id)))
                )
            ).scalar_one_or_none()
            if recording is None:
                return {"error": "recording not found", "recording_id": recording_id}
            await vi.process_recording(db, recording, context=context, use_ai=use_ai)
            await db.commit()
            return {"recording_id": recording_id, "status": recording.status}

    return _run_async(_run())
