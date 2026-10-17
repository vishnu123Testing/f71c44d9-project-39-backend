"""Unit tests for Ticket Service implementation [FR-1, FR-2, FR-3, FR-7, FR-8, FR-9, NFR-3, NFR-4]."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.connectors.storage import LocalStorageAdapter
from src.models.database import Base
from src.models.ticket import (
    CSATRating,
    SupportTicket,
    TicketAttachment,
    TicketMessage,
)
from src.services.support.ticket_service import (
    TicketService,
    add_message,
    create_ticket,
    delete_ticket,
    get_csat_rating,
    get_support_analytics,
    get_ticket,
    get_ticket_messages,
    list_tickets,
    submit_csat_rating,
    update_ticket,
)


@pytest.fixture
def db_session() -> Session:
    """Fixture providing an isolated in-memory SQLite database session."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def storage_adapter(tmp_path) -> LocalStorageAdapter:
    """Fixture providing a temporary local storage adapter for attachment testing."""
    return LocalStorageAdapter(storage_dir=str(tmp_path / "attachments"))


def test_create_ticket_initialization(db_session: Session) -> None:
    """Verify support ticket creation, SLA tracking initialization, and initial message threading [FR-1, FR-2, FR-7]."""
    created_time = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
    ticket = create_ticket(
        session=db_session,
        subject="Database Connection Timeout",
        description="Application cannot connect to primary Postgres node.",
        client_id="client_corp_200",
        priority="P1",
        assigned_tier="Tier 1",
        created_at=created_time,
    )
    db_session.commit()

    assert ticket.id is not None
    assert ticket.subject == "Database Connection Timeout"
    assert ticket.priority == "P1"
    assert ticket.status == "open"
    assert ticket.client_id == "client_corp_200"
    assert ticket.first_responded_at is None
    assert ticket.resolved_at is None

    # Check SLA trackings initialized
    assert len(ticket.sla_trackings) == 2
    target_types = {t.target_type for t in ticket.sla_trackings}
    assert target_types == {"first_response", "resolution"}

    # Check initial message thread entry created
    messages = get_ticket_messages(db_session, ticket.id)
    assert len(messages) == 1
    assert messages[0].sender_id == "client_corp_200"
    assert messages[0].sender_type == "client"
    assert messages[0].message_body == "Application cannot connect to primary Postgres node."


def test_get_and_list_tickets(db_session: Session) -> None:
    """Verify fetching single ticket and listing tickets with filtering and search [FR-1]."""
    t1 = create_ticket(
        session=db_session,
        subject="Login Failure",
        description="User cannot login with OAuth provider.",
        client_id="client_alpha",
        priority="P2",
    )
    t2 = create_ticket(
        session=db_session,
        subject="Billing Inquiry",
        description="Question regarding recent invoice line items.",
        client_id="client_beta",
        priority="P3",
    )
    db_session.commit()

    fetched = get_ticket(db_session, t1.id)
    assert fetched is not None
    assert fetched.id == t1.id

    # Filter by client_id
    tickets, count = list_tickets(db_session, client_id="client_alpha")
    assert count == 1
    assert tickets[0].id == t1.id

    # Filter by priority
    tickets_p3, count_p3 = list_tickets(db_session, priority="P3")
    assert count_p3 == 1
    assert tickets_p3[0].id == t2.id

    # Filter by search_query
    tickets_search, count_search = list_tickets(db_session, search_query="OAuth")
    assert count_search == 1
    assert tickets_search[0].id == t1.id


def test_update_ticket_status_and_priority(db_session: Session) -> None:
    """Verify ticket updates, priority recalculations, and resolution timestamps [FR-1, FR-7]."""
    ticket = create_ticket(
        session=db_session,
        subject="Performance Lag",
        description="Dashboard UI loading slowly.",
        client_id="client_corp_201",
        priority="P3",
    )
    db_session.commit()

    # Escalate priority to P1
    update_time = datetime.now(timezone.utc)
    updated = update_ticket(
        session=db_session,
        ticket_id=ticket.id,
        priority="P1",
        updated_at=update_time,
    )
    db_session.commit()

    assert updated.priority == "P1"

    # Mark ticket resolved
    resolve_time = datetime.now(timezone.utc)
    resolved_ticket = update_ticket(
        session=db_session,
        ticket_id=ticket.id,
        status="resolved",
        updated_at=resolve_time,
    )
    db_session.commit()

    assert resolved_ticket.status == "resolved"
    assert resolved_ticket.resolved_at is not None


def test_delete_ticket(db_session: Session) -> None:
    """Verify ticket deletion and cascade cleanup [FR-1]."""
    ticket = create_ticket(
        session=db_session,
        subject="Temporary Ticket",
        description="To be deleted.",
        client_id="client_temp",
    )
    db_session.commit()

    ticket_id = ticket.id
    success = delete_ticket(db_session, ticket_id)
    db_session.commit()

    assert success is True
    assert get_ticket(db_session, ticket_id) is None


def test_add_message_and_first_agent_response(
    db_session: Session,
    storage_adapter: LocalStorageAdapter,
) -> None:
    """Verify appending thread messages, setting first agent response time, and binding attachments [FR-2, FR-3, FR-7]."""
    ticket = create_ticket(
        session=db_session,
        subject="File Upload Issue",
        description="Cannot upload PDF documents.",
        client_id="client_corp_202",
        priority="P2",
    )
    db_session.commit()

    assert ticket.first_responded_at is None

    # Agent responds with attachment
    agent_msg_time = datetime.now(timezone.utc)
    file_bytes = b"Hello, this is a test log file content."
    msg = add_message(
        session=db_session,
        ticket_id=ticket.id,
        sender_id="agent_smith",
        sender_type="agent",
        message_body="Please attach the error log file for inspection.",
        attachments=[
            {
                "filename": "error.txt",
                "content": file_bytes,
                "mime_type": "text/plain",
            }
        ],
        storage_port=storage_adapter,
        created_at=agent_msg_time,
    )
    db_session.commit()

    assert msg.id is not None
    assert ticket.first_responded_at == agent_msg_time
    assert len(msg.attachments) == 1
    assert msg.attachments[0].filename == "error.txt"

    # Retrieve attached file from storage adapter
    retrieved_content = storage_adapter.retrieve_file(msg.attachments[0].storage_key)
    assert retrieved_content == file_bytes


def test_add_message_attachment_validation_failure(
    db_session: Session,
    storage_adapter: LocalStorageAdapter,
) -> None:
    """Verify that illegal attachment file types are rejected during message upload [FR-3, NFR-2]."""
    ticket = create_ticket(
        session=db_session,
        subject="Executable Upload Test",
        description="Testing rejection of executable files.",
        client_id="client_corp_203",
    )
    db_session.commit()

    with pytest.raises(ValueError) as exc_info:
        add_message(
            session=db_session,
            ticket_id=ticket.id,
            sender_id="client_corp_203",
            sender_type="client",
            message_body="Uploading malware test binary.",
            attachments=[
                {
                    "filename": "malicious_script.exe",
                    "content": b"MZ...",
                    "mime_type": "application/x-msdownload",
                }
            ],
            storage_port=storage_adapter,
        )

    assert "Attachment validation failed" in str(exc_info.value)


def test_csat_rating_lifecycle(db_session: Session) -> None:
    """Verify CSAT rating submission rules and status prerequisites [FR-9]."""
    ticket = create_ticket(
        session=db_session,
        subject="CSAT Evaluation Ticket",
        description="Testing CSAT submission.",
        client_id="client_csat",
    )
    db_session.commit()

    # Attempting CSAT submission on open ticket should fail
    with pytest.raises(ValueError) as exc_info_open:
        submit_csat_rating(
            session=db_session,
            ticket_id=ticket.id,
            score=5,
            feedback_text="Great support!",
        )
    assert "resolved or closed tickets" in str(exc_info_open.value)

    # Resolve ticket
    update_ticket(db_session, ticket.id, status="resolved")
    db_session.commit()

    # Attempting score out of range (6) should fail
    with pytest.raises(ValueError) as exc_info_score:
        submit_csat_rating(
            session=db_session,
            ticket_id=ticket.id,
            score=6,
        )
    assert "between 1 and 5" in str(exc_info_score.value)

    # Valid CSAT submission
    rating = submit_csat_rating(
        session=db_session,
        ticket_id=ticket.id,
        score=5,
        feedback_text="Prompt resolution and friendly agent.",
    )
    db_session.commit()

    assert rating.id is not None
    assert rating.score == 5
    assert rating.feedback_text == "Prompt resolution and friendly agent."

    # Duplicate CSAT submission should fail
    with pytest.raises(ValueError) as exc_info_dup:
        submit_csat_rating(
            session=db_session,
            ticket_id=ticket.id,
            score=4,
        )
    assert "already been submitted" in str(exc_info_dup.value)

    # Get CSAT rating
    fetched_rating = get_csat_rating(db_session, ticket.id)
    assert fetched_rating is not None
    assert fetched_rating.score == 5


def test_get_support_analytics(db_session: Session) -> None:
    """Verify support operations metrics aggregation over time windows [FR-8]."""
    t_start = datetime(2026, 10, 10, 8, 0, 0, tzinfo=timezone.utc)

    # Ticket 1: Responded in 30 mins, resolved in 120 mins, CSAT = 5
    t1 = create_ticket(
        session=db_session,
        subject="SLA Test 1",
        description="First ticket.",
        client_id="client_analytics",
        priority="P1",
        created_at=t_start,
    )
    add_message(
        session=db_session,
        ticket_id=t1.id,
        sender_id="agent_1",
        sender_type="agent",
        message_body="Investigating issue.",
        created_at=t_start + timedelta(minutes=30),
    )
    update_ticket(
        session=db_session,
        ticket_id=t1.id,
        status="resolved",
        updated_at=t_start + timedelta(minutes=120),
    )
    submit_csat_rating(
        session=db_session,
        ticket_id=t1.id,
        score=5,
        submitted_at=t_start + timedelta(minutes=130),
    )

    # Ticket 2: Responded in 60 mins, resolved in 240 mins, CSAT = 3
    t2 = create_ticket(
        session=db_session,
        subject="SLA Test 2",
        description="Second ticket.",
        client_id="client_analytics",
        priority="P2",
        created_at=t_start,
    )
    add_message(
        session=db_session,
        ticket_id=t2.id,
        sender_id="agent_2",
        sender_type="agent",
        message_body="Processing request.",
        created_at=t_start + timedelta(minutes=60),
    )
    update_ticket(
        session=db_session,
        ticket_id=t2.id,
        status="resolved",
        updated_at=t_start + timedelta(minutes=240),
    )
    submit_csat_rating(
        session=db_session,
        ticket_id=t2.id,
        score=3,
        submitted_at=t_start + timedelta(minutes=250),
    )

    db_session.commit()

    analytics = get_support_analytics(
        session=db_session,
        start_time=t_start - timedelta(hours=1),
        end_time=t_start + timedelta(hours=24),
    )

    assert analytics["total_tickets"] == 2
    assert analytics["resolved_tickets"] == 2
    assert analytics["open_tickets"] == 0

    # Avg response time: (1800s + 3600s) / 2 = 2700s
    assert analytics["average_first_response_time_seconds"] == 2700.0

    # MTTR: (7200s + 14400s) / 2 = 10800s
    assert analytics["mean_time_to_resolution_seconds"] == 10800.0

    # Avg CSAT: (5 + 3) / 2 = 4.0
    assert analytics["average_csat_score"] == 4.0
    assert analytics["csat_response_count"] == 2

    # Priority breakdown
    assert analytics["priority_breakdown"]["P1"] == 1
    assert analytics["priority_breakdown"]["P2"] == 1
    assert analytics["priority_breakdown"]["P3"] == 0


def test_ticket_service_class_wrapper(
    db_session: Session,
    storage_adapter: LocalStorageAdapter,
) -> None:
    """Verify TicketService class wrapper and dependency injection [NFR-4]."""
    service = TicketService(session=db_session, storage_port=storage_adapter)

    ticket = service.create_ticket(
        subject="Service Class Test",
        description="Testing class wrapper.",
        client_id="client_wrapper",
        priority="P2",
    )
    db_session.commit()

    assert ticket.id is not None
    assert service.get_ticket(ticket.id) is not None

    msg = service.add_message(
        ticket_id=ticket.id,
        sender_id="agent_007",
        sender_type="agent",
        message_body="Agent replying via service class.",
    )
    db_session.commit()

    messages = service.get_messages(ticket.id)
    assert len(messages) == 2  # initial + agent reply

    updated = service.update_ticket(ticket_id=ticket.id, status="resolved")
    db_session.commit()
    assert updated.status == "resolved"

    rating = service.submit_csat_rating(ticket.id, score=4)
    db_session.commit()
    assert rating.score == 4

    csat = service.get_csat_rating(ticket.id)
    assert csat is not None and csat.score == 4

    analytics = service.get_analytics()
    assert analytics["total_tickets"] >= 1
