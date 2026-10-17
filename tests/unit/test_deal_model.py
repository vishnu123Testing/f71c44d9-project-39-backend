from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.database import Base
from src.models.deal import ActionItem, Deal, DealForecast, Stakeholder, StakeholderActivity


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


def test_deal_tablenames() -> None:
    """Test table names for all deal-related ORM models."""
    assert Deal.__tablename__ == "deals"
    assert ActionItem.__tablename__ == "action_items"
    assert Stakeholder.__tablename__ == "stakeholders"
    assert StakeholderActivity.__tablename__ == "stakeholder_activities"
    assert DealForecast.__tablename__ == "deal_forecasts"


def test_deal_creation_and_defaults(test_db_session: Session) -> None:
    """Test creating a Deal record with default values."""
    deal = Deal(
        title="Enterprise Software License",
        deal_value=50000.00,
        milestone_stage="Proposal",
        win_probability=0.6,
    )
    test_db_session.add(deal)
    test_db_session.commit()
    test_db_session.refresh(deal)

    assert isinstance(deal.id, uuid.UUID)
    assert deal.title == "Enterprise Software License"
    assert deal.deal_value == 50000.00
    assert deal.milestone_stage == "Proposal"
    assert deal.win_probability == 0.6
    assert isinstance(deal.created_on, datetime)
    assert isinstance(deal.updated_on, datetime)
    assert repr(deal).startswith("<Deal(")


def test_action_item_creation_and_relationship(test_db_session: Session) -> None:
    """Test creating an ActionItem linked to a Deal."""
    deal = Deal(
        title="Acme Cloud Migration",
        deal_value=120000.00,
        milestone_stage="Qualification",
        win_probability=0.3,
    )
    test_db_session.add(deal)
    test_db_session.commit()

    due_time = datetime(2026, 10, 15, 17, 0, 0, tzinfo=timezone.utc)
    action_item = ActionItem(
        deal_id=deal.id,
        title="Send technical architecture deck",
        assigned_owner_id="usr_owner_101",
        target_due_date=due_time,
        sla_threshold_hours=48,
    )
    test_db_session.add(action_item)
    test_db_session.commit()
    test_db_session.refresh(action_item)

    assert isinstance(action_item.id, uuid.UUID)
    assert action_item.deal_id == deal.id
    assert action_item.title == "Send technical architecture deck"
    assert action_item.assigned_owner_id == "usr_owner_101"
    assert action_item.target_due_date == due_time
    assert action_item.sla_threshold_hours == 48
    assert action_item.is_completed is False
    assert action_item.completed_on is None
    assert action_item.is_escalated is False
    assert action_item.escalated_on is None
    assert isinstance(action_item.created_on, datetime)
    assert action_item.deal.title == "Acme Cloud Migration"
    assert len(deal.action_items) == 1
    assert repr(action_item).startswith("<ActionItem(")


def test_stakeholder_and_activity_creation(test_db_session: Session) -> None:
    """Test creating a Stakeholder and associated StakeholderActivity records."""
    deal = Deal(
        title="Global Logistics Expansion",
        deal_value=250000.00,
        milestone_stage="Negotiation",
        win_probability=0.8,
    )
    test_db_session.add(deal)
    test_db_session.commit()

    stakeholder = Stakeholder(
        deal_id=deal.id,
        full_name="Jane Doe",
        email="jane.doe@example.com",
        org_role="VP Engineering",
        influence_level="High",
    )
    test_db_session.add(stakeholder)
    test_db_session.commit()
    test_db_session.refresh(stakeholder)

    assert isinstance(stakeholder.id, uuid.UUID)
    assert stakeholder.deal_id == deal.id
    assert stakeholder.full_name == "Jane Doe"
    assert stakeholder.email == "jane.doe@example.com"
    assert stakeholder.org_role == "VP Engineering"
    assert stakeholder.influence_level == "High"
    assert stakeholder.is_active is True
    assert repr(stakeholder).startswith("<Stakeholder(")

    occurred_time = datetime(2026, 10, 5, 14, 30, 0, tzinfo=timezone.utc)
    activity = StakeholderActivity(
        stakeholder_id=stakeholder.id,
        deal_id=deal.id,
        activity_type="Meeting",
        summary="Reviewed security and compliance requirements.",
        occurred_on=occurred_time,
    )
    test_db_session.add(activity)
    test_db_session.commit()
    test_db_session.refresh(activity)

    assert isinstance(activity.id, uuid.UUID)
    assert activity.stakeholder_id == stakeholder.id
    assert activity.deal_id == deal.id
    assert activity.activity_type == "Meeting"
    assert activity.summary == "Reviewed security and compliance requirements."
    assert activity.occurred_on == occurred_time
    assert activity.stakeholder.full_name == "Jane Doe"
    assert activity.deal.title == "Global Logistics Expansion"
    assert len(stakeholder.activities) == 1
    assert repr(activity).startswith("<StakeholderActivity(")


def test_deal_forecast_creation_and_one_to_one(test_db_session: Session) -> None:
    """Test creating a DealForecast record with one-to-one relationship to Deal."""
    deal = Deal(
        title="Fintech Security Audit",
        deal_value=75000.00,
        milestone_stage="Discovery",
        win_probability=0.4,
    )
    test_db_session.add(deal)
    test_db_session.commit()

    est_close = datetime(2026, 11, 30, 0, 0, 0, tzinfo=timezone.utc)
    forecast = DealForecast(
        deal_id=deal.id,
        estimated_completion_date=est_close,
        confidence_score_pct=85.5,
        status_code="READY",
    )
    test_db_session.add(forecast)
    test_db_session.commit()
    test_db_session.refresh(forecast)

    assert isinstance(forecast.id, uuid.UUID)
    assert forecast.deal_id == deal.id
    assert forecast.estimated_completion_date == est_close
    assert forecast.confidence_score_pct == 85.5
    assert forecast.status_code == "READY"
    assert isinstance(forecast.calculated_on, datetime)
    assert forecast.deal.title == "Fintech Security Audit"
    assert deal.forecast is not None
    assert deal.forecast.id == forecast.id
    assert repr(forecast).startswith("<DealForecast(")


def test_deal_cascade_delete(test_db_session: Session) -> None:
    """Test that deleting a Deal cascades deletion to child action items, stakeholders, activities, and forecasts."""
    deal = Deal(
        title="Temporary Deal",
        deal_value=10000.00,
        milestone_stage="Lead",
        win_probability=0.1,
    )
    test_db_session.add(deal)
    test_db_session.commit()

    action = ActionItem(
        deal_id=deal.id,
        title="Intro call",
        assigned_owner_id="owner1",
        target_due_date=datetime.now(timezone.utc),
    )
    stakeholder = Stakeholder(
        deal_id=deal.id,
        full_name="Bob Smith",
        email="bob@example.com",
        org_role="Manager",
        influence_level="Low",
    )
    test_db_session.add_all([action, stakeholder])
    test_db_session.commit()

    activity = StakeholderActivity(
        stakeholder_id=stakeholder.id,
        deal_id=deal.id,
        activity_type="Call",
        summary="Introductory phone call",
        occurred_on=datetime.now(timezone.utc),
    )
    forecast = DealForecast(
        deal_id=deal.id,
        status_code="INSUFFICIENT_DATA",
    )
    test_db_session.add_all([activity, forecast])
    test_db_session.commit()

    # Verify children exist
    assert test_db_session.query(ActionItem).count() == 1
    assert test_db_session.query(Stakeholder).count() == 1
    assert test_db_session.query(StakeholderActivity).count() == 1
    assert test_db_session.query(DealForecast).count() == 1

    # Delete deal
    test_db_session.delete(deal)
    test_db_session.commit()

    # Verify cascade deletion
    assert test_db_session.query(ActionItem).count() == 0
    assert test_db_session.query(Stakeholder).count() == 0
    assert test_db_session.query(StakeholderActivity).count() == 0
    assert test_db_session.query(DealForecast).count() == 0
