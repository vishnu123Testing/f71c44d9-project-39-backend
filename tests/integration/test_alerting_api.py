"""Integration tests for FastAPI alerting and notification endpoints.

Verifies trigger rule CRUD, optimistic-locked notification preference updates,
in-app alert listing, RBAC enforcement, and boundary validation [FR-5, FR-6, FR-7, FR-9, FR-10].
"""

from datetime import datetime, timezone
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.api.deps import create_access_token
from src.api.main import app
from src.models.alerting import (
    AlertTrigger,
    AlertUrgency,
    NotificationChannel,
    NotificationDeliveryStatus,
    NotificationLog,
    NotificationPreference,
)
from src.models.database import Base, get_db
from src.models.role import Role, UserRole
from src.models.tenant import Tenant
from src.models.user import User
from src.services.access_control.rbac import RoleName
from src.services.security import get_password_hash


@pytest.fixture
def db_session() -> Session:
    """In-memory SQLite database session for alerting API integration testing."""
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
def auth_headers(db_session: Session) -> dict[str, str]:
    """Seed database with tenant, user, role and generate Bearer auth headers with ClientStandard role."""
    tenant = Tenant(name="Alerting API Test Tenant")
    db_session.add(tenant)
    db_session.commit()

    hashed_pwd = get_password_hash("SecretPassword123!")
    user = User(
        email="alert_user@test.com",
        password_hash=hashed_pwd,
        tenant_id=tenant.tenant_id,
    )
    db_session.add(user)
    db_session.commit()

    role = Role(
        name=RoleName.CLIENT_STANDARD.value,
        description="Client Standard Role",
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

    token = create_access_token(
        subject=user.user_id,
        tenant_id=tenant.tenant_id,
        email=user.email,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def viewer_auth_headers(db_session: Session) -> dict[str, str]:
    """Seed database with user having ClientViewer role (read-only)."""
    tenant = db_session.query(Tenant).first()
    if not tenant:
        tenant = Tenant(name="Alerting Viewer Tenant")
        db_session.add(tenant)
        db_session.commit()

    user = User(
        email="viewer_user@test.com",
        password_hash=get_password_hash("Secret123!"),
        tenant_id=tenant.tenant_id,
    )
    db_session.add(user)

    role = db_session.query(Role).filter(Role.name == RoleName.CLIENT_VIEWER.value).first()
    if not role:
        role = Role(name=RoleName.CLIENT_VIEWER.value, description="Viewer Role")
        db_session.add(role)
        db_session.commit()

    db_session.commit()

    user_role = UserRole(
        user_id=user.user_id,
        role_id=role.role_id,
        tenant_id=tenant.tenant_id,
    )
    db_session.add(user_role)
    db_session.commit()

    token = create_access_token(
        subject=user.user_id,
        tenant_id=tenant.tenant_id,
        email=user.email,
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Unauthenticated and Authorization Tests
# ---------------------------------------------------------------------------


def test_unauthenticated_access_rejected(client: TestClient) -> None:
    """Verify unauthenticated requests return HTTP 401 Unauthorized."""
    response = client.get("/api/v1/notifications/in-app")
    assert response.status_code == 401

    response = client.post(
        "/api/v1/notifications/triggers",
        json={
            "rule_name": "Test Rule",
            "financial_criteria": "past_due_balance",
            "threshold_amount": 5000.0,
        },
    )
    assert response.status_code == 401


def test_viewer_role_write_rejected(client: TestClient, viewer_auth_headers: dict[str, str]) -> None:
    """Verify users with read-only ClientViewer role cannot create trigger rules [FR-9]."""
    response = client.post(
        "/api/v1/notifications/triggers",
        json={
            "rule_name": "Unauthorized Rule",
            "financial_criteria": "past_due_balance",
            "threshold_amount": 1000.0,
        },
        headers=viewer_auth_headers,
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# In-App Alerts Endpoint Tests [FR-5]
# ---------------------------------------------------------------------------


def test_list_in_app_alerts(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Verify listing in-app alert notifications and filtering by unread status [FR-5]."""
    now = datetime.now(timezone.utc)
    recipient = uuid.uuid4()

    log1 = NotificationLog(
        notification_id=uuid.uuid4(),
        recipient_id=recipient,
        urgency=AlertUrgency.MEDIUM.value,
        channel=NotificationChannel.IN_APP.value,
        delivery_status=NotificationDeliveryStatus.SENT.value,
        message="Test alert message 1",
        read_status=False,
        dispatched_at=now,
        correlation_id="corr-123",
    )
    log2 = NotificationLog(
        notification_id=uuid.uuid4(),
        recipient_id=recipient,
        urgency=AlertUrgency.HIGH.value,
        channel=NotificationChannel.IN_APP.value,
        delivery_status=NotificationDeliveryStatus.SENT.value,
        message="Test alert message 2",
        read_status=True,
        dispatched_at=now,
        correlation_id="corr-456",
    )
    db_session.add_all([log1, log2])
    db_session.commit()

    # Get all in-app alerts
    response = client.get("/api/v1/notifications/in-app", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2

    # Get unread only
    response = client.get("/api/v1/notifications/in-app?unread_only=true", headers=auth_headers)
    assert response.status_code == 200
    unread_data = response.json()
    assert len(unread_data) == 1
    assert unread_data[0]["message"] == "Test alert message 1"


def test_mark_in_app_alert_read(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Verify patching in-app alert read status [FR-5]."""
    log_id = uuid.uuid4()
    log = NotificationLog(
        notification_id=log_id,
        urgency=AlertUrgency.LOW.value,
        channel=NotificationChannel.IN_APP.value,
        delivery_status=NotificationDeliveryStatus.SENT.value,
        message="Unread notification",
        read_status=False,
        dispatched_at=datetime.now(timezone.utc),
        correlation_id="corr-789",
    )
    db_session.add(log)
    db_session.commit()

    response = client.patch(f"/api/v1/notifications/in-app/{log_id}/read", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["read_status"] is True

    # Confirm updated in DB
    updated_log = db_session.query(NotificationLog).get(log_id)
    assert updated_log.read_status is True


# ---------------------------------------------------------------------------
# Trigger Rules CRUD Endpoint Tests [FR-6, FR-10]
# ---------------------------------------------------------------------------


def test_trigger_rules_crud_flow(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Verify full CRUD lifecycle for custom alert trigger rules [FR-6]."""
    # 1. Create trigger rule
    payload = {
        "rule_name": "High Balance Alert <script>alert(1)</script>",
        "financial_criteria": "past_due_invoices_exceed_threshold",
        "threshold_amount": 10000.0,
        "urgency": "High",
        "is_active": True,
    }
    response = client.post("/api/v1/notifications/triggers", json=payload, headers=auth_headers)
    assert response.status_code == 201
    created = response.json()
    trigger_id = created["trigger_id"]
    # Check input sanitization
    assert "<script>" not in created["rule_name"]
    assert created["rule_name"] == "High Balance Alert alert(1)"
    assert created["threshold_amount"] == 10000.0
    assert created["urgency"] == "High"

    # 2. Get trigger rule by ID
    get_res = client.get(f"/api/v1/notifications/triggers/{trigger_id}", headers=auth_headers)
    assert get_res.status_code == 200
    assert get_res.json()["rule_name"] == created["rule_name"]

    # 3. List trigger rules
    list_res = client.get("/api/v1/notifications/triggers", headers=auth_headers)
    assert list_res.status_code == 200
    assert len(list_res.json()) >= 1

    # 4. Update trigger rule
    update_payload = {
        "threshold_amount": 15000.0,
        "urgency": "Medium",
    }
    put_res = client.put(f"/api/v1/notifications/triggers/{trigger_id}", json=update_payload, headers=auth_headers)
    assert put_res.status_code == 200
    updated = put_res.json()
    assert updated["threshold_amount"] == 15000.0
    assert updated["urgency"] == "Medium"

    # 5. Delete trigger rule (204 No Content with empty body)
    del_res = client.delete(f"/api/v1/notifications/triggers/{trigger_id}", headers=auth_headers)
    assert del_res.status_code == 204
    assert del_res.text == ""

    # Verify deleted
    get_again = client.get(f"/api/v1/notifications/triggers/{trigger_id}", headers=auth_headers)
    assert get_again.status_code == 404


def test_create_duplicate_trigger_rule_fails(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Verify creating a trigger rule with duplicate name returns HTTP 409 Conflict [FR-6]."""
    payload = {
        "rule_name": "Unique Trigger Name",
        "financial_criteria": "overdue_balance",
        "threshold_amount": 500.0,
    }
    res1 = client.post("/api/v1/notifications/triggers", json=payload, headers=auth_headers)
    assert res1.status_code == 201

    res2 = client.post("/api/v1/notifications/triggers", json=payload, headers=auth_headers)
    assert res2.status_code == 409


# ---------------------------------------------------------------------------
# Notification Preferences & Optimistic Locking Tests [FR-7]
# ---------------------------------------------------------------------------


def test_notification_preferences_optimistic_locking(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Verify optimistic locking version checks on notification preference updates [FR-7]."""
    pref_payload = {
        "role_name": "ClientStandard",
        "event_type": "invoice_past_due",
        "urgency": "High",
        "channels_json": ["InApp", "Email"],
        "version": 1,
    }

    # 1. First update creates preference with version=1
    res1 = client.put("/api/v1/notifications/preferences", json=pref_payload, headers=auth_headers)
    assert res1.status_code == 200
    pref1 = res1.json()
    assert pref1["version"] == 1
    assert pref1["channels_json"] == ["InApp", "Email"]

    # 2. Update with correct version (version=1) succeeds and increments version to 2
    update_payload_valid = {
        "role_name": "ClientStandard",
        "event_type": "invoice_past_due",
        "urgency": "High",
        "channels_json": ["InApp", "Email", "SMS"],
        "version": 1,
    }
    res2 = client.put("/api/v1/notifications/preferences", json=update_payload_valid, headers=auth_headers)
    assert res2.status_code == 200
    pref2 = res2.json()
    assert pref2["version"] == 2
    assert pref2["channels_json"] == ["InApp", "Email", "SMS"]

    # 3. Update with stale version (version=1 when DB is at 2) returns 409 Conflict
    stale_payload = {
        "role_name": "ClientStandard",
        "event_type": "invoice_past_due",
        "urgency": "Low",
        "channels_json": ["InApp"],
        "version": 1,
    }
    res3 = client.put("/api/v1/notifications/preferences", json=stale_payload, headers=auth_headers)
    assert res3.status_code == 409
    assert "Optimistic lock conflict" in res3.json()["detail"]

    # 4. List preferences
    list_res = client.get("/api/v1/notifications/preferences?role_name=ClientStandard", headers=auth_headers)
    assert list_res.status_code == 200
    items = list_res.json()
    assert len(items) == 1
    assert items[0]["version"] == 2
