"""Financial transparency ORM models for Quotes, Invoices, and Payments."""

from datetime import datetime, timezone
import enum
import uuid

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.database import Base


class QuoteApprovalState(str, enum.Enum):
    """Enumeration of valid quote approval states."""

    DRAFT = "Draft"
    PENDING_APPROVAL = "Pending_Approval"
    APPROVED = "Approved"
    EXPIRED = "Expired"


class InvoiceStatus(str, enum.Enum):
    """Enumeration of valid invoice statuses."""

    CURRENT = "Current"
    PENDING = "Pending"
    PAST_DUE = "Past_Due"
    PAID = "Paid"


class Quote(Base):
    """ORM model representing a financial quote with approval states and expiration dates."""

    __tablename__ = "quotes"

    quote_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        index=True,
    )
    approval_state: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=QuoteApprovalState.DRAFT.value,
        index=True,
    )
    expiration_date_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    total_amount: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        nullable=False,
        default=0.0,
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

    invoices: Mapped[list["Invoice"]] = relationship(
        "Invoice",
        back_populates="quote",
    )

    def __repr__(self) -> str:
        return (
            f"<Quote(quote_id={self.quote_id!r}, customer_id={self.customer_id!r}, "
            f"approval_state={self.approval_state!r}, total_amount={self.total_amount!r})>"
        )


class Invoice(Base):
    """ORM model representing an invoice with status tracking and payments linkage."""

    __tablename__ = "invoices"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    quote_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("quotes.quote_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=InvoiceStatus.CURRENT.value,
        index=True,
    )
    issue_date_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    due_date_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    total_amount: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        nullable=False,
        default=0.0,
    )
    outstanding_amount: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        nullable=False,
        default=0.0,
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

    quote: Mapped["Quote | None"] = relationship(
        "Quote",
        back_populates="invoices",
    )
    payments: Mapped[list["Payment"]] = relationship(
        "Payment",
        back_populates="invoice",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<Invoice(invoice_id={self.invoice_id!r}, customer_id={self.customer_id!r}, "
            f"status={self.status!r}, outstanding_amount={self.outstanding_amount!r})>"
        )


class Payment(Base):
    """ORM model representing a payment record linked to an invoice."""

    __tablename__ = "payments"

    payment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("invoices.invoice_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    payment_date_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    amount_paid: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        nullable=False,
        default=0.0,
    )
    payment_method: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
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

    invoice: Mapped["Invoice"] = relationship(
        "Invoice",
        back_populates="payments",
    )

    def __repr__(self) -> str:
        return (
            f"<Payment(payment_id={self.payment_id!r}, invoice_id={self.invoice_id!r}, "
            f"amount_paid={self.amount_paid!r}, payment_method={self.payment_method!r})>"
        )
