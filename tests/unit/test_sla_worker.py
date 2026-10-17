"""Unit tests for SLA background worker task and periodic evaluation scan [FR-4, FR-5, FR-6, NFR-3, NFR-4]."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.connectors.notification import InAppNotificationAdapter, NotificationConnector
from src.models.database import Base
from src.models.ticket import SLATracking, SupportTicket, TicketEscalation
from src.services.support.sla_service import initialize_sla_tracking
from src.workers.sla_worker import (
    _ensure_utc,
    process_sla_evaluations,
    run_sla_evaluation_scan,
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


def test_ensure_utc_helper() -> None:
    """Verify _ensure_utc correctly handles naive datetimes, aware datetimes, and ISO strings."""
    # None
    assert _ensure_utc(None) is None

    # Naive datetime
    dt_naive = datetime(2026, 10, 15, 12, 0, 0)
    dt_utc = _ensure_utc(dt_naive)
    assert dt_utc is not None
    assert dt_utc.tzinfo == timezone.utc

    # ISO string
    dt_str = "2026-10-15T12:00:00+00:00"
    dt_from_str = _ensure_utc(dt_str)
    assert dt_from_str is not None
    assert dt_from_str.year == 2026

    # Invalid ISO string
    assert _ensure_utc("invalid-date-string") is None


def test_run_sla_evaluation_scan_empty_db(test_db_session: Session) -> None:
    """Verify SLA scan returns zero counts when no active tickets exist [FR-4]."""
    summary = run_sla_evaluation_scan(db=test_db_session)

    assert summary["evaluated_count"] == 0
    assert summary["warnings_triggered_count"] == 0
    assert summary["breaches_triggered_count"] == 0
    assert summary["escalations_triggered_count"] == 0
    assert summary["details"] == []


def test_run_sla_evaluation_scan_active_ticket(test_db_session: Session) -> None:
    """Verify SLA scan evaluates an open support ticket and initializes SLA tracking [FR-4]."""
    now = datetime.now(timezone.utc)
    ticket = SupportTicket(
        id=uuid.uuid4(),
        priority="P2",
        status="open",
        subject="Standard Support Request",
        description="Issue with user access.",
        client_id="client_200",
        assigned_tier="Tier 1",
        created_at=now,
    )
    test_db_session.add(ticket)
    test_db_session.commit()

    summary = run_sla_evaluation_scan(db=test_db_session, reference_time=now)

    assert summary["evaluated_count"] == 1
    assert summary["warnings_triggered_count"] == 0
    assert summary["breaches_triggered_count"] == 0
    assert summary["escalations_triggered_count"] == 0

    # Verify tracking records were created in DB
    trackings = test_db_session.query(SLATracking).filter(SLATracking.ticket_id == ticket.id).all()
    assert len(trackings) == 2


def test_run_sla_evaluation_scan_warning_threshold(test_db_session: Session) -> None:
    """Verify SLA scan triggers breach warning when warning threshold (80%) is crossed [FR-5]."""
    # P1 ticket created 3.5 hours ago (resolution SLA = 4 hours; 3.5/4 = 87.5% > 80%)
    now = datetime.now(timezone.utc)
    created_at = now - timedelta(hours=3, minutes=30)

    ticket = SupportTicket(
        id=uuid.uuid4(),
        priority="P1",
        status="open",
        subject="High Priority Alert",
        description="Database query latency spike.",
        client_id="client_300",
        assigned_tier="Tier 1",
        created_at=created_at,
    )
    test_db_session.add(ticket)
    test_db_session.commit()

    initialize_sla_tracking(test_db_session, ticket)
    test_db_session.commit()

    notification_adapter = InAppNotificationAdapter()
    connector = NotificationConnector(channels=["in_app"])
    connector.in_app_adapter = notification_adapter

    summary = run_sla_evaluation_scan(
        db=test_db_session,
        reference_time=now,
        notification_port=connector,
    )

    assert summary["evaluated_count"] == 1
    assert summary["warnings_triggered_count"] >= 1

    # Verify warning_sent_at was recorded
    tracking = (
        test_db_session.query(SLATracking)
        .filter(SLATracking.ticket_id == ticket.id, SLATracking.target_type == "resolution")
        .first()
    )
    assert tracking is not None
    assert tracking.warning_sent_at is not None


def test_run_sla_evaluation_scan_breach_and_escalation(test_db_session: Session) -> None:
    """Verify SLA scan detects overdue target breach and executes tier escalation pathway [FR-5, FR-6]."""
    # P1 ticket created 5 hours ago (resolution SLA = 4 hours; breached & > 80% threshold)
    now = datetime.now(timezone.utc)
    created_at = now - timedelta(hours=5)

    ticket = SupportTicket(
        id=uuid.uuid4(),
        priority="P1",
        status="open",
        subject="Breached Production Outage",
        description="Critical system offline.",
        client_id="client_400",
        assigned_tier="Tier 1",
        created_at=created_at,
    )
    test_db_session.add(ticket)
    test_db_session.commit()

    initialize_sla_tracking(test_db_session, ticket)
    test_db_session.commit()

    summary = run_sla_evaluation_scan(db=test_db_session, reference_time=now)

    assert summary["evaluated_count"] == 1
    assert summary["breaches_triggered_count"] >= 1
    assert summary["escalations_triggered_count"] == 1

    # Check updated tier in DB
    updated_ticket = test_db_session.query(SupportTicket).filter(SupportTicket.id == ticket.id).first()
    assert updated_ticket is not None
    assert updated_ticket.assigned_tier == "Tier 2"

    # Check escalation record created
    escalation = test_db_session.query(TicketEscalation).filter(TicketEscalation.ticket_id == ticket.id).first()
    assert escalation is not None
    assert escalation.source_tier == "Tier 1"
    assert escalation.target_tier == "Tier 2"


def test_process_sla_evaluations_celery_task(test_db_session: Session) -> None:
    """Test process_sla_evaluations Celery task execution with SessionLocal patch [FR-4, FR-5, FR-6]."""
    now_str = datetime.now(timezone.utc).isoformat()

    with patch("src.workers.sla_worker.SessionLocal", return_value=test_db_session):
        result = process_sla_evaluations(reference_time_str=now_str)

    assert "evaluated_count" in result
    assert "warnings_triggered_count" in result
    assert "breaches_triggered_count" in result
    assert "escalations_triggered_count" in result
