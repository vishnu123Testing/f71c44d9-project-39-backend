"""Unit tests for SLA evaluation service [FR-4, FR-5, FR-6, NFR-3, NFR-4]."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.connectors.notification import InAppNotificationAdapter
from src.models.database import Base
from src.models.ticket import (
    SLATracking,
    SupportTicket,
    TicketEscalation,
)
from src.services.support.sla_service import (
    SLAService,
    calculate_target_deadlines,
    evaluate_all_active_slas,
    evaluate_ticket_sla,
    initialize_sla_tracking,
    recalculate_sla_targets,
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


def test_calculate_target_deadlines_p1_p2_p3() -> None:
    """Verify SLA deadline calculations for P1, P2, P3 priorities [FR-4]."""
    base_time = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)

    # P1: 1h response / 4h resolution
    deadlines_p1 = calculate_target_deadlines("P1", created_at=base_time)
    assert deadlines_p1["first_response"] == base_time + timedelta(hours=1)
    assert deadlines_p1["resolution"] == base_time + timedelta(hours=4)

    # P2: 4h response / 24h resolution
    deadlines_p2 = calculate_target_deadlines("P2", created_at=base_time)
    assert deadlines_p2["first_response"] == base_time + timedelta(hours=4)
    assert deadlines_p2["resolution"] == base_time + timedelta(hours=24)

    # P3: 24h response / 72h resolution
    deadlines_p3 = calculate_target_deadlines("P3", created_at=base_time)
    assert deadlines_p3["first_response"] == base_time + timedelta(hours=24)
    assert deadlines_p3["resolution"] == base_time + timedelta(hours=72)


def test_initialize_sla_tracking(db_session: Session) -> None:
    """Verify SLA tracking record initialization for a new ticket [FR-4]."""
    created_at = datetime.now(timezone.utc)
    ticket = SupportTicket(
        priority="P1",
        subject="Critical Outage",
        description="Production API endpoint down.",
        client_id="client_corp_100",
        created_at=created_at,
    )
    db_session.add(ticket)
    db_session.commit()

    trackings = initialize_sla_tracking(db_session, ticket)
    db_session.commit()

    assert len(trackings) == 2
    target_types = {t.target_type for t in trackings}
    assert target_types == {"first_response", "resolution"}

    for t in trackings:
        assert t.is_breached is False
        assert t.warning_sent_at is None
        assert t.breached_at is None


def test_recalculate_sla_targets_on_priority_escalation(db_session: Session) -> None:
    """Verify deadline recalculation when ticket priority is escalated from P3 to P1 [FR-4]."""
    created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    ticket = SupportTicket(
        priority="P3",
        subject="Minor UI issue",
        description="Typo on landing page.",
        client_id="client_corp_101",
        created_at=created_at,
    )
    db_session.add(ticket)
    db_session.commit()

    initialize_sla_tracking(db_session, ticket)
    db_session.commit()

    # Priority escalated to P1 2 hours after creation
    # P1 first_response SLA is 1h from creation. Since created_at was 2 hours ago, first_response target is breached!
    updated_trackings = recalculate_sla_targets(db_session, ticket, "P1")
    db_session.commit()

    assert ticket.priority == "P1"
    first_resp = next(t for t in updated_trackings if t.target_type == "first_response")
    resolution = next(t for t in updated_trackings if t.target_type == "resolution")

    assert first_resp.target_due_at == created_at + timedelta(hours=1)
    assert first_resp.is_breached is True

    assert resolution.target_due_at == created_at + timedelta(hours=4)
    assert resolution.is_breached is False


def test_evaluate_ticket_sla_warning_and_breach(db_session: Session) -> None:
    """Verify proactive warning dispatches and breach alerts during SLA evaluation [FR-5]."""
    created_at = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
    ticket = SupportTicket(
        priority="P1",  # Response: 1h, Resolution: 4h
        subject="Intermittent Latency",
        description="High latency on DB reads.",
        client_id="client_corp_102",
        created_at=created_at,
    )
    db_session.add(ticket)
    db_session.commit()

    initialize_sla_tracking(db_session, ticket)
    db_session.commit()

    notification_adapter = InAppNotificationAdapter()

    # Evaluate at 50 minutes after creation (50m / 60m = 83.3% of 1h response SLA -> >= 80% warning threshold)
    eval_time_warning = created_at + timedelta(minutes=50)
    result_warning = evaluate_ticket_sla(
        session=db_session,
        ticket=ticket,
        notification_port=notification_adapter,
        now=eval_time_warning,
    )

    assert "first_response" in result_warning["warnings_sent"]
    assert len(notification_adapter.sent_notifications) == 1
    assert notification_adapter.sent_notifications[0]["notification_type"] == "breach_warning"

    # Evaluate at 1 hour 10 minutes after creation (70m > 60m -> breach!)
    eval_time_breach = created_at + timedelta(minutes=70)
    result_breach = evaluate_ticket_sla(
        session=db_session,
        ticket=ticket,
        notification_port=notification_adapter,
        now=eval_time_breach,
    )

    assert "first_response" in result_breach["breaches_detected"]
    assert len(notification_adapter.sent_notifications) == 2
    assert notification_adapter.sent_notifications[1]["notification_type"] == "breach_alert"


def test_evaluate_ticket_sla_escalation_pathway(db_session: Session) -> None:
    """Verify automated escalation pathway reassigning support tiers [FR-6]."""
    created_at = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
    ticket = SupportTicket(
        priority="P1",  # Resolution SLA: 4 hours
        subject="Server Crash",
        description="Primary node crashed.",
        client_id="client_corp_103",
        assigned_tier="Tier 1",
        created_at=created_at,
    )
    db_session.add(ticket)
    db_session.commit()

    initialize_sla_tracking(db_session, ticket)
    db_session.commit()

    notification_adapter = InAppNotificationAdapter()

    # Evaluate at 3 hours 30 minutes (3.5h / 4h = 87.5% >= 80% escalation threshold)
    eval_time = created_at + timedelta(hours=3, minutes=30)
    eval_result = evaluate_ticket_sla(
        session=db_session,
        ticket=ticket,
        notification_port=notification_adapter,
        now=eval_time,
    )

    assert eval_result["escalated"] is True
    assert eval_result["source_tier"] == "Tier 1"
    assert eval_result["target_tier"] == "Tier 2"
    assert ticket.assigned_tier == "Tier 2"

    # Check escalation log record in DB
    escalations = list(ticket.escalations)
    assert len(escalations) == 1
    assert escalations[0].source_tier == "Tier 1"
    assert escalations[0].target_tier == "Tier 2"


def test_batch_evaluate_all_active_slas(db_session: Session) -> None:
    """Verify batch scanning of all active tickets [FR-4, FR-5, FR-6]."""
    now = datetime.now(timezone.utc)
    ticket1 = SupportTicket(
        priority="P1",
        subject="Ticket 1",
        description="Desc 1",
        client_id="c1",
        status="open",
        created_at=now - timedelta(minutes=55),  # Trigger warning
    )
    ticket2 = SupportTicket(
        priority="P1",
        subject="Ticket 2",
        description="Desc 2",
        client_id="c2",
        status="resolved",  # Resolved, should be skipped in active query
        created_at=now - timedelta(minutes=90),
    )
    db_session.add_all([ticket1, ticket2])
    db_session.commit()

    initialize_sla_tracking(db_session, ticket1)
    db_session.commit()

    notification_adapter = InAppNotificationAdapter()
    batch_result = evaluate_all_active_slas(
        session=db_session,
        notification_port=notification_adapter,
        now=now,
    )

    assert batch_result["evaluated_count"] == 1
    assert batch_result["warnings_triggered_count"] >= 1


def test_sla_service_class_wrapper(db_session: Session) -> None:
    """Verify SLAService dependency injection class wrapper [NFR-4]."""
    notification_adapter = InAppNotificationAdapter()
    service = SLAService(session=db_session, notification_port=notification_adapter)

    created_at = datetime.now(timezone.utc)
    ticket = SupportTicket(
        priority="P2",
        subject="Feature request inquiry",
        description="Asking about upcoming API capabilities.",
        client_id="client_corp_104",
        created_at=created_at,
    )
    db_session.add(ticket)
    db_session.commit()

    trackings = service.initialize_tracking(ticket)
    assert len(trackings) == 2

    deadlines = service.calculate_deadlines("P2", created_at=created_at)
    assert deadlines["first_response"] == created_at + timedelta(hours=4)

    eval_result = service.evaluate_ticket(ticket)
    assert eval_result["ticket_id"] == str(ticket.id)
