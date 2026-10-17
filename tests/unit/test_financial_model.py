from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.models.database import Base
from src.models.financial import (
    Invoice,
    InvoiceStatus,
    Payment,
    Quote,
    QuoteApprovalState,
)


@pytest.fixture
def test_db_session() -> Session:
    """Fixture providing an isolated in-memory SQLite database session with foreign keys enabled."""
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


def test_financial_tablenames() -> None:
    """Test table names for financial ORM models."""
    assert Quote.__tablename__ == "quotes"
    assert Invoice.__tablename__ == "invoices"
    assert Payment.__tablename__ == "payments"


def test_quote_approval_state_enum() -> None:
    """Test QuoteApprovalState enum values."""
    assert QuoteApprovalState.DRAFT.value == "Draft"
    assert QuoteApprovalState.PENDING_APPROVAL.value == "Pending_Approval"
    assert QuoteApprovalState.APPROVED.value == "Approved"
    assert QuoteApprovalState.EXPIRED.value == "Expired"


def test_invoice_status_enum() -> None:
    """Test InvoiceStatus enum values."""
    assert InvoiceStatus.CURRENT.value == "Current"
    assert InvoiceStatus.PENDING.value == "Pending"
    assert InvoiceStatus.PAST_DUE.value == "Past_Due"
    assert InvoiceStatus.PAID.value == "Paid"


def test_quote_creation_and_defaults(test_db_session: Session) -> None:
    """Test creating a Quote entity with default and explicit fields."""
    customer_id = uuid.uuid4()
    exp_time = datetime.now(timezone.utc)

    quote = Quote(
        customer_id=customer_id,
        approval_state=QuoteApprovalState.DRAFT.value,
        expiration_date_time=exp_time,
        total_amount=1500.50,
    )
    test_db_session.add(quote)
    test_db_session.commit()
    test_db_session.refresh(quote)

    assert isinstance(quote.quote_id, uuid.UUID)
    assert quote.customer_id == customer_id
    assert quote.approval_state == "Draft"
    assert quote.expiration_date_time == exp_time
    assert quote.total_amount == 1500.50
    assert isinstance(quote.created_at, datetime)
    assert isinstance(quote.updated_at, datetime)
    assert quote.invoices == []
    assert "<Quote(" in repr(quote)


def test_invoice_creation_and_relationship(test_db_session: Session) -> None:
    """Test creating an Invoice linked to a Quote."""
    customer_id = uuid.uuid4()
    issue_time = datetime.now(timezone.utc)
    due_time = datetime.now(timezone.utc)
    exp_time = datetime.now(timezone.utc)

    quote = Quote(
        customer_id=customer_id,
        approval_state=QuoteApprovalState.APPROVED.value,
        expiration_date_time=exp_time,
        total_amount=5000.00,
    )
    test_db_session.add(quote)
    test_db_session.commit()

    invoice = Invoice(
        quote_id=quote.quote_id,
        customer_id=customer_id,
        status=InvoiceStatus.CURRENT.value,
        issue_date_time=issue_time,
        due_date_time=due_time,
        total_amount=5000.00,
        outstanding_amount=5000.00,
    )
    test_db_session.add(invoice)
    test_db_session.commit()
    test_db_session.refresh(invoice)

    assert isinstance(invoice.invoice_id, uuid.UUID)
    assert invoice.quote_id == quote.quote_id
    assert invoice.quote == quote
    assert invoice in quote.invoices
    assert invoice.status == "Current"
    assert invoice.total_amount == 5000.00
    assert invoice.outstanding_amount == 5000.00
    assert "<Invoice(" in repr(invoice)


def test_payment_creation_and_cascade_delete(test_db_session: Session) -> None:
    """Test creating Payments for an Invoice and verifying relationship and cascade deletion."""
    customer_id = uuid.uuid4()
    now_time = datetime.now(timezone.utc)

    invoice = Invoice(
        customer_id=customer_id,
        status=InvoiceStatus.CURRENT.value,
        issue_date_time=now_time,
        due_date_time=now_time,
        total_amount=2000.00,
        outstanding_amount=1000.00,
    )
    test_db_session.add(invoice)
    test_db_session.commit()

    payment = Payment(
        invoice_id=invoice.invoice_id,
        payment_date_time=now_time,
        amount_paid=1000.00,
        payment_method="Credit Card",
    )
    test_db_session.add(payment)
    test_db_session.commit()
    test_db_session.refresh(payment)

    assert isinstance(payment.payment_id, uuid.UUID)
    assert payment.invoice_id == invoice.invoice_id
    assert payment.invoice == invoice
    assert payment.amount_paid == 1000.00
    assert payment.payment_method == "Credit Card"
    assert payment in invoice.payments
    assert "<Payment(" in repr(payment)

    # Test cascade deletion when invoice is deleted
    test_db_session.delete(invoice)
    test_db_session.commit()

    deleted_payment = test_db_session.get(Payment, payment.payment_id)
    assert deleted_payment is None
