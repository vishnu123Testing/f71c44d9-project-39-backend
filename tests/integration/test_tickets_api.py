"""Integration tests for FastAPI support ticket CRUD and filtering endpoints."""

from datetime import datetime, timezone
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
    """In-memory SQLite database session for ticket API integration testing."""
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
    tenant = Tenant(name="Tickets API Test Tenant")
    db_session.add(tenant)
    db_session.commit()

    hashed_pwd = get_password_hash("SecretPassword123!")
    user = User(
        email="ticket_user@test.com",
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


def test_create_and_get_ticket(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test creating a support ticket and retrieving details by ID [FR-1, FR-4, FR-7]."""
    payload = {
        "subject": "System Login Issue",
        "description": "Users are experiencing 500 errors when attempting SSO authentication.",
        "client_id": "client_acme_corp",
        "priority": "P1",
        "assigned_tier": "Tier 1",
    }
    response = client.post(
        f"{settings.API_V1_STR}/tickets",
        json=payload,
        headers=auth_headers,
    )
    assert response.status_code == 201
    data = response.json()
    assert data["subject"] == "System Login Issue"
    assert data["description"] == "Users are experiencing 500 errors when attempting SSO authentication."
    assert data["client_id"] == "client_acme_corp"
    assert data["priority"] == "P1"
    assert data["status"] == "open"
    assert data["assigned_tier"] == "Tier 1"
    assert "id" in data
    ticket_id = data["id"]

    # Verify SLA targets were initialized
    assert len(data["sla_trackings"]) == 2
    target_types = {tr["target_type"] for tr in data["sla_trackings"]}
    assert target_types == {"first_response", "resolution"}

    # Get single ticket by ID
    get_res = client.get(
        f"{settings.API_V1_STR}/tickets/{ticket_id}",
        headers=auth_headers,
    )
    assert get_res.status_code == 200
    get_data = get_res.json()
    assert get_data["id"] == ticket_id
    assert get_data["subject"] == "System Login Issue"
    assert len(get_data["messages"]) == 1
    assert get_data["messages"][0]["message_body"] == "Users are experiencing 500 errors when attempting SSO authentication."


def test_list_and_filter_tickets(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test listing and filtering support tickets by priority, status, client, and search query [FR-1]."""
    # Create 3 distinct tickets
    client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "Billing Invoice Query",
            "description": "Question regarding monthly subscription invoice line items.",
            "client_id": "client_alpha",
            "priority": "P3",
        },
        headers=auth_headers,
    )
    client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "Database Outage Alert",
            "description": "Primary PostgreSQL database cluster instance failover event.",
            "client_id": "client_beta",
            "priority": "P1",
        },
        headers=auth_headers,
    )
    client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "API Rate Limit Error",
            "description": "API requests throttled due to rate limit threshold breached.",
            "client_id": "client_alpha",
            "priority": "P2",
        },
        headers=auth_headers,
    )

    # 1. Unfiltered list
    res_all = client.get(
        f"{settings.API_V1_STR}/tickets",
        headers=auth_headers,
    )
    assert res_all.status_code == 200
    data_all = res_all.json()
    assert data_all["total_count"] == 3
    assert len(data_all["items"]) == 3

    # 2. Filter by client_id="client_alpha"
    res_client = client.get(
        f"{settings.API_V1_STR}/tickets?client_id=client_alpha",
        headers=auth_headers,
    )
    assert res_client.status_code == 200
    assert res_client.json()["total_count"] == 2

    # 3. Filter by priority="P1"
    res_p1 = client.get(
        f"{settings.API_V1_STR}/tickets?priority=P1",
        headers=auth_headers,
    )
    assert res_p1.status_code == 200
    assert res_p1.json()["total_count"] == 1
    assert res_p1.json()["items"][0]["subject"] == "Database Outage Alert"

    # 4. Search query filter
    res_search = client.get(
        f"{settings.API_V1_STR}/tickets?search=invoice",
        headers=auth_headers,
    )
    assert res_search.status_code == 200
    assert res_search.json()["total_count"] == 1
    assert res_search.json()["items"][0]["subject"] == "Billing Invoice Query"


def test_update_ticket_lifecycle(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test updating ticket status, priority, assignments, and verifying SLA recalculation [FR-1, FR-7]."""
    res = client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "Update Test Ticket",
            "description": "Initial ticket description text.",
            "client_id": "client_gamma",
            "priority": "P3",
        },
        headers=auth_headers,
    )
    ticket_id = res.json()["id"]

    # PATCH update status to "resolved" and tier to "Tier 2"
    patch_res = client.patch(
        f"{settings.API_V1_STR}/tickets/{ticket_id}",
        json={
            "status": "resolved",
            "assigned_tier": "Tier 2",
            "assigned_agent_id": "agent_99",
        },
        headers=auth_headers,
    )
    assert patch_res.status_code == 200
    updated_data = patch_res.json()
    assert updated_data["status"] == "resolved"
    assert updated_data["assigned_tier"] == "Tier 2"
    assert updated_data["assigned_agent_id"] == "agent_99"
    assert updated_data["resolved_at"] is not None

    # PUT update alias: update priority to "P1"
    put_res = client.put(
        f"{settings.API_V1_STR}/tickets/{ticket_id}",
        json={
            "priority": "P1",
        },
        headers=auth_headers,
    )
    assert put_res.status_code == 200
    assert put_res.json()["priority"] == "P1"


def test_delete_ticket_endpoint(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test deleting a ticket returning HTTP 204 No Content with empty body [FR-1, ARCHITECTURE.md §5]."""
    res = client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "Delete Test Ticket",
            "description": "To be deleted ticket description.",
            "client_id": "client_delta",
            "priority": "P2",
        },
        headers=auth_headers,
    )
    ticket_id = res.json()["id"]

    # Delete ticket -> HTTP 204 with empty body
    del_res = client.delete(
        f"{settings.API_V1_STR}/tickets/{ticket_id}",
        headers=auth_headers,
    )
    assert del_res.status_code == 204
    assert del_res.text == "" or del_res.content == b""

    # Verify ticket is deleted -> 404 Not Found
    get_res = client.get(
        f"{settings.API_V1_STR}/tickets/{ticket_id}",
        headers=auth_headers,
    )
    assert get_res.status_code == 404


def test_ticket_validation_and_errors(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test boundary validation errors and non-existent resource handlers [NFR-2, NFR-3]."""
    non_existent_id = str(uuid.uuid4())

    # 1. Create ticket with whitespace-only subject -> 422
    res_bad_sub = client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "   ",
            "description": "Valid description text",
            "client_id": "client_1",
        },
        headers=auth_headers,
    )
    assert res_bad_sub.status_code == 422

    # 2. Create ticket with invalid priority "P9" -> 422
    res_bad_prio = client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "Valid Subject",
            "description": "Valid description text",
            "client_id": "client_1",
            "priority": "P9",
        },
        headers=auth_headers,
    )
    assert res_bad_prio.status_code == 422

    # 3. GET non-existent ticket -> 404
    res_get_404 = client.get(
        f"{settings.API_V1_STR}/tickets/{non_existent_id}",
        headers=auth_headers,
    )
    assert res_get_404.status_code == 404

    # 4. PATCH non-existent ticket -> 404
    res_patch_404 = client.patch(
        f"{settings.API_V1_STR}/tickets/{non_existent_id}",
        json={"status": "resolved"},
        headers=auth_headers,
    )
    assert res_patch_404.status_code == 404

    # 5. DELETE non-existent ticket -> 404
    res_del_404 = client.delete(
        f"{settings.API_V1_STR}/tickets/{non_existent_id}",
        headers=auth_headers,
    )
    assert res_del_404.status_code == 404

    # 6. Test HTML / XSS sanitization on creation input [NFR-2]
    res_xss = client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "<script>alert('xss')</script> Safe Subject Title",
            "description": "<b>Clean Description</b>",
            "client_id": "client_xss",
        },
        headers=auth_headers,
    )
    assert res_xss.status_code == 201
    xss_data = res_xss.json()
    assert "<script>" not in xss_data["subject"]
    assert "Safe Subject Title" in xss_data["subject"]
    assert "<b>" not in xss_data["description"]


def test_post_message_and_get_thread(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test posting messages to a ticket thread and retrieving thread history [FR-2, FR-7]."""
    # Create a ticket
    res_ticket = client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "Thread Test Ticket",
            "description": "Initial ticket description.",
            "client_id": "client_thread_1",
            "priority": "P2",
        },
        headers=auth_headers,
    )
    assert res_ticket.status_code == 201
    ticket_id = res_ticket.json()["id"]

    # Post client message
    msg_res = client.post(
        f"{settings.API_V1_STR}/tickets/{ticket_id}/messages",
        json={
            "message_body": "Additional context regarding the issue.",
            "sender_type": "client",
        },
        headers=auth_headers,
    )
    assert msg_res.status_code == 201
    msg_data = msg_res.json()
    assert msg_data["ticket_id"] == ticket_id
    assert msg_data["message_body"] == "Additional context regarding the issue."
    assert msg_data["sender_type"] == "client"

    # Post agent response message
    agent_msg_res = client.post(
        f"{settings.API_V1_STR}/tickets/{ticket_id}/messages",
        json={
            "message_body": "We are looking into this immediately.",
            "sender_id": "agent_smith",
            "sender_type": "agent",
        },
        headers=auth_headers,
    )
    assert agent_msg_res.status_code == 201
    agent_msg_data = agent_msg_res.json()
    assert agent_msg_data["sender_type"] == "agent"
    assert agent_msg_data["sender_id"] == "agent_smith"

    # Verify ticket first_responded_at was updated on agent reply
    get_res = client.get(
        f"{settings.API_V1_STR}/tickets/{ticket_id}",
        headers=auth_headers,
    )
    assert get_res.status_code == 200
    ticket_detail = get_res.json()
    assert ticket_detail["first_responded_at"] is not None
    assert len(ticket_detail["messages"]) == 3  # initial description + client msg + agent msg

    # Retrieve message thread directly via messages endpoint
    thread_res = client.get(
        f"{settings.API_V1_STR}/tickets/{ticket_id}/messages",
        headers=auth_headers,
    )
    assert thread_res.status_code == 200
    messages = thread_res.json()
    assert len(messages) == 3
    assert messages[0]["message_body"] == "Initial ticket description."
    assert messages[1]["message_body"] == "Additional context regarding the issue."
    assert messages[2]["message_body"] == "We are looking into this immediately."


def test_message_posting_sanitization_and_validation(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test message input sanitization, empty body validation, and error cases [NFR-2]."""
    res_ticket = client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "Sanitization Test Ticket",
            "description": "Initial description.",
            "client_id": "client_sanitization",
        },
        headers=auth_headers,
    )
    ticket_id = res_ticket.json()["id"]

    # 1. Whitespace only message body -> 422
    bad_msg = client.post(
        f"{settings.API_V1_STR}/tickets/{ticket_id}/messages",
        json={"message_body": "    ", "sender_type": "client"},
        headers=auth_headers,
    )
    assert bad_msg.status_code == 422

    # 2. HTML / script tag sanitization -> tags removed
    xss_msg = client.post(
        f"{settings.API_V1_STR}/tickets/{ticket_id}/messages",
        json={
            "message_body": "<script>alert('hack')</script>Clean message body text",
            "sender_type": "client",
        },
        headers=auth_headers,
    )
    assert xss_msg.status_code == 201
    msg_data = xss_msg.json()
    assert "<script>" not in msg_data["message_body"]
    assert "Clean message body text" in msg_data["message_body"]

    # 3. Post message to non-existent ticket -> 404
    non_existent = str(uuid.uuid4())
    msg_404 = client.post(
        f"{settings.API_V1_STR}/tickets/{non_existent}/messages",
        json={"message_body": "Hello", "sender_type": "client"},
        headers=auth_headers,
    )
    assert msg_404.status_code == 404


def test_attachment_upload_and_download(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test file attachment upload and binary retrieval [FR-3, NFR-2]."""
    res_ticket = client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "Attachment Test Ticket",
            "description": "Initial description.",
            "client_id": "client_att",
        },
        headers=auth_headers,
    )
    ticket_id = res_ticket.json()["id"]

    # Upload attachment via POST /tickets/{ticket_id}/attachments
    file_content = b"Error log details: NullPointerException at line 42."
    upload_res = client.post(
        f"{settings.API_V1_STR}/tickets/{ticket_id}/attachments",
        files={"file": ("error_log.txt", file_content, "text/plain")},
        headers=auth_headers,
    )
    assert upload_res.status_code == 201
    att_data = upload_res.json()
    assert att_data["filename"] == "error_log.txt"
    assert att_data["mime_type"] == "text/plain"
    assert att_data["file_size_bytes"] == len(file_content)
    att_id = att_data["id"]

    # Download attachment content via GET /tickets/attachments/{attachment_id}
    download_res = client.get(
        f"{settings.API_V1_STR}/tickets/attachments/{att_id}",
        headers=auth_headers,
    )
    assert download_res.status_code == 200
    assert download_res.content == file_content
    assert download_res.headers["content-type"].startswith("text/plain")

    # Download attachment content via GET /tickets/{ticket_id}/attachments/{attachment_id}
    download_t_res = client.get(
        f"{settings.API_V1_STR}/tickets/{ticket_id}/attachments/{att_id}",
        headers=auth_headers,
    )
    assert download_t_res.status_code == 200
    assert download_t_res.content == file_content


def test_attachment_validation_failures(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test boundary validation rules rejecting illegal extensions or missing files [FR-3, NFR-2]."""
    res_ticket = client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "Bad Attachment Ticket",
            "description": "Testing rejection.",
            "client_id": "client_bad_att",
        },
        headers=auth_headers,
    )
    ticket_id = res_ticket.json()["id"]

    # 1. Upload forbidden executable extension (.exe) -> 400 Bad Request
    bad_ext_res = client.post(
        f"{settings.API_V1_STR}/tickets/{ticket_id}/attachments",
        files={"file": ("payload.exe", b"MZ...", "application/x-msdownload")},
        headers=auth_headers,
    )
    assert bad_ext_res.status_code == 400
    assert "not permitted" in bad_ext_res.json()["detail"].lower()

    # 2. Download non-existent attachment ID -> 404
    non_existent = str(uuid.uuid4())
    get_404 = client.get(
        f"{settings.API_V1_STR}/tickets/attachments/{non_existent}",
        headers=auth_headers,
    )
    assert get_404.status_code == 404


def test_csat_rating_submission_and_retrieval(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test CSAT rating submission flow for resolved support tickets [FR-9]."""
    # 1. Create a support ticket
    res_ticket = client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "CSAT Test Ticket",
            "description": "Issue requiring resolution before CSAT.",
            "client_id": "client_csat_1",
            "priority": "P2",
        },
        headers=auth_headers,
    )
    assert res_ticket.status_code == 201
    ticket_id = res_ticket.json()["id"]

    # 2. Attempt CSAT submission while ticket is still open -> 409 Conflict
    csat_open_res = client.post(
        f"{settings.API_V1_STR}/tickets/{ticket_id}/csat",
        json={"score": 5, "feedback_text": "Great service!"},
        headers=auth_headers,
    )
    assert csat_open_res.status_code == 409
    assert "resolved or closed" in csat_open_res.json()["detail"].lower()

    # 3. Resolve ticket
    resolve_res = client.patch(
        f"{settings.API_V1_STR}/tickets/{ticket_id}",
        json={"status": "resolved"},
        headers=auth_headers,
    )
    assert resolve_res.status_code == 200
    assert resolve_res.json()["status"] == "resolved"

    # 4. Submit CSAT rating on resolved ticket -> 201 Created
    csat_res = client.post(
        f"{settings.API_V1_STR}/tickets/{ticket_id}/csat",
        json={
            "score": 5,
            "feedback_text": "<script>alert('xss')</script>Prompt resolution and friendly agent!",
        },
        headers=auth_headers,
    )
    assert csat_res.status_code == 201
    csat_data = csat_res.json()
    assert csat_data["ticket_id"] == ticket_id
    assert csat_data["score"] == 5
    assert "<script>" not in csat_data["feedback_text"]
    assert "Prompt resolution and friendly agent!" in csat_data["feedback_text"]

    # 5. Retrieve CSAT rating via GET endpoint -> 200 OK
    get_csat_res = client.get(
        f"{settings.API_V1_STR}/tickets/{ticket_id}/csat",
        headers=auth_headers,
    )
    assert get_csat_res.status_code == 200
    assert get_csat_res.json()["score"] == 5

    # 6. Verify ticket detail includes csat_rating object
    get_ticket_res = client.get(
        f"{settings.API_V1_STR}/tickets/{ticket_id}",
        headers=auth_headers,
    )
    assert get_ticket_res.status_code == 200
    ticket_detail = get_ticket_res.json()
    assert ticket_detail["csat_rating"] is not None
    assert ticket_detail["csat_rating"]["score"] == 5

    # 7. Duplicate CSAT submission on same ticket -> 409 Conflict
    dup_csat_res = client.post(
        f"{settings.API_V1_STR}/tickets/{ticket_id}/csat",
        json={"score": 4, "feedback_text": "Submitting again"},
        headers=auth_headers,
    )
    assert dup_csat_res.status_code == 409
    assert "already been submitted" in dup_csat_res.json()["detail"].lower()


def test_csat_rating_error_cases(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test error handling and validation for CSAT rating submission [FR-9, NFR-2]."""
    non_existent = str(uuid.uuid4())

    # 1. CSAT submission for non-existent ticket -> 404
    post_404 = client.post(
        f"{settings.API_V1_STR}/tickets/{non_existent}/csat",
        json={"score": 5},
        headers=auth_headers,
    )
    assert post_404.status_code == 404

    # 2. Get CSAT for non-existent ticket -> 404
    get_404 = client.get(
        f"{settings.API_V1_STR}/tickets/{non_existent}/csat",
        headers=auth_headers,
    )
    assert get_404.status_code == 404

    # 3. Create ticket and resolve it
    res_ticket = client.post(
        f"{settings.API_V1_STR}/tickets",
        json={
            "subject": "CSAT Validation Ticket",
            "description": "Testing CSAT score validation.",
            "client_id": "client_csat_2",
        },
        headers=auth_headers,
    )
    ticket_id = res_ticket.json()["id"]

    client.patch(
        f"{settings.API_V1_STR}/tickets/{ticket_id}",
        json={"status": "resolved"},
        headers=auth_headers,
    )

    # 4. Get CSAT before submission -> 404
    get_no_csat = client.get(
        f"{settings.API_V1_STR}/tickets/{ticket_id}/csat",
        headers=auth_headers,
    )
    assert get_no_csat.status_code == 404

    # 5. Invalid score < 1 -> 400 or 422
    bad_score_low = client.post(
        f"{settings.API_V1_STR}/tickets/{ticket_id}/csat",
        json={"score": 0},
        headers=auth_headers,
    )
    assert bad_score_low.status_code in (400, 422)

    # 6. Invalid score > 5 -> 400 or 422
    bad_score_high = client.post(
        f"{settings.API_V1_STR}/tickets/{ticket_id}/csat",
        json={"score": 6},
        headers=auth_headers,
    )
    assert bad_score_high.status_code in (400, 422)
