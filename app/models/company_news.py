"""Company news tracking model."""

import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text, ForeignKey, TIMESTAMP, Boolean
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import OrgScopedMixin


class CompanyNews(Base, OrgScopedMixin):
    __tablename__ = "company_news"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source: Mapped[str | None] = mapped_column(String(200), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)  # funding, expansion, leadership, acquisition, product, partnership
    relevance_score: Mapped[int | None] = mapped_column(nullable=True)  # 1-10
    ai_analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    is_actionable: Mapped[bool] = mapped_column(Boolean, default=False)
    actioned: Mapped[bool] = mapped_column(Boolean, default=False)

    company = relationship("Company", backref="news_items")
