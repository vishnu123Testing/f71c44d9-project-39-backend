"""Financial metrics domain service for filtering quotes, invoices, payments, and calculating AR aging metrics.

Encapsulates domain logic for:
- Filtering quotes by customer and approval state with expiration detection (FR-1)
- Filtering invoices by customer and status (FR-2)
- Filtering payment history records linked to invoices (FR-3)
- Aggregating Accounts Receivable (AR) aging into 0-30, 31-60, 61-90, and 90+ day duration buckets (FR-4)
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.financial import (
    Invoice,
    InvoiceStatus,
    Payment,
    Quote,
    QuoteApprovalState,
)

logger = logging.getLogger(__name__)


class FinancialError(ValueError):
    """Base exception for financial service errors."""

    pass


class QuoteNotFoundError(FinancialError):
    """Raised when a requested quote record is not found."""

    pass


class InvoiceNotFoundError(FinancialError):
    """Raised when a requested invoice record is not found."""

    pass


class PaymentNotFoundError(FinancialError):
    """Raised when a requested payment record is not found."""

    pass


class InvalidFinancialDataError(FinancialError):
    """Raised when financial input data violates domain constraints."""

    pass


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime object is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class QuoteDTO:
    """Data transfer object representing a financial quote."""

    quote_id: UUID
    customer_id: UUID
    approval_state: str
    expiration_date_time: datetime
    total_amount: float
    created_at: datetime
    updated_at: datetime
    is_expired: bool


@dataclass
class InvoiceDTO:
    """Data transfer object representing an invoice."""

    invoice_id: UUID
    quote_id: UUID | None
    customer_id: UUID
    status: str
    issue_date_time: datetime
    due_date_time: datetime
    total_amount: float
    outstanding_amount: float
    created_at: datetime
    updated_at: datetime
    days_overdue: int


@dataclass
class PaymentDTO:
    """Data transfer object representing a payment record."""

    payment_id: UUID
    invoice_id: UUID
    payment_date_time: datetime
    amount_paid: float
    payment_method: str
    created_at: datetime


@dataclass
class ARAgingBucketDTO:
    """Data transfer object for an AR aging duration bucket."""

    bucket_name: str
    total_amount: float
    invoice_count: int


@dataclass
class ARAgingSummaryDTO:
    """Data transfer object summarizing AR aging duration buckets."""

    customer_id: UUID | None
    as_of_date_time: datetime
    total_outstanding: float
    total_invoices: int
    bucket_0_30: ARAgingBucketDTO
    bucket_31_60: ARAgingBucketDTO
    bucket_61_90: ARAgingBucketDTO
    bucket_90_plus: ARAgingBucketDTO


def to_quote_dto(quote: Quote, now_utc: datetime | None = None) -> QuoteDTO:
    """Convert a Quote ORM instance into a QuoteDTO."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    exp_utc = _ensure_utc(quote.expiration_date_time) or quote.expiration_date_time
    is_expired = (
        quote.approval_state == QuoteApprovalState.EXPIRED.value
        or (now_utc > exp_utc and quote.approval_state != QuoteApprovalState.APPROVED.value)
    )
    return QuoteDTO(
        quote_id=quote.quote_id,
        customer_id=quote.customer_id,
        approval_state=quote.approval_state,
        expiration_date_time=exp_utc,
        total_amount=round(float(quote.total_amount), 2),
        created_at=_ensure_utc(quote.created_at) or quote.created_at,
        updated_at=_ensure_utc(quote.updated_at) or quote.updated_at,
        is_expired=is_expired,
    )


def to_invoice_dto(invoice: Invoice, now_utc: datetime | None = None) -> InvoiceDTO:
    """Convert an Invoice ORM instance into an InvoiceDTO."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    due_utc = _ensure_utc(invoice.due_date_time) or invoice.due_date_time
    delta_days = (now_utc - due_utc).days
    days_overdue = max(0, delta_days) if invoice.outstanding_amount > 0 else 0
    return InvoiceDTO(
        invoice_id=invoice.invoice_id,
        quote_id=invoice.quote_id,
        customer_id=invoice.customer_id,
        status=invoice.status,
        issue_date_time=_ensure_utc(invoice.issue_date_time) or invoice.issue_date_time,
        due_date_time=due_utc,
        total_amount=round(float(invoice.total_amount), 2),
        outstanding_amount=round(float(invoice.outstanding_amount), 2),
        created_at=_ensure_utc(invoice.created_at) or invoice.created_at,
        updated_at=_ensure_utc(invoice.updated_at) or invoice.updated_at,
        days_overdue=days_overdue,
    )


def to_payment_dto(payment: Payment) -> PaymentDTO:
    """Convert a Payment ORM instance into a PaymentDTO."""
    return PaymentDTO(
        payment_id=payment.payment_id,
        invoice_id=payment.invoice_id,
        payment_date_time=_ensure_utc(payment.payment_date_time) or payment.payment_date_time,
        amount_paid=round(float(payment.amount_paid), 2),
        payment_method=payment.payment_method,
        created_at=_ensure_utc(payment.created_at) or payment.created_at,
    )


def create_quote(
    db: Session,
    customer_id: UUID,
    expiration_date_time: datetime,
    total_amount: float,
    approval_state: str | QuoteApprovalState = QuoteApprovalState.DRAFT,
) -> QuoteDTO:
    """Create a new quote record."""
    if total_amount < 0:
        raise InvalidFinancialDataError("Quote total amount cannot be negative.")

    state_val = (
        approval_state.value
        if isinstance(approval_state, QuoteApprovalState)
        else str(approval_state)
    )
    quote = Quote(
        customer_id=customer_id,
        approval_state=state_val,
        expiration_date_time=_ensure_utc(expiration_date_time) or expiration_date_time,
        total_amount=round(total_amount, 2),
    )
    db.add(quote)
    db.commit()
    db.refresh(quote)
    return to_quote_dto(quote)


def get_quote_by_id(db: Session, quote_id: UUID) -> QuoteDTO:
    """Fetch a single quote by ID or raise QuoteNotFoundError."""
    stmt = select(Quote).where(Quote.quote_id == quote_id)
    quote = db.scalars(stmt).first()
    if not quote:
        raise QuoteNotFoundError(f"Quote with ID {quote_id} not found.")
    return to_quote_dto(quote)


def list_quotes(
    db: Session,
    customer_id: UUID | None = None,
    approval_state: str | QuoteApprovalState | None = None,
) -> list[QuoteDTO]:
    """Retrieve and filter quotes from database (FR-1)."""
    stmt = select(Quote)
    if customer_id is not None:
        stmt = stmt.where(Quote.customer_id == customer_id)
    if approval_state is not None:
        state_val = (
            approval_state.value
            if isinstance(approval_state, QuoteApprovalState)
            else str(approval_state)
        )
        stmt = stmt.where(Quote.approval_state == state_val)
    stmt = stmt.order_by(Quote.created_at.desc())
    quotes = db.scalars(stmt).all()
    now_utc = datetime.now(timezone.utc)
    return [to_quote_dto(q, now_utc) for q in quotes]


def create_invoice(
    db: Session,
    customer_id: UUID,
    issue_date_time: datetime,
    due_date_time: datetime,
    total_amount: float,
    quote_id: UUID | None = None,
    status: str | InvoiceStatus = InvoiceStatus.CURRENT,
    outstanding_amount: float | None = None,
) -> InvoiceDTO:
    """Create a new invoice record."""
    if total_amount < 0:
        raise InvalidFinancialDataError("Invoice total amount cannot be negative.")

    status_val = (
        status.value
        if isinstance(status, InvoiceStatus)
        else str(status)
    )
    out_amt = total_amount if outstanding_amount is None else outstanding_amount
    if out_amt < 0:
        raise InvalidFinancialDataError("Outstanding amount cannot be negative.")

    invoice = Invoice(
        quote_id=quote_id,
        customer_id=customer_id,
        status=status_val,
        issue_date_time=_ensure_utc(issue_date_time) or issue_date_time,
        due_date_time=_ensure_utc(due_date_time) or due_date_time,
        total_amount=round(total_amount, 2),
        outstanding_amount=round(out_amt, 2),
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return to_invoice_dto(invoice)


def get_invoice_by_id(db: Session, invoice_id: UUID) -> InvoiceDTO:
    """Fetch a single invoice by ID or raise InvoiceNotFoundError."""
    stmt = select(Invoice).where(Invoice.invoice_id == invoice_id)
    invoice = db.scalars(stmt).first()
    if not invoice:
        raise InvoiceNotFoundError(f"Invoice with ID {invoice_id} not found.")
    return to_invoice_dto(invoice)


def list_invoices(
    db: Session,
    customer_id: UUID | None = None,
    status: str | InvoiceStatus | None = None,
) -> list[InvoiceDTO]:
    """Retrieve and filter invoices from database (FR-2)."""
    stmt = select(Invoice)
    if customer_id is not None:
        stmt = stmt.where(Invoice.customer_id == customer_id)
    if status is not None:
        status_val = (
            status.value
            if isinstance(status, InvoiceStatus)
            else str(status)
        )
        stmt = stmt.where(Invoice.status == status_val)
    stmt = stmt.order_by(Invoice.issue_date_time.desc())
    invoices = db.scalars(stmt).all()
    now_utc = datetime.now(timezone.utc)
    return [to_invoice_dto(inv, now_utc) for inv in invoices]


def record_payment(
    db: Session,
    invoice_id: UUID,
    amount_paid: float,
    payment_method: str,
    payment_date_time: datetime | None = None,
) -> PaymentDTO:
    """Record a payment linked to an invoice and update the invoice outstanding balance (FR-3)."""
    if amount_paid <= 0:
        raise InvalidFinancialDataError("Payment amount must be greater than zero.")

    stmt = select(Invoice).where(Invoice.invoice_id == invoice_id)
    invoice = db.scalars(stmt).first()
    if not invoice:
        raise InvoiceNotFoundError(f"Invoice with ID {invoice_id} not found.")

    pay_dt = _ensure_utc(payment_date_time) or datetime.now(timezone.utc)

    payment = Payment(
        invoice_id=invoice_id,
        payment_date_time=pay_dt,
        amount_paid=round(amount_paid, 2),
        payment_method=payment_method,
    )
    db.add(payment)

    new_outstanding = max(0.0, float(invoice.outstanding_amount) - amount_paid)
    invoice.outstanding_amount = round(new_outstanding, 2)
    if invoice.outstanding_amount == 0.0:
        invoice.status = InvoiceStatus.PAID.value

    db.commit()
    db.refresh(payment)
    db.refresh(invoice)
    return to_payment_dto(payment)


def list_payments(
    db: Session,
    invoice_id: UUID | None = None,
    customer_id: UUID | None = None,
) -> list[PaymentDTO]:
    """Retrieve and filter payment history records linked to invoices (FR-3)."""
    stmt = select(Payment)
    if invoice_id is not None:
        stmt = stmt.where(Payment.invoice_id == invoice_id)
    elif customer_id is not None:
        stmt = stmt.join(Invoice, Payment.invoice_id == Invoice.invoice_id).where(
            Invoice.customer_id == customer_id
        )
    stmt = stmt.order_by(Payment.payment_date_time.desc())
    payments = db.scalars(stmt).all()
    return [to_payment_dto(p) for p in payments]


def calculate_ar_aging(
    db: Session,
    customer_id: UUID | None = None,
    as_of_date_time: datetime | None = None,
) -> ARAgingSummaryDTO:
    """Aggregate Accounts Receivable (AR) aging metrics into duration buckets (FR-4).

    Duration buckets:
    - 0-30 days: Current or <= 30 days overdue
    - 31-60 days: 31 to 60 days overdue
    - 61-90 days: 61 to 90 days overdue
    - 90+ days: >= 91 days overdue
    """
    now_utc = _ensure_utc(as_of_date_time) or datetime.now(timezone.utc)

    stmt = select(Invoice).where(
        Invoice.outstanding_amount > 0,
        Invoice.status != InvoiceStatus.PAID.value,
    )
    if customer_id is not None:
        stmt = stmt.where(Invoice.customer_id == customer_id)

    invoices = db.scalars(stmt).all()

    b0_30_amount, b0_30_count = 0.0, 0
    b31_60_amount, b31_60_count = 0.0, 0
    b61_90_amount, b61_90_count = 0.0, 0
    b90_plus_amount, b90_plus_count = 0.0, 0

    total_outstanding = 0.0
    total_invoices = len(invoices)

    for inv in invoices:
        due_utc = _ensure_utc(inv.due_date_time) or inv.due_date_time
        days_overdue = (now_utc - due_utc).days
        amount = float(inv.outstanding_amount)
        total_outstanding += amount

        if days_overdue <= 30:
            b0_30_amount += amount
            b0_30_count += 1
        elif 31 <= days_overdue <= 60:
            b31_60_amount += amount
            b31_60_count += 1
        elif 61 <= days_overdue <= 90:
            b61_90_amount += amount
            b61_90_count += 1
        else:
            b90_plus_amount += amount
            b90_plus_count += 1

    return ARAgingSummaryDTO(
        customer_id=customer_id,
        as_of_date_time=now_utc,
        total_outstanding=round(total_outstanding, 2),
        total_invoices=total_invoices,
        bucket_0_30=ARAgingBucketDTO("0-30", round(b0_30_amount, 2), b0_30_count),
        bucket_31_60=ARAgingBucketDTO("31-60", round(b31_60_amount, 2), b31_60_count),
        bucket_61_90=ARAgingBucketDTO("61-90", round(b61_90_amount, 2), b61_90_count),
        bucket_90_plus=ARAgingBucketDTO("90+", round(b90_plus_amount, 2), b90_plus_count),
    )
