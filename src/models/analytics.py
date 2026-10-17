from datetime import datetime, timezone
import uuid

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.database import Base


class AcquisitionChannel(Base):
    """ORM model representing marketing acquisition channels and spend data."""

    __tablename__ = "acquisition_channels"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    channel_name: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
    )
    spend_amount: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        nullable=False,
        default=0.0,
    )
    created_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    lead_events: Mapped[list["LeadEvent"]] = relationship(
        "LeadEvent",
        back_populates="channel",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<AcquisitionChannel(id={self.id!r}, "
            f"channel_name={self.channel_name!r}, "
            f"spend_amount={self.spend_amount!r})>"
        )


class Campaign(Base):
    """ORM model representing marketing campaign metadata and budget allocations."""

    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    campaign_name: Mapped[str] = mapped_column(
        String(255),
        index=True,
        nullable=False,
    )
    start_date_val: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    end_date_val: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    budget: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        nullable=False,
        default=0.0,
    )
    created_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    lead_events: Mapped[list["LeadEvent"]] = relationship(
        "LeadEvent",
        back_populates="campaign",
    )

    def __repr__(self) -> str:
        return (
            f"<Campaign(id={self.id!r}, "
            f"campaign_name={self.campaign_name!r}, "
            f"budget={self.budget!r})>"
        )


class LeadEvent(Base):
    """ORM model representing lead progression, channel attribution, and revenue events."""

    __tablename__ = "lead_events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        index=True,
        nullable=False,
    )
    channel_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("acquisition_channels.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    stage_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )
    quality_score: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    deal_value: Mapped[float | None] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        nullable=True,
    )
    revenue_amount: Mapped[float | None] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        nullable=True,
    )
    postal_code: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )
    territory_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("territories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        default=lambda: datetime.now(timezone.utc),
    )

    channel: Mapped["AcquisitionChannel | None"] = relationship(
        "AcquisitionChannel",
        back_populates="lead_events",
    )
    campaign: Mapped["Campaign | None"] = relationship(
        "Campaign",
        back_populates="lead_events",
    )

    def __repr__(self) -> str:
        return (
            f"<LeadEvent(id={self.id!r}, "
            f"lead_id={self.lead_id!r}, "
            f"stage_name={self.stage_name!r}, "
            f"event_timestamp={self.event_timestamp!r})>"
        )


class FunnelThreshold(Base):
    """ORM model representing stage-by-stage drop-off rate threshold settings for bottleneck detection."""

    __tablename__ = "funnel_thresholds"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    stage_name: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        index=True,
        nullable=False,
    )
    max_dropoff_percent: Mapped[float] = mapped_column(
        Numeric(5, 2, asdecimal=False),
        nullable=False,
    )
    updated_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<FunnelThreshold(id={self.id!r}, "
            f"stage_name={self.stage_name!r}, "
            f"max_dropoff_percent={self.max_dropoff_percent!r})>"
        )


class ReportAuditLog(Base):
    """ORM model representing immutable audit logs for administrative configuration changes to reporting definitions."""

    __tablename__ = "report_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        index=True,
        nullable=True,
    )
    action_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    config_delta: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    created_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<ReportAuditLog(id={self.id!r}, "
            f"action_type={self.action_type!r}, "
            f"created_on={self.created_on!r})>"
        )
