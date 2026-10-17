"""Unit tests for the RBAC evaluation engine and tenant boundary enforcement."""

import time
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.models.database import Base
from src.models.role import Role, UserRole
from src.models.tenant import Tenant
from src.models.user import User
from src.services.access_control.rbac import (
    PermissionDeniedError,
    RoleName,
    TenantAccessDeniedError,
    enforce_access,
    enforce_tenant_boundary,
    evaluate_access,
    get_authorized_navigation_items,
    get_user_roles,
    has_role_permission,
    verify_tenant_boundary,
)


@pytest.fixture
def db_session() -> Session:
    """Fixture providing an isolated in-memory SQLite database session."""
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


def test_verify_tenant_boundary_matching() -> None:
    """Test verify_tenant_boundary returns True for identical tenant IDs."""
    tenant_id = uuid.uuid4()
    assert verify_tenant_boundary(tenant_id, tenant_id) is True
    assert verify_tenant_boundary(str(tenant_id), str(tenant_id)) is True
    assert verify_tenant_boundary(tenant_id, str(tenant_id)) is True


def test_verify_tenant_boundary_non_matching() -> None:
    """Test verify_tenant_boundary returns False for differing tenant IDs."""
    tenant1 = uuid.uuid4()
    tenant2 = uuid.uuid4()
    assert verify_tenant_boundary(tenant1, tenant2) is False
    assert verify_tenant_boundary(str(tenant1), str(tenant2)) is False


def test_verify_tenant_boundary_none_inputs() -> None:
    """Test verify_tenant_boundary handles None gracefully."""
    tenant_id = uuid.uuid4()
    assert verify_tenant_boundary(tenant_id, None) is False
    assert verify_tenant_boundary(None, tenant_id) is False


def test_enforce_tenant_boundary_success() -> None:
    """Test enforce_tenant_boundary passes when tenant IDs match."""
    tenant_id = uuid.uuid4()
    # Should not raise
    enforce_tenant_boundary(tenant_id, tenant_id)


def test_enforce_tenant_boundary_denied() -> None:
    """Test enforce_tenant_boundary raises TenantAccessDeniedError for different tenant IDs."""
    tenant1 = uuid.uuid4()
    tenant2 = uuid.uuid4()
    with pytest.raises(TenantAccessDeniedError) as exc_info:
        enforce_tenant_boundary(tenant1, tenant2)
    assert "Cross-tenant access denied" in str(exc_info.value)


def test_get_user_roles(db_session: Session) -> None:
    """Test retrieving user roles from database via SQLAlchemy query."""
    tenant = Tenant(name="Acme Corp")
    db_session.add(tenant)
    db_session.commit()

    user = User(
        email="test@acme.com",
        password_hash="hashed_secret",
        tenant_id=tenant.tenant_id,
    )
    db_session.add(user)

    admin_role = Role(name=RoleName.ADMIN.value, description="Admin role")
    viewer_role = Role(name=RoleName.CLIENT_VIEWER.value, description="Viewer role")
    db_session.add_all([admin_role, viewer_role])
    db_session.commit()

    user_role1 = UserRole(
        user_id=user.user_id,
        role_id=admin_role.role_id,
        tenant_id=tenant.tenant_id,
    )
    user_role2 = UserRole(
        user_id=user.user_id,
        role_id=viewer_role.role_id,
        tenant_id=tenant.tenant_id,
    )
    db_session.add_all([user_role1, user_role2])
    db_session.commit()

    roles = get_user_roles(
        session=db_session,
        user_id=user.user_id,
        tenant_id=tenant.tenant_id,
    )
    assert len(roles) == 2
    assert RoleName.ADMIN.value in roles
    assert RoleName.CLIENT_VIEWER.value in roles


def test_has_role_permission_admin() -> None:
    """Test Admin role permissions across arbitrary actions and resources."""
    roles = [RoleName.ADMIN.value]
    assert has_role_permission(roles, action="read", resource="dashboard") is True
    assert has_role_permission(roles, action="delete", resource="financials") is True
    assert has_role_permission(roles, action="export", resource="audit") is True


def test_has_role_permission_client_standard() -> None:
    """Test ClientStandard permissions for operational actions vs administrative ones."""
    roles = [RoleName.CLIENT_STANDARD.value]
    assert has_role_permission(roles, action="read", resource="dashboard") is True
    assert has_role_permission(roles, action="write", resource="contracts") is True
    assert has_role_permission(roles, action="create", resource="support") is True

    # Audit log read should be denied for ClientStandard
    assert has_role_permission(roles, action="export", resource="audit") is False
    assert has_role_permission(roles, action="delete", resource="system") is False


def test_has_role_permission_client_viewer() -> None:
    """Test ClientViewer read-only permissions and rejection of mutation actions."""
    roles = [RoleName.CLIENT_VIEWER.value]
    assert has_role_permission(roles, action="read", resource="dashboard") is True
    assert has_role_permission(roles, action="view", resource="financials") is True

    # Write and delete operations must be denied
    assert has_role_permission(roles, action="write", resource="contracts") is False
    assert has_role_permission(roles, action="delete", resource="support") is False
    assert has_role_permission(roles, action="create", resource="knowledge") is False


def test_has_role_permission_empty_and_unknown() -> None:
    """Test permission check with empty roles or unrecognized role names."""
    assert has_role_permission([], action="read", resource="dashboard") is False
    assert (
        has_role_permission(["UnknownRole"], action="read", resource="dashboard")
        is False
    )


def test_evaluate_access_success() -> None:
    """Test evaluate_access returning allowed result when boundaries and roles match."""
    tenant_id = uuid.uuid4()
    result = evaluate_access(
        caller_tenant_id=tenant_id,
        target_tenant_id=tenant_id,
        user_roles=[RoleName.CLIENT_STANDARD.value],
        action="read",
        resource="contracts",
    )
    assert result.allowed is True
    assert result.reason == "Access granted."
    assert result.caller_tenant_id == str(tenant_id)
    assert result.target_tenant_id == str(tenant_id)
    assert result.evaluated_at_ms < 20.0


def test_evaluate_access_cross_tenant_denied() -> None:
    """Test evaluate_access rejecting cross-tenant attempts regardless of user role."""
    tenant1 = uuid.uuid4()
    tenant2 = uuid.uuid4()
    result = evaluate_access(
        caller_tenant_id=tenant1,
        target_tenant_id=tenant2,
        user_roles=[RoleName.ADMIN.value],  # Admin role still blocked across tenants
        action="read",
        resource="dashboard",
    )
    assert result.allowed is False
    assert "Cross-tenant access rejected" in result.reason
    assert result.evaluated_at_ms < 20.0


def test_evaluate_access_permission_denied() -> None:
    """Test evaluate_access rejecting unauthorized action within the same tenant."""
    tenant_id = uuid.uuid4()
    result = evaluate_access(
        caller_tenant_id=tenant_id,
        target_tenant_id=tenant_id,
        user_roles=[RoleName.CLIENT_VIEWER.value],
        action="delete",
        resource="contracts",
    )
    assert result.allowed is False
    assert "Permission denied" in result.reason


def test_enforce_access_exceptions() -> None:
    """Test enforce_access raising specific exception types on failure."""
    tenant1 = uuid.uuid4()
    tenant2 = uuid.uuid4()

    # Cross tenant raises TenantAccessDeniedError
    with pytest.raises(TenantAccessDeniedError):
        enforce_access(
            caller_tenant_id=tenant1,
            target_tenant_id=tenant2,
            user_roles=[RoleName.ADMIN.value],
            action="read",
            resource="dashboard",
        )

    # Permission failure raises PermissionDeniedError
    with pytest.raises(PermissionDeniedError):
        enforce_access(
            caller_tenant_id=tenant1,
            target_tenant_id=tenant1,
            user_roles=[RoleName.CLIENT_VIEWER.value],
            action="delete",
            resource="dashboard",
        )


def test_get_authorized_navigation_items() -> None:
    """Test building authorized navigation items list for various user roles."""
    viewer_items = get_authorized_navigation_items([RoleName.CLIENT_VIEWER.value])
    assert len(viewer_items) == 6
    paths = [item["path"] for item in viewer_items]
    assert "/dashboard" in paths
    assert "/performance" in paths
    assert "/contracts" in paths
    assert "/support" in paths
    assert "/financials" in paths
    assert "/knowledge" in paths
    assert all(item["is_accessible"] is True for item in viewer_items)

    no_role_items = get_authorized_navigation_items([])
    assert len(no_role_items) == 6
    assert all(item["is_accessible"] is False for item in no_role_items)


def test_rbac_sub_20ms_benchmark() -> None:
    """Benchmark test verifying that 1000 evaluations execute well within 20ms per evaluation."""
    tenant_id = uuid.uuid4()
    roles = [RoleName.CLIENT_STANDARD.value]

    start_time = time.perf_counter()
    iterations = 1000
    for _ in range(iterations):
        res = evaluate_access(
            caller_tenant_id=tenant_id,
            target_tenant_id=tenant_id,
            user_roles=roles,
            action="read",
            resource="dashboard",
        )
        assert res.allowed is True
    total_elapsed_ms = (time.perf_counter() - start_time) * 1000.0
    avg_per_eval_ms = total_elapsed_ms / iterations

    # Average evaluation time must be < 1ms (far below the 20ms NFR limit)
    assert avg_per_eval_ms < 1.0, f"Average eval time was {avg_per_eval_ms:.4f} ms"
