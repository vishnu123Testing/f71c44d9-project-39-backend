from datetime import datetime, timezone
import uuid

from sqlalchemy import DateTime, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from src.models.database import Base


class AuditLog(Base):
    """ORM model representing security and entity access audit events."""

    __tablename__ = "audit_log"

    __table_args__ = (
        Index("ix_audit_log_tenant_id_logged_at", "tenant_id", "logged_at"),
    )

    audit_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    logged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
    )
    assigned_role: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )
    resource_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    client_ip: Mapped[str | None] = mapped_column(
        String(45),
        nullable=True,
    )
    outcome_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        default=uuid.uuid4,
        nullable=True,
        index=True,
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog(audit_id={self.audit_id!r}, "
            f"action={self.action!r}, "
            f"outcome_status={self.outcome_status!r}, "
            f"logged_at={self.logged_at!r})>"
        )
