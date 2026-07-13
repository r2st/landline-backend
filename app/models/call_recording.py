"""Call-recording + voice-intelligence model (feature 3.5).

One row per recorded cold call. Holds the recording reference, the transcript
(once transcribed), and the structured AI analysis (key moments, objections,
action items, coaching notes, deal signals, talk/listen ratio).
"""

import uuid
from datetime import datetime

from sqlalchemy import Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Lifecycle of a recording as it flows through transcription + analysis.
RECORDING_STATUSES = ("uploaded", "transcribing", "transcribed", "analyzing", "analyzed", "failed")


class CallRecording(Base):
    __tablename__ = "call_recordings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    call_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("call_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recording_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="uploaded", index=True)
    transcription: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcription_source: Mapped[str | None] = mapped_column(String(30), nullable=True)  # whisper|manual|provided
    # Structured Claude analysis. See voice_intelligence.analyze_transcript.
    analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    talk_listen_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    call_task = relationship("CallTask")

    def __repr__(self) -> str:
        return f"<CallRecording task={self.call_task_id} {self.status}>"
