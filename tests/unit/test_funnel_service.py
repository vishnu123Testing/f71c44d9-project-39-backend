"""Unit tests for the conversion funnel service module (FR-004, FR-005)."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.analytics import AcquisitionChannel, Campaign, FunnelThreshold, LeadEvent
from src.models.database import Base
from src.services.analytics.funnel import (
    DEFAULT_FUNNEL_STAGES,
    FunnelNotFoundError,
    FunnelStageDTO,
    InvalidFunnelDataError,
    analyze_conversion_funnel,
    calculate_stage_dropoff,
    detect_bottleneck,
    get_bottleneck_stages,
    get_funnel_summary,
    get_funnel_thresholds,
    set_funnel_threshold,
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


def test_calculate_stage_dropoff() -> None:
    """Test step conversion rate and drop-off rate calculations."""
    # Zero or negative previous count safety
    assert calculate_stage_dropoff(10, 0) == (0.0, 0.0)
    assert calculate_stage_dropoff(10, -5) == (0.0, 0.0)

    # 100 previous, 50 current -> 50% conversion, 50% drop-off
    cr, dropoff = calculate_stage_dropoff(50, 100)
    assert cr == 50.0
    assert dropoff == 50.0

    # 100 previous, 0 current -> 0% conversion, 100% drop-off
    cr, dropoff = calculate_stage_dropoff(0, 100)
    assert cr == 0.0
    assert dropoff == 100.0

    # 100 previous, 100 current -> 100% conversion, 0% drop-off
    cr, dropoff = calculate_stage_dropoff(100, 100)
    assert cr == 100.0
    assert dropoff == 0.0

    # Fractional calculations
    cr, dropoff = calculate_stage_dropoff(1, 3)
    assert cr == 33.33
    assert dropoff == 66.67


def test_detect_bottleneck() -> None:
    """Test bottleneck detection flag based on drop-off threshold."""
    assert detect_bottleneck(50.0, 50.0) is True
    assert detect_bottleneck(60.0, 50.0) is True
    assert detect_bottleneck(49.9, 50.0) is False
    assert detect_bottleneck(0.0, 50.0) is False


def test_set_and_get_funnel_thresholds(test_db_session: Session) -> None:
    """Test setting, updating, and retrieving funnel stage drop-off thresholds."""
    # Initial empty dict
    assert get_funnel_thresholds(test_db_session) == {}

    # Set threshold for MQL
    rec1 = set_funnel_threshold(test_db_session, "MQL", 40.0)
    assert rec1.stage_name == "MQL"
    assert rec1.max_dropoff_percent == 40.0

    # Update threshold for MQL
    rec2 = set_funnel_threshold(test_db_session, "MQL", 35.0)
    assert rec2.max_dropoff_percent == 35.0

    # Retrieve all thresholds
    thresholds = get_funnel_thresholds(test_db_session)
    assert thresholds == {"MQL": 35.0}


def test_set_funnel_threshold_validations(test_db_session: Session) -> None:
    """Test input validations for setting funnel thresholds."""
    with pytest.raises(InvalidFunnelDataError, match="Stage name cannot be empty"):
        set_funnel_threshold(test_db_session, "   ", 50.0)

    with pytest.raises(InvalidFunnelDataError, match="between 0.0 and 100.0"):
        set_funnel_threshold(test_db_session, "MQL", -10.0)

    with pytest.raises(InvalidFunnelDataError, match="between 0.0 and 100.0"):
        set_funnel_threshold(test_db_session, "MQL", 105.0)


def test_analyze_conversion_funnel_empty_db(test_db_session: Session) -> None:
    """Test funnel analysis on empty database returns default stages with zero counts."""
    results = analyze_conversion_funnel(test_db_session)
    assert len(results) == len(DEFAULT_FUNNEL_STAGES)
    for idx, stage_dto in enumerate(results):
        assert stage_dto.stage_name == DEFAULT_FUNNEL_STAGES[idx]
        assert stage_dto.lead_count == 0
        assert stage_dto.conversion_rate == 0.0
        assert stage_dto.dropoff_rate == 0.0
        assert stage_dto.is_bottleneck is False
        assert stage_dto.overall_conversion_rate == 0.0


def test_analyze_conversion_funnel_progression_and_bottlenecks(
    test_db_session: Session,
) -> None:
    """Test stage progression lead aggregation and drop-off rate bottleneck detection (FR-004, FR-005)."""
    now = datetime.now(timezone.utc)

    # Populate lead events:
    # 100 distinct leads at 'Lead' stage
    # 40 distinct leads at 'MQL' stage (60% drop-off -> bottleneck since > 50% default)
    # 30 distinct leads at 'SQL' stage (25% drop-off -> no bottleneck)
    # 10 distinct leads at 'Opportunity' stage (66.67% drop-off -> bottleneck)
    # 5 distinct leads at 'Customer' stage (50% drop-off -> bottleneck)

    lead_ids_top = [uuid.uuid4() for _ in range(100)]
    mql_ids = lead_ids_top[:40]
    sql_ids = mql_ids[:30]
    opp_ids = sql_ids[:10]
    cust_ids = opp_ids[:5]

    events = []
    for lid in lead_ids_top:
        events.append(LeadEvent(lead_id=lid, stage_name="Lead", event_timestamp=now))
    for lid in mql_ids:
        events.append(LeadEvent(lead_id=lid, stage_name="MQL", event_timestamp=now))
    for lid in sql_ids:
        events.append(LeadEvent(lead_id=lid, stage_name="SQL", event_timestamp=now))
    for lid in opp_ids:
        events.append(LeadEvent(lead_id=lid, stage_name="Opportunity", event_timestamp=now))
    for lid in cust_ids:
        events.append(LeadEvent(lead_id=lid, stage_name="Customer", event_timestamp=now))

    test_db_session.add_all(events)
    test_db_session.commit()

    results = analyze_conversion_funnel(test_db_session, default_threshold=50.0)

    assert len(results) == 5

    # Stage 0: Lead (100)
    s0 = results[0]
    assert s0.stage_name == "Lead"
    assert s0.lead_count == 100
    assert s0.conversion_rate == 100.0
    assert s0.dropoff_rate == 0.0
    assert s0.is_bottleneck is False
    assert s0.overall_conversion_rate == 100.0

    # Stage 1: MQL (40) - 100 -> 40 = 60% dropoff -> Bottleneck!
    s1 = results[1]
    assert s1.stage_name == "MQL"
    assert s1.lead_count == 40
    assert s1.conversion_rate == 40.0
    assert s1.dropoff_rate == 60.0
    assert s1.is_bottleneck is True
    assert s1.overall_conversion_rate == 40.0

    # Stage 2: SQL (30) - 40 -> 30 = 25% dropoff -> No Bottleneck!
    s2 = results[2]
    assert s2.stage_name == "SQL"
    assert s2.lead_count == 30
    assert s2.conversion_rate == 75.0
    assert s2.dropoff_rate == 25.0
    assert s2.is_bottleneck is False
    assert s2.overall_conversion_rate == 30.0

    # Stage 3: Opportunity (10) - 30 -> 10 = 66.67% dropoff -> Bottleneck!
    s3 = results[3]
    assert s3.stage_name == "Opportunity"
    assert s3.lead_count == 10
    assert s3.conversion_rate == 33.33
    assert s3.dropoff_rate == 66.67
    assert s3.is_bottleneck is True
    assert s3.overall_conversion_rate == 10.0

    # Stage 4: Customer (5) - 10 -> 5 = 50% dropoff -> Bottleneck!
    s4 = results[4]
    assert s4.stage_name == "Customer"
    assert s4.lead_count == 5
    assert s4.conversion_rate == 50.0
    assert s4.dropoff_rate == 50.0
    assert s4.is_bottleneck is True
    assert s4.overall_conversion_rate == 5.0


def test_analyze_conversion_funnel_custom_thresholds_and_stages(
    test_db_session: Session,
) -> None:
    """Test custom stage threshold overrides from database and custom stage sequences."""
    now = datetime.now(timezone.utc)

    # Configure custom threshold for MQL to 70% max dropoff
    set_funnel_threshold(test_db_session, "MQL", 70.0)

    # 100 Leads -> 40 MQL (60% dropoff). With 70% threshold, 60% < 70%, so NOT a bottleneck.
    lead_ids = [uuid.uuid4() for _ in range(100)]
    mql_ids = lead_ids[:40]

    events = [LeadEvent(lead_id=lid, stage_name="Lead", event_timestamp=now) for lid in lead_ids]
    events.extend([LeadEvent(lead_id=lid, stage_name="MQL", event_timestamp=now) for lid in mql_ids])

    test_db_session.add_all(events)
    test_db_session.commit()

    results = analyze_conversion_funnel(test_db_session)
    s1 = results[1]
    assert s1.stage_name == "MQL"
    assert s1.dropoff_rate == 60.0
    assert s1.threshold_percent == 70.0
    assert s1.is_bottleneck is False


def test_analyze_conversion_funnel_filtering(test_db_session: Session) -> None:
    """Test date, channel_id, and campaign_id filtering for funnel analysis."""
    now = datetime.now(timezone.utc)
    channel1 = AcquisitionChannel(channel_name="Direct", spend_amount=100.0)
    campaign1 = Campaign(campaign_name="Campaign A", budget=1000.0)
    test_db_session.add_all([channel1, campaign1])
    test_db_session.commit()

    lead_id = uuid.uuid4()
    # Event matching filters
    e_matching = LeadEvent(
        lead_id=lead_id,
        channel_id=channel1.id,
        campaign_id=campaign1.id,
        stage_name="Lead",
        event_timestamp=now,
    )
    # Event outside date filter
    e_old = LeadEvent(
        lead_id=uuid.uuid4(),
        channel_id=channel1.id,
        campaign_id=campaign1.id,
        stage_name="Lead",
        event_timestamp=now - timedelta(days=10),
    )
    test_db_session.add_all([e_matching, e_old])
    test_db_session.commit()

    # Filter by date range (last 1 day)
    results = analyze_conversion_funnel(
        test_db_session,
        start_date_val=now - timedelta(days=1),
        end_date_val=now + timedelta(days=1),
        channel_id=channel1.id,
        campaign_id=campaign1.id,
    )
    assert results[0].lead_count == 1


def test_analyze_conversion_funnel_invalid_inputs(test_db_session: Session) -> None:
    """Test exception raising for invalid funnel analysis input parameters."""
    now = datetime.now(timezone.utc)

    # start_date_val > end_date_val
    with pytest.raises(InvalidFunnelDataError, match="cannot be after"):
        analyze_conversion_funnel(
            test_db_session,
            start_date_val=now,
            end_date_val=now - timedelta(days=1),
        )

    # Invalid channel_id format
    with pytest.raises(InvalidFunnelDataError, match="Invalid UUID string format"):
        analyze_conversion_funnel(test_db_session, channel_id="not-a-uuid")

    # Empty stage list
    with pytest.raises(InvalidFunnelDataError, match="Stage list cannot be empty"):
        analyze_conversion_funnel(test_db_session, stages=[])

    # Negative default threshold
    with pytest.raises(InvalidFunnelDataError, match="between 0.0 and 100.0"):
        analyze_conversion_funnel(test_db_session, default_threshold=-5.0)


def test_get_funnel_summary(test_db_session: Session) -> None:
    """Test generating full funnel summary dictionary with bottleneck details."""
    now = datetime.now(timezone.utc)
    lead_ids = [uuid.uuid4() for _ in range(10)]
    mql_ids = lead_ids[:2]  # 80% drop-off -> bottleneck

    events = [LeadEvent(lead_id=lid, stage_name="Lead", event_timestamp=now) for lid in lead_ids]
    events.extend([LeadEvent(lead_id=lid, stage_name="MQL", event_timestamp=now) for lid in mql_ids])

    test_db_session.add_all(events)
    test_db_session.commit()

    summary = get_funnel_summary(test_db_session, default_threshold=50.0)

    assert summary["total_top_leads"] == 10
    assert summary["has_bottlenecks"] is True
    assert summary["bottleneck_count"] >= 1
    assert isinstance(summary["stages"], list)
    assert isinstance(summary["bottlenecks"], list)
