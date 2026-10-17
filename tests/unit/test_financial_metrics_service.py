"""Unit tests for financial metrics service functions (FR-1, FR-2, FR-3, FR-4)."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.models.database import Base
from src.models.financial import (
    InvoiceStatus,
    QuoteApprovalState,
)
from src.services.financial.metrics import (
    ARAgingSummaryDTO,
    InvalidFinancialDataError,
    InvoiceNotFoundError,
    QuoteNotFoundError,
    calculate_ar_aging,
    create_invoice,
    create_quote,
    get_invoice_by_id,
    get_quote_by_id,
    list_invoices,
    list_payments,
    list_quotes,
    record_payment,
)


@pytest.fixture
def db_session() -> Session:
    """Fixture providing an isolated in-memory SQLite database session."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    session = testing_session_local()
    try:
        yield session
    finally:
        session.close()


def test_quote_filtering_and_expiration(db_session: Session) -> None:
    """Test creating, fetching, and filtering quotes (FR-1)."""
    customer_a = uuid.uuid4()
    customer_b = uuid.uuid4()
    now_utc = datetime.now(timezone.utc)

    # Quote 1: Draft for Customer A, future exp
    q1 = create_quote(
        db_session,
        customer_id=customer_a,
        expiration_date_time=now_utc + timedelta(days=10),
        total_amount=1000.00,
        approval_state=QuoteApprovalState.DRAFT,
    )

    # Quote 2: Approved for Customer A, past exp
    q2 = create_quote(
        db_session,
        customer_id=customer_a,
        expiration_date_time=now_utc - timedelta(days=5),
        total_amount=2500.00,
        approval_state=QuoteApprovalState.APPROVED,
    )

    # Quote 3: Pending Approval for Customer B, past exp
    q3 = create_quote(
        db_session,
        customer_id=customer_b,
        expiration_date_time=now_utc - timedelta(days=2),
        total_amount=500.00,
        approval_state=QuoteApprovalState.PENDING_APPROVAL,
    )

    # Verify get_quote_by_id
    fetched_q1 = get_quote_by_id(db_session, q1.quote_id)
    assert fetched_q1.quote_id == q1.quote_id
    assert fetched_q1.total_amount == 1000.00
    assert not fetched_q1.is_expired

    # q2 is approved so not marked expired even if expiration date is past
    fetched_q2 = get_quote_by_id(db_session, q2.quote_id)
    assert not fetched_q2.is_expired

    # q3 is pending approval and past expiration date, so is_expired should be True
    fetched_q3 = get_quote_by_id(db_session, q3.quote_id)
    assert fetched_q3.is_expired

    # Filter by Customer A
    quotes_a = list_quotes(db_session, customer_id=customer_a)
    assert len(quotes_a) == 2
    assert {q.quote_id for q in quotes_a} == {q1.quote_id, q2.quote_id}

    # Filter by approval_state Approved
    approved_quotes = list_quotes(
        db_session, approval_state=QuoteApprovalState.APPROVED
    )
    assert len(approved_quotes) == 1
    assert approved_quotes[0].quote_id == q2.quote_id


def test_invoice_filtering(db_session: Session) -> None:
    """Test creating and filtering invoices by customer and status (FR-2)."""
    customer_id = uuid.uuid4()
    now_utc = datetime.now(timezone.utc)

    inv1 = create_invoice(
        db_session,
        customer_id=customer_id,
        issue_date_time=now_utc - timedelta(days=10),
        due_date_time=now_utc + timedelta(days=20),
        total_amount=1200.00,
        status=InvoiceStatus.CURRENT,
    )

    inv2 = create_invoice(
        db_session,
        customer_id=customer_id,
        issue_date_time=now_utc - timedelta(days=40),
        due_date_time=now_utc - timedelta(days=10),
        total_amount=800.00,
        status=InvoiceStatus.PAST_DUE,
    )

    # Fetch single invoice
    fetched_inv1 = get_invoice_by_id(db_session, inv1.invoice_id)
    assert fetched_inv1.outstanding_amount == 1200.00
    assert fetched_inv1.status == "Current"

    # Filter by customer
    cust_invoices = list_invoices(db_session, customer_id=customer_id)
    assert len(cust_invoices) == 2

    # Filter by status PAST_DUE
    past_due_invoices = list_invoices(db_session, status=InvoiceStatus.PAST_DUE)
    assert len(past_due_invoices) == 1
    assert past_due_invoices[0].invoice_id == inv2.invoice_id


def test_payment_recording_and_linking(db_session: Session) -> None:
    """Test recording payments and verifying invoice status updates (FR-3)."""
    customer_id = uuid.uuid4()
    now_utc = datetime.now(timezone.utc)

    inv = create_invoice(
        db_session,
        customer_id=customer_id,
        issue_date_time=now_utc - timedelta(days=15),
        due_date_time=now_utc + timedelta(days=15),
        total_amount=1000.00,
        status=InvoiceStatus.CURRENT,
    )

    # Partial payment
    p1 = record_payment(
        db_session,
        invoice_id=inv.invoice_id,
        amount_paid=400.00,
        payment_method="Credit Card",
    )
    assert p1.amount_paid == 400.00

    updated_inv = get_invoice_by_id(db_session, inv.invoice_id)
    assert updated_inv.outstanding_amount == 600.00
    assert updated_inv.status == "Current"

    # Full remaining payment
    p2 = record_payment(
        db_session,
        invoice_id=inv.invoice_id,
        amount_paid=600.00,
        payment_method="Bank Transfer",
    )
    assert p2.amount_paid == 600.00

    paid_inv = get_invoice_by_id(db_session, inv.invoice_id)
    assert paid_inv.outstanding_amount == 0.0
    assert paid_inv.status == InvoiceStatus.PAID.value

    # List payments for invoice
    inv_payments = list_payments(db_session, invoice_id=inv.invoice_id)
    assert len(inv_payments) == 2

    # List payments for customer
    cust_payments = list_payments(db_session, customer_id=customer_id)
    assert len(cust_payments) == 2


def test_ar_aging_buckets_calculation(db_session: Session) -> None:
    """Test accounts receivable aging segmentation across duration buckets (FR-4)."""
    customer_id = uuid.uuid4()
    as_of = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)

    # 0-30 day bucket: Due in 10 days (overdue = -10 days)
    create_invoice(
        db_session,
        customer_id=customer_id,
        issue_date_time=as_of - timedelta(days=20),
        due_date_time=as_of + timedelta(days=10),
        total_amount=100.00,
    )

    # 0-30 day bucket: Overdue by 15 days
    create_invoice(
        db_session,
        customer_id=customer_id,
        issue_date_time=as_of - timedelta(days=45),
        due_date_time=as_of - timedelta(days=15),
        total_amount=200.00,
    )

    # 31-60 day bucket: Overdue by 45 days
    create_invoice(
        db_session,
        customer_id=customer_id,
        issue_date_time=as_of - timedelta(days=75),
        due_date_time=as_of - timedelta(days=45),
        total_amount=300.00,
    )

    # 61-90 day bucket: Overdue by 75 days
    create_invoice(
        db_session,
        customer_id=customer_id,
        issue_date_time=as_of - timedelta(days=105),
        due_date_time=as_of - timedelta(days=75),
        total_amount=400.00,
    )

    # 90+ day bucket: Overdue by 120 days
    create_invoice(
        db_session,
        customer_id=customer_id,
        issue_date_time=as_of - timedelta(days=150),
        due_date_time=as_of - timedelta(days=120),
        total_amount=500.00,
    )

    summary: ARAgingSummaryDTO = calculate_ar_aging(
        db_session, customer_id=customer_id, as_of_date_time=as_of
    )

    assert summary.total_outstanding == 1500.00
    assert summary.total_invoices == 5

    assert summary.bucket_0_30.bucket_name == "0-30"
    assert summary.bucket_0_30.total_amount == 300.00
    assert summary.bucket_0_30.invoice_count == 2

    assert summary.bucket_31_60.bucket_name == "31-60"
    assert summary.bucket_31_60.total_amount == 300.00
    assert summary.bucket_31_60.invoice_count == 1

    assert summary.bucket_61_90.bucket_name == "61-90"
    assert summary.bucket_61_90.total_amount == 400.00
    assert summary.bucket_61_90.invoice_count == 1

    assert summary.bucket_90_plus.bucket_name == "90+"
    assert summary.bucket_90_plus.total_amount == 500.00
    assert summary.bucket_90_plus.invoice_count == 1


def test_ar_aging_empty_state(db_session: Session) -> None:
    """Test AR aging calculation when no outstanding records exist (empty state)."""
    customer_id = uuid.uuid4()

    summary = calculate_ar_aging(db_session, customer_id=customer_id)

    assert summary.total_outstanding == 0.0
    assert summary.total_invoices == 0
    assert summary.bucket_0_30.total_amount == 0.0
    assert summary.bucket_0_30.invoice_count == 0
    assert summary.bucket_31_60.total_amount == 0.0
    assert summary.bucket_31_60.invoice_count == 0
    assert summary.bucket_61_90.total_amount == 0.0
    assert summary.bucket_61_90.invoice_count == 0
    assert summary.bucket_90_plus.total_amount == 0.0
    assert summary.bucket_90_plus.invoice_count == 0


def test_financial_metrics_exceptions(db_session: Session) -> None:
    """Test error handling for missing records and invalid inputs."""
    random_id = uuid.uuid4()

    with pytest.raises(QuoteNotFoundError):
        get_quote_by_id(db_session, random_id)

    with pytest.raises(InvoiceNotFoundError):
        get_invoice_by_id(db_session, random_id)

    with pytest.raises(InvoiceNotFoundError):
        record_payment(db_session, invoice_id=random_id, amount_paid=100.0, payment_method="Check")

    with pytest.raises(InvalidFinancialDataError):
        create_quote(db_session, customer_id=random_id, expiration_date_time=datetime.now(timezone.utc), total_amount=-50.0)

    with pytest.raises(InvalidFinancialDataError):
        create_invoice(
            db_session,
            customer_id=random_id,
            issue_date_time=datetime.now(timezone.utc),
            due_date_time=datetime.now(timezone.utc),
            total_amount=-100.0,
        )
