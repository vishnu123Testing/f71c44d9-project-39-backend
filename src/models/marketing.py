from datetime import datetime, timezone
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.database import Base


class Territory(Base):
    """ORM model representing geographic territory definitions and postal code patterns."""

    __tablename__ = "territories"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    territory_code: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=False,
    )
    territory_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    postal_code_patterns: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    created_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<Territory(id={self.id!r}, "
            f"territory_code={self.territory_code!r}, "
            f"territory_name={self.territory_name!r})>"
        )


class LeadMeeting(Base):
    """ORM model representing lead meetings, prep notes, and meeting outcomes."""

    __tablename__ = "lead_meetings"

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
    scheduled_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=False,
    )
    prep_notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    meeting_notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    created_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    followup_tasks: Mapped[list["FollowupTask"]] = relationship(
        "FollowupTask",
        back_populates="meeting",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<LeadMeeting(id={self.id!r}, "
            f"lead_id={self.lead_id!r}, "
            f"scheduled_time={self.scheduled_time!r})>"
        )


class FollowupTask(Base):
    """ORM model representing follow-up tasks generated from lead meeting notes."""

    __tablename__ = "followup_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("lead_meetings.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        index=True,
        nullable=False,
    )
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        index=True,
        nullable=True,
    )
    due_date_val: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=True,
    )
    is_completed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    task_description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    created_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    meeting: Mapped["LeadMeeting | None"] = relationship(
        "LeadMeeting",
        back_populates="followup_tasks",
    )

    def __repr__(self) -> str:
        return (
            f"<FollowupTask(id={self.id!r}, "
            f"meeting_id={self.meeting_id!r}, "
            f"is_completed={self.is_completed!r})>"
        )
