"""Unit tests for authentication endpoints in backend/src/api/v1/endpoints/auth.py."""

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
    """In-memory SQLite database session for endpoint testing."""
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
def test_user_data(db_session: Session) -> tuple[Tenant, User, Role]:
    """Fixture seeding database with a tenant, user, and ClientStandard role."""
    tenant = Tenant(name="Test Auth Tenant")
    db_session.add(tenant)
    db_session.commit()

    hashed_pwd = get_password_hash("Secret123!")
    user = User(
        email="testuser@auth.com",
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

    return tenant, user, role


def test_login_success(
    client: TestClient, test_user_data: tuple[Tenant, User, Role]
) -> None:
    """Test successful user login returns HTTP 200 with JWT bearer access token."""
    tenant, user, _ = test_user_data

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "testuser@auth.com", "password": "Secret123!"},
    )

    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert data["user_id"] == str(user.user_id)
    assert data["tenant_id"] == str(tenant.tenant_id)


def test_login_invalid_password(
    client: TestClient, test_user_data: tuple[Tenant, User, Role]
) -> None:
    """Test login with incorrect password returns HTTP 401 Unauthorized."""
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "testuser@auth.com", "password": "WrongPassword!"},
    )

    assert response.status_code == 401
    assert (
        "Authentication credentials were not provided or are invalid."
        in response.json()["detail"]
    )


def test_login_nonexistent_user(client: TestClient) -> None:
    """Test login with non-existent email returns HTTP 401 Unauthorized."""
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@auth.com", "password": "Secret123!"},
    )

    assert response.status_code == 401


def test_register_success(client: TestClient, db_session: Session) -> None:
    """Test successful user registration creates user with ClientViewer role and returns 201."""
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "newuser@auth.com",
            "password": "NewUserPass123!",
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert "user_id" in data
    assert data["email"] == "newuser@auth.com"
    assert "tenant_id" in data

    # Verify user created in DB
    user_uuid = uuid.UUID(data["user_id"])
    created_user = db_session.query(User).filter(User.user_id == user_uuid).first()
    assert created_user is not None
    assert created_user.email == "newuser@auth.com"

    # Verify default role ClientViewer assigned
    user_roles = (
        db_session.query(Role.name)
        .join(UserRole, UserRole.role_id == Role.role_id)
        .filter(UserRole.user_id == user_uuid)
        .all()
    )
    assigned_roles = [r[0] for r in user_roles]
    assert RoleName.CLIENT_VIEWER.value in assigned_roles


def test_register_with_provided_tenant_id(
    client: TestClient, db_session: Session
) -> None:
    """Test registration when explicit tenant_id is supplied."""
    tenant = Tenant(name="Existing Tenant")
    db_session.add(tenant)
    db_session.commit()

    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "tenantuser@auth.com",
            "password": "Password123!",
            "tenant_id": str(tenant.tenant_id),
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["tenant_id"] == str(tenant.tenant_id)


def test_register_duplicate_email(
    client: TestClient, test_user_data: tuple[Tenant, User, Role]
) -> None:
    """Test registration with existing email returns HTTP 409 Conflict."""
    _, user, _ = test_user_data

    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": user.email,
            "password": "AnotherPassword123!",
        },
    )

    assert response.status_code == 409
    assert "User with this email already exists." in response.json()["detail"]


def test_get_me_success(
    client: TestClient, test_user_data: tuple[Tenant, User, Role]
) -> None:
    """Test GET /api/v1/auth/me returns active identity details for valid token."""
    tenant, user, _ = test_user_data

    token = create_access_token(
        subject=user.user_id,
        tenant_id=tenant.tenant_id,
        email=user.email,
    )

    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == str(user.user_id)
    assert data["email"] == user.email
    assert data["tenant_id"] == str(tenant.tenant_id)
    assert RoleName.CLIENT_STANDARD.value in data["roles"]


def test_get_me_unauthenticated(client: TestClient) -> None:
    """Test GET /api/v1/auth/me without authorization header returns HTTP 401."""
    response = client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert (
        "Authentication credentials were not provided or are invalid."
        in response.json()["detail"]
    )
