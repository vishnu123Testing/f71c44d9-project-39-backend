"""Unit tests for Action Item Service, SLA breach scanning, completion, and escalation handling."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.database import Base
from src.models.deal import Deal
from src.services.action_items.service import (
    ActionItemNotFoundError,
    ActionItemService,
    DealNotFoundError,
    InvalidActionItemDataError,
    clear_escalation,
    complete_action_item,
    create_action_item,
    delete_action_item,
    get_action_item_by_id,
    get_action_items_for_deal,
    scan_and_escalate_sla_breaches,
)
from src.services.deals.service import create_deal


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


@pytest.fixture
def sample_deal(test_db_session: Session) -> Deal:
    """Fixture creating a sample deal in the database."""
    return create_deal(
        session=test_db_session,
        title="Sample Enterprise Deal",
        deal_value=50000.00,
        milestone_stage="Qualification",
    )


def test_create_action_item_success(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test successful creation of an action item."""
    due_date = datetime.now(timezone.utc) + timedelta(days=2)
    item = create_action_item(
        session=test_db_session,
        deal_id=sample_deal.id,
        title="Send proposal document",
        assigned_owner_id="usr_rep123",
        target_due_date=due_date,
        sla_threshold_hours=24,
    )

    assert isinstance(item.id, uuid.UUID)
    assert item.deal_id == sample_deal.id
    assert item.title == "Send proposal document"
    assert item.assigned_owner_id == "usr_rep123"
    assert item.sla_threshold_hours == 24
    assert item.is_completed is False
    assert item.completed_on is None
    assert item.is_escalated is False
    assert item.is_overdue is False


def test_create_action_item_iso_string_due_date(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test action item creation with ISO formatted string target due date."""
    due_str = "2026-12-31T15:00:00+00:00"
    item = create_action_item(
        session=test_db_session,
        deal_id=str(sample_deal.id),
        title="Follow up phone call",
        assigned_owner_id="usr_mgr456",
        target_due_date=due_str,
    )

    assert item.title == "Follow up phone call"
    assert item.target_due_date.year == 2026
    assert item.target_due_date.month == 12


def test_create_action_item_deal_not_found(test_db_session: Session) -> None:
    """Test creation failure when target deal ID does not exist."""
    random_id = uuid.uuid4()
    with pytest.raises(DealNotFoundError, match="Deal with ID"):
        create_action_item(
            session=test_db_session,
            deal_id=random_id,
            title="Nonexistent deal action",
            assigned_owner_id="usr_123",
            target_due_date=datetime.now(timezone.utc),
        )


def test_create_action_item_invalid_inputs(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test creation failures for empty title or assigned owner ID."""
    with pytest.raises(
        InvalidActionItemDataError, match="title cannot be empty"
    ):
        create_action_item(
            session=test_db_session,
            deal_id=sample_deal.id,
            title="   ",
            assigned_owner_id="usr_123",
            target_due_date=datetime.now(timezone.utc),
        )

    with pytest.raises(
        InvalidActionItemDataError, match="owner ID cannot be empty"
    ):
        create_action_item(
            session=test_db_session,
            deal_id=sample_deal.id,
            title="Valid title",
            assigned_owner_id="",
            target_due_date=datetime.now(timezone.utc),
        )

    with pytest.raises(InvalidActionItemDataError, match="Invalid deal ID format"):
        create_action_item(
            session=test_db_session,
            deal_id="not-a-uuid",
            title="Valid title",
            assigned_owner_id="usr_123",
            target_due_date=datetime.now(timezone.utc),
        )


def test_get_action_items(test_db_session: Session, sample_deal: Deal) -> None:
    """Test retrieving action items by ID and by deal ID."""
    due1 = datetime.now(timezone.utc) + timedelta(days=1)
    due2 = datetime.now(timezone.utc) + timedelta(days=3)

    item1 = create_action_item(
        session=test_db_session,
        deal_id=sample_deal.id,
        title="Action Item 1",
        assigned_owner_id="usr_1",
        target_due_date=due1,
    )
    item2 = create_action_item(
        session=test_db_session,
        deal_id=sample_deal.id,
        title="Action Item 2",
        assigned_owner_id="usr_2",
        target_due_date=due2,
    )

    fetched = get_action_item_by_id(test_db_session, item1.id)
    assert fetched.id == item1.id
    assert fetched.title == "Action Item 1"

    all_items = get_action_items_for_deal(test_db_session, sample_deal.id)
    assert len(all_items) == 2
    assert all_items[0].id == item1.id
    assert all_items[1].id == item2.id

    complete_action_item(test_db_session, item1.id)

    uncompleted_only = get_action_items_for_deal(
        test_db_session, sample_deal.id, include_completed=False
    )
    assert len(uncompleted_only) == 1
    assert uncompleted_only[0].id == item2.id


def test_complete_action_item_clears_escalation(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test completing an action item updates completion state and clears escalation."""
    dispatched_events = []

    def mock_dispatcher(payload: dict) -> None:
        dispatched_events.append(payload)

    due_past = datetime.now(timezone.utc) - timedelta(hours=5)
    item = create_action_item(
        session=test_db_session,
        deal_id=sample_deal.id,
        title="Overdue Contract Review",
        assigned_owner_id="usr_owner",
        target_due_date=due_past,
    )

    # Scan to trigger SLA escalation
    scan_res = scan_and_escalate_sla_breaches(test_db_session)
    assert scan_res.newly_escalated_count == 1

    escalated_item = get_action_item_by_id(test_db_session, item.id)
    assert escalated_item.is_escalated is True

    # Complete item
    completed = complete_action_item(
        test_db_session, item.id, notification_dispatcher=mock_dispatcher
    )
    assert completed.is_completed is True
    assert completed.completed_on is not None
    assert completed.is_escalated is False
    assert len(dispatched_events) == 1
    assert dispatched_events[0]["event_type"] == "action_item_completed"


def test_scan_and_escalate_sla_breaches(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test SLA breach scanning and duplicate suppression."""
    notifications = []

    def mock_dispatcher(payload: dict) -> None:
        notifications.append(payload)

    now_utc = datetime.now(timezone.utc)
    future_due = now_utc + timedelta(days=5)
    past_due = now_utc - timedelta(hours=2)

    # Item 1: Not overdue
    item1 = create_action_item(
        session=test_db_session,
        deal_id=sample_deal.id,
        title="Future Task",
        assigned_owner_id="usr_1",
        target_due_date=future_due,
        sla_threshold_hours=24,
    )

    # Item 2: Overdue
    item2 = create_action_item(
        session=test_db_session,
        deal_id=sample_deal.id,
        title="Past Due Task",
        assigned_owner_id="usr_2",
        target_due_date=past_due,
        sla_threshold_hours=24,
    )

    # First scan
    scan1 = scan_and_escalate_sla_breaches(
        test_db_session,
        reference_time=now_utc,
        notification_dispatcher=mock_dispatcher,
    )
    assert scan1.scanned_count == 2
    assert scan1.newly_escalated_count == 1
    assert scan1.already_escalated_count == 0
    assert len(scan1.escalated_items) == 1
    assert scan1.escalated_items[0].id == item2.id
    assert len(notifications) == 1
    assert notifications[0]["action_item_id"] == str(item2.id)

    # Second scan: Duplicate alert is suppressed
    scan2 = scan_and_escalate_sla_breaches(
        test_db_session,
        reference_time=now_utc,
        notification_dispatcher=mock_dispatcher,
    )
    assert scan2.scanned_count == 2
    assert scan2.newly_escalated_count == 0
    assert scan2.already_escalated_count == 1
    assert len(notifications) == 1  # No duplicate notification sent


def test_clear_escalation_and_delete(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test clearing escalation state manually and deleting action items."""
    past_due = datetime.now(timezone.utc) - timedelta(hours=10)
    item = create_action_item(
        session=test_db_session,
        deal_id=sample_deal.id,
        title="To be deleted",
        assigned_owner_id="usr_del",
        target_due_date=past_due,
    )

    scan_and_escalate_sla_breaches(test_db_session)
    fetched = get_action_item_by_id(test_db_session, item.id)
    assert fetched.is_escalated is True

    cleared = clear_escalation(test_db_session, item.id)
    assert cleared.is_escalated is False

    deleted = delete_action_item(test_db_session, item.id)
    assert deleted is True

    with pytest.raises(ActionItemNotFoundError):
        get_action_item_by_id(test_db_session, item.id)


def test_action_item_service_class(
    test_db_session: Session, sample_deal: Deal
) -> None:
    """Test ActionItemService class wrapper methods."""
    service = ActionItemService(test_db_session)

    due = datetime.now(timezone.utc) + timedelta(days=1)
    item = service.create(
        deal_id=sample_deal.id,
        title="Service class test",
        assigned_owner_id="usr_class",
        target_due_date=due,
    )

    assert item.title == "Service class test"

    fetched = service.get_by_id(item.id)
    assert fetched.id == item.id

    deal_items = service.get_for_deal(sample_deal.id)
    assert len(deal_items) == 1

    scan_res = service.scan_sla_breaches()
    assert scan_res.scanned_count == 1

    completed = service.complete(item.id)
    assert completed.is_completed is True

    deleted = service.delete(item.id)
    assert deleted is True
