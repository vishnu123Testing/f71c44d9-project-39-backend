from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.models.database import Base
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


def test_user_tablename() -> None:
    """Test that User model has the expected table name."""
    assert User.__tablename__ == "user"


def test_user_creation_defaults(test_db_session: Session) -> None:
    """Test creating a User record with default UUID and created_at values."""
    tenant = Tenant(name="Tenant Alpha")
    test_db_session.add(tenant)
    test_db_session.commit()

    user = User(
        email="user1@example.com",
        password_hash="$2b$12$eImiTXuWVxfM37uY4JANjO39XA/9.4Yf7Lg0G.2J2.",
        tenant_id=tenant.tenant_id,
    )
    test_db_session.add(user)
    test_db_session.commit()
    test_db_session.refresh(user)

    assert isinstance(user.user_id, uuid.UUID)
    assert user.email == "user1@example.com"
    assert user.password_hash == "$2b$12$eImiTXuWVxfM37uY4JANjO39XA/9.4Yf7Lg0G.2J2."
    assert user.tenant_id == tenant.tenant_id
    assert isinstance(user.created_at, datetime)


def test_user_custom_attributes(test_db_session: Session) -> None:
    """Test creating a User record with explicit user_id and created_at."""
    tenant = Tenant(name="Tenant Beta")
    test_db_session.add(tenant)
    test_db_session.commit()

    custom_id = uuid.uuid4()
    custom_time = datetime(2025, 2, 1, 10, 0, 0, tzinfo=timezone.utc)

    user = User(
        user_id=custom_id,
        email="user2@example.com",
        password_hash="hashed_secret_123",
        tenant_id=tenant.tenant_id,
        created_at=custom_time,
    )
    test_db_session.add(user)
    test_db_session.commit()
    test_db_session.refresh(user)

    assert user.user_id == custom_id
    assert user.email == "user2@example.com"
    assert user.password_hash == "hashed_secret_123"
    assert user.tenant_id == tenant.tenant_id
    assert user.created_at == custom_time


def test_user_tenant_relationship(test_db_session: Session) -> None:
    """Test accessing tenant relationship from user instance."""
    tenant = Tenant(name="Tenant Gamma")
    test_db_session.add(tenant)
    test_db_session.commit()

    user = User(
        email="user3@example.com",
        password_hash="hashed_pass",
        tenant_id=tenant.tenant_id,
    )
    test_db_session.add(user)
    test_db_session.commit()
    test_db_session.refresh(user)

    assert user.tenant is not None
    assert user.tenant.tenant_id == tenant.tenant_id
    assert user.tenant.name == "Tenant Gamma"


def test_user_repr() -> None:
    """Test string representation of User model."""
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user = User(
        user_id=user_id,
        email="test@example.com",
        tenant_id=tenant_id,
    )
    expected_repr = (
        f"<User(user_id={user_id!r}, email='test@example.com', tenant_id={tenant_id!r})>"
    )
    assert repr(user) == expected_repr


def test_user_email_uniqueness_constraint(test_db_session: Session) -> None:
    """Test that creating two users with the same email raises IntegrityError."""
    tenant = Tenant(name="Tenant Delta")
    test_db_session.add(tenant)
    test_db_session.commit()

    user1 = User(
        email="duplicate@example.com",
        password_hash="pass1",
        tenant_id=tenant.tenant_id,
    )
    test_db_session.add(user1)
    test_db_session.commit()

    user2 = User(
        email="duplicate@example.com",
        password_hash="pass2",
        tenant_id=tenant.tenant_id,
    )
    test_db_session.add(user2)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_user_email_non_nullable(test_db_session: Session) -> None:
    """Test that creating a user without email raises IntegrityError."""
    tenant = Tenant(name="Tenant Epsilon")
    test_db_session.add(tenant)
    test_db_session.commit()

    user = User(
        email=None,  # type: ignore[arg-type]
        password_hash="pass",
        tenant_id=tenant.tenant_id,
    )
    test_db_session.add(user)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_user_foreign_key_constraint(test_db_session: Session) -> None:
    """Test that creating a user with invalid tenant_id raises IntegrityError."""
    non_existent_tenant_id = uuid.uuid4()
    user = User(
        email="orphan@example.com",
        password_hash="pass",
        tenant_id=non_existent_tenant_id,
    )
    test_db_session.add(user)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()
