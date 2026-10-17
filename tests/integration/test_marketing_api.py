"""Integration tests for GET /api/v1/marketing/calendar/meetings endpoint (FR-009, NFR-003, NFR-004)."""

from datetime import datetime, timedelta, timezone
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.api.deps import create_access_token
from src.api.main import app
from src.models.database import Base, get_db
from src.models.marketing import FollowupTask, LeadMeeting
from src.models.role import Role, UserRole
from src.models.tenant import Tenant
from src.models.user import User
from src.services.access_control.rbac import RoleName
from src.services.security import get_password_hash


@pytest.fixture
def db_session() -> Session:
    """In-memory SQLite database session for marketing endpoint integration testing."""
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
    tenant = Tenant(name="Marketing Test Tenant")
    db_session.add(tenant)
    db_session.commit()

    hashed_pwd = get_password_hash("MarketingSecret123!")
    user = User(
        email="marketer@test.com",
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


def test_get_calendar_meetings_unauthenticated(client: TestClient) -> None:
    """Test requesting calendar meetings without auth header returns 401 Unauthorized."""
    response = client.get("/api/v1/marketing/calendar/meetings")
    assert response.status_code == 401


def test_get_calendar_meetings_empty(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Test requesting calendar meetings when database is empty returns 200 OK with empty list."""
    response = client.get("/api/v1/marketing/calendar/meetings", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == []


def test_get_calendar_meetings_success(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test retrieving calendar meetings with prep notes and linked tasks (FR-009)."""
    lead_id = uuid.uuid4()
    assigned_user_id = uuid.uuid4()
    now_utc = datetime.now(timezone.utc)

    meeting = LeadMeeting(
        lead_id=lead_id,
        scheduled_time=now_utc + timedelta(days=1),
        prep_notes="Review enterprise solution deck <script>alert(1)</script>",
        meeting_notes="Discussed quarterly goals.",
    )
    db_session.add(meeting)
    db_session.commit()
    db_session.refresh(meeting)

    task = FollowupTask(
        meeting_id=meeting.id,
        lead_id=lead_id,
        assigned_user_id=assigned_user_id,
        due_date_val=now_utc + timedelta(days=2),
        is_completed=False,
        task_description="Send updated pricing sheet & proposal",
    )
    db_session.add(task)
    db_session.commit()

    response = client.get("/api/v1/marketing/calendar/meetings", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    m = data[0]
    assert m["id"] == str(meeting.id)
    assert m["lead_id"] == str(lead_id)
    # Verify input sanitization removed <script> tags and escaped special characters
    assert "<script>" not in m["prep_notes"]
    assert "Review enterprise solution deck" in m["prep_notes"]
    assert m["meeting_notes"] == "Discussed quarterly goals."
    assert len(m["followup_tasks"]) == 1
    t = m["followup_tasks"][0]
    assert t["id"] == str(task.id)
    assert t["meeting_id"] == str(meeting.id)
    assert t["lead_id"] == str(lead_id)
    assert t["assigned_user_id"] == str(assigned_user_id)
    assert t["task_description"] == "Send updated pricing sheet &amp; proposal"


def test_get_calendar_meetings_filtering(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test filtering meetings by user_id, lead_id, and date range."""
    lead_1 = uuid.uuid4()
    lead_2 = uuid.uuid4()
    user_1 = uuid.uuid4()
    user_2 = uuid.uuid4()

    dt_1 = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    dt_2 = datetime(2026, 5, 20, 14, 0, tzinfo=timezone.utc)

    m1 = LeadMeeting(
        lead_id=lead_1,
        scheduled_time=dt_1,
        prep_notes="Meeting 1",
    )
    m2 = LeadMeeting(
        lead_id=lead_2,
        scheduled_time=dt_2,
        prep_notes="Meeting 2",
    )
    db_session.add_all([m1, m2])
    db_session.commit()

    t1 = FollowupTask(
        meeting_id=m1.id,
        lead_id=lead_1,
        assigned_user_id=user_1,
        task_description="Task for M1",
    )
    t2 = FollowupTask(
        meeting_id=m2.id,
        lead_id=lead_2,
        assigned_user_id=user_2,
        task_description="Task for M2",
    )
    db_session.add_all([t1, t2])
    db_session.commit()

    # Filter by user_1
    res = client.get(
        f"/api/v1/marketing/calendar/meetings?user_id={user_1}",
        headers=auth_headers,
    )
    assert res.status_code == 200
    assert len(res.json()) == 1
    assert res.json()[0]["id"] == str(m1.id)

    # Filter by lead_2
    res = client.get(
        f"/api/v1/marketing/calendar/meetings?lead_id={lead_2}",
        headers=auth_headers,
    )
    assert res.status_code == 200
    assert len(res.json()) == 1
    assert res.json()[0]["id"] == str(m2.id)

    # Filter by date range covering only dt_1
    start_str = "2026-05-01T00:00:00Z"
    end_str = "2026-05-15T23:59:59Z"
    res = client.get(
        f"/api/v1/marketing/calendar/meetings?start_date_val={start_str}&end_date_val={end_str}",
        headers=auth_headers,
    )
    assert res.status_code == 200
    assert len(res.json()) == 1
    assert res.json()[0]["id"] == str(m1.id)


def test_get_calendar_meetings_invalid_uuid_param(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Test passing invalid UUID parameter returns 400 Bad Request."""
    res = client.get(
        "/api/v1/marketing/calendar/meetings?user_id=not-a-valid-uuid",
        headers=auth_headers,
    )
    assert res.status_code == 400
    assert "Invalid UUID value format" in res.json()["detail"]


def test_post_meeting_notes_unauthenticated(client: TestClient) -> None:
    """Test updating meeting notes without auth returns 401 Unauthorized."""
    meeting_id = uuid.uuid4()
    response = client.post(
        f"/api/v1/marketing/calendar/meetings/{meeting_id}/notes",
        json={"meeting_notes": "Test notes"},
    )
    assert response.status_code == 401


def test_post_meeting_notes_success(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test logging meeting notes, updating lead status, and generating tasks [FR-010, NFR-004]."""
    lead_id = uuid.uuid4()
    assigned_user = uuid.uuid4()
    now_utc = datetime.now(timezone.utc)

    meeting = LeadMeeting(
        lead_id=lead_id,
        scheduled_time=now_utc + timedelta(hours=1),
        prep_notes="Initial discovery briefing.",
    )
    db_session.add(meeting)
    db_session.commit()

    due_dt = now_utc + timedelta(days=3)
    payload = {
        "meeting_notes": "Client requested proposal with pricing. <script>alert('xss')</script>",
        "prep_notes": "Updated prep briefing",
        "lead_status": "Qualified",
        "tasks": [
            {
                "task_description": "Send proposal deck &amp; quote",
                "assigned_user_id": str(assigned_user),
                "due_date_val": due_dt.isoformat(),
            },
            {
                "description": "Schedule technical review session",
                "assigned_user_id": str(assigned_user),
                "due_date": due_dt.isoformat(),
            },
        ],
    }

    res = client.post(
        f"/api/v1/marketing/calendar/meetings/{meeting.id}/notes",
        headers=auth_headers,
        json=payload,
    )
    assert res.status_code == 200
    data = res.json()

    assert data["id"] == str(meeting.id)
    assert data["lead_id"] == str(lead_id)
    assert "Client requested proposal with pricing." in data["meeting_notes"]
    assert "<script>" not in data["meeting_notes"]
    assert data["prep_notes"] == "Updated prep briefing"
    assert data["lead_status"] == "Qualified"
    assert len(data["followup_tasks"]) == 2

    task_descs = [t["task_description"] for t in data["followup_tasks"]]
    assert any("Send proposal deck" in desc for desc in task_descs)
    assert any("Schedule technical review" in desc for desc in task_descs)


def test_post_meeting_notes_not_found(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Test updating notes for a non-existent meeting returns 404 Not Found."""
    random_id = uuid.uuid4()
    res = client.post(
        f"/api/v1/marketing/calendar/meetings/{random_id}/notes",
        headers=auth_headers,
        json={"meeting_notes": "Some notes"},
    )
    assert res.status_code == 404
    assert f"Lead meeting with ID '{random_id}' not found." in res.json()["detail"]


def test_post_meeting_notes_invalid_task_data(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Test updating notes with invalid task payload returns 400 Bad Request."""
    lead_id = uuid.uuid4()
    meeting = LeadMeeting(
        lead_id=lead_id,
        scheduled_time=datetime.now(timezone.utc),
    )
    db_session.add(meeting)
    db_session.commit()

    # Empty task description
    res = client.post(
        f"/api/v1/marketing/calendar/meetings/{meeting.id}/notes",
        headers=auth_headers,
        json={"tasks": [{"task_description": "   "}]},
    )
    assert res.status_code == 400
    assert "Follow-up task description must be a non-empty string." in res.json()["detail"]
