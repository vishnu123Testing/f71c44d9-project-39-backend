from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models.database import Base


class MetricSnapshot(Base):
    """ORM model representing baseline and captured domain metric snapshots."""

    __tablename__ = "metric_snapshots"

    __table_args__ = (
        UniqueConstraint(
            "domain_name",
            "metric_key",
            "recorded_at",
            name="uq_metric_snapshots_domain_key_recorded_at",
        ),
        Index("ix_metric_snapshots_domain_recorded_at", "domain_name", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    domain_name: Mapped[str] = mapped_column(
        String(50),
        index=True,
        nullable=False,
    )
    metric_key: Mapped[str] = mapped_column(
        String(100),
        index=True,
        nullable=False,
    )
    current_value: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )
    baseline_value: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )
    status_code: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    error_detail: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<MetricSnapshot(id={self.id!r}, "
            f"domain_name={self.domain_name!r}, "
            f"metric_key={self.metric_key!r}, "
            f"status_code={self.status_code!r}, "
            f"recorded_at={self.recorded_at!r})>"
        )
