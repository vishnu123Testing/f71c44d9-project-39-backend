"""Integration tests for GET /api/v1/dashboard/summary endpoint."""

import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.api.dashboard import get_connectors, router as dashboard_router
from src.api.deps import create_access_token
from src.api.main import app
from src.lib.config import settings
from src.models.database import Base, get_db
from src.models.role import Role, UserRole
from src.models.tenant import Tenant
from src.models.user import User
from src.services.access_control.rbac import RoleName
from src.services.dashboard.domain_ports import (
    BreakdownItemDTO,
    DomainConnectorPort,
    DomainIdentifier,
    DomainMetricsResult,
    MetricResult,
    MetricStatus,
    MetricUnit,
)
from src.services.security import get_password_hash


# Ensure dashboard router is registered for integration testing
if not any(getattr(route, "path", "").startswith(f"{settings.API_V1_STR}/dashboard") for route in app.routes):
    app.include_router(dashboard_router, prefix=settings.API_V1_STR)


@pytest.fixture
def db_session() -> Session:
    """In-memory SQLite database session for dashboard summary endpoint testing."""
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
    tenant = Tenant(name="Dashboard Test Tenant")
    db_session.add(tenant)
    db_session.commit()

    hashed_pwd = get_password_hash("DashboardSecret123!")
    user = User(
        email="executive@test.com",
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


class MockConnector(DomainConnectorPort):
    """Mock connector for testing custom metric outcomes."""

    def __init__(self, domain: str, metrics: list[MetricResult], status: str = "COMPLETE") -> None:
        self._domain = domain
        self._metrics = metrics
        self._status = status

    @property
    def domain_identifier(self) -> str:
        return self._domain

    def fetch_metrics(self, timeframe: str = "30d") -> DomainMetricsResult:
        return DomainMetricsResult(
            domain=self._domain,
            status=self._status,
            metrics=self._metrics,
            breakdown_items=[],
        )


def test_get_dashboard_summary_unauthenticated(client: TestClient) -> None:
    """Test GET /api/v1/dashboard/summary returns 401 Unauthorized when missing token."""
    response = client.get("/api/v1/dashboard/summary")
    assert response.status_code == 401
    assert "detail" in response.json()


def test_get_dashboard_summary_success_200(
    client: TestClient, test_user: tuple[Tenant, User]
) -> None:
    """Test GET /api/v1/dashboard/summary returns consolidated KPI summary (FR-1, FR-2)."""
    tenant, user = test_user
    token = create_access_token(subject=user.user_id, tenant_id=tenant.tenant_id)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get("/api/v1/dashboard/summary", headers=headers)
    assert response.status_code == 200

    data = response.json()
    assert "status" in data
    assert data["status"] in ("COMPLETE", "PARTIAL")
    assert "generated_at" in data
    assert "metrics" in data
    assert isinstance(data["metrics"], list)
    assert len(data["metrics"]) > 0

    # Verify domains represented in metrics list
    domains = {m["domain"] for m in data["metrics"]}
    assert "lead_gen" in domains
    assert "sales_pipeline" in domains
    assert "support" in domains
    assert "financials" in domains

    # Verify KPI card fields (FR-2)
    sample_metric = data["metrics"][0]
    assert "domain" in sample_metric
    assert "metric_key" in sample_metric
    assert "display_name" in sample_metric
    assert "formatted_value" in sample_metric
    assert "status" in sample_metric
    assert "unit" in sample_metric


def test_get_dashboard_summary_timeframe_validation(
    client: TestClient, test_user: tuple[Tenant, User]
) -> None:
    """Test GET /api/v1/dashboard/summary handles valid and invalid timeframes."""
    tenant, user = test_user
    token = create_access_token(subject=user.user_id, tenant_id=tenant.tenant_id)
    headers = {"Authorization": f"Bearer {token}"}

    # Valid timeframes
    for tf in ["7d", "30d", "90d"]:
        resp = client.get(f"/api/v1/dashboard/summary?timeframe={tf}", headers=headers)
        assert resp.status_code == 200

    # Invalid timeframe
    resp_bad = client.get("/api/v1/dashboard/summary?timeframe=invalid_tf", headers=headers)
    assert resp_bad.status_code == 400
    assert "Invalid timeframe parameter" in resp_bad.json()["detail"]


def test_get_dashboard_summary_partial_failure(
    client: TestClient, test_user: tuple[Tenant, User]
) -> None:
    """Test GET /api/v1/dashboard/summary handles partial connector outages (FR-3)."""
    tenant, user = test_user
    token = create_access_token(subject=user.user_id, tenant_id=tenant.tenant_id)
    headers = {"Authorization": f"Bearer {token}"}

    valid_connector = MockConnector(
        domain="lead_gen",
        metrics=[
            MetricResult(
                domain="lead_gen",
                metric_key="lead_volume",
                display_name="Lead Volume",
                raw_value=100.0,
                baseline_value=80.0,
                formatted_value="100",
                percentage_change=25.0,
                status=MetricStatus.VALID,
                unit=MetricUnit.COUNT.value,
            )
        ],
    )

    failed_connector = MockConnector(
        domain="support",
        status="UNAVAILABLE",
        metrics=[
            MetricResult(
                domain="support",
                metric_key="open_tickets",
                display_name="Open Tickets",
                raw_value=None,
                baseline_value=None,
                formatted_value="Unavailable",
                percentage_change=None,
                status=MetricStatus.UNAVAILABLE,
                unit=MetricUnit.COUNT.value,
                error_detail="Downstream support timeout",
            )
        ],
    )

    def _override_connectors() -> list[DomainConnectorPort]:
        return [valid_connector, failed_connector]

    app.dependency_overrides[get_connectors] = _override_connectors

    try:
        resp = client.get("/api/v1/dashboard/summary", headers=headers)
        assert resp.status_code == 200

        data = resp.json()
        assert data["status"] == "PARTIAL"

        support_metric = next(m for m in data["metrics"] if m["domain"] == "support")
        assert support_metric["status"] == "UNAVAILABLE"
        assert support_metric["formatted_value"] == "Unavailable"
        assert support_metric["raw_value"] is None
    finally:
        del app.dependency_overrides[get_connectors]


def test_get_dashboard_summary_zero_value_preservation(
    client: TestClient, test_user: tuple[Tenant, User]
) -> None:
    """Test GET /api/v1/dashboard/summary preserves explicit zero values distinct from unavailable (FR-4)."""
    tenant, user = test_user
    token = create_access_token(subject=user.user_id, tenant_id=tenant.tenant_id)
    headers = {"Authorization": f"Bearer {token}"}

    zero_connector = MockConnector(
        domain="financials",
        metrics=[
            MetricResult(
                domain="financials",
                metric_key="outstanding_invoices",
                display_name="Outstanding Invoices",
                raw_value=0.0,
                baseline_value=100.0,
                formatted_value="$0",
                percentage_change=-100.0,
                status=MetricStatus.ZERO,
                unit=MetricUnit.CURRENCY.value,
            )
        ],
    )

    def _override_connectors() -> list[DomainConnectorPort]:
        return [zero_connector]

    app.dependency_overrides[get_connectors] = _override_connectors

    try:
        resp = client.get("/api/v1/dashboard/summary", headers=headers)
        assert resp.status_code == 200

        data = resp.json()
        fin_metric = data["metrics"][0]
        assert fin_metric["status"] == "ZERO"
        assert fin_metric["raw_value"] == 0.0
        assert fin_metric["formatted_value"] in ("$0", "0")
        assert fin_metric["formatted_value"] != "Unavailable"
    finally:
        del app.dependency_overrides[get_connectors]


def test_get_domain_drilldown_unauthenticated(client: TestClient) -> None:
    """Test GET /api/v1/dashboard/domains/{domain_id} returns 401 Unauthorized when missing token."""
    response = client.get("/api/v1/dashboard/domains/lead_gen")
    assert response.status_code == 401
    assert "detail" in response.json()


def test_get_domain_drilldown_all_domains_success(
    client: TestClient, test_user: tuple[Tenant, User]
) -> None:
    """Test GET /api/v1/dashboard/domains/{domain_id} returns detailed metrics for all domains (FR-6, FR-7, FR-8, FR-9)."""
    tenant, user = test_user
    token = create_access_token(subject=user.user_id, tenant_id=tenant.tenant_id)
    headers = {"Authorization": f"Bearer {token}"}

    domains_to_check = ["lead_gen", "sales_pipeline", "support", "financials"]

    for domain in domains_to_check:
        resp = client.get(f"/api/v1/dashboard/domains/{domain}", headers=headers)
        assert resp.status_code == 200

        data = resp.json()
        assert data["domain"] == domain
        assert data["status"] == "COMPLETE"
        assert "detailed_metrics" in data
        assert isinstance(data["detailed_metrics"], list)
        assert len(data["detailed_metrics"]) > 0
        assert "breakdown_items" in data
        assert isinstance(data["breakdown_items"], list)

        # Check metric keys for specific domain FR requirements
        metric_keys = {m["metric_key"] for m in data["detailed_metrics"]}
        if domain == "lead_gen":
            # FR-6: volume, conversion rates
            assert "lead_volume" in metric_keys
            assert "conversion_rate" in metric_keys
        elif domain == "sales_pipeline":
            # FR-7: deal values, stage distribution
            assert "pipeline_value" in metric_keys
            assert "avg_deal_size" in metric_keys
        elif domain == "support":
            # FR-8: open tickets, resolution times, SLA compliance
            assert "open_tickets" in metric_keys
            assert "avg_resolution_time" in metric_keys
            assert "sla_compliance" in metric_keys
        elif domain == "financials":
            # FR-9: recurring revenue, outstanding invoices
            assert "recurring_revenue" in metric_keys
            assert "outstanding_invoices" in metric_keys


def test_get_domain_drilldown_invalid_domain_400(
    client: TestClient, test_user: tuple[Tenant, User]
) -> None:
    """Test GET /api/v1/dashboard/domains/{domain_id} returns 400 Bad Request for unmapped domain."""
    tenant, user = test_user
    token = create_access_token(subject=user.user_id, tenant_id=tenant.tenant_id)
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.get("/api/v1/dashboard/domains/unmapped_domain", headers=headers)
    assert resp.status_code == 400
    assert "Invalid domain_id parameter" in resp.json()["detail"]


def test_get_domain_drilldown_invalid_timeframe_400(
    client: TestClient, test_user: tuple[Tenant, User]
) -> None:
    """Test GET /api/v1/dashboard/domains/{domain_id} returns 400 Bad Request for invalid timeframe."""
    tenant, user = test_user
    token = create_access_token(subject=user.user_id, tenant_id=tenant.tenant_id)
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.get("/api/v1/dashboard/domains/lead_gen?timeframe=bad_tf", headers=headers)
    assert resp.status_code == 400
    assert "Invalid timeframe parameter" in resp.json()["detail"]


def test_get_domain_drilldown_connector_unavailable(
    client: TestClient, test_user: tuple[Tenant, User]
) -> None:
    """Test GET /api/v1/dashboard/domains/{domain_id} handles single domain connector outage gracefully."""
    tenant, user = test_user
    token = create_access_token(subject=user.user_id, tenant_id=tenant.tenant_id)
    headers = {"Authorization": f"Bearer {token}"}

    unavail_connector = MockConnector(
        domain="support",
        status="UNAVAILABLE",
        metrics=[
            MetricResult(
                domain="support",
                metric_key="open_tickets",
                display_name="Open Tickets",
                raw_value=None,
                baseline_value=None,
                formatted_value="Unavailable",
                percentage_change=None,
                status=MetricStatus.UNAVAILABLE,
                unit=MetricUnit.COUNT.value,
                error_detail="Support connector offline",
            )
        ],
    )

    def _override_connectors() -> list[DomainConnectorPort]:
        return [unavail_connector]

    app.dependency_overrides[get_connectors] = _override_connectors

    try:
        resp = client.get("/api/v1/dashboard/domains/support", headers=headers)
        assert resp.status_code == 200

        data = resp.json()
        assert data["domain"] == "support"
        assert data["status"] == "UNAVAILABLE"
        assert len(data["detailed_metrics"]) > 0
        assert data["detailed_metrics"][0]["status"] == "UNAVAILABLE"
    finally:
        del app.dependency_overrides[get_connectors]
