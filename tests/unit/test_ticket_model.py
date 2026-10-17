from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.models.database import Base
from src.models.ticket import (
    CSATRating,
    SLATracking,
    SupportTicket,
    TicketAttachment,
    TicketEscalation,
    TicketMessage,
)


@pytest.fixture
def test_db_session() -> Session:
    """Fixture providing an isolated in-memory SQLite database session."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    session = testing_session_local()
    try:
        yield session
    finally:
        session.close()


def test_ticket_tablenames() -> None:
    """Test table names for all support ticket ORM models."""
    assert SupportTicket.__tablename__ == "support_tickets"
    assert TicketMessage.__tablename__ == "ticket_messages"
    assert TicketAttachment.__tablename__ == "ticket_attachments"
    assert SLATracking.__tablename__ == "sla_tracking"
    assert TicketEscalation.__tablename__ == "ticket_escalations"
    assert CSATRating.__tablename__ == "csat_ratings"


def test_support_ticket_creation_and_defaults(test_db_session: Session) -> None:
    """Test creating a SupportTicket record with default values."""
    ticket = SupportTicket(
        priority="P1",
        subject="Database connection timeout",
        description="Users experiencing intermittent 500 errors.",
        client_id="client_acme_001",
    )
    test_db_session.add(ticket)
    test_db_session.commit()
    test_db_session.refresh(ticket)

    assert isinstance(ticket.id, uuid.UUID)
    assert ticket.priority == "P1"
    assert ticket.status == "open"
    assert ticket.subject == "Database connection timeout"
    assert ticket.description == "Users experiencing intermittent 500 errors."
    assert ticket.client_id == "client_acme_001"
    assert ticket.assigned_tier == "Tier 1"
    assert ticket.assigned_agent_id is None
    assert ticket.first_responded_at is None
    assert ticket.resolved_at is None
    assert isinstance(ticket.created_at, datetime)
    assert isinstance(ticket.updated_at, datetime)
    assert repr(ticket).startswith("<SupportTicket(")


def test_ticket_messages_and_attachments(test_db_session: Session) -> None:
    """Test creating a TicketMessage and an associated TicketAttachment."""
    ticket = SupportTicket(
        priority="P2",
        subject="API rate limit inquiry",
        description="Need higher quota for batch processing.",
        client_id="client_globex_002",
    )
    test_db_session.add(ticket)
    test_db_session.commit()

    message = TicketMessage(
        ticket_id=ticket.id,
        sender_id="user_client_123",
        sender_type="client",
        message_body="Attached is our current batch volume log.",
    )
    test_db_session.add(message)
    test_db_session.commit()
    test_db_session.refresh(message)

    attachment = TicketAttachment(
        message_id=message.id,
        filename="batch_log.txt",
        file_size_bytes=2048,
        mime_type="text/plain",
        storage_key="attachments/2026/10/batch_log.txt",
    )
    test_db_session.add(attachment)
    test_db_session.commit()
    test_db_session.refresh(attachment)

    assert isinstance(message.id, uuid.UUID)
    assert message.ticket_id == ticket.id
    assert message.sender_id == "user_client_123"
    assert message.sender_type == "client"
    assert message.message_body == "Attached is our current batch volume log."
    assert isinstance(message.created_at, datetime)
    assert repr(message).startswith("<TicketMessage(")

    assert isinstance(attachment.id, uuid.UUID)
    assert attachment.message_id == message.id
    assert attachment.filename == "batch_log.txt"
    assert attachment.file_size_bytes == 2048
    assert attachment.mime_type == "text/plain"
    assert attachment.storage_key == "attachments/2026/10/batch_log.txt"
    assert isinstance(attachment.uploaded_at, datetime)
    assert repr(attachment).startswith("<TicketAttachment(")

    # Verify relationships
    assert len(ticket.messages) == 1
    assert ticket.messages[0].id == message.id
    assert len(message.attachments) == 1
    assert message.attachments[0].id == attachment.id
    assert attachment.message.id == message.id


def test_sla_tracking_creation_and_uniqueness(test_db_session: Session) -> None:
    """Test creating SLATracking records and enforcing uniqueness constraint."""
    ticket = SupportTicket(
        priority="P1",
        subject="Production outage",
        description="Main API is unresponsive.",
        client_id="client_stark_003",
    )
    test_db_session.add(ticket)
    test_db_session.commit()

    due_at = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
    sla1 = SLATracking(
        ticket_id=ticket.id,
        target_type="first_response",
        target_due_at=due_at,
        is_breached=False,
    )
    test_db_session.add(sla1)
    test_db_session.commit()
    test_db_session.refresh(sla1)

    assert isinstance(sla1.id, uuid.UUID)
    assert sla1.target_type == "first_response"
    assert sla1.target_due_at == due_at
    assert sla1.is_breached is False
    assert sla1.warning_sent_at is None
    assert sla1.breached_at is None
    assert repr(sla1).startswith("<SLATracking(")

    # Test duplicate (ticket_id, target_type) raises IntegrityError
    sla_duplicate = SLATracking(
        ticket_id=ticket.id,
        target_type="first_response",
        target_due_at=due_at,
    )
    test_db_session.add(sla_duplicate)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_ticket_escalation_creation(test_db_session: Session) -> None:
    """Test creating a TicketEscalation record."""
    ticket = SupportTicket(
        priority="P1",
        subject="SLA Breach Impending",
        description="Unassigned high priority issue.",
        client_id="client_cyber_004",
    )
    test_db_session.add(ticket)
    test_db_session.commit()

    escalation = TicketEscalation(
        ticket_id=ticket.id,
        source_tier="Tier 1",
        target_tier="Tier 2",
        reason="Automated escalation due to 80% SLA elapsed.",
    )
    test_db_session.add(escalation)
    test_db_session.commit()
    test_db_session.refresh(escalation)

    assert isinstance(escalation.id, uuid.UUID)
    assert escalation.ticket_id == ticket.id
    assert escalation.source_tier == "Tier 1"
    assert escalation.target_tier == "Tier 2"
    assert escalation.reason == "Automated escalation due to 80% SLA elapsed."
    assert isinstance(escalation.escalated_at, datetime)
    assert repr(escalation).startswith("<TicketEscalation(")
    assert len(ticket.escalations) == 1


def test_csat_rating_creation_and_check_constraint(
    test_db_session: Session,
) -> None:
    """Test creating a CSATRating and enforcing score bounds."""
    ticket = SupportTicket(
        priority="P3",
        subject="Feature request inquiry",
        description="How to configure custom webhooks?",
        client_id="client_wayne_005",
        status="resolved",
    )
    test_db_session.add(ticket)
    test_db_session.commit()

    csat = CSATRating(
        ticket_id=ticket.id,
        score=5,
        feedback_text="Excellent support from agent!",
    )
    test_db_session.add(csat)
    test_db_session.commit()
    test_db_session.refresh(csat)

    assert isinstance(csat.id, uuid.UUID)
    assert csat.ticket_id == ticket.id
    assert csat.score == 5
    assert csat.feedback_text == "Excellent support from agent!"
    assert isinstance(csat.submitted_at, datetime)
    assert repr(csat).startswith("<CSATRating(")
    assert ticket.csat_rating.id == csat.id

    # Test invalid CSAT score (> 5) raises IntegrityError
    ticket2 = SupportTicket(
        priority="P3",
        subject="Billing question",
        description="Invoice clarifying.",
        client_id="client_wayne_005",
        status="resolved",
    )
    test_db_session.add(ticket2)
    test_db_session.commit()

    invalid_csat = CSATRating(
        ticket_id=ticket2.id,
        score=6,
    )
    test_db_session.add(invalid_csat)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_ticket_cascade_delete(test_db_session: Session) -> None:
    """Test deleting a ticket cascades deletion to all related records."""
    ticket = SupportTicket(
        priority="P2",
        subject="Temporary issue",
        description="Delete test",
        client_id="client_test",
    )
    test_db_session.add(ticket)
    test_db_session.commit()

    msg = TicketMessage(
        ticket_id=ticket.id,
        sender_id="sender_1",
        sender_type="client",
        message_body="Test message",
    )
    test_db_session.add(msg)
    test_db_session.commit()

    att = TicketAttachment(
        message_id=msg.id,
        filename="test.png",
        file_size_bytes=100,
        mime_type="image/png",
        storage_key="key/1",
    )
    sla = SLATracking(
        ticket_id=ticket.id,
        target_type="resolution",
        target_due_at=datetime.now(timezone.utc),
    )
    esc = TicketEscalation(
        ticket_id=ticket.id,
        source_tier="Tier 1",
        target_tier="Tier 2",
        reason="Test",
    )
    csat = CSATRating(
        ticket_id=ticket.id,
        score=4,
    )
    test_db_session.add_all([att, sla, esc, csat])
    test_db_session.commit()

    # Now delete the ticket
    test_db_session.delete(ticket)
    test_db_session.commit()

    assert test_db_session.query(SupportTicket).count() == 0
    assert test_db_session.query(TicketMessage).count() == 0
    assert test_db_session.query(TicketAttachment).count() == 0
    assert test_db_session.query(SLATracking).count() == 0
    assert test_db_session.query(TicketEscalation).count() == 0
    assert test_db_session.query(CSATRating).count() == 0
