"""Unit tests for the territory service module (FR-007, FR-008, NFR-002)."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.analytics import LeadEvent
from src.models.database import Base
from src.models.marketing import Territory
from src.services.marketing.territory import (
    InvalidTerritoryFilterError,
    TerritoryAccessDeniedError,
    TerritoryNotFoundError,
    assign_territory_by_postal_code,
    get_territory_by_id_or_code,
    get_territory_heatmap,
    match_postal_code,
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


def test_match_postal_code_patterns() -> None:
    """Test postal code pattern matching for exact, wildcard, range, and multi-pattern inputs."""
    # Empty inputs
    assert match_postal_code(None, "900*") is False
    assert match_postal_code("90210", None) is False
    assert match_postal_code("   ", "900*") is False

    # Exact match
    assert match_postal_code("90210", "90210") is True
    assert match_postal_code("90210", "90211") is False

    # Zip+4 hyphenated handling
    assert match_postal_code("90210-1234", "90210") is True

    # Wildcards (*, ?)
    assert match_postal_code("90210", "90*") is True
    assert match_postal_code("SW1A 1AA", "SW1*") is True
    assert match_postal_code("90210", "9021?") is True

    # Ranges (numeric)
    assert match_postal_code("90210", "90001-90899") is True
    assert match_postal_code("91000", "90001-90899") is False

    # Range (alphanumeric string)
    assert match_postal_code("B10", "A00-C99") is True

    # Multi-pattern string (comma & newline separated)
    patterns = "100*, 101*, 10200-10299\n200*"
    assert match_postal_code("10001", patterns) is True
    assert match_postal_code("10250", patterns) is True
    assert match_postal_code("20005", patterns) is True
    assert match_postal_code("30000", patterns) is False


def test_get_territory_by_id_or_code(test_db_session: Session) -> None:
    """Test retrieving territory by UUID or territory_code string."""
    t1 = Territory(
        territory_code="US-WEST",
        territory_name="US West Region",
        postal_code_patterns="90*",
    )
    test_db_session.add(t1)
    test_db_session.commit()

    # Query by UUID
    res1 = get_territory_by_id_or_code(test_db_session, t1.id)
    assert res1 is not None
    assert res1.territory_code == "US-WEST"

    # Query by string representation of UUID
    res2 = get_territory_by_id_or_code(test_db_session, str(t1.id))
    assert res2 is not None
    assert res2.id == t1.id

    # Query by territory_code
    res3 = get_territory_by_id_or_code(test_db_session, "US-WEST")
    assert res3 is not None
    assert res3.id == t1.id

    # Missing territory
    assert get_territory_by_id_or_code(test_db_session, "NON-EXISTENT") is None


def test_assign_territory_by_postal_code(test_db_session: Session) -> None:
    """Test matching a postal code against database territory records."""
    t1 = Territory(
        territory_code="US-WEST",
        territory_name="US West Region",
        postal_code_patterns="90*, 91*",
    )
    t2 = Territory(
        territory_code="US-EAST",
        territory_name="US East Region",
        postal_code_patterns="10*, 11*",
    )
    test_db_session.add_all([t1, t2])
    test_db_session.commit()

    assert assign_territory_by_postal_code(test_db_session, None) is None
    assert assign_territory_by_postal_code(test_db_session, "30301") is None

    matched_west = assign_territory_by_postal_code(test_db_session, "90210")
    assert matched_west is not None
    assert matched_west.territory_code == "US-WEST"

    matched_east = assign_territory_by_postal_code(test_db_session, "10001")
    assert matched_east is not None
    assert matched_east.territory_code == "US-EAST"


def test_get_territory_heatmap_basic(test_db_session: Session) -> None:
    """Test generating basic territory heat map with region density and unassigned drawer (FR-007)."""
    now = datetime.now(timezone.utc)

    t1 = Territory(
        territory_code="US-WEST",
        territory_name="US West Region",
        postal_code_patterns="90*",
    )
    t2 = Territory(
        territory_code="US-EAST",
        territory_name="US East Region",
        postal_code_patterns="10*",
    )
    test_db_session.add_all([t1, t2])
    test_db_session.commit()

    # Lead events:
    # 2 in US-WEST (90210 with deal $100, 90211 with deal $200)
    # 1 in US-EAST (10001 with deal $300)
    # 1 unmapped postal code (30301 with deal $50)
    # 1 missing postal code (None with deal $0)
    l1 = LeadEvent(
        lead_id=uuid.uuid4(),
        stage_name="NEW",
        postal_code="90210",
        deal_value=100.0,
        event_timestamp=now,
    )
    l2 = LeadEvent(
        lead_id=uuid.uuid4(),
        stage_name="QUALIFIED",
        postal_code="90211",
        deal_value=200.0,
        event_timestamp=now,
    )
    l3 = LeadEvent(
        lead_id=uuid.uuid4(),
        stage_name="NEW",
        postal_code="10001",
        deal_value=300.0,
        event_timestamp=now,
    )
    l4 = LeadEvent(
        lead_id=uuid.uuid4(),
        stage_name="NEW",
        postal_code="30301",
        deal_value=50.0,
        event_timestamp=now,
    )
    l5 = LeadEvent(
        lead_id=uuid.uuid4(),
        stage_name="NEW",
        postal_code=None,
        deal_value=0.0,
        event_timestamp=now,
    )

    test_db_session.add_all([l1, l2, l3, l4, l5])
    test_db_session.commit()

    result = get_territory_heatmap(test_db_session)

    assert result.total_leads == 5
    assert result.total_value == 650.0
    assert len(result.territories) == 2

    # Check US-WEST DTO
    west_dto = next(t for t in result.territories if t.territory_code == "US-WEST")
    assert west_dto.lead_count == 2
    assert west_dto.opportunity_count == 2
    assert west_dto.total_deal_value == 300.0
    assert west_dto.average_deal_value == 150.0
    assert west_dto.postal_code_breakdown == {"90210": 1, "90211": 1}

    # Check US-EAST DTO
    east_dto = next(t for t in result.territories if t.territory_code == "US-EAST")
    assert east_dto.lead_count == 1
    assert east_dto.opportunity_count == 1
    assert east_dto.total_deal_value == 300.0
    assert east_dto.average_deal_value == 300.0

    # Check Unassigned Drawer DTO
    drawer = result.unassigned_drawer
    assert drawer.total_unassigned_leads == 2
    assert drawer.total_unassigned_value == 50.0
    assert len(drawer.unassigned_items) == 2

    reasons = {item.reason for item in drawer.unassigned_items}
    assert "Missing postal code" in reasons
    assert "Unmapped postal code" in reasons


def test_get_territory_heatmap_filters(test_db_session: Session) -> None:
    """Test lead status, deal value range, and date range filtering on heat map (FR-008)."""
    now = datetime.now(timezone.utc)

    t1 = Territory(
        territory_code="US-WEST",
        territory_name="US West Region",
        postal_code_patterns="90*",
    )
    test_db_session.add(t1)
    test_db_session.commit()

    l_qual = LeadEvent(
        lead_id=uuid.uuid4(),
        stage_name="QUALIFIED",
        postal_code="90210",
        deal_value=500.0,
        event_timestamp=now,
    )
    l_new = LeadEvent(
        lead_id=uuid.uuid4(),
        stage_name="NEW",
        postal_code="90210",
        deal_value=50.0,
        event_timestamp=now,
    )
    l_old = LeadEvent(
        lead_id=uuid.uuid4(),
        stage_name="QUALIFIED",
        postal_code="90210",
        deal_value=500.0,
        event_timestamp=now - timedelta(days=10),
    )

    test_db_session.add_all([l_qual, l_new, l_old])
    test_db_session.commit()

    # Filter by lead_status="QUALIFIED"
    res1 = get_territory_heatmap(test_db_session, lead_status="QUALIFIED")
    assert res1.total_leads == 2

    # Filter by deal value range (min_deal_value=100.0)
    res2 = get_territory_heatmap(test_db_session, min_deal_value=100.0)
    assert res2.total_leads == 2

    # Filter by max_deal_value=200.0
    res3 = get_territory_heatmap(test_db_session, max_deal_value=200.0)
    assert res3.total_leads == 1

    # Filter by recent date range (last 2 days)
    res4 = get_territory_heatmap(
        test_db_session,
        start_date_val=now - timedelta(days=2),
        end_date_val=now + timedelta(days=1),
    )
    assert res4.total_leads == 2


def test_get_territory_heatmap_single_region(test_db_session: Session) -> None:
    """Test filtering heat map for a single region by territory_id or code."""
    t1 = Territory(
        territory_code="US-WEST",
        territory_name="US West Region",
        postal_code_patterns="90*",
    )
    t2 = Territory(
        territory_code="US-EAST",
        territory_name="US East Region",
        postal_code_patterns="10*",
    )
    test_db_session.add_all([t1, t2])
    test_db_session.commit()

    l1 = LeadEvent(
        lead_id=uuid.uuid4(),
        stage_name="NEW",
        postal_code="90210",
        deal_value=100.0,
        event_timestamp=datetime.now(timezone.utc),
    )
    test_db_session.add(l1)
    test_db_session.commit()

    # Filter by code string
    res1 = get_territory_heatmap(test_db_session, territory_id="US-WEST")
    assert len(res1.territories) == 1
    assert res1.territories[0].territory_code == "US-WEST"
    assert res1.unassigned_drawer.total_unassigned_leads == 0

    # Filter by UUID
    res2 = get_territory_heatmap(test_db_session, territory_id=t2.id)
    assert len(res2.territories) == 1
    assert res2.territories[0].territory_code == "US-EAST"


def test_get_territory_heatmap_multi_region_user_scoping(
    test_db_session: Session,
) -> None:
    """Test multi-region user access scoping enforcing authorized region visibility (NFR-002)."""
    t1 = Territory(
        territory_code="US-WEST",
        territory_name="US West Region",
        postal_code_patterns="90*",
    )
    t2 = Territory(
        territory_code="US-EAST",
        territory_name="US East Region",
        postal_code_patterns="10*",
    )
    t3 = Territory(
        territory_code="CA-NORTH",
        territory_name="Canada North",
        postal_code_patterns="V*",
    )
    test_db_session.add_all([t1, t2, t3])
    test_db_session.commit()

    # User authorized for US-WEST and CA-NORTH
    auth_list = ["US-WEST", str(t3.id)]
    res = get_territory_heatmap(
        test_db_session, user_authorized_territories=auth_list
    )

    assert len(res.territories) == 2
    codes = {t.territory_code for t in res.territories}
    assert codes == {"US-WEST", "CA-NORTH"}
    assert "US-EAST" not in codes

    # User requesting an unauthorized region raises TerritoryAccessDeniedError
    with pytest.raises(
        TerritoryAccessDeniedError, match="not authorized to access territory"
    ):
        get_territory_heatmap(
            test_db_session,
            territory_id="US-EAST",
            user_authorized_territories=["US-WEST"],
        )

    # Empty authorized territories list returns 0 territories
    empty_res = get_territory_heatmap(
        test_db_session, user_authorized_territories=[]
    )
    assert len(empty_res.territories) == 0


def test_get_territory_heatmap_invalid_inputs(test_db_session: Session) -> None:
    """Test exception handling for invalid filter input parameters."""
    now = datetime.now(timezone.utc)

    # start_date_val > end_date_val
    with pytest.raises(InvalidTerritoryFilterError, match="cannot be later than"):
        get_territory_heatmap(
            test_db_session,
            start_date_val=now,
            end_date_val=now - timedelta(days=1),
        )

    # min_deal_value > max_deal_value
    with pytest.raises(InvalidTerritoryFilterError, match="cannot be greater than"):
        get_territory_heatmap(
            test_db_session,
            min_deal_value=500.0,
            max_deal_value=100.0,
        )

    # Non-existent territory ID
    with pytest.raises(TerritoryNotFoundError, match="not found"):
        get_territory_heatmap(test_db_session, territory_id="NON-EXISTENT")
