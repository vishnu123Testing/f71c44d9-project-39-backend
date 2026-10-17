"""Unit tests for FastAPI security and authentication dependencies in src/api/deps.py."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import uuid

from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
import jwt
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.api.deps import (
    CurrentUser,
    RequirePermission,
    RequireRole,
    create_access_token,
    decode_access_token,
    enforce_tenant_scope,
    get_current_active_user,
    get_current_user,
)
from src.lib.config import settings
from src.models.database import Base
from src.models.role import Role, UserRole
from src.models.tenant import Tenant
from src.models.user import User
from src.services.access_control.rbac import RoleName


@pytest.fixture
def db_session() -> Session:
    """In-memory SQLite database session fixture."""
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
def test_tenant_and_user(db_session: Session) -> tuple[Tenant, User, Role]:
    """Fixture creating a test tenant, user, and assigned role."""
    tenant = Tenant(name="Test Security Corp")
    db_session.add(tenant)
    db_session.commit()

    user = User(
        email="secuser@test.com",
        password_hash="secret_hash",
        tenant_id=tenant.tenant_id,
    )
    db_session.add(user)
    db_session.commit()

    role = Role(name=RoleName.CLIENT_STANDARD.value, description="Client Standard Role")
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


def test_create_and_decode_access_token() -> None:
    """Test generating a signed access token and decoding its payload."""
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    email = "test@example.com"

    token = create_access_token(
        subject=user_id,
        tenant_id=tenant_id,
        email=email,
    )

    assert isinstance(token, str)
    payload = decode_access_token(token)

    assert payload["sub"] == str(user_id)
    assert payload["tenant_id"] == str(tenant_id)
    assert payload["email"] == email


def test_decode_access_token_expired() -> None:
    """Test decoding an expired access token raises HTTP 401."""
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    token = create_access_token(
        subject=user_id,
        tenant_id=tenant_id,
        expires_delta=timedelta(seconds=-10),
    )

    with pytest.raises(HTTPException) as exc_info:
        decode_access_token(token)

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "Authentication credentials were not provided or are invalid." in str(
        exc_info.value.detail
    )


def test_decode_access_token_invalid_signature() -> None:
    """Test decoding a token with an invalid signature raises HTTP 401."""
    now = datetime.now(timezone.utc)
    tampered_payload = {
        "sub": str(uuid.uuid4()),
        "tenant_id": str(uuid.uuid4()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
    }
    bad_token = jwt.encode(tampered_payload, "wrong_secret_key", algorithm="HS256")

    with pytest.raises(HTTPException) as exc_info:
        decode_access_token(bad_token)

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED


def test_get_current_user_valid(
    db_session: Session, test_tenant_and_user: tuple[Tenant, User, Role]
) -> None:
    """Test get_current_user dependency returns CurrentUser context for valid token."""
    tenant, user, role = test_tenant_and_user

    token = create_access_token(
        subject=user.user_id,
        tenant_id=tenant.tenant_id,
        email=user.email,
    )

    mock_request = MagicMock(spec=Request)
    mock_request.client = MagicMock(host="127.0.0.1")
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    current_user = get_current_user(
        request=mock_request,
        credentials=credentials,
        db=db_session,
    )

    assert current_user.user_id == user.user_id
    assert current_user.tenant_id == tenant.tenant_id
    assert current_user.email == user.email
    assert RoleName.CLIENT_STANDARD.value in current_user.roles


def test_get_current_user_missing_credentials(db_session: Session) -> None:
    """Test get_current_user raises HTTP 401 when token is missing."""
    mock_request = MagicMock(spec=Request)
    mock_request.headers = {}
    mock_request.client = MagicMock(host="127.0.0.1")

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(
            request=mock_request,
            credentials=None,
            db=db_session,
        )

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED


def test_get_current_user_user_not_in_db(db_session: Session) -> None:
    """Test get_current_user raises HTTP 401 if user UUID does not exist in DB."""
    non_existent_user = uuid.uuid4()
    tenant_id = uuid.uuid4()

    token = create_access_token(
        subject=non_existent_user,
        tenant_id=tenant_id,
    )

    mock_request = MagicMock(spec=Request)
    mock_request.client = MagicMock(host="127.0.0.1")
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(
            request=mock_request,
            credentials=credentials,
            db=db_session,
        )

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED


def test_get_current_active_user() -> None:
    """Test get_current_active_user passes through current user context."""
    dummy_user = CurrentUser(
        user_id=uuid.uuid4(),
        email="active@test.com",
        tenant_id=uuid.uuid4(),
        roles=["ClientStandard"],
        user=MagicMock(),
    )
    res = get_current_active_user(current_user=dummy_user)
    assert res == dummy_user


def test_require_permission_allowed(db_session: Session) -> None:
    """Test RequirePermission dependency allows authorized action."""
    tenant_id = uuid.uuid4()
    user_context = CurrentUser(
        user_id=uuid.uuid4(),
        email="user@test.com",
        tenant_id=tenant_id,
        roles=[RoleName.CLIENT_STANDARD.value],
        user=MagicMock(),
    )

    mock_request = MagicMock(spec=Request)
    mock_request.path_params = {}
    mock_request.query_params = {}
    mock_request.client = MagicMock(host="127.0.0.1")

    checker = RequirePermission(action="read", resource="dashboard")
    res = checker(
        request=mock_request,
        current_user=user_context,
        db=db_session,
    )

    assert res == user_context


def test_require_permission_denied(db_session: Session) -> None:
    """Test RequirePermission dependency raises HTTP 403 on unauthorized action."""
    tenant_id = uuid.uuid4()
    user_context = CurrentUser(
        user_id=uuid.uuid4(),
        email="viewer@test.com",
        tenant_id=tenant_id,
        roles=[RoleName.CLIENT_VIEWER.value],
        user=MagicMock(),
    )

    mock_request = MagicMock(spec=Request)
    mock_request.path_params = {}
    mock_request.query_params = {}
    mock_request.client = MagicMock(host="127.0.0.1")

    checker = RequirePermission(action="write", resource="dashboard")

    with pytest.raises(HTTPException) as exc_info:
        checker(
            request=mock_request,
            current_user=user_context,
            db=db_session,
        )

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert "Access to the requested resource is denied" in str(exc_info.value.detail)


def test_require_permission_cross_tenant_denied(db_session: Session) -> None:
    """Test RequirePermission dependency raises HTTP 403 on cross-tenant request."""
    caller_tenant = uuid.uuid4()
    target_tenant = uuid.uuid4()

    user_context = CurrentUser(
        user_id=uuid.uuid4(),
        email="admin@test.com",
        tenant_id=caller_tenant,
        roles=[RoleName.ADMIN.value],
        user=MagicMock(),
    )

    mock_request = MagicMock(spec=Request)
    mock_request.path_params = {"target_tenant_id": str(target_tenant)}
    mock_request.query_params = {}
    mock_request.client = MagicMock(host="127.0.0.1")

    checker = RequirePermission(
        action="read", resource="dashboard", target_tenant_param="target_tenant_id"
    )

    with pytest.raises(HTTPException) as exc_info:
        checker(
            request=mock_request,
            current_user=user_context,
            db=db_session,
        )

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN


def test_require_role_allowed_and_denied(db_session: Session) -> None:
    """Test RequireRole dependency enforcement."""
    tenant_id = uuid.uuid4()
    admin_user = CurrentUser(
        user_id=uuid.uuid4(),
        email="admin@test.com",
        tenant_id=tenant_id,
        roles=[RoleName.ADMIN.value],
        user=MagicMock(),
    )
    viewer_user = CurrentUser(
        user_id=uuid.uuid4(),
        email="viewer@test.com",
        tenant_id=tenant_id,
        roles=[RoleName.CLIENT_VIEWER.value],
        user=MagicMock(),
    )

    mock_request = MagicMock(spec=Request)
    mock_request.client = MagicMock(host="127.0.0.1")

    admin_checker = RequireRole(allowed_roles=[RoleName.ADMIN.value])

    # Admin passes
    res = admin_checker(
        request=mock_request,
        current_user=admin_user,
        db=db_session,
    )
    assert res == admin_user

    # Viewer fails with 403
    with pytest.raises(HTTPException) as exc_info:
        admin_checker(
            request=mock_request,
            current_user=viewer_user,
            db=db_session,
        )
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN


def test_enforce_tenant_scope_matching_and_cross() -> None:
    """Test enforce_tenant_scope utility function."""
    tenant1 = uuid.uuid4()
    tenant2 = uuid.uuid4()

    # Matching passes
    enforce_tenant_scope(caller_tenant_id=tenant1, target_tenant_id=tenant1)

    # Cross-tenant raises 403
    with pytest.raises(HTTPException) as exc_info:
        enforce_tenant_scope(caller_tenant_id=tenant1, target_tenant_id=tenant2)

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
