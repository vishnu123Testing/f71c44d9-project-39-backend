import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.models.database import Base
from src.models.role import Role, UserRole
from src.models.tenant import Tenant
from src.models.user import User


@pytest.fixture
def test_db_session() -> Session:
    """Fixture providing an isolated in-memory SQLite database session with FK enabled."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    session = testing_session_local()
    try:
        yield session
    finally:
        session.close()


def test_role_tablename() -> None:
    """Test that Role model has the expected table name."""
    assert Role.__tablename__ == "role"


def test_user_role_tablename() -> None:
    """Test that UserRole model has the expected table name."""
    assert UserRole.__tablename__ == "user_role"


def test_role_creation_defaults(test_db_session: Session) -> None:
    """Test creating a Role record with default UUID."""
    role = Role(name="Admin", description="Administrator role with full permissions")
    test_db_session.add(role)
    test_db_session.commit()
    test_db_session.refresh(role)

    assert isinstance(role.role_id, uuid.UUID)
    assert role.name == "Admin"
    assert role.description == "Administrator role with full permissions"


def test_role_custom_attributes(test_db_session: Session) -> None:
    """Test creating a Role record with explicit role_id."""
    custom_id = uuid.uuid4()
    role = Role(
        role_id=custom_id,
        name="ClientViewer",
        description="Read-only access to client resources",
    )
    test_db_session.add(role)
    test_db_session.commit()
    test_db_session.refresh(role)

    assert role.role_id == custom_id
    assert role.name == "ClientViewer"
    assert role.description == "Read-only access to client resources"


def test_role_repr() -> None:
    """Test string representation of Role model."""
    role_id = uuid.uuid4()
    role = Role(role_id=role_id, name="ClientStandard")
    expected_repr = f"<Role(role_id={role_id!r}, name='ClientStandard')>"
    assert repr(role) == expected_repr


def test_user_role_repr() -> None:
    """Test string representation of UserRole model."""
    u_id = uuid.uuid4()
    r_id = uuid.uuid4()
    t_id = uuid.uuid4()
    user_role = UserRole(user_id=u_id, role_id=r_id, tenant_id=t_id)
    expected_repr = f"<UserRole(user_id={u_id!r}, role_id={r_id!r}, tenant_id={t_id!r})>"
    assert repr(user_role) == expected_repr


def test_role_name_uniqueness_constraint(test_db_session: Session) -> None:
    """Test that creating two roles with the same name raises IntegrityError."""
    role1 = Role(name="Admin")
    test_db_session.add(role1)
    test_db_session.commit()

    role2 = Role(name="Admin")
    test_db_session.add(role2)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_role_name_non_nullable(test_db_session: Session) -> None:
    """Test that creating a role without a name raises IntegrityError."""
    role = Role(name=None)  # type: ignore[arg-type]
    test_db_session.add(role)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_user_role_creation_and_relationships(test_db_session: Session) -> None:
    """Test UserRole creation and navigation through user, role, and tenant relationships."""
    tenant = Tenant(name="Acme Corp")
    role = Role(name="Admin", description="Admin role")
    test_db_session.add_all([tenant, role])
    test_db_session.commit()

    user = User(
        email="admin@acme.com",
        password_hash="secret_hash",
        tenant_id=tenant.tenant_id,
    )
    test_db_session.add(user)
    test_db_session.commit()

    user_role = UserRole(
        user_id=user.user_id,
        role_id=role.role_id,
        tenant_id=tenant.tenant_id,
    )
    test_db_session.add(user_role)
    test_db_session.commit()
    test_db_session.refresh(user_role)

    assert user_role.user_id == user.user_id
    assert user_role.role_id == role.role_id
    assert user_role.tenant_id == tenant.tenant_id

    # Test relationship mappings
    assert user_role.user.email == "admin@acme.com"
    assert user_role.role.name == "Admin"
    assert user_role.tenant.name == "Acme Corp"


def test_user_role_composite_pk_uniqueness(test_db_session: Session) -> None:
    """Test that duplicate composite PK (user_id, role_id, tenant_id) raises IntegrityError."""
    tenant = Tenant(name="Corp A")
    role = Role(name="ClientViewer")
    test_db_session.add_all([tenant, role])
    test_db_session.commit()

    user = User(
        email="viewer@corpa.com",
        password_hash="secret_hash",
        tenant_id=tenant.tenant_id,
    )
    test_db_session.add(user)
    test_db_session.commit()

    ur1 = UserRole(
        user_id=user.user_id,
        role_id=role.role_id,
        tenant_id=tenant.tenant_id,
    )
    test_db_session.add(ur1)
    test_db_session.commit()

    ur2 = UserRole(
        user_id=user.user_id,
        role_id=role.role_id,
        tenant_id=tenant.tenant_id,
    )
    test_db_session.add(ur2)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_user_role_cascade_delete_user(test_db_session: Session) -> None:
    """Test that deleting a user cascades to UserRole."""
    tenant = Tenant(name="Corp B")
    role = Role(name="ClientStandard")
    test_db_session.add_all([tenant, role])
    test_db_session.commit()

    user = User(
        email="user@corpb.com",
        password_hash="secret_hash",
        tenant_id=tenant.tenant_id,
    )
    test_db_session.add(user)
    test_db_session.commit()

    user_role = UserRole(
        user_id=user.user_id,
        role_id=role.role_id,
        tenant_id=tenant.tenant_id,
    )
    test_db_session.add(user_role)
    test_db_session.commit()

    # Delete user
    test_db_session.delete(user)
    test_db_session.commit()

    # Verify user_role is deleted
    found_ur = test_db_session.query(UserRole).filter_by(
        user_id=user.user_id, role_id=role.role_id, tenant_id=tenant.tenant_id
    ).first()
    assert found_ur is None
