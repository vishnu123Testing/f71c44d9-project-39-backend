"""Integration tests for GET /api/v1/analytics/acquisition endpoint (FR-001, FR-002, FR-003, NFR-003, NFR-004)."""

from datetime import datetime, timedelta, timezone
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.api.deps import create_access_token
from src.api.main import app
from src.lib.config import settings
from src.models.analytics import (
    AcquisitionChannel,
    Campaign,
    FunnelThreshold,
    LeadEvent,
    ReportAuditLog,
)
from src.models.marketing import Territory
from src.models.database import Base, get_db
from src.models.role import Role, UserRole
from src.models.tenant import Tenant
from src.models.user import User
from src.services.access_control.rbac import RoleName
from src.services.security import get_password_hash


@pytest.fixture
def db_session() -> Session:
    """In-memory SQLite database session for analytics endpoint integration testing."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session: Session) -> TestClient:
    """TestClient instance with overridden database session dependency."""

    def _override_get_db() -> Session:
        return db_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def test_user(db_session: Session) -> tuple[Tenant, User]:
    """Seed database with tenant, user, and ClientViewer role."""
    tenant = Tenant(name="Analytics Test Tenant")
    db_session.add(tenant)
    db_session.commit()

    hashed_pwd = get_password_hash("AnalyticsSecret123!")
    user = User(
        email="analyst@test.com",
        password_hash=hashed_pwd,
        tenant_id=tenant.tenant_id,
    )
    db_session.add(user)
    db_session.commit()

    role = Role(
        name=RoleName.CLIENT_VIEWER.value,
        description="Client Viewer Role",
    )
    db_session.add(role)
    db_session.commit()

    user_role = UserRole(
        user_id=user.user_id,
        role_id=role.role_id,
        tenant_id=tenant.tenant_id,
    )
    db_session.add(user_role)
    db_session.commit()

    return tenant, user


@pytest.fixture
def auth_headers(test_user: tuple[Tenant, User]) -> dict[str, str]:
    """Generate authorization header with Bearer token for test user."""
    tenant, user = test_user
    token = create_access_token(
        subject=user.user_id,
        tenant_id=tenant.tenant_id,
        email=user.email,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def seed_acquisition_data(db_session: Session) -> tuple[AcquisitionChannel, AcquisitionChannel, AcquisitionChannel]:
    """Seed sample acquisition channels and lead events."""
    ch_paid = AcquisitionChannel(
        channel_name="Paid Search",
        spend_amount=1000.0,
    )
    ch_organic = AcquisitionChannel(
        channel_name="Organic Search",
        spend_amount=0.0,  # Zero spend
    )
    ch_social = AcquisitionChannel(
        channel_name="Social Ads",
        spend_amount=500.0,
    )
    db_session.add_all([ch_paid, ch_organic, ch_social])
    db_session.commit()

    now = datetime.now(timezone.utc)

    # Lead events for Paid Search (High quality, $2000 revenue)
    e1 = LeadEvent(
        lead_id=uuid.uuid4(),
        channel_id=ch_paid.id,
        stage_name="Closed Won",
        quality_score=90,
        revenue_amount=1200.0,
        event_timestamp=now - timedelta(days=5),
    )
    e2 = LeadEvent(
        lead_id=uuid.uuid4(),
        channel_id=ch_paid.id,
        stage_name="SQL",
        quality_score=80,
        revenue_amount=800.0,
        event_timestamp=now - timedelta(days=2),
    )

    # Lead events for Organic Search (Medium quality, $1500 revenue, $0 spend -> ROI N/A)
    e3 = LeadEvent(
        lead_id=uuid.uuid4(),
        channel_id=ch_organic.id,
        stage_name="Closed Won",
        quality_score=70,
        revenue_amount=1500.0,
        event_timestamp=now - timedelta(days=10),
    )

    # Lead events for Social Ads (Low quality, $250 revenue)
    e4 = LeadEvent(
        lead_id=uuid.uuid4(),
        channel_id=ch_social.id,
        stage_name="Lead",
        quality_score=50,
        revenue_amount=250.0,
        event_timestamp=now - timedelta(days=1),
    )

    db_session.add_all([e1, e2, e3, e4])
    db_session.commit()

    return ch_paid, ch_organic, ch_social


def test_get_acquisition_report_unauthorized(client: TestClient) -> None:
    """Test that requests without Bearer authorization token return 401 Unauthorized."""
    response = client.get("/api/v1/analytics/acquisition")
    assert response.status_code == 401


def test_get_acquisition_report_success(
    client: TestClient,
    auth_headers: dict[str, str],
    seed_acquisition_data: tuple[AcquisitionChannel, AcquisitionChannel, AcquisitionChannel],
) -> None:
    """Test retrieving acquisition channel report returns all channels (FR-1)."""
    response = client.get("/api/v1/analytics/acquisition", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 3

    # Verify response schema fields
    item = data[0]
    assert "channel_id" in item
    assert "channel_name" in item
    assert "lead_count" in item
    assert "quality_score" in item
    assert "spend_amount" in item
    assert "revenue_amount" in item
    assert "roi" in item


def test_get_acquisition_report_sorting_desc(
    client: TestClient,
    auth_headers: dict[str, str],
    seed_acquisition_data: tuple[AcquisitionChannel, AcquisitionChannel, AcquisitionChannel],
) -> None:
    """Test sorting channels by lead quality score descending (FR-2)."""
    response = client.get(
        "/api/v1/analytics/acquisition?sort_by=quality_score&sort_order=desc",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3

    scores = [item["quality_score"] for item in data]
    assert scores == sorted(scores, reverse=True)
    assert data[0]["channel_name"] == "Paid Search"  # Quality score 85.0
    assert data[1]["channel_name"] == "Organic Search"  # Quality score 70.0
    assert data[2]["channel_name"] == "Social Ads"  # Quality score 50.0


def test_get_acquisition_report_zero_spend_roi_safety(
    client: TestClient,
    auth_headers: dict[str, str],
    seed_acquisition_data: tuple[AcquisitionChannel, AcquisitionChannel, AcquisitionChannel],
) -> None:
    """Test zero-spend channels output 'N/A' string for ROI without division-by-zero errors (FR-3)."""
    response = client.get("/api/v1/analytics/acquisition", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()

    organic_item = next(item for item in data if item["channel_name"] == "Organic Search")
    assert organic_item["spend_amount"] == 0.0
    assert organic_item["roi"] == "N/A"

    paid_item = next(item for item in data if item["channel_name"] == "Paid Search")
    # Revenue $2000, Spend $1000 -> ROI = ((2000 - 1000) / 1000) * 100 = 100.00%
    assert paid_item["roi"] == "100.00%"


def test_get_acquisition_report_channel_filter(
    client: TestClient,
    auth_headers: dict[str, str],
    seed_acquisition_data: tuple[AcquisitionChannel, AcquisitionChannel, AcquisitionChannel],
) -> None:
    """Test filtering report by a specific channel UUID."""
    ch_paid, _, _ = seed_acquisition_data
    response = client.get(
        f"/api/v1/analytics/acquisition?channel_id={ch_paid.id}",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["channel_name"] == "Paid Search"
    assert data[0]["lead_count"] == 2


def test_get_acquisition_report_channel_not_found(
    client: TestClient,
    auth_headers: dict[str, str],
    seed_acquisition_data: tuple[AcquisitionChannel, AcquisitionChannel, AcquisitionChannel],
) -> None:
    """Test requesting non-existent channel ID returns 404 Not Found."""
    fake_uuid = str(uuid.uuid4())
    response = client.get(
        f"/api/v1/analytics/acquisition?channel_id={fake_uuid}",
        headers=auth_headers,
    )
    assert response.status_code == 404


def test_get_acquisition_report_invalid_date_range(
    client: TestClient,
    auth_headers: dict[str, str],
    seed_acquisition_data: tuple[AcquisitionChannel, AcquisitionChannel, AcquisitionChannel],
) -> None:
    """Test start_date_val later than end_date_val returns 400 Bad Request."""
    start = datetime.now(timezone.utc).isoformat()
    end = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    response = client.get(
        f"/api/v1/analytics/acquisition?start_date_val={start}&end_date_val={end}",
        headers=auth_headers,
    )
    assert response.status_code == 400


def test_get_acquisition_report_input_sanitization(
    client: TestClient,
    auth_headers: dict[str, str],
    seed_acquisition_data: tuple[AcquisitionChannel, AcquisitionChannel, AcquisitionChannel],
) -> None:
    """Test entry point sanitizes raw script/HTML parameters (NFR-4)."""
    response = client.get(
        "/api/v1/analytics/acquisition?sort_by=<script>alert(1)</script>quality_score",
        headers=auth_headers,
    )
    assert response.status_code == 200


def test_get_funnel_unauthenticated(client: TestClient) -> None:
    """Test GET /api/v1/analytics/funnel without auth returns 401 Unauthorized."""
    response = client.get("/api/v1/analytics/funnel")
    assert response.status_code == 401


def test_get_funnel_default_stages(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test GET /api/v1/analytics/funnel calculates conversion rates and stage counts (FR-4)."""
    lead_uuid_1 = uuid.uuid4()
    lead_uuid_2 = uuid.uuid4()
    lead_uuid_3 = uuid.uuid4()
    now = datetime.now(timezone.utc)

    # Add 3 distinct leads at Lead stage, 2 at MQL, 1 at SQL
    events = [
        LeadEvent(lead_id=lead_uuid_1, stage_name="Lead", event_timestamp=now),
        LeadEvent(lead_id=lead_uuid_2, stage_name="Lead", event_timestamp=now),
        LeadEvent(lead_id=lead_uuid_3, stage_name="Lead", event_timestamp=now),
        LeadEvent(lead_id=lead_uuid_1, stage_name="MQL", event_timestamp=now),
        LeadEvent(lead_id=lead_uuid_2, stage_name="MQL", event_timestamp=now),
        LeadEvent(lead_id=lead_uuid_1, stage_name="SQL", event_timestamp=now),
    ]
    db_session.add_all(events)
    db_session.commit()

    response = client.get("/api/v1/analytics/funnel", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 5  # DEFAULT_FUNNEL_STAGES has 5 stages

    lead_stage = next(item for item in data if item["stage_name"] == "Lead")
    mql_stage = next(item for item in data if item["stage_name"] == "MQL")
    sql_stage = next(item for item in data if item["stage_name"] == "SQL")

    assert lead_stage["count"] == 3
    assert mql_stage["count"] == 2
    assert sql_stage["count"] == 1

    # Lead to MQL: 2/3 = 66.67% conversion, 33.33% dropoff
    assert mql_stage["conversion_rate"] == 66.67
    assert mql_stage["dropoff_rate"] == 33.33


def test_get_funnel_bottleneck_detection(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test drop-off rate exceeding configured threshold flags stage as bottleneck (FR-5)."""
    # Configure threshold for MQL at 25% max dropoff
    threshold = FunnelThreshold(stage_name="MQL", max_dropoff_percent=25.0)
    db_session.add(threshold)

    lead_1 = uuid.uuid4()
    lead_2 = uuid.uuid4()
    now = datetime.now(timezone.utc)

    # 2 leads at Lead, only 1 progresses to MQL (50% dropoff > 25% threshold)
    events = [
        LeadEvent(lead_id=lead_1, stage_name="Lead", event_timestamp=now),
        LeadEvent(lead_id=lead_2, stage_name="Lead", event_timestamp=now),
        LeadEvent(lead_id=lead_1, stage_name="MQL", event_timestamp=now),
    ]
    db_session.add_all(events)
    db_session.commit()

    response = client.get("/api/v1/analytics/funnel", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()

    mql_stage = next(item for item in data if item["stage_name"] == "MQL")
    assert mql_stage["dropoff_rate"] == 50.0
    assert mql_stage["threshold_percent"] == 25.0
    assert mql_stage["is_bottleneck"] is True


def test_get_funnel_custom_threshold_query_param(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test overriding default drop-off threshold via query parameter."""
    response = client.get(
        "/api/v1/analytics/funnel?default_threshold=20.0",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    for item in data:
        # Default fallback threshold should match query param
        assert item["threshold_percent"] == 20.0


def test_get_funnel_invalid_parameters(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Test passing invalid parameters returns 400 Bad Request."""
    start = datetime.now(timezone.utc).isoformat()
    end = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    response = client.get(
        f"/api/v1/analytics/funnel?start_date_val={start}&end_date_val={end}",
        headers=auth_headers,
    )
    assert response.status_code == 400


def test_get_funnel_input_sanitization(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Test input sanitization on stage names and parameters (NFR-4)."""
    response = client.get(
        "/api/v1/analytics/funnel?stages=<script>alert(1)</script>MQL,SQL",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    stage_names = [item["stage_name"] for item in data]
    assert "<script>" not in "".join(stage_names)
    assert "MQL" in stage_names
    assert "SQL" in stage_names


def test_compare_campaign_trends_unauthenticated(client: TestClient) -> None:
    """Test GET /api/v1/analytics/campaigns/compare without auth returns 401 Unauthorized."""
    response = client.get("/api/v1/analytics/campaigns/compare?campaign_ids=c1,c2")
    assert response.status_code == 401


def test_compare_campaign_trends_success(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test comparing 2 to 5 campaigns returns overlay trend time-series metrics (FR-006)."""
    now = datetime.now(timezone.utc)
    c1 = Campaign(
        campaign_name="Summer Promo",
        budget=1000.0,
        start_date_val=now - timedelta(days=7),
        end_date_val=now,
    )
    c2 = Campaign(
        campaign_name="Fall Campaign",
        budget=2000.0,
        start_date_val=now - timedelta(days=7),
        end_date_val=now,
    )
    db_session.add_all([c1, c2])
    db_session.commit()

    lead1 = LeadEvent(
        lead_id=uuid.uuid4(),
        campaign_id=c1.id,
        stage_name="MQL",
        quality_score=85.0,
        event_timestamp=now - timedelta(days=1),
    )
    lead2 = LeadEvent(
        lead_id=uuid.uuid4(),
        campaign_id=c2.id,
        stage_name="SQL",
        quality_score=90.0,
        event_timestamp=now - timedelta(days=1),
    )
    db_session.add_all([lead1, lead2])
    db_session.commit()

    response = client.get(
        f"/api/v1/analytics/campaigns/compare?campaign_ids={c1.id},{c2.id}",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert "campaigns" in data
    assert len(data["campaigns"]) == 2

    camp1_res = next(item for item in data["campaigns"] if item["campaign_id"] == str(c1.id))
    assert camp1_res["campaign_name"] == "Summer Promo"
    assert camp1_res["budget"] == 1000.0
    assert camp1_res["total_leads"] == 1
    assert camp1_res["overall_cpl"] == 1000.0
    assert camp1_res["overall_engagement_rate"] == 85.0
    assert len(camp1_res["trend_points"]) > 0


def test_compare_campaign_trends_fewer_than_2_campaigns(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test requesting fewer than 2 campaigns returns 400 Bad Request."""
    c1 = Campaign(
        campaign_name="Single Campaign",
        budget=500.0,
    )
    db_session.add(c1)
    db_session.commit()

    response = client.get(
        f"/api/v1/analytics/campaigns/compare?campaign_ids={c1.id}",
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert "At least 2 campaigns required" in response.json()["detail"]


def test_compare_campaign_trends_more_than_5_campaigns(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test requesting more than 5 campaigns returns 400 Bad Request."""
    campaigns = [
        Campaign(campaign_name=f"Campaign {i}", budget=100.0)
        for i in range(1, 7)
    ]
    db_session.add_all(campaigns)
    db_session.commit()

    c_ids = ",".join(str(c.id) for c in campaigns)
    response = client.get(
        f"/api/v1/analytics/campaigns/compare?campaign_ids={c_ids}",
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert "Maximum 5 campaigns allowed" in response.json()["detail"]


def test_compare_campaign_trends_non_existent_campaign(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test requesting a non-existent campaign ID returns 404 Not Found."""
    c1 = Campaign(campaign_name="Valid Campaign", budget=500.0)
    db_session.add(c1)
    db_session.commit()

    fake_id = str(uuid.uuid4())
    response = client.get(
        f"/api/v1/analytics/campaigns/compare?campaign_ids={c1.id},{fake_id}",
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert f"Campaign '{fake_id}' not found" in response.json()["detail"]


def test_compare_campaign_trends_invalid_date_range(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test start_date_val later than end_date_val returns 400 Bad Request."""
    c1 = Campaign(campaign_name="Camp A", budget=100.0)
    c2 = Campaign(campaign_name="Camp B", budget=100.0)
    db_session.add_all([c1, c2])
    db_session.commit()

    start = datetime.now(timezone.utc).isoformat()
    end = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()

    response = client.get(
        f"/api/v1/analytics/campaigns/compare?campaign_ids={c1.id},{c2.id}&start_date_val={start}&end_date_val={end}",
        headers=auth_headers,
    )
    assert response.status_code == 400


def test_compare_campaign_trends_input_sanitization(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test entry point sanitizes raw script/HTML markup in query parameters (NFR-4)."""
    c1 = Campaign(campaign_name="Camp A", budget=100.0)
    c2 = Campaign(campaign_name="Camp B", budget=100.0)
    db_session.add_all([c1, c2])
    db_session.commit()

    # Pass campaign_ids with script injection attempt
    response = client.get(
        f"/api/v1/analytics/campaigns/compare?campaign_ids=<script>alert(1)</script>{c1.id},{c2.id}",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["campaigns"]) == 2


def test_get_territory_heatmap_success(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test GET /api/v1/analytics/territory/heatmap returning territory density and unassigned drawer (FR-007, FR-008)."""
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
    db_session.add_all([t1, t2])
    db_session.commit()

    # Lead in US-WEST
    e1 = LeadEvent(
        lead_id=uuid.uuid4(),
        stage_name="Qualified",
        deal_value=5000.0,
        postal_code="90210",
        territory_id=t1.id,
        event_timestamp=datetime.now(timezone.utc),
    )
    # Lead in US-EAST by postal code pattern
    e2 = LeadEvent(
        lead_id=uuid.uuid4(),
        stage_name="Opportunity",
        deal_value=3000.0,
        postal_code="10001",
        event_timestamp=datetime.now(timezone.utc),
    )
    # Lead unassigned (unmapped postal code)
    e3 = LeadEvent(
        lead_id=uuid.uuid4(),
        stage_name="Lead",
        deal_value=1000.0,
        postal_code="99999",
        event_timestamp=datetime.now(timezone.utc),
    )
    db_session.add_all([e1, e2, e3])
    db_session.commit()

    response = client.get(
        "/api/v1/analytics/territory/heatmap",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()

    assert "territories" in data
    assert "unassigned_drawer" in data
    assert data["total_leads"] == 3
    assert data["total_value"] == 9000.0

    territories_map = {t["territory_code"]: t for t in data["territories"]}
    assert "US-WEST" in territories_map
    assert territories_map["US-WEST"]["lead_count"] == 1
    assert territories_map["US-WEST"]["total_deal_value"] == 5000.0

    assert "US-EAST" in territories_map
    assert territories_map["US-EAST"]["lead_count"] == 1

    drawer = data["unassigned_drawer"]
    assert drawer["total_unassigned_leads"] == 1
    assert drawer["total_unassigned_value"] == 1000.0
    assert len(drawer["unassigned_items"]) == 1
    assert drawer["unassigned_items"][0]["postal_code"] == "99999"


def test_get_territory_heatmap_territory_filter_and_auth_scoping(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test territory authorization scoping and explicit territory filtering (NFR-002)."""
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
    db_session.add_all([t1, t2])
    db_session.commit()

    # User authorized only for US-WEST
    response = client.get(
        f"/api/v1/analytics/territory/heatmap?user_authorized_territories={t1.id}",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["territories"]) == 1
    assert data["territories"][0]["territory_code"] == "US-WEST"

    # User authorized for US-WEST, but attempts to query US-EAST specifically -> 403 Forbidden
    response_forbidden = client.get(
        f"/api/v1/analytics/territory/heatmap?territory_id={t2.id}&user_authorized_territories={t1.id}",
        headers=auth_headers,
    )
    assert response_forbidden.status_code == 403
    assert "not authorized" in response_forbidden.json()["detail"]


def test_get_territory_heatmap_not_found(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test non-existent territory_id query returns 404 Not Found."""
    fake_id = str(uuid.uuid4())
    response = client.get(
        f"/api/v1/analytics/territory/heatmap?territory_id={fake_id}",
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert f"Territory '{fake_id}' not found" in response.json()["detail"]


def test_get_territory_heatmap_invalid_filters(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test min_deal_value > max_deal_value returns 400 Bad Request."""
    response = client.get(
        "/api/v1/analytics/territory/heatmap?min_deal_value=5000&max_deal_value=1000",
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert "min_deal_value cannot be greater than max_deal_value" in response.json()["detail"]


def test_get_territory_heatmap_input_sanitization(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test query inputs are sanitized for script tags (NFR-004)."""
    t1 = Territory(
        territory_code="US-WEST",
        territory_name="US West Region",
        postal_code_patterns="90*",
    )
    db_session.add(t1)
    db_session.commit()

    response = client.get(
        f"/api/v1/analytics/territory/heatmap?lead_status=<script>alert('xss')</script>Qualified&territory_id={t1.id}",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["territories"]) == 1


def test_create_report_audit_log_unauthorized(client: TestClient) -> None:
    """Test POST /api/v1/analytics/config/audit without Bearer token returns 401 Unauthorized."""
    response = client.post(
        "/api/v1/analytics/config/audit",
        json={"action_type": "UPDATE_METRIC_RULE", "config_delta": "Changed dropoff threshold"},
    )
    assert response.status_code == 401


def test_create_report_audit_log_success(
    client: TestClient,
    auth_headers: dict[str, str],
    test_user: tuple[Tenant, User],
    db_session: Session,
) -> None:
    """Test POST /api/v1/analytics/config/audit creates report audit log record (FR-011, NFR-003)."""
    _, user = test_user
    payload = {
        "action_type": "<script>alert('xss')</script>CONFIG_UPDATE",
        "config_delta": "{\"threshold_percent\": 25.0}",
    }
    response = client.post(
        "/api/v1/analytics/config/audit",
        json=payload,
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert "id" in data
    assert data["action_type"] == "CONFIG_UPDATE"  # HTML tags sanitized
    assert data["config_delta"] == "{\"threshold_percent\": 25.0}"
    assert data["user_id"] == str(user.user_id)
    assert "created_on" in data

    # Verify database record
    audit_rec = db_session.query(ReportAuditLog).filter_by(id=uuid.UUID(data["id"])).first()
    assert audit_rec is not None
    assert audit_rec.action_type == "CONFIG_UPDATE"


def test_create_report_audit_log_empty_action_type(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Test POST /api/v1/analytics/config/audit with empty action_type returns 422 or 400."""
    response = client.post(
        "/api/v1/analytics/config/audit",
        json={"action_type": "  ", "config_delta": "some delta"},
        headers=auth_headers,
    )
    assert response.status_code in (400, 422)


def test_delete_report_configuration_unauthorized(client: TestClient) -> None:
    """Test DELETE /api/v1/analytics/config/{id} without Bearer token returns 401 Unauthorized."""
    response = client.delete(f"/api/v1/analytics/config/{uuid.uuid4()}")
    assert response.status_code == 401


def test_delete_report_configuration_success_funnel_threshold(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test DELETE /api/v1/analytics/config/{id} deletes FunnelThreshold configuration (FR-011)."""
    threshold = FunnelThreshold(stage_name="SQL", max_dropoff_percent=20.0)
    db_session.add(threshold)
    db_session.commit()

    threshold_id = str(threshold.id)

    response = client.delete(
        f"/api/v1/analytics/config/{threshold_id}",
        headers=auth_headers,
    )
    assert response.status_code == 204
    assert response.text == "" or response.content == b""

    # Verify deleted in database
    db_rec = db_session.query(FunnelThreshold).filter_by(id=threshold.id).first()
    assert db_rec is None


def test_delete_report_configuration_success_audit_log(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test DELETE /api/v1/analytics/config/{id} deletes ReportAuditLog record."""
    audit_rec = ReportAuditLog(action_type="TEMP_TEST", config_delta="delta")
    db_session.add(audit_rec)
    db_session.commit()

    audit_id = str(audit_rec.id)

    response = client.delete(
        f"/api/v1/analytics/config/{audit_id}",
        headers=auth_headers,
    )
    assert response.status_code == 204
    assert response.text == "" or response.content == b""

    # Verify deleted in database
    db_rec = db_session.query(ReportAuditLog).filter_by(id=audit_rec.id).first()
    assert db_rec is None


def test_delete_report_configuration_not_found(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Test DELETE /api/v1/analytics/config/{id} with non-existent ID returns 404 Not Found."""
    fake_id = str(uuid.uuid4())
    response = client.delete(
        f"/api/v1/analytics/config/{fake_id}",
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert f"Reporting configuration '{fake_id}' not found" in response.json()["detail"]


def test_create_dataset_export_unauthorized(client: TestClient) -> None:
    """Test POST /api/v1/analytics/exports without Bearer token returns 401 Unauthorized."""
    response = client.post(
        "/api/v1/analytics/exports",
        json={"report_type": "acquisition", "export_format": "csv"},
    )
    assert response.status_code == 401


def test_create_dataset_export_csv_success(
    client: TestClient,
    auth_headers: dict[str, str],
    seed_acquisition_data: tuple[AcquisitionChannel, AcquisitionChannel, AcquisitionChannel],
) -> None:
    """Test POST /api/v1/analytics/exports submits async export task for CSV format (FR-012, NFR-004)."""
    payload = {
        "report_type": "acquisition",
        "export_format": "csv",
        "channel_id": str(seed_acquisition_data[0].id),
    }
    response = client.post(
        "/api/v1/analytics/exports",
        json=payload,
        headers=auth_headers,
    )
    assert response.status_code == 202
    data = response.json()
    assert "task_id" in data
    assert data["report_type"] == "acquisition"
    assert data["export_format"] == "csv"
    assert data["status"] in ("PENDING", "ACCEPTED", "COMPLETED")
    assert "created_at" in data


def test_create_dataset_export_pdf_success(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Test POST /api/v1/analytics/exports submits async export task for PDF format (FR-012)."""
    payload = {
        "report_type": "funnel",
        "export_format": "pdf",
    }
    response = client.post(
        "/api/v1/analytics/exports",
        json=payload,
        headers=auth_headers,
    )
    assert response.status_code == 202
    data = response.json()
    assert "task_id" in data
    assert data["report_type"] == "funnel"
    assert data["export_format"] == "pdf"
    assert data["status"] in ("PENDING", "ACCEPTED", "COMPLETED")


def test_create_dataset_export_unsupported_format(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Test POST /api/v1/analytics/exports with unsupported format returns 422 Unprocessable Entity."""
    payload = {
        "report_type": "acquisition",
        "export_format": "excel",
    }
    response = client.post(
        "/api/v1/analytics/exports",
        json=payload,
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_create_dataset_export_empty_report_type(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Test POST /api/v1/analytics/exports with empty report_type returns 422 Unprocessable Entity."""
    payload = {
        "report_type": "  ",
        "export_format": "csv",
    }
    response = client.post(
        "/api/v1/analytics/exports",
        json=payload,
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_create_dataset_export_input_sanitization(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Test POST /api/v1/analytics/exports sanitizes input strings (NFR-004)."""
    payload = {
        "report_type": "<script>alert('xss')</script>acquisition",
        "export_format": "CSV",
        "channel_id": "<b>channel-1</b>",
    }
    response = client.post(
        "/api/v1/analytics/exports",
        json=payload,
        headers=auth_headers,
    )
    assert response.status_code == 202
    data = response.json()
    assert data["report_type"] == "acquisition"
    assert data["export_format"] == "csv"


# ---------------------------------------------------------------------------
# Support Analytics Endpoint Integration Tests [FR-8, NFR-2, NFR-3]
# ---------------------------------------------------------------------------


def test_get_support_analytics_empty_metrics(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Test GET /api/v1/support/analytics when no ticket data exists [FR-8]."""
    response = client.get(
        f"{settings.API_V1_STR}/support/analytics",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total_tickets"] == 0
    assert data["open_tickets"] == 0
    assert data["pending_tickets"] == 0
    assert data["resolved_tickets"] == 0
    assert data["average_first_response_time_seconds"] == 0.0
    assert data["mean_time_to_resolution_seconds"] == 0.0
    assert data["total_sla_targets"] == 0
    assert data["breached_sla_targets"] == 0
    assert data["sla_breach_rate_percent"] == 0.0
    assert data["average_csat_score"] == 0.0
    assert data["csat_response_count"] == 0
    assert data["priority_breakdown"] == {"P1": 0, "P2": 0, "P3": 0}

    # Verify alias path /api/v1/analytics/support also works
    alias_res = client.get(
        f"{settings.API_V1_STR}/analytics/support",
        headers=auth_headers,
    )
    assert alias_res.status_code == 200
    assert alias_res.json()["total_tickets"] == 0


def test_get_support_analytics_with_data(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test GET /api/v1/support/analytics calculates metrics over seeded tickets and CSAT ratings [FR-8]."""
    now = datetime.now(timezone.utc)
    t1_created = now - timedelta(hours=5)
    t1_responded = now - timedelta(hours=4)
    t1_resolved = now - timedelta(hours=1)

    # 1. Create a resolved P1 ticket
    res1 = client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "Critical Server Down",
            "description": "Production database cluster unavailable.",
            "client_id": "client_enterprise",
            "priority": "P1",
        },
        headers=auth_headers,
    )
    assert res1.status_code == 201
    ticket1_id = res1.json()["id"]

    # Update timestamps and status in DB
    from src.models.ticket import SupportTicket
    ticket1 = db_session.get(SupportTicket, uuid.UUID(ticket1_id))
    assert ticket1 is not None
    ticket1.created_at = t1_created
    ticket1.first_responded_at = t1_responded
    ticket1.resolved_at = t1_resolved
    ticket1.status = "resolved"
    db_session.commit()

    # Submit CSAT rating for ticket1
    csat_res = client.post(
        f"{settings.API_V1_STR}/tickets/{ticket1_id}/csat",
        json={"score": 5, "feedback_text": "Excellent rapid resolution!"},
        headers=auth_headers,
    )
    assert csat_res.status_code == 201

    # 2. Create an open P3 ticket
    client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "Minor UI Alignment",
            "description": "Button alignment in settings page offset by 2px.",
            "client_id": "client_standard",
            "priority": "P3",
        },
        headers=auth_headers,
    )

    # Fetch support analytics
    response = client.get(
        f"{settings.API_V1_STR}/support/analytics",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total_tickets"] == 2
    assert data["open_tickets"] == 1
    assert data["resolved_tickets"] == 1
    assert data["priority_breakdown"]["P1"] == 1
    assert data["priority_breakdown"]["P3"] == 1
    assert data["average_csat_score"] == 5.0
    assert data["csat_response_count"] == 1
    assert data["average_first_response_time_seconds"] > 0
    assert data["mean_time_to_resolution_seconds"] > 0


def test_get_support_analytics_filtering(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Test filtering GET /api/v1/support/analytics by priority and client_id [FR-8]."""
    # Create distinct priority tickets
    client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "Network Latency Spike",
            "description": "High packet loss observed across US East region.",
            "client_id": "client_alpha",
            "priority": "P1",
        },
        headers=auth_headers,
    )
    client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "Account Email Change",
            "description": "Requesting primary email update.",
            "client_id": "client_beta",
            "priority": "P2",
        },
        headers=auth_headers,
    )

    # Filter by priority=P1
    res_p1 = client.get(
        f"{settings.API_V1_STR}/support/analytics?priority=p1",
        headers=auth_headers,
    )
    assert res_p1.status_code == 200
    p1_data = res_p1.json()
    assert p1_data["total_tickets"] == 1
    assert p1_data["priority_breakdown"]["P1"] == 1
    assert p1_data["priority_breakdown"]["P2"] == 0

    # Filter by client_id=client_beta
    res_client = client.get(
        f"{settings.API_V1_STR}/support/analytics?client_id=client_beta",
        headers=auth_headers,
    )
    assert res_client.status_code == 200
    client_data = res_client.json()
    assert client_data["total_tickets"] == 1
    assert client_data["priority_breakdown"]["P2"] == 1


def test_get_support_analytics_validation_errors(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Test validation errors on GET /api/v1/support/analytics [NFR-2]."""
    # Invalid priority code
    res_bad_p = client.get(
        f"{settings.API_V1_STR}/support/analytics?priority=INVALID_P4",
        headers=auth_headers,
    )
    assert res_bad_p.status_code == 400
    assert "Invalid priority filter" in res_bad_p.json()["detail"]

    # start_time > end_time
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=1)
    past = now - timedelta(days=1)
    res_bad_times = client.get(
        f"{settings.API_V1_STR}/support/analytics?start_time={future.isoformat()}&end_time={past.isoformat()}",
        headers=auth_headers,
    )
    assert res_bad_times.status_code == 400
    assert "start_time cannot be after end_time" in res_bad_times.json()["detail"]





