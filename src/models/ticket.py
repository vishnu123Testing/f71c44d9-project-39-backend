from datetime import datetime, timezone
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.database import Base


class SupportTicket(Base):
    """ORM model representing a client support ticket."""

    __tablename__ = "support_tickets"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    priority: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="open",
        index=True,
    )
    subject: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    client_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    assigned_tier: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="Tier 1",
    )
    assigned_agent_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        index=True,
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
    first_responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    messages: Mapped[list["TicketMessage"]] = relationship(
        "TicketMessage",
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="TicketMessage.created_at",
    )
    sla_trackings: Mapped[list["SLATracking"]] = relationship(
        "SLATracking",
        back_populates="ticket",
        cascade="all, delete-orphan",
    )
    escalations: Mapped[list["TicketEscalation"]] = relationship(
        "TicketEscalation",
        back_populates="ticket",
        cascade="all, delete-orphan",
    )
    csat_rating: Mapped["CSATRating | None"] = relationship(
        "CSATRating",
        back_populates="ticket",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<SupportTicket(id={self.id!r}, priority={self.priority!r}, "
            f"status={self.status!r}, client_id={self.client_id!r}, "
            f"assigned_tier={self.assigned_tier!r})>"
        )


class TicketMessage(Base):
    """ORM model representing a sequential message thread entry for a ticket."""

    __tablename__ = "ticket_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("support_tickets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sender_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    sender_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    message_body: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    ticket: Mapped[SupportTicket] = relationship(
        "SupportTicket",
        back_populates="messages",
    )
    attachments: Mapped[list["TicketAttachment"]] = relationship(
        "TicketAttachment",
        back_populates="message",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<TicketMessage(id={self.id!r}, ticket_id={self.ticket_id!r}, "
            f"sender_id={self.sender_id!r}, sender_type={self.sender_type!r})>"
        )


class TicketAttachment(Base):
    """ORM model representing a file attachment associated with a ticket message."""

    __tablename__ = "ticket_attachments"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("ticket_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    file_size_bytes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    mime_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    storage_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    message: Mapped[TicketMessage] = relationship(
        "TicketMessage",
        back_populates="attachments",
    )

    def __repr__(self) -> str:
        return (
            f"<TicketAttachment(id={self.id!r}, message_id={self.message_id!r}, "
            f"filename={self.filename!r}, storage_key={self.storage_key!r})>"
        )


class SLATracking(Base):
    """ORM model representing real-time SLA targets and compliance tracking for a ticket."""

    __tablename__ = "sla_tracking"
    __table_args__ = (
        UniqueConstraint(
            "ticket_id",
            "target_type",
            name="uq_sla_tracking_ticket_target_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("support_tickets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    target_due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    warning_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    breached_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    is_breached: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    ticket: Mapped[SupportTicket] = relationship(
        "SupportTicket",
        back_populates="sla_trackings",
    )

    def __repr__(self) -> str:
        return (
            f"<SLATracking(id={self.id!r}, ticket_id={self.ticket_id!r}, "
            f"target_type={self.target_type!r}, is_breached={self.is_breached!r})>"
        )


class TicketEscalation(Base):
    """ORM model representing a historical tier escalation record for a ticket."""

    __tablename__ = "ticket_escalations"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("support_tickets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_tier: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    target_tier: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    escalated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    ticket: Mapped[SupportTicket] = relationship(
        "SupportTicket",
        back_populates="escalations",
    )

    def __repr__(self) -> str:
        return (
            f"<TicketEscalation(id={self.id!r}, ticket_id={self.ticket_id!r}, "
            f"source_tier={self.source_tier!r}, target_tier={self.target_tier!r})>"
        )


class CSATRating(Base):
    """ORM model representing a customer satisfaction rating submitted for a resolved ticket."""

    __tablename__ = "csat_ratings"
    __table_args__ = (
        CheckConstraint(
            "score >= 1 AND score <= 5",
            name="ck_csat_rating_score_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("support_tickets.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    score: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    feedback_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    ticket: Mapped[SupportTicket] = relationship(
        "SupportTicket",
        back_populates="csat_rating",
    )

    def __repr__(self) -> str:
        return (
            f"<CSATRating(id={self.id!r}, ticket_id={self.ticket_id!r}, "
            f"score={self.score!r})>"
        )
