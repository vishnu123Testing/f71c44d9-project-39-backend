"""Unit tests for navigation endpoints in backend/src/api/v1/endpoints/navigation.py."""

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
    """In-memory SQLite database session for navigation endpoint testing."""
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
def test_setup(db_session: Session) -> tuple[Tenant, User, Role]:
    """Fixture seeding database with tenant, user, and ClientViewer role."""
    tenant = Tenant(name="Test Nav Tenant")
    db_session.add(tenant)
    db_session.commit()

    hashed_pwd = get_password_hash("Secret123!")
    user = User(
        email="navuser@test.com",
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

    return tenant, user, role


def test_get_navigation_menu_unauthenticated(client: TestClient) -> None:
    """Test GET /api/v1/navigation/menu returns 401 Unauthorized when missing token."""
    response = client.get("/api/v1/navigation/menu")
    assert response.status_code == 401
    assert "detail" in response.json()


def test_get_navigation_menu_authenticated(
    client: TestClient, test_setup: tuple[Tenant, User, Role]
) -> None:
    """Test GET /api/v1/navigation/menu returns authorized routes for ClientViewer user."""
    tenant, user, _ = test_setup
    token = create_access_token(subject=user.user_id, tenant_id=tenant.tenant_id)

    response = client.get(
        "/api/v1/navigation/menu",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    items = data["items"]
    assert len(items) == 6

    paths = [item["path"] for item in items]
    assert "/dashboard" in paths
    assert "/performance" in paths
    assert "/contracts" in paths
    assert "/support" in paths
    assert "/financials" in paths
    assert "/knowledge" in paths

    for item in items:
        assert "title" in item
        assert "path" in item
        assert "is_accessible" in item
        assert "icon_id" in item
        assert item["is_accessible"] is True


def test_get_navigation_menu_no_roles(
    client: TestClient, db_session: Session
) -> None:
    """Test navigation endpoint returns is_accessible=False when user has no roles assigned."""
    tenant = Tenant(name="Roleless Tenant")
    db_session.add(tenant)
    db_session.commit()

    user = User(
        email="noroles@test.com",
        password_hash=get_password_hash("Secret123!"),
        tenant_id=tenant.tenant_id,
    )
    db_session.add(user)
    db_session.commit()

    token = create_access_token(subject=user.user_id, tenant_id=tenant.tenant_id)

    response = client.get(
        "/api/v1/navigation/menu",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    items = data["items"]
    assert len(items) == 6
    assert all(item["is_accessible"] is False for item in items)


def test_get_navigation_endpoint_alias(
    client: TestClient, test_setup: tuple[Tenant, User, Role]
) -> None:
    """Test GET /api/v1/navigation works as endpoint route alias."""
    tenant, user, _ = test_setup
    token = create_access_token(subject=user.user_id, tenant_id=tenant.tenant_id)

    response = client.get(
        "/api/v1/navigation",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 6
