"""SQLAlchemy ORM models for alert triggers, notification preferences, logs, and suppressions."""

from datetime import datetime, timezone
import enum
from typing import Any
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
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


class AlertUrgency(str, enum.Enum):
    """Enumeration of valid alert urgency levels."""

    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


class NotificationChannel(str, enum.Enum):
    """Enumeration of valid notification channels."""

    IN_APP = "InApp"
    EMAIL = "Email"
    SMS = "SMS"


class NotificationDeliveryStatus(str, enum.Enum):
    """Enumeration of valid notification delivery statuses."""

    SENT = "Sent"
    FAILED = "Failed"
    FALLBACK = "Fallback"


class AlertTrigger(Base):
    """ORM model representing custom alert trigger rules based on financial criteria."""

    __tablename__ = "alert_triggers"

    trigger_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    rule_name: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
    )
    financial_criteria: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    threshold_amount: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        nullable=False,
        default=0.0,
    )
    urgency: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AlertUrgency.MEDIUM.value,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    notification_logs: Mapped[list["NotificationLog"]] = relationship(
        "NotificationLog",
        back_populates="trigger",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<AlertTrigger(trigger_id={self.trigger_id!r}, rule_name={self.rule_name!r}, "
            f"financial_criteria={self.financial_criteria!r}, urgency={self.urgency!r})>"
        )


class NotificationPreference(Base):
    """ORM model representing role-based user notification preferences."""

    __tablename__ = "notification_preferences"

    preference_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    role_name: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    urgency: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AlertUrgency.MEDIUM.value,
    )
    channels_json: Mapped[dict[str, Any] | list[Any] | Any] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<NotificationPreference(preference_id={self.preference_id!r}, role_name={self.role_name!r}, "
            f"event_type={self.event_type!r}, version={self.version!r})>"
        )


class NotificationLog(Base):
    """ORM model recording audit logs for all notification dispatches."""

    __tablename__ = "notification_logs"

    notification_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        index=True,
    )
    trigger_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("alert_triggers.trigger_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    urgency: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AlertUrgency.MEDIUM.value,
    )
    channel: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=NotificationChannel.IN_APP.value,
    )
    delivery_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=NotificationDeliveryStatus.SENT.value,
    )
    message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    read_status: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    dispatched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    correlation_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    trigger: Mapped["AlertTrigger | None"] = relationship(
        "AlertTrigger",
        back_populates="notification_logs",
    )

    def __repr__(self) -> str:
        return (
            f"<NotificationLog(notification_id={self.notification_id!r}, channel={self.channel!r}, "
            f"delivery_status={self.delivery_status!r}, dispatched_at={self.dispatched_at!r})>"
        )


class AlertSuppression(Base):
    """ORM model representing duplicate alert suppression entries."""

    __tablename__ = "alert_suppressions"

    suppression_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    dedup_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
    )
    last_triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<AlertSuppression(suppression_id={self.suppression_id!r}, dedup_hash={self.dedup_hash!r}, "
            f"expires_at={self.expires_at!r})>"
        )
