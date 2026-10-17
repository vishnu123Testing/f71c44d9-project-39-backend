"""Unit tests for the analytics reporting service module (FR-001, FR-002, FR-003, FR-006)."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.analytics import AcquisitionChannel, Campaign, LeadEvent
from src.models.database import Base
from src.services.analytics.reporting import (
    CampaignMetricsDTO,
    CampaignNotFoundError,
    CampaignTrendPointDTO,
    ChannelAcquisitionReportDTO,
    ChannelNotFoundError,
    ComparativeCampaignTrendDTO,
    InvalidReportingDataError,
    calculate_channel_roi,
    calculate_cost_per_lead,
    calculate_engagement_rate,
    compute_lead_quality_score,
    get_campaign_trend_comparison,
    get_channel_acquisition_report,
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


def test_calculate_channel_roi_zero_and_none_safety() -> None:
    """Test zero-spend, zero-revenue, and None safety in ROI calculations (FR-003)."""
    assert calculate_channel_roi(0.0, 100.0) == "N/A"
    assert calculate_channel_roi(100.0, 0.0) == "N/A"
    assert calculate_channel_roi(0.0, 0.0) == "N/A"
    assert calculate_channel_roi(None, 100.0) == "N/A"
    assert calculate_channel_roi(100.0, None) == "N/A"
    assert calculate_channel_roi(None, None) == "N/A"
    assert calculate_channel_roi(-50.0, 100.0) == "N/A"
    assert calculate_channel_roi(100.0, -50.0) == "N/A"


def test_calculate_channel_roi_valid_percentages() -> None:
    """Test accurate ROI calculation when spend and revenue are positive (FR-003)."""
    # Revenue $200, Spend $100 -> ROI = (200 - 100)/100 * 100 = 100.00%
    assert calculate_channel_roi(200.0, 100.0) == "100.00%"

    # Revenue $150, Spend $100 -> ROI = (150 - 100)/100 * 100 = 50.00%
    assert calculate_channel_roi(150.0, 100.0) == "50.00%"

    # Revenue $50, Spend $100 -> ROI = (50 - 100)/100 * 100 = -50.00%
    assert calculate_channel_roi(50.0, 100.0) == "-50.00%"


def test_compute_lead_quality_score() -> None:
    """Test lead quality score average calculation."""
    assert compute_lead_quality_score([]) == 0.0
    assert compute_lead_quality_score([None, None]) == 0.0
    assert compute_lead_quality_score([80, 90, 70]) == 80.0
    assert compute_lead_quality_score([85, None, 95]) == 90.0
    assert compute_lead_quality_score([75.5, 82.3]) == 78.9


def test_calculate_cost_per_lead() -> None:
    """Test CPL calculation with zero, None, and positive budgets/counts."""
    assert calculate_cost_per_lead(None, 10) == 0.0
    assert calculate_cost_per_lead(0.0, 10) == 0.0
    assert calculate_cost_per_lead(1000.0, 0) == 0.0
    assert calculate_cost_per_lead(1000.0, 50) == 20.0
    assert calculate_cost_per_lead(500.0, 3) == 166.67


def test_calculate_engagement_rate() -> None:
    """Test engagement rate calculations from quality scores and stage progression."""
    assert calculate_engagement_rate([]) == 0.0

    # With quality scores
    e1 = LeadEvent(lead_id=uuid.uuid4(), stage_name="Lead", quality_score=80)
    e2 = LeadEvent(lead_id=uuid.uuid4(), stage_name="MQL", quality_score=90)
    assert calculate_engagement_rate([e1, e2]) == 85.0

    # Without quality scores -> stage progression fallback
    e3 = LeadEvent(lead_id=uuid.uuid4(), stage_name="Lead", quality_score=None)
    e4 = LeadEvent(lead_id=uuid.uuid4(), stage_name="SQL", quality_score=None)
    assert calculate_engagement_rate([e3, e4]) == 50.0


def test_get_channel_acquisition_report_empty_db(test_db_session: Session) -> None:
    """Test retrieving channel acquisition report from empty database."""
    reports = get_channel_acquisition_report(test_db_session)
    assert reports == []


def test_get_channel_acquisition_report_metrics_and_sorting(
    test_db_session: Session,
) -> None:
    """Test metrics aggregation, zero-spend ROI safety, and quality score sorting (FR-001, FR-002, FR-003)."""
    ch_organic = AcquisitionChannel(channel_name="Organic Search", spend_amount=0.0)
    ch_paid = AcquisitionChannel(channel_name="Paid Search", spend_amount=1000.0)
    ch_social = AcquisitionChannel(channel_name="Paid Social", spend_amount=2000.0)

    test_db_session.add_all([ch_organic, ch_paid, ch_social])
    test_db_session.commit()

    now = datetime.now(timezone.utc)

    # Organic search events (zero spend -> ROI = "N/A")
    e1 = LeadEvent(
        lead_id=uuid.uuid4(),
        channel_id=ch_organic.id,
        stage_name="Lead",
        quality_score=90,
        revenue_amount=500.0,
        event_timestamp=now,
    )
    e2 = LeadEvent(
        lead_id=uuid.uuid4(),
        channel_id=ch_organic.id,
        stage_name="MQL",
        quality_score=95,
        revenue_amount=1500.0,
        event_timestamp=now,
    )

    # Paid search events ($1000 spend, $3000 revenue -> ROI = 200.00%, avg QS = 70.0)
    e3 = LeadEvent(
        lead_id=uuid.uuid4(),
        channel_id=ch_paid.id,
        stage_name="SQL",
        quality_score=70,
        revenue_amount=3000.0,
        event_timestamp=now,
    )

    # Paid social events ($2000 spend, $0 revenue -> ROI = "N/A", avg QS = 80.0)
    e4 = LeadEvent(
        lead_id=uuid.uuid4(),
        channel_id=ch_social.id,
        stage_name="Lead",
        quality_score=80,
        revenue_amount=0.0,
        event_timestamp=now,
    )

    test_db_session.add_all([e1, e2, e3, e4])
    test_db_session.commit()

    # Query with quality_score sort descending (FR-002 Scenario 1.2)
    reports = get_channel_acquisition_report(
        test_db_session,
        sort_by="quality_score",
        sort_order="desc",
    )

    assert len(reports) == 3

    # First should be Organic Search (QS = 92.5)
    assert reports[0].channel_name == "Organic Search"
    assert reports[0].lead_count == 2
    assert reports[0].quality_score == 92.5
    assert reports[0].spend_amount == 0.0
    assert reports[0].revenue_amount == 2000.0
    assert reports[0].roi == "N/A"  # Zero spend safety (FR-003)

    # Second should be Paid Social (QS = 80.0)
    assert reports[1].channel_name == "Paid Social"
    assert reports[1].lead_count == 1
    assert reports[1].quality_score == 80.0
    assert reports[1].spend_amount == 2000.0
    assert reports[1].revenue_amount == 0.0
    assert reports[1].roi == "N/A"  # Zero revenue safety (FR-003)

    # Third should be Paid Search (QS = 70.0)
    assert reports[2].channel_name == "Paid Search"
    assert reports[2].lead_count == 1
    assert reports[2].quality_score == 70.0
    assert reports[2].spend_amount == 1000.0
    assert reports[2].revenue_amount == 3000.0
    assert reports[2].roi == "200.00%"


def test_get_channel_acquisition_report_date_filtering(
    test_db_session: Session,
) -> None:
    """Test filtering lead acquisition events by date range (FR-001)."""
    ch = AcquisitionChannel(channel_name="Email Marketing", spend_amount=500.0)
    test_db_session.add(ch)
    test_db_session.commit()

    base_time = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)

    # Out of range (earlier)
    e1 = LeadEvent(
        lead_id=uuid.uuid4(),
        channel_id=ch.id,
        stage_name="Lead",
        quality_score=60,
        revenue_amount=200.0,
        event_timestamp=base_time - timedelta(days=10),
    )
    # In range
    e2 = LeadEvent(
        lead_id=uuid.uuid4(),
        channel_id=ch.id,
        stage_name="MQL",
        quality_score=80,
        revenue_amount=1000.0,
        event_timestamp=base_time,
    )
    # Out of range (later)
    e3 = LeadEvent(
        lead_id=uuid.uuid4(),
        channel_id=ch.id,
        stage_name="SQL",
        quality_score=90,
        revenue_amount=500.0,
        event_timestamp=base_time + timedelta(days=10),
    )

    test_db_session.add_all([e1, e2, e3])
    test_db_session.commit()

    start_filter = base_time - timedelta(days=1)
    end_filter = base_time + timedelta(days=1)

    reports = get_channel_acquisition_report(
        test_db_session,
        start_date_val=start_filter,
        end_date_val=end_filter,
    )

    assert len(reports) == 1
    assert reports[0].lead_count == 1
    assert reports[0].quality_score == 80.0
    assert reports[0].revenue_amount == 1000.0


def test_get_channel_acquisition_report_channel_filter_and_errors(
    test_db_session: Session,
) -> None:
    """Test filtering by channel_id and invalid input handling."""
    ch = AcquisitionChannel(channel_name="Referral", spend_amount=100.0)
    test_db_session.add(ch)
    test_db_session.commit()

    # Filter by valid UUID
    reports = get_channel_acquisition_report(test_db_session, channel_id=ch.id)
    assert len(reports) == 1
    assert reports[0].channel_name == "Referral"

    # Filter by valid channel name string
    reports_name = get_channel_acquisition_report(
        test_db_session, channel_id="Referral"
    )
    assert len(reports_name) == 1
    assert reports_name[0].channel_name == "Referral"

    # Filter by non-existent channel_id raises ChannelNotFoundError
    with pytest.raises(ChannelNotFoundError):
        get_channel_acquisition_report(test_db_session, channel_id=uuid.uuid4())

    # Invalid date range raises InvalidReportingDataError
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 1, tzinfo=timezone.utc)
    with pytest.raises(InvalidReportingDataError):
        get_channel_acquisition_report(
            test_db_session, start_date_val=start, end_date_val=end
        )


def test_get_campaign_trend_comparison_valid_multi_campaign(
    test_db_session: Session,
) -> None:
    """Test comparative campaign trend calculation for 2 to 5 campaigns (FR-006)."""
    c1 = Campaign(campaign_name="Summer Promo 2026", budget=1000.0)
    c2 = Campaign(campaign_name="Fall Launch 2026", budget=2000.0)
    c3 = Campaign(campaign_name="Winter Sale 2026", budget=3000.0)

    test_db_session.add_all([c1, c2, c3])
    test_db_session.commit()

    base_time = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)

    # c1 events: 2 events on base_time, QS=80 and QS=90 (avg=85.0)
    e1 = LeadEvent(
        lead_id=uuid.uuid4(),
        campaign_id=c1.id,
        stage_name="Lead",
        quality_score=80,
        event_timestamp=base_time,
    )
    e2 = LeadEvent(
        lead_id=uuid.uuid4(),
        campaign_id=c1.id,
        stage_name="MQL",
        quality_score=90,
        event_timestamp=base_time,
    )

    # c2 events: 1 event on base_time + 1 day, QS=70
    e3 = LeadEvent(
        lead_id=uuid.uuid4(),
        campaign_id=c2.id,
        stage_name="SQL",
        quality_score=70,
        event_timestamp=base_time + timedelta(days=1),
    )

    test_db_session.add_all([e1, e2, e3])
    test_db_session.commit()

    start_filter = base_time - timedelta(hours=1)
    end_filter = base_time + timedelta(days=2)

    # Call with list of UUIDs
    res = get_campaign_trend_comparison(
        test_db_session,
        campaign_ids=[c1.id, c2.id, c3.id],
        start_date_val=start_filter,
        end_date_val=end_filter,
    )

    assert isinstance(res, ComparativeCampaignTrendDTO)
    assert len(res.campaigns) == 3

    # Check c1 metrics
    c1_metrics = next(c for c in res.campaigns if c.campaign_id == c1.id)
    assert c1_metrics.campaign_name == "Summer Promo 2026"
    assert c1_metrics.total_leads == 2
    assert c1_metrics.budget == 1000.0
    assert c1_metrics.overall_cpl == 500.0  # $1000 / 2
    assert c1_metrics.overall_engagement_rate == 85.0
    assert len(c1_metrics.trend_points) >= 3

    # Check c2 metrics
    c2_metrics = next(c for c in res.campaigns if c.campaign_id == c2.id)
    assert c2_metrics.total_leads == 1
    assert c2_metrics.overall_cpl == 2000.0  # $2000 / 1
    assert c2_metrics.overall_engagement_rate == 70.0

    # Check c3 metrics (0 leads)
    c3_metrics = next(c for c in res.campaigns if c.campaign_id == c3.id)
    assert c3_metrics.total_leads == 0
    assert c3_metrics.overall_cpl == 0.0
    assert c3_metrics.overall_engagement_rate == 0.0


def test_get_campaign_trend_comparison_csv_string_and_names(
    test_db_session: Session,
) -> None:
    """Test campaign comparison using CSV string and campaign names."""
    c1 = Campaign(campaign_name="Campaign Alpha", budget=500.0)
    c2 = Campaign(campaign_name="Campaign Beta", budget=750.0)

    test_db_session.add_all([c1, c2])
    test_db_session.commit()

    # Pass CSV string of campaign names
    res = get_campaign_trend_comparison(
        test_db_session,
        campaign_ids="Campaign Alpha, Campaign Beta",
    )

    assert len(res.campaigns) == 2
    names = {c.campaign_name for c in res.campaigns}
    assert names == {"Campaign Alpha", "Campaign Beta"}


def test_get_campaign_trend_comparison_validation_errors(
    test_db_session: Session,
) -> None:
    """Test limit, missing campaign, and date validation errors for campaign comparison (FR-006)."""
    # 1. Less than 2 campaigns raises InvalidReportingDataError
    with pytest.raises(InvalidReportingDataError, match="At least 2 campaigns required"):
        get_campaign_trend_comparison(test_db_session, campaign_ids=[uuid.uuid4()])

    # 2. More than 5 campaigns raises InvalidReportingDataError
    six_ids = [uuid.uuid4() for _ in range(6)]
    with pytest.raises(InvalidReportingDataError, match="Maximum 5 campaigns allowed"):
        get_campaign_trend_comparison(test_db_session, campaign_ids=six_ids)

    # 3. Missing campaign raises CampaignNotFoundError
    c1 = Campaign(campaign_name="C1", budget=100.0)
    test_db_session.add(c1)
    test_db_session.commit()

    with pytest.raises(CampaignNotFoundError):
        get_campaign_trend_comparison(
            test_db_session, campaign_ids=[c1.id, uuid.uuid4()]
        )

    # 4. Invalid date range raises InvalidReportingDataError
    c2 = Campaign(campaign_name="C2", budget=100.0)
    test_db_session.add(c2)
    test_db_session.commit()

    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)

    with pytest.raises(InvalidReportingDataError, match="start_date_val cannot be later"):
        get_campaign_trend_comparison(
            test_db_session,
            campaign_ids=[c1.id, c2.id],
            start_date_val=start,
            end_date_val=end,
        )

