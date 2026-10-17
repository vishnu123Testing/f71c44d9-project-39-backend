from datetime import datetime, timezone
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.database import Base

if TYPE_CHECKING:
    from src.models.deal import Deal


class ForecastEvaluationLog(Base):
    """ORM model for storing historical AI forecast evaluations and recalculation logs.

    Records forecast computation events, estimated completion dates, confidence scores,
    status codes (READY, INSUFFICIENT_DATA, COMPUTING, FAILED), correlation IDs,
    and trigger sources for trend auditing and operational observability.
    """

    __tablename__ = "forecast_evaluation_logs"

    __table_args__ = (
        Index("ix_forecast_eval_logs_deal_recalculated", "deal_id", "recalculated_on"),
        Index("ix_forecast_eval_logs_correlation_id", "correlation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    deal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("deals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    estimated_completion_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    confidence_score_pct: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )
    status_code: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="READY",
    )
    trigger_source: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="deal_update",
    )
    correlation_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        index=True,
    )
    error_detail: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    recalculated_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    deal: Mapped["Deal"] = relationship("Deal")

    def __repr__(self) -> str:
        return (
            f"<ForecastEvaluationLog(id={self.id!r}, deal_id={self.deal_id!r}, "
            f"status_code={self.status_code!r}, "
            f"confidence_score_pct={self.confidence_score_pct!r}, "
            f"trigger_source={self.trigger_source!r}, "
            f"correlation_id={self.correlation_id!r}, "
            f"recalculated_on={self.recalculated_on!r})>"
        )


# Alias for compatibility with varying domain nomenclature
ForecastRecalculationLog = ForecastEvaluationLog
