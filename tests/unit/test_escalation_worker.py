"""Unit tests for SLA escalation background worker task and breach scan execution [FR-005, NFR-004]."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.database import Base
from src.models.deal import Deal
from src.services.action_items.service import complete_action_item, create_action_item
from src.services.deals.service import create_deal
from src.workers.escalation_worker import process_sla_escalations, run_escalation_scan


@pytest.fixture
def test_db_session() -> Session:
    """Fixture providing an isolated in-memory SQLite database session for testing."""
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


@pytest.fixture
def sample_deal(test_db_session: Session) -> Deal:
    """Fixture creating a sample deal record."""
    return create_deal(
        session=test_db_session,
        title="Escalation Test Deal",
        deal_value=120000.00,
        milestone_stage="Qualification",
    )


def test_run_escalation_scan_newly_escalated_item(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test SLA scan detects overdue action item and triggers escalation alert."""
    past_due = datetime.now(timezone.utc) - timedelta(hours=5)
    item = create_action_item(
        session=test_db_session,
        deal_id=sample_deal.id,
        title="Overdue Contract Review",
        assigned_owner_id="owner_123",
        target_due_date=past_due,
        sla_threshold_hours=24,
    )

    dispatcher_mock = MagicMock()
    summary = run_escalation_scan(
        db=test_db_session,
        notification_dispatcher=dispatcher_mock,
    )

    assert summary["scanned_count"] == 1
    assert summary["newly_escalated_count"] == 1
    assert summary["already_escalated_count"] == 0
    assert len(summary["escalated_items"]) == 1
    assert summary["escalated_items"][0]["id"] == str(item.id)

    dispatcher_mock.assert_called_once()
    payload = dispatcher_mock.call_args[0][0]
    assert payload["event_type"] == "action_item_sla_escalation"
    assert payload["action_item_id"] == str(item.id)


def test_run_escalation_scan_alert_suppression_on_rescan(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test re-scanning an already-escalated action item suppresses duplicate alerts."""
    past_due = datetime.now(timezone.utc) - timedelta(hours=10)
    create_action_item(
        session=test_db_session,
        deal_id=sample_deal.id,
        title="Overdue Escalated Item",
        assigned_owner_id="owner_456",
        target_due_date=past_due,
    )

    # First run triggers escalation
    summary_1 = run_escalation_scan(db=test_db_session)
    assert summary_1["newly_escalated_count"] == 1
    assert summary_1["already_escalated_count"] == 0

    # Second run suppresses duplicate alert
    summary_2 = run_escalation_scan(db=test_db_session)
    assert summary_2["scanned_count"] == 1
    assert summary_2["newly_escalated_count"] == 0
    assert summary_2["already_escalated_count"] == 1
    assert len(summary_2["escalated_items"]) == 0


def test_run_escalation_scan_completed_item_clears_escalation(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test completing an escalated action item clears escalation state and excludes from future scans."""
    past_due = datetime.now(timezone.utc) - timedelta(hours=12)
    item = create_action_item(
        session=test_db_session,
        deal_id=sample_deal.id,
        title="Item To Complete",
        assigned_owner_id="owner_789",
        target_due_date=past_due,
    )

    # Escalate item
    summary_1 = run_escalation_scan(db=test_db_session)
    assert summary_1["newly_escalated_count"] == 1

    # Complete action item
    completed_item = complete_action_item(
        session=test_db_session, item_id=item.id
    )
    assert completed_item.is_completed is True
    assert completed_item.is_escalated is False

    # Next scan finds no uncompleted items
    summary_2 = run_escalation_scan(db=test_db_session)
    assert summary_2["scanned_count"] == 0
    assert summary_2["newly_escalated_count"] == 0
    assert summary_2["already_escalated_count"] == 0


def test_run_escalation_scan_with_string_reference_time(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test SLA scan accepts an ISO string reference timestamp."""
    future_due = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    create_action_item(
        session=test_db_session,
        deal_id=sample_deal.id,
        title="Future Item",
        assigned_owner_id="owner_future",
        target_due_date=future_due,
    )

    # Scan with evaluation time after target_due_date
    ref_str = "2026-06-02T00:00:00+00:00"
    summary = run_escalation_scan(db=test_db_session, reference_time=ref_str)

    assert summary["scanned_count"] == 1
    assert summary["newly_escalated_count"] == 1


def test_process_sla_escalations_celery_task(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test Celery task process_sla_escalations executes correctly with SessionLocal patch."""
    past_due = datetime.now(timezone.utc) - timedelta(hours=2)
    create_action_item(
        session=test_db_session,
        deal_id=sample_deal.id,
        title="Celery Task Action Item",
        assigned_owner_id="owner_celery",
        target_due_date=past_due,
    )

    with patch(
        "src.workers.escalation_worker.SessionLocal",
        return_value=test_db_session,
    ):
        result = process_sla_escalations()

    assert result["scanned_count"] == 1
    assert result["newly_escalated_count"] == 1
