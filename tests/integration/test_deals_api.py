"""Integration tests for FastAPI deal pipeline, stage transition, action items, stakeholders, and forecast endpoints."""

from datetime import datetime, timedelta, timezone
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.api.deps import create_access_token
from src.api.main import app
from src.lib.config import settings
from src.models.database import Base, get_db
from src.models.role import Role, UserRole
from src.models.tenant import Tenant
from src.models.user import User
from src.services.access_control.rbac import RoleName
from src.services.security import get_password_hash


@pytest.fixture
def db_session() -> Session:
    """In-memory SQLite database session for deal API integration testing."""
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
    """Seed database with tenant, user, role and generate Bearer auth headers."""
    tenant = Tenant(name="Deals API Test Tenant")
    db_session.add(tenant)
    db_session.commit()

    hashed_pwd = get_password_hash("SecretPassword123!")
    user = User(
        email="deal_user@test.com",
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

    token = create_access_token(
        subject=user.user_id,
        tenant_id=tenant.tenant_id,
        email=user.email,
    )
    return {"Authorization": f"Bearer {token}"}


def test_create_and_get_deal(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test creating a deal and retrieving it by ID."""
    payload = {
        "title": "Acme Corp Enterprise License",
        "deal_value": 150000.0,
        "milestone_stage": "Qualification",
    }
    response = client.post(
        f"{settings.API_V1_STR}/deals",
        json=payload,
        headers=auth_headers,
    )
    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Acme Corp Enterprise License"
    assert data["deal_value"] == 150000.0
    assert data["milestone_stage"] == "Qualification"
    assert data["win_probability"] == 0.20
    assert "id" in data
    deal_id = data["id"]

    # Get single deal
    get_res = client.get(
        f"{settings.API_V1_STR}/deals/{deal_id}",
        headers=auth_headers,
    )
    assert get_res.status_code == 200
    assert get_res.json()["id"] == deal_id


def test_get_deal_pipeline_board(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test retrieving visual deal pipeline board showing cards grouped by stage."""
    # Create two deals
    client.post(
        f"{settings.API_V1_STR}/deals",
        json={"title": "Deal 1", "deal_value": 50000.0, "milestone_stage": "Qualification"},
        headers=auth_headers,
    )
    client.post(
        f"{settings.API_V1_STR}/deals",
        json={"title": "Deal 2", "deal_value": 100000.0, "milestone_stage": "Proposal"},
        headers=auth_headers,
    )

    response = client.get(
        f"{settings.API_V1_STR}/deals/pipeline",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert "stages" in data
    assert data["total_pipeline_value"] == 150000.0
    assert data["total_deal_count"] == 2
    assert len(data["deals"]) == 2


def test_update_deal_stage_transition(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test updating deal stage transition directly from pipeline interface."""
    res = client.post(
        f"{settings.API_V1_STR}/deals",
        json={"title": "Stage Test Deal", "deal_value": 75000.0, "milestone_stage": "Qualification"},
        headers=auth_headers,
    )
    deal_id = res.json()["id"]

    # Transition to Negotiation
    patch_res = client.patch(
        f"{settings.API_V1_STR}/deals/{deal_id}/stage",
        json={"milestone_stage": "Negotiation"},
        headers=auth_headers,
    )
    assert patch_res.status_code == 200
    updated_data = patch_res.json()
    assert updated_data["milestone_stage"] == "Negotiation"
    assert updated_data["win_probability"] == 0.80


def test_action_items_lifecycle(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test creating, listing, completing, and deleting action items for a deal."""
    res = client.post(
        f"{settings.API_V1_STR}/deals",
        json={"title": "Action Item Test Deal", "deal_value": 30000.0, "milestone_stage": "Proposal"},
        headers=auth_headers,
    )
    deal_id = res.json()["id"]

    due_dt = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    ai_res = client.post(
        f"{settings.API_V1_STR}/deals/{deal_id}/action-items",
        json={
            "title": "Send Proposal PDF",
            "assigned_owner_id": "owner_123",
            "target_due_date": due_dt,
            "sla_threshold_hours": 24,
        },
        headers=auth_headers,
    )
    assert ai_res.status_code == 201
    item_data = ai_res.json()
    assert item_data["title"] == "Send Proposal PDF"
    assert item_data["is_completed"] is False
    assert item_data["is_escalated"] is False
    item_id = item_data["id"]

    # List action items (include_completed=True)
    list_res = client.get(
        f"{settings.API_V1_STR}/deals/{deal_id}/action-items",
        headers=auth_headers,
    )
    assert list_res.status_code == 200
    assert len(list_res.json()) == 1

    # Complete action item
    comp_res = client.patch(
        f"{settings.API_V1_STR}/deals/{deal_id}/action-items/{item_id}/complete",
        json={},
        headers=auth_headers,
    )
    assert comp_res.status_code == 200
    assert comp_res.json()["is_completed"] is True
    assert comp_res.json()["completed_on"] is not None
    assert comp_res.json()["is_escalated"] is False

    # List action items excluding completed
    list_active_res = client.get(
        f"{settings.API_V1_STR}/deals/{deal_id}/action-items?include_completed=false",
        headers=auth_headers,
    )
    assert list_active_res.status_code == 200
    assert len(list_active_res.json()) == 0

    # Delete action item -> HTTP 204 with empty body
    del_res = client.delete(
        f"{settings.API_V1_STR}/deals/{deal_id}/action-items/{item_id}",
        headers=auth_headers,
    )
    assert del_res.status_code == 204
    assert del_res.text == "" or del_res.content == b""

    # Verify action item is deleted
    list_after_del = client.get(
        f"{settings.API_V1_STR}/deals/{deal_id}/action-items",
        headers=auth_headers,
    )
    assert list_after_del.status_code == 200
    assert len(list_after_del.json()) == 0


def test_action_items_error_handling_and_validation(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Test error conditions and boundary validation for action item endpoints [FR-004, FR-005, NFR-003]."""
    res = client.post(
        f"{settings.API_V1_STR}/deals",
        json={"title": "Error Handling Deal", "deal_value": 50000.0, "milestone_stage": "Qualification"},
        headers=auth_headers,
    )
    deal_id = res.json()["id"]
    non_existent_deal_id = str(uuid.uuid4())
    non_existent_item_id = str(uuid.uuid4())

    due_dt = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    # 1. Create action item for non-existent deal -> 404
    res_404 = client.post(
        f"{settings.API_V1_STR}/deals/{non_existent_deal_id}/action-items",
        json={
            "title": "Task Title",
            "assigned_owner_id": "owner_123",
            "target_due_date": due_dt,
        },
        headers=auth_headers,
    )
    assert res_404.status_code == 404

    # 2. Create action item with empty/whitespace title -> 422
    res_bad_title = client.post(
        f"{settings.API_V1_STR}/deals/{deal_id}/action-items",
        json={
            "title": "   ",
            "assigned_owner_id": "owner_123",
            "target_due_date": due_dt,
        },
        headers=auth_headers,
    )
    assert res_bad_title.status_code == 422

    # 3. Create action item with invalid sla_threshold_hours -> 422
    res_bad_sla = client.post(
        f"{settings.API_V1_STR}/deals/{deal_id}/action-items",
        json={
            "title": "Valid Title",
            "assigned_owner_id": "owner_123",
            "target_due_date": due_dt,
            "sla_threshold_hours": 0,
        },
        headers=auth_headers,
    )
    assert res_bad_sla.status_code == 422

    # 4. Get action items for non-existent deal -> 404
    res_get_404 = client.get(
        f"{settings.API_V1_STR}/deals/{non_existent_deal_id}/action-items",
        headers=auth_headers,
    )
    assert res_get_404.status_code == 404

    # 5. Complete action item on non-existent deal -> 404
    res_comp_deal_404 = client.patch(
        f"{settings.API_V1_STR}/deals/{non_existent_deal_id}/action-items/{non_existent_item_id}/complete",
        json={},
        headers=auth_headers,
    )
    assert res_comp_deal_404.status_code == 404

    # 6. Complete non-existent action item on existing deal -> 404
    res_comp_item_404 = client.patch(
        f"{settings.API_V1_STR}/deals/{deal_id}/action-items/{non_existent_item_id}/complete",
        json={},
        headers=auth_headers,
    )
    assert res_comp_item_404.status_code == 404

    # 7. Create a second deal and action item, test completing item under wrong deal_id -> 400
    res_deal2 = client.post(
        f"{settings.API_V1_STR}/deals",
        json={"title": "Deal Two", "deal_value": 20000.0, "milestone_stage": "Proposal"},
        headers=auth_headers,
    )
    deal2_id = res_deal2.json()["id"]

    ai_deal1 = client.post(
        f"{settings.API_V1_STR}/deals/{deal_id}/action-items",
        json={
            "title": "Deal 1 Task",
            "assigned_owner_id": "owner_123",
            "target_due_date": due_dt,
        },
        headers=auth_headers,
    )
    item1_id = ai_deal1.json()["id"]

    res_comp_wrong_deal = client.patch(
        f"{settings.API_V1_STR}/deals/{deal2_id}/action-items/{item1_id}/complete",
        json={},
        headers=auth_headers,
    )
    assert res_comp_wrong_deal.status_code == 400

    # 8. Delete action item on non-existent deal -> 404
    res_del_deal_404 = client.delete(
        f"{settings.API_V1_STR}/deals/{non_existent_deal_id}/action-items/{non_existent_item_id}",
        headers=auth_headers,
    )
    assert res_del_deal_404.status_code == 404

    # 9. Delete non-existent action item on existing deal -> 404
    res_del_item_404 = client.delete(
        f"{settings.API_V1_STR}/deals/{deal_id}/action-items/{non_existent_item_id}",
        headers=auth_headers,
    )
    assert res_del_item_404.status_code == 404

    # 10. Delete action item under wrong deal_id -> 400
    res_del_wrong_deal = client.delete(
        f"{settings.API_V1_STR}/deals/{deal2_id}/action-items/{item1_id}",
        headers=auth_headers,
    )
    assert res_del_wrong_deal.status_code == 400


def test_stakeholders_and_activities(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test mapping stakeholders, influence levels, and chronological activity feeds on a deal [FR-006, FR-007]."""
    res = client.post(
        f"{settings.API_V1_STR}/deals",
        json={"title": "Stakeholder Test Deal", "deal_value": 45000.0, "milestone_stage": "Discovery"},
        headers=auth_headers,
    )
    deal_id = res.json()["id"]

    # 1. Create first stakeholder (High influence)
    stk1_res = client.post(
        f"{settings.API_V1_STR}/deals/{deal_id}/stakeholders",
        json={
            "full_name": "Jane Doe",
            "email": "jane.doe@acme.com",
            "org_role": "VP of Engineering",
            "influence_level": "high",
        },
        headers=auth_headers,
    )
    assert stk1_res.status_code == 201
    stk1_data = stk1_res.json()
    assert stk1_data["full_name"] == "Jane Doe"
    assert stk1_data["org_role"] == "VP of Engineering"
    assert stk1_data["influence_level"] == "High"
    assert stk1_data["is_active"] is True
    stk1_id = stk1_data["id"]

    # 2. Create second stakeholder (Medium influence)
    stk2_res = client.post(
        f"{settings.API_V1_STR}/deals/{deal_id}/stakeholders",
        json={
            "full_name": "Bob Smith",
            "email": "bob.smith@acme.com",
            "org_role": "Procurement Lead",
            "influence_level": "medium",
        },
        headers=auth_headers,
    )
    assert stk2_res.status_code == 201
    stk2_data = stk2_res.json()
    assert stk2_data["influence_level"] == "Medium"
    stk2_id = stk2_data["id"]

    # 3. Log chronological activities
    now_dt = datetime.now(timezone.utc)
    act1_time = (now_dt - timedelta(hours=2)).isoformat()
    act2_time = (now_dt - timedelta(hours=1)).isoformat()
    act3_time = (now_dt - timedelta(minutes=15)).isoformat()

    # Activity 1: Email (2 hours ago)
    act1_res = client.post(
        f"{settings.API_V1_STR}/deals/{deal_id}/stakeholders/activities",
        json={
            "stakeholder_id": stk1_id,
            "activity_type": "email",
            "summary": "Sent proposal draft email.",
            "occurred_on": act1_time,
        },
        headers=auth_headers,
    )
    assert act1_res.status_code == 201
    assert act1_res.json()["activity_type"] == "Email"

    # Activity 2: Meeting (1 hour ago)
    act2_res = client.post(
        f"{settings.API_V1_STR}/deals/{deal_id}/stakeholders/activities",
        json={
            "stakeholder_id": stk2_id,
            "activity_type": "meeting",
            "summary": "Procurement sync meeting.",
            "occurred_on": act2_time,
        },
        headers=auth_headers,
    )
    assert act2_res.status_code == 201
    assert act2_res.json()["activity_type"] == "Meeting"

    # Activity 3: Call (15 mins ago)
    act3_res = client.post(
        f"{settings.API_V1_STR}/deals/{deal_id}/stakeholders/activities",
        json={
            "stakeholder_id": stk1_id,
            "activity_type": "call",
            "summary": "Follow-up phone call regarding timeline.",
            "occurred_on": act3_time,
        },
        headers=auth_headers,
    )
    assert act3_res.status_code == 201
    assert act3_res.json()["activity_type"] == "Call"

    # 4. Get stakeholder map and activity feed
    map_res = client.get(
        f"{settings.API_V1_STR}/deals/{deal_id}/stakeholders",
        headers=auth_headers,
    )
    assert map_res.status_code == 200
    map_data = map_res.json()
    assert map_data["deal_id"] == deal_id
    assert map_data["total_stakeholders"] == 2
    assert map_data["total_activities"] == 3
    assert len(map_data["stakeholders"]) == 2

    # Verify chronological descending feed (Act 3, then Act 2, then Act 1)
    activities = map_data["recent_activities"]
    assert len(activities) == 3
    assert activities[0]["summary"] == "Follow-up phone call regarding timeline."
    assert activities[0]["stakeholder_name"] == "Jane Doe"
    assert activities[1]["summary"] == "Procurement sync meeting."
    assert activities[1]["stakeholder_name"] == "Bob Smith"
    assert activities[2]["summary"] == "Sent proposal draft email."
    assert activities[2]["stakeholder_name"] == "Jane Doe"


def test_stakeholders_error_handling_and_validation(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Test error handling and boundary input validation for stakeholder endpoints [FR-006, FR-007, NFR-003]."""
    res = client.post(
        f"{settings.API_V1_STR}/deals",
        json={"title": "Stakeholder Validation Deal", "deal_value": 30000.0, "milestone_stage": "Qualification"},
        headers=auth_headers,
    )
    deal_id = res.json()["id"]
    non_existent_deal_id = str(uuid.uuid4())
    non_existent_stk_id = str(uuid.uuid4())

    # 1. Get stakeholder map for non-existent deal -> 404
    res_map_404 = client.get(
        f"{settings.API_V1_STR}/deals/{non_existent_deal_id}/stakeholders",
        headers=auth_headers,
    )
    assert res_map_404.status_code == 404

    # 2. Create stakeholder on non-existent deal -> 404
    res_create_404 = client.post(
        f"{settings.API_V1_STR}/deals/{non_existent_deal_id}/stakeholders",
        json={
            "full_name": "Ghost User",
            "email": "ghost@test.com",
            "org_role": "Advisor",
            "influence_level": "Low",
        },
        headers=auth_headers,
    )
    assert res_create_404.status_code == 404

    # 3. Create stakeholder with invalid influence level -> 422
    res_bad_inf = client.post(
        f"{settings.API_V1_STR}/deals/{deal_id}/stakeholders",
        json={
            "full_name": "User One",
            "email": "user1@test.com",
            "org_role": "Manager",
            "influence_level": "Critical",
        },
        headers=auth_headers,
    )
    assert res_bad_inf.status_code == 422

    # 4. Create stakeholder with invalid email format -> 422
    res_bad_email = client.post(
        f"{settings.API_V1_STR}/deals/{deal_id}/stakeholders",
        json={
            "full_name": "User Two",
            "email": "not-an-email",
            "org_role": "Manager",
            "influence_level": "Low",
        },
        headers=auth_headers,
    )
    assert res_bad_email.status_code == 422

    # 5. Create stakeholder with empty name -> 422
    res_empty_name = client.post(
        f"{settings.API_V1_STR}/deals/{deal_id}/stakeholders",
        json={
            "full_name": "   ",
            "email": "valid@test.com",
            "org_role": "Manager",
            "influence_level": "Low",
        },
        headers=auth_headers,
    )
    assert res_empty_name.status_code == 422

    # 6. Log activity for non-existent deal -> 404
    res_act_deal_404 = client.post(
        f"{settings.API_V1_STR}/deals/{non_existent_deal_id}/stakeholders/activities",
        json={
            "stakeholder_id": non_existent_stk_id,
            "activity_type": "Note",
            "summary": "Sample summary text.",
        },
        headers=auth_headers,
    )
    assert res_act_deal_404.status_code == 404

    # 7. Log activity for non-existent stakeholder on existing deal -> 404
    res_act_stk_404 = client.post(
        f"{settings.API_V1_STR}/deals/{deal_id}/stakeholders/activities",
        json={
            "stakeholder_id": non_existent_stk_id,
            "activity_type": "Note",
            "summary": "Sample summary text.",
        },
        headers=auth_headers,
    )
    assert res_act_stk_404.status_code == 404

    # 8. Mismatched stakeholder (stakeholder belongs to Deal A, activity logged under Deal B) -> 422
    res_deal_b = client.post(
        f"{settings.API_V1_STR}/deals",
        json={"title": "Deal B", "deal_value": 10000.0, "milestone_stage": "Proposal"},
        headers=auth_headers,
    )
    deal_b_id = res_deal_b.json()["id"]

    stk_a = client.post(
        f"{settings.API_V1_STR}/deals/{deal_id}/stakeholders",
        json={
            "full_name": "Alice Wonderland",
            "email": "alice@wonder.com",
            "org_role": "Director",
            "influence_level": "High",
        },
        headers=auth_headers,
    ).json()
    stk_a_id = stk_a["id"]

    res_mismatch = client.post(
        f"{settings.API_V1_STR}/deals/{deal_b_id}/stakeholders/activities",
        json={
            "stakeholder_id": stk_a_id,
            "activity_type": "Call",
            "summary": "Mismatched deal activity attempt.",
        },
        headers=auth_headers,
    )
    assert res_mismatch.status_code == 422

    # 9. Test XSS / HTML sanitization on stakeholder input [NFR-003]
    res_xss = client.post(
        f"{settings.API_V1_STR}/deals/{deal_id}/stakeholders",
        json={
            "full_name": "  <script>alert('xss')</script> Charlie Chaplin  ",
            "email": "charlie@film.com",
            "org_role": "<b>Director</b>",
            "influence_level": "High",
        },
        headers=auth_headers,
    )
    assert res_xss.status_code == 201
    xss_data = res_xss.json()
    assert "<script>" not in xss_data["full_name"]
    assert "Charlie Chaplin" in xss_data["full_name"]
    assert "<b>" not in xss_data["org_role"]


def test_forecast_endpoint(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test retrieving and recalculating AI completion timeline forecast for a deal [FR-008, FR-009, NFR-003]."""
    res = client.post(
        f"{settings.API_V1_STR}/deals",
        json={"title": "Forecast Test Deal", "deal_value": 120000.0, "milestone_stage": "Proposal"},
        headers=auth_headers,
    )
    deal_id = res.json()["id"]

    # 1. GET forecast -> 200 OK
    f_res = client.get(
        f"{settings.API_V1_STR}/deals/{deal_id}/forecast",
        headers=auth_headers,
    )
    assert f_res.status_code == 200
    f_data = f_res.json()
    assert "status_code" in f_data
    assert f_data["deal_id"] == deal_id
    assert f_data["status_code"] in ("READY", "INSUFFICIENT_DATA", "COMPUTING")
    assert "confidence_score_pct" in f_data
    assert "estimated_completion_date" in f_data
    assert "calculated_on" in f_data

    # 2. POST recalculate forecast -> 200 OK
    recalc_res = client.post(
        f"{settings.API_V1_STR}/deals/{deal_id}/forecast/recalculate",
        headers=auth_headers,
    )
    assert recalc_res.status_code == 200
    recalc_data = recalc_res.json()
    assert recalc_data["deal_id"] == deal_id
    assert "status_code" in recalc_data

    # 3. POST forecast route alias -> 200 OK
    post_f_res = client.post(
        f"{settings.API_V1_STR}/deals/{deal_id}/forecast",
        headers=auth_headers,
    )
    assert post_f_res.status_code == 200
    assert post_f_res.json()["deal_id"] == deal_id

    # 4. Non-existent deal ID forecast query -> 404
    non_existent_id = str(uuid.uuid4())
    res_404 = client.get(
        f"{settings.API_V1_STR}/deals/{non_existent_id}/forecast",
        headers=auth_headers,
    )
    assert res_404.status_code == 404

    # 5. Invalid UUID deal ID forecast query -> 422
    res_422 = client.get(
        f"{settings.API_V1_STR}/deals/not-a-uuid/forecast",
        headers=auth_headers,
    )
    assert res_422.status_code == 422


def test_pydantic_input_validation_errors(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test boundary validation rejecting invalid payloads [NFR-003]."""
    # Negative deal value
    res = client.post(
        f"{settings.API_V1_STR}/deals",
        json={"title": "Invalid Deal", "deal_value": -50.0, "milestone_stage": "Proposal"},
        headers=auth_headers,
    )
    assert res.status_code == 422

    # Win probability out of bounds
    res2 = client.post(
        f"{settings.API_V1_STR}/deals",
        json={
            "title": "Invalid Deal",
            "deal_value": 100.0,
            "milestone_stage": "Proposal",
            "win_probability": 1.5,
        },
        headers=auth_headers,
    )
    assert res2.status_code == 422
