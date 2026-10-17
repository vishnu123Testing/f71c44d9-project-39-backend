"""Integration tests for FastAPI routes in `backend/src/api/contracts.py` [FR-1, FR-2, FR-3, FR-5, FR-6, FR-7, FR-8, FR-10, FR-11, NFR-1, NFR-2]."""

import io
import tempfile
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.api.contracts import get_storage_connector, router as contracts_router
from src.api.deps import create_access_token
from src.api.main import app
from src.connectors.storage_connector import LocalStorageConnector
from src.lib.config import settings
from src.models.database import Base, get_db
from src.models.role import Role, UserRole
from src.models.tenant import Tenant
from src.models.user import User
from src.services.access_control.rbac import RoleName
from src.services.security import get_password_hash


@pytest.fixture
def db_session() -> Session:
    """In-memory SQLite database session for contracts API integration testing."""
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
def temp_storage():
    """Temporary storage directory for integration file upload testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield LocalStorageConnector(storage_root=temp_dir, enable_encryption=True)


@pytest.fixture
def client(db_session: Session, temp_storage: LocalStorageConnector) -> TestClient:
    """TestClient instance with overridden database session and storage connector dependencies."""

    def _override_get_db() -> Session:
        return db_session

    def _override_get_storage() -> LocalStorageConnector:
        return temp_storage

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_storage_connector] = _override_get_storage

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture
def test_user(db_session: Session) -> tuple[Tenant, User, str]:
    """Seed database with tenant, user, role, and generate valid Bearer token."""
    tenant = Tenant(name="Contract API Test Tenant")
    db_session.add(tenant)
    db_session.commit()

    hashed_pwd = get_password_hash("ContractSecret123!")
    user = User(
        email="legal.officer@test.com",
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

    return tenant, user, token


def test_create_and_get_contract(client: TestClient, test_user: tuple[Tenant, User, str]) -> None:
    """Test creating a contract and fetching detail view [FR-1, FR-3]."""
    _, user, token = test_user
    headers = {"Authorization": f"Bearer {token}"}

    payload = {
        "title": "Master Services Agreement 2026",
        "category": "Contract",
        "counterparty": "Enterprise Solutions LLC",
        "start_on": "2026-01-01",
        "expires_on": "2027-01-01",
        "clauses": [
            {
                "clause_type": "Indemnification",
                "title": "Mutual Indemnity",
                "body_text": "Each party agrees to indemnify...",
                "is_highlighted": True,
            }
        ],
        "slas": [
            {
                "metric_name": "Uptime",
                "target_value": "99.9%",
                "penalty_terms": "Credit",
            }
        ],
    }

    create_resp = client.post(f"{settings.API_V1_STR}/contracts", json=payload, headers=headers)
    assert create_resp.status_code == 201, create_resp.text
    data = create_resp.json()

    assert "id" in data
    contract_id = data["id"]
    assert data["title"] == "Master Services Agreement 2026"
    assert data["category"] == "Contract"
    assert data["status"] == "DRAFT"
    assert len(data["clauses"]) == 1
    assert len(data["highlighted_clauses"]) == 1
    assert len(data["slas"]) == 1

    # Fetch contract detail
    get_resp = client.get(f"{settings.API_V1_STR}/contracts/{contract_id}", headers=headers)
    assert get_resp.status_code == 200
    detail = get_resp.json()
    assert detail["id"] == contract_id
    assert detail["counterparty"] == "Enterprise Solutions LLC"


def test_list_and_search_contracts(client: TestClient, test_user: tuple[Tenant, User, str]) -> None:
    """Test filtering and searching contract repository [FR-11]."""
    _, user, token = test_user
    headers = {"Authorization": f"Bearer {token}"}

    # Create two contracts
    client.post(
        f"{settings.API_V1_STR}/contracts",
        json={"title": "Vendor SOW A", "category": "SOW", "counterparty": "Vendor A"},
        headers=headers,
    )
    client.post(
        f"{settings.API_V1_STR}/contracts",
        json={"title": "Non-Disclosure Agreement B", "category": "NDA", "counterparty": "Partner B"},
        headers=headers,
    )

    # Filter by category SOW
    resp = client.get(f"{settings.API_V1_STR}/contracts?category=SOW", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] == 1
    assert data["items"][0]["title"] == "Vendor SOW A"

    # Search query
    resp2 = client.get(f"{settings.API_V1_STR}/contracts?search_query=Partner", headers=headers)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["total_count"] == 1
    assert data2["items"][0]["title"] == "Non-Disclosure Agreement B"


def test_document_upload_and_download(client: TestClient, test_user: tuple[Tenant, User, str]) -> None:
    """Test uploading document versions and downloading file payloads [FR-3, FR-4]."""
    _, user, token = test_user
    headers = {"Authorization": f"Bearer {token}"}

    # Create contract
    c_resp = client.post(
        f"{settings.API_V1_STR}/contracts",
        json={"title": "SOW Project Alpha", "category": "SOW", "counterparty": "Tech Corp"},
        headers=headers,
    )
    contract_id = c_resp.json()["id"]

    # Upload document version 1
    file_bytes = b"Hello, this is version 1 contract content payload."
    files = {"file": ("sow_v1.pdf", io.BytesIO(file_bytes), "application/pdf")}
    data = {"category_type": "SOW"}

    up_resp = client.post(
        f"{settings.API_V1_STR}/contracts/{contract_id}/documents",
        files=files,
        data=data,
        headers=headers,
    )
    assert up_resp.status_code == 201, up_resp.text
    doc_data = up_resp.json()
    assert doc_data["version_number"] == 1
    version_id = doc_data["id"]

    # Download document version payload
    dl_resp = client.get(
        f"{settings.API_V1_STR}/contracts/{contract_id}/documents/{version_id}",
        headers=headers,
    )
    assert dl_resp.status_code == 200
    assert dl_resp.content == file_bytes


def test_document_upload_validation_errors(client: TestClient, test_user: tuple[Tenant, User, str]) -> None:
    """Test input validation and file extension checks during upload [NFR-2]."""
    _, user, token = test_user
    headers = {"Authorization": f"Bearer {token}"}

    c_resp = client.post(
        f"{settings.API_V1_STR}/contracts",
        json={"title": "NDA Validation Test", "category": "NDA", "counterparty": "Partner X"},
        headers=headers,
    )
    contract_id = c_resp.json()["id"]

    # Invalid category
    files = {"file": ("test.pdf", io.BytesIO(b"content"), "application/pdf")}
    bad_cat_resp = client.post(
        f"{settings.API_V1_STR}/contracts/{contract_id}/documents",
        files=files,
        data={"category_type": "InvalidCategory"},
        headers=headers,
    )
    assert bad_cat_resp.status_code == 400

    # Disallowed file extension
    files_exe = {"file": ("malicious.exe", io.BytesIO(b"binary"), "application/x-msdownload")}
    bad_ext_resp = client.post(
        f"{settings.API_V1_STR}/contracts/{contract_id}/documents",
        files=files_exe,
        data={"category_type": "NDA"},
        headers=headers,
    )
    assert bad_ext_resp.status_code == 400


def test_amendments_append_only_and_immutability(client: TestClient, test_user: tuple[Tenant, User, str]) -> None:
    """Test adding amendments and blocking PUT/DELETE modifications [FR-7, FR-8, NFR-4]."""
    _, user, token = test_user
    headers = {"Authorization": f"Bearer {token}"}

    c_resp = client.post(
        f"{settings.API_V1_STR}/contracts",
        json={"title": "Agreement to Amend", "category": "Contract", "counterparty": "Global Inc"},
        headers=headers,
    )
    contract_id = c_resp.json()["id"]

    # Append amendment
    a_resp = client.post(
        f"{settings.API_V1_STR}/contracts/{contract_id}/amendments",
        json={"summary": "Extended payment terms from Net 30 to Net 60."},
        headers=headers,
    )
    assert a_resp.status_code == 201
    amendment_data = a_resp.json()
    assert amendment_data["sequence_number"] == 1
    amendment_id = amendment_data["id"]

    # List amendments
    list_resp = client.get(f"{settings.API_V1_STR}/contracts/{contract_id}/amendments", headers=headers)
    assert list_resp.status_code == 200
    assert list_resp.json()["total_count"] == 1

    # Attempt PUT modification -> 403 Forbidden
    put_resp = client.put(
        f"{settings.API_V1_STR}/contracts/{contract_id}/amendments/{amendment_id}",
        json={"summary": "Modified summary"},
        headers=headers,
    )
    assert put_resp.status_code == 403

    # Attempt DELETE modification -> 403 Forbidden
    del_resp = client.delete(
        f"{settings.API_V1_STR}/contracts/{contract_id}/amendments/{amendment_id}",
        headers=headers,
    )
    assert del_resp.status_code == 403


def test_e_signature_workflow(client: TestClient, test_user: tuple[Tenant, User, str]) -> None:
    """Test requesting signature and executing e-signature workflow [FR-5, FR-6]."""
    _, user, token = test_user
    headers = {"Authorization": f"Bearer {token}"}

    c_resp = client.post(
        f"{settings.API_V1_STR}/contracts",
        json={"title": "Contract Pending Signature", "category": "Contract", "counterparty": "Signer Inc"},
        headers=headers,
    )
    contract_id = c_resp.json()["id"]

    # Request signature
    sig_req_resp = client.post(
        f"{settings.API_V1_STR}/contracts/{contract_id}/signatures/request",
        json={"signatory_id": str(user.user_id)},
        headers=headers,
    )
    assert sig_req_resp.status_code == 201
    sig_req_id = sig_req_resp.json()["id"]

    # Check pending signatures list
    pending_resp = client.get(f"{settings.API_V1_STR}/signatures/pending", headers=headers)
    assert pending_resp.status_code == 200
    assert pending_resp.json()["total_count"] >= 1

    # Execute signature
    exec_resp = client.post(
        f"{settings.API_V1_STR}/signatures/{sig_req_id}/execute",
        json={"signature_digest": "sha256_crypto_signature_digest_xyz123"},
        headers=headers,
    )
    assert exec_resp.status_code == 200
    assert exec_resp.json()["status"] == "EXECUTED"

    # Verify contract status transitioned to ACTIVE
    get_c = client.get(f"{settings.API_V1_STR}/contracts/{contract_id}", headers=headers)
    assert get_c.json()["status"] == "ACTIVE"


def test_delete_contract_cleanup(client: TestClient, test_user: tuple[Tenant, User, str]) -> None:
    """Test DELETE /contracts/{id} yields HTTP 204 No Content with empty response body [Architecture §5]."""
    _, user, token = test_user
    headers = {"Authorization": f"Bearer {token}"}

    c_resp = client.post(
        f"{settings.API_V1_STR}/contracts",
        json={"title": "Contract To Delete", "category": "Contract", "counterparty": "Disposable Co"},
        headers=headers,
    )
    contract_id = c_resp.json()["id"]

    # Delete contract
    del_resp = client.delete(f"{settings.API_V1_STR}/contracts/{contract_id}", headers=headers)
    assert del_resp.status_code == 204
    assert len(del_resp.content) == 0

    # Verify GET returns 404
    get_resp = client.get(f"{settings.API_V1_STR}/contracts/{contract_id}", headers=headers)
    assert get_resp.status_code == 404
