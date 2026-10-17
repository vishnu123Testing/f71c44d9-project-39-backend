"""Integration tests for FastAPI financial transparency endpoints (quotes, invoices, payments, AR aging).

Verifies RBAC enforcement, input sanitization, and endpoint contracts [FR-1, FR-2, FR-3, FR-4, FR-9, FR-10].
"""

from datetime import datetime, timedelta, timezone
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.api.deps import create_access_token
from src.api.main import app
from src.models.database import Base, get_db
from src.models.role import Role, UserRole
from src.models.tenant import Tenant
from src.models.user import User
from src.services.access_control.rbac import RoleName
from src.services.security import get_password_hash


@pytest.fixture
def db_session() -> Session:
    """In-memory SQLite database session for financial API integration testing."""
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
    tenant = Tenant(name="Financial API Test Tenant")
    db_session.add(tenant)
    db_session.commit()

    hashed_pwd = get_password_hash("SecretPassword123!")
    user = User(
        email="financial_user@test.com",
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


# ---------------------------------------------------------------------------
# Unauthenticated and RBAC Tests
# ---------------------------------------------------------------------------


def test_unauthenticated_requests_denied(client: TestClient) -> None:
    """Verify that endpoints reject requests missing Bearer tokens with HTTP 401 [FR-9]."""
    res_quotes = client.get("/api/v1/financial/quotes")
    assert res_quotes.status_code == 401

    res_invoices = client.get("/api/v1/financial/invoices")
    assert res_invoices.status_code == 401

    res_payments = client.get("/api/v1/financial/payments")
    assert res_payments.status_code == 401

    res_ar = client.get("/api/v1/financial/ar-aging")
    assert res_ar.status_code == 401


# ---------------------------------------------------------------------------
# Quotes Endpoint Tests (FR-1)
# ---------------------------------------------------------------------------


def test_quotes_crud_and_filtering(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test creating, fetching, listing, and filtering quotes [FR-1, FR-9, FR-10]."""
    customer_a = str(uuid.uuid4())
    customer_b = str(uuid.uuid4())
    exp_dt = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()

    # Create quote 1 for Customer A
    payload1 = {
        "customer_id": customer_a,
        "expiration_date_time": exp_dt,
        "total_amount": 1250.50,
        "approval_state": "Draft",
    }
    res1 = client.post("/api/v1/financial/quotes", json=payload1, headers=auth_headers)
    assert res1.status_code == 201
    data1 = res1.json()
    assert data1["customer_id"] == customer_a
    assert data1["total_amount"] == 1250.50
    assert data1["approval_state"] == "Draft"
    assert not data1["is_expired"]
    quote_id_1 = data1["quote_id"]

    # Create quote 2 for Customer B
    payload2 = {
        "customer_id": customer_b,
        "expiration_date_time": exp_dt,
        "total_amount": 5000.00,
        "approval_state": "Approved",
    }
    res2 = client.post("/api/v1/financial/quotes", json=payload2, headers=auth_headers)
    assert res2.status_code == 201

    # Get single quote
    res_single = client.get(f"/api/v1/financial/quotes/{quote_id_1}", headers=auth_headers)
    assert res_single.status_code == 200
    assert res_single.json()["quote_id"] == quote_id_1

    # Get non-existent quote
    res_not_found = client.get(f"/api/v1/financial/quotes/{uuid.uuid4()}", headers=auth_headers)
    assert res_not_found.status_code == 404

    # List all quotes
    res_list = client.get("/api/v1/financial/quotes", headers=auth_headers)
    assert res_list.status_code == 200
    assert len(res_list.json()) >= 2

    # Filter quotes by customer_id
    res_filter_cust = client.get(
        f"/api/v1/financial/quotes?customer_id={customer_a}", headers=auth_headers
    )
    assert res_filter_cust.status_code == 200
    quotes_cust = res_filter_cust.json()
    assert len(quotes_cust) == 1
    assert quotes_cust[0]["quote_id"] == quote_id_1

    # Filter quotes by approval_state
    res_filter_state = client.get(
        "/api/v1/financial/quotes?approval_state=Approved", headers=auth_headers
    )
    assert res_filter_state.status_code == 200
    quotes_approved = res_filter_state.json()
    assert all(q["approval_state"] == "Approved" for q in quotes_approved)


# ---------------------------------------------------------------------------
# Invoices Endpoint Tests (FR-2)
# ---------------------------------------------------------------------------


def test_invoices_crud_and_filtering(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test creating, fetching, listing, and filtering invoices [FR-2, FR-9, FR-10]."""
    customer_id = str(uuid.uuid4())
    now_utc = datetime.now(timezone.utc)
    issue_dt = now_utc.isoformat()
    due_dt = (now_utc + timedelta(days=30)).isoformat()

    payload = {
        "customer_id": customer_id,
        "issue_date_time": issue_dt,
        "due_date_time": due_dt,
        "total_amount": 3000.00,
        "status": "Current",
    }
    res = client.post("/api/v1/financial/invoices", json=payload, headers=auth_headers)
    assert res.status_code == 201
    data = res.json()
    assert data["customer_id"] == customer_id
    assert data["total_amount"] == 3000.00
    assert data["outstanding_amount"] == 3000.00
    assert data["status"] == "Current"
    assert data["days_overdue"] == 0
    invoice_id = data["invoice_id"]

    # Get single invoice
    res_get = client.get(f"/api/v1/financial/invoices/{invoice_id}", headers=auth_headers)
    assert res_get.status_code == 200
    assert res_get.json()["invoice_id"] == invoice_id

    # Get non-existent invoice
    res_404 = client.get(f"/api/v1/financial/invoices/{uuid.uuid4()}", headers=auth_headers)
    assert res_404.status_code == 404

    # List invoices filtered by status
    res_list = client.get(
        f"/api/v1/financial/invoices?status=Current&customer_id={customer_id}",
        headers=auth_headers,
    )
    assert res_list.status_code == 200
    items = res_list.json()
    assert len(items) == 1
    assert items[0]["invoice_id"] == invoice_id


# ---------------------------------------------------------------------------
# Payments Endpoint Tests (FR-3)
# ---------------------------------------------------------------------------


def test_payments_recording_and_history(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test recording a payment and listing payment history linked to an invoice [FR-3]."""
    customer_id = str(uuid.uuid4())
    now_utc = datetime.now(timezone.utc)

    # Create an invoice for $1,000
    inv_payload = {
        "customer_id": customer_id,
        "issue_date_time": now_utc.isoformat(),
        "due_date_time": (now_utc + timedelta(days=15)).isoformat(),
        "total_amount": 1000.00,
        "status": "Current",
    }
    res_inv = client.post("/api/v1/financial/invoices", json=inv_payload, headers=auth_headers)
    assert res_inv.status_code == 201
    invoice_id = res_inv.json()["invoice_id"]

    # Record partial payment of $400
    pay_payload1 = {
        "invoice_id": invoice_id,
        "amount_paid": 400.00,
        "payment_method": "Wire Transfer",
    }
    res_p1 = client.post("/api/v1/financial/payments", json=pay_payload1, headers=auth_headers)
    assert res_p1.status_code == 201
    p1_data = res_p1.json()
    assert p1_data["amount_paid"] == 400.00
    assert p1_data["payment_method"] == "Wire Transfer"

    # Verify updated invoice outstanding amount is now $600
    res_inv_updated = client.get(f"/api/v1/financial/invoices/{invoice_id}", headers=auth_headers)
    assert res_inv_updated.json()["outstanding_amount"] == 600.00
    assert res_inv_updated.json()["status"] == "Current"

    # Record remaining payment of $600
    pay_payload2 = {
        "invoice_id": invoice_id,
        "amount_paid": 600.00,
        "payment_method": "Credit Card",
    }
    res_p2 = client.post("/api/v1/financial/payments", json=pay_payload2, headers=auth_headers)
    assert res_p2.status_code == 201

    # Verify invoice is now Paid and outstanding amount is $0
    res_inv_paid = client.get(f"/api/v1/financial/invoices/{invoice_id}", headers=auth_headers)
    assert res_inv_paid.json()["outstanding_amount"] == 0.00
    assert res_inv_paid.json()["status"] == "Paid"

    # List payment history for this invoice
    res_history = client.get(
        f"/api/v1/financial/payments?invoice_id={invoice_id}", headers=auth_headers
    )
    assert res_history.status_code == 200
    history = res_history.json()
    assert len(history) == 2


def test_payment_validation_errors(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test validation errors when recording invalid payments [FR-10]."""
    # Non-existent invoice
    res_404 = client.post(
        "/api/v1/financial/payments",
        json={
            "invoice_id": str(uuid.uuid4()),
            "amount_paid": 100.00,
            "payment_method": "Credit Card",
        },
        headers=auth_headers,
    )
    assert res_404.status_code == 404

    # Negative or zero payment amount
    res_invalid_amt = client.post(
        "/api/v1/financial/payments",
        json={
            "invoice_id": str(uuid.uuid4()),
            "amount_paid": 0.00,
            "payment_method": "Credit Card",
        },
        headers=auth_headers,
    )
    assert res_invalid_amt.status_code == 422


# ---------------------------------------------------------------------------
# AR Aging Endpoint Tests (FR-4)
# ---------------------------------------------------------------------------


def test_ar_aging_empty_state(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test AR aging summary returns zero buckets when no open invoices exist [FR-4]."""
    customer_id = str(uuid.uuid4())
    res = client.get(f"/api/v1/financial/ar-aging?customer_id={customer_id}", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["total_outstanding"] == 0.0
    assert data["total_invoices"] == 0
    assert data["bucket_0_30"]["total_amount"] == 0.0
    assert data["bucket_0_30"]["invoice_count"] == 0
    assert data["bucket_31_60"]["total_amount"] == 0.0
    assert data["bucket_61_90"]["total_amount"] == 0.0
    assert data["bucket_90_plus"]["total_amount"] == 0.0


def test_ar_aging_duration_buckets(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test AR aging duration buckets categorization across 0-30, 31-60, 61-90, 90+ days [FR-4]."""
    customer_id = str(uuid.uuid4())
    now_utc = datetime.now(timezone.utc)

    # Invoices in different overdue buckets
    # 1. Bucket 0-30: due 10 days ago ($1,000)
    client.post(
        "/api/v1/financial/invoices",
        json={
            "customer_id": customer_id,
            "issue_date_time": (now_utc - timedelta(days=40)).isoformat(),
            "due_date_time": (now_utc - timedelta(days=10)).isoformat(),
            "total_amount": 1000.00,
            "status": "Past_Due",
        },
        headers=auth_headers,
    )

    # 2. Bucket 31-60: due 45 days ago ($2,000)
    client.post(
        "/api/v1/financial/invoices",
        json={
            "customer_id": customer_id,
            "issue_date_time": (now_utc - timedelta(days=75)).isoformat(),
            "due_date_time": (now_utc - timedelta(days=45)).isoformat(),
            "total_amount": 2000.00,
            "status": "Past_Due",
        },
        headers=auth_headers,
    )

    # 3. Bucket 61-90: due 75 days ago ($3,000)
    client.post(
        "/api/v1/financial/invoices",
        json={
            "customer_id": customer_id,
            "issue_date_time": (now_utc - timedelta(days=105)).isoformat(),
            "due_date_time": (now_utc - timedelta(days=75)).isoformat(),
            "total_amount": 3000.00,
            "status": "Past_Due",
        },
        headers=auth_headers,
    )

    # 4. Bucket 90+: due 120 days ago ($4,000)
    client.post(
        "/api/v1/financial/invoices",
        json={
            "customer_id": customer_id,
            "issue_date_time": (now_utc - timedelta(days=150)).isoformat(),
            "due_date_time": (now_utc - timedelta(days=120)).isoformat(),
            "total_amount": 4000.00,
            "status": "Past_Due",
        },
        headers=auth_headers,
    )

    # Fetch AR aging summary
    res = client.get(
        f"/api/v1/financial/ar-aging?customer_id={customer_id}",
        headers=auth_headers,
    )
    assert res.status_code == 200
    summary = res.json()
    assert summary["total_outstanding"] == 10000.00
    assert summary["total_invoices"] == 4

    assert summary["bucket_0_30"]["total_amount"] == 1000.00
    assert summary["bucket_0_30"]["invoice_count"] == 1

    assert summary["bucket_31_60"]["total_amount"] == 2000.00
    assert summary["bucket_31_60"]["invoice_count"] == 1

    assert summary["bucket_61_90"]["total_amount"] == 3000.00
    assert summary["bucket_61_90"]["invoice_count"] == 1

    assert summary["bucket_90_plus"]["total_amount"] == 4000.00
    assert summary["bucket_90_plus"]["invoice_count"] == 1


# ---------------------------------------------------------------------------
# Input Sanitization Tests (FR-10)
# ---------------------------------------------------------------------------


def test_input_sanitization_on_payloads(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Test that input strings containing script or HTML markup are sanitized [FR-10]."""
    customer_id = str(uuid.uuid4())
    now_utc = datetime.now(timezone.utc)

    payload = {
        "customer_id": customer_id,
        "expiration_date_time": (now_utc + timedelta(days=7)).isoformat(),
        "total_amount": 500.00,
        "approval_state": "<script>alert('xss')</script>Approved",
    }
    res = client.post("/api/v1/financial/quotes", json=payload, headers=auth_headers)
    assert res.status_code == 201
    data = res.json()
    # Verify script tag removed
    assert "<script>" not in data["approval_state"]
