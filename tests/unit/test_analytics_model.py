from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.models.analytics import (
    AcquisitionChannel,
    Campaign,
    FunnelThreshold,
    LeadEvent,
    ReportAuditLog,
)
from src.models.database import Base


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


def test_analytics_models_tablenames() -> None:
    """Test that all analytics models have the expected table names."""
    assert AcquisitionChannel.__tablename__ == "acquisition_channels"
    assert Campaign.__tablename__ == "campaigns"
    assert LeadEvent.__tablename__ == "lead_events"
    assert FunnelThreshold.__tablename__ == "funnel_thresholds"
    assert ReportAuditLog.__tablename__ == "report_audit_logs"


def test_acquisition_channel_creation_and_unique_constraint(
    test_db_session: Session,
) -> None:
    """Test creation and unique constraint of AcquisitionChannel."""
    channel = AcquisitionChannel(
        channel_name="Organic Search",
        spend_amount=1500.0,
    )
    test_db_session.add(channel)
    test_db_session.commit()
    test_db_session.refresh(channel)

    assert isinstance(channel.id, uuid.UUID)
    assert channel.channel_name == "Organic Search"
    assert channel.spend_amount == 1500.0
    assert isinstance(channel.created_on, datetime)
    assert "<AcquisitionChannel" in repr(channel)

    duplicate = AcquisitionChannel(
        channel_name="Organic Search",
        spend_amount=2000.0,
    )
    test_db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_campaign_creation(test_db_session: Session) -> None:
    """Test creation of Campaign record."""
    start_time = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    end_time = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

    campaign = Campaign(
        campaign_name="Q1 Growth",
        start_date_val=start_time,
        end_date_val=end_time,
        budget=50000.0,
    )
    test_db_session.add(campaign)
    test_db_session.commit()
    test_db_session.refresh(campaign)

    assert isinstance(campaign.id, uuid.UUID)
    assert campaign.campaign_name == "Q1 Growth"
    assert campaign.start_date_val == start_time
    assert campaign.end_date_val == end_time
    assert campaign.budget == 50000.0
    assert isinstance(campaign.created_on, datetime)
    assert "<Campaign" in repr(campaign)


def test_lead_event_creation_and_relationships(
    test_db_session: Session,
) -> None:
    """Test creation of LeadEvent and relationship wiring with channel and campaign."""
    channel = AcquisitionChannel(
        channel_name="Paid Search",
        spend_amount=3000.0,
    )
    campaign = Campaign(
        campaign_name="Search Summer Promo",
        budget=10000.0,
    )
    test_db_session.add_all([channel, campaign])
    test_db_session.commit()

    lead_id = uuid.uuid4()
    event = LeadEvent(
        lead_id=lead_id,
        channel_id=channel.id,
        campaign_id=campaign.id,
        stage_name="SQL",
        quality_score=85,
        deal_value=12000.0,
        revenue_amount=12000.0,
        postal_code="90210",
    )
    test_db_session.add(event)
    test_db_session.commit()
    test_db_session.refresh(event)

    assert isinstance(event.id, uuid.UUID)
    assert event.lead_id == lead_id
    assert event.stage_name == "SQL"
    assert event.quality_score == 85
    assert event.deal_value == 12000.0
    assert event.revenue_amount == 12000.0
    assert event.postal_code == "90210"
    assert event.channel is not None
    assert event.channel.channel_name == "Paid Search"
    assert event.campaign is not None
    assert event.campaign.campaign_name == "Search Summer Promo"
    assert "<LeadEvent" in repr(event)


def test_funnel_threshold_creation_and_unique_constraint(
    test_db_session: Session,
) -> None:
    """Test creation and stage_name unique constraint on FunnelThreshold."""
    threshold = FunnelThreshold(
        stage_name="MQL_to_SQL",
        max_dropoff_percent=45.5,
    )
    test_db_session.add(threshold)
    test_db_session.commit()
    test_db_session.refresh(threshold)

    assert isinstance(threshold.id, uuid.UUID)
    assert threshold.stage_name == "MQL_to_SQL"
    assert threshold.max_dropoff_percent == 45.5
    assert isinstance(threshold.updated_on, datetime)
    assert "<FunnelThreshold" in repr(threshold)

    duplicate = FunnelThreshold(
        stage_name="MQL_to_SQL",
        max_dropoff_percent=50.0,
    )
    test_db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_report_audit_log_creation(test_db_session: Session) -> None:
    """Test creation of ReportAuditLog record."""
    user_id = uuid.uuid4()
    audit = ReportAuditLog(
        user_id=user_id,
        action_type="UPDATE_BOTTLENECK_THRESHOLD",
        config_delta='{"stage_name": "MQL_to_SQL", "old": 50, "new": 45.5}',
    )
    test_db_session.add(audit)
    test_db_session.commit()
    test_db_session.refresh(audit)

    assert isinstance(audit.id, uuid.UUID)
    assert audit.user_id == user_id
    assert audit.action_type == "UPDATE_BOTTLENECK_THRESHOLD"
    assert audit.config_delta is not None
    assert isinstance(audit.created_on, datetime)
    assert "<ReportAuditLog" in repr(audit)
