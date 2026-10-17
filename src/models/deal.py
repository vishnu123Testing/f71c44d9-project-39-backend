from datetime import datetime, timezone
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.database import Base


class Deal(Base):
    """ORM model representing a deal in the sales pipeline."""

    __tablename__ = "deals"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    deal_value: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        nullable=False,
    )
    milestone_stage: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )
    win_probability: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
    )
    created_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    action_items: Mapped[list["ActionItem"]] = relationship(
        "ActionItem",
        back_populates="deal",
        cascade="all, delete-orphan",
    )
    stakeholders: Mapped[list["Stakeholder"]] = relationship(
        "Stakeholder",
        back_populates="deal",
        cascade="all, delete-orphan",
    )
    stakeholder_activities: Mapped[list["StakeholderActivity"]] = relationship(
        "StakeholderActivity",
        back_populates="deal",
        cascade="all, delete-orphan",
    )
    forecast: Mapped["DealForecast | None"] = relationship(
        "DealForecast",
        back_populates="deal",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<Deal(id={self.id!r}, title={self.title!r}, "
            f"milestone_stage={self.milestone_stage!r}, "
            f"deal_value={self.deal_value!r}, "
            f"win_probability={self.win_probability!r})>"
        )


class ActionItem(Base):
    """ORM model representing an action item assigned to a deal with SLA tracking."""

    __tablename__ = "action_items"

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
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    assigned_owner_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    target_due_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    sla_threshold_hours: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=24,
    )
    is_completed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    completed_on: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    is_escalated: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
    )
    escalated_on: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    deal: Mapped["Deal"] = relationship("Deal", back_populates="action_items")

    def __repr__(self) -> str:
        return (
            f"<ActionItem(id={self.id!r}, deal_id={self.deal_id!r}, "
            f"title={self.title!r}, is_completed={self.is_completed!r}, "
            f"is_escalated={self.is_escalated!r})>"
        )


class Stakeholder(Base):
    """ORM model representing a mapped contact stakeholder for a deal."""

    __tablename__ = "stakeholders"

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
    full_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    org_role: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    influence_level: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    created_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    deal: Mapped["Deal"] = relationship("Deal", back_populates="stakeholders")
    activities: Mapped[list["StakeholderActivity"]] = relationship(
        "StakeholderActivity",
        back_populates="stakeholder",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<Stakeholder(id={self.id!r}, deal_id={self.deal_id!r}, "
            f"full_name={self.full_name!r}, org_role={self.org_role!r}, "
            f"influence_level={self.influence_level!r})>"
        )


class StakeholderActivity(Base):
    """ORM model representing a CRM interaction activity with a mapped stakeholder."""

    __tablename__ = "stakeholder_activities"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    stakeholder_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("stakeholders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    deal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("deals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    activity_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    summary: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    occurred_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    created_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    stakeholder: Mapped["Stakeholder"] = relationship(
        "Stakeholder", back_populates="activities"
    )
    deal: Mapped["Deal"] = relationship("Deal", back_populates="stakeholder_activities")

    def __repr__(self) -> str:
        return (
            f"<StakeholderActivity(id={self.id!r}, stakeholder_id={self.stakeholder_id!r}, "
            f"deal_id={self.deal_id!r}, activity_type={self.activity_type!r}, "
            f"occurred_on={self.occurred_on!r})>"
        )


class DealForecast(Base):
    """ORM model representing the current AI forecast calculation for a deal."""

    __tablename__ = "deal_forecasts"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    deal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("deals.id", ondelete="CASCADE"),
        unique=True,
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
        default="COMPUTING",
    )
    calculated_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    deal: Mapped["Deal"] = relationship("Deal", back_populates="forecast")

    def __repr__(self) -> str:
        return (
            f"<DealForecast(id={self.id!r}, deal_id={self.deal_id!r}, "
            f"status_code={self.status_code!r}, "
            f"confidence_score_pct={self.confidence_score_pct!r}, "
            f"estimated_completion_date={self.estimated_completion_date!r})>"
        )
