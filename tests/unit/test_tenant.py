from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.models.database import Base
from src.models.tenant import Tenant


@pytest.fixture
def test_db_session() -> Session:
    """Fixture providing an isolated in-memory SQLite database session."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    session = testing_session_local()
    try:
        yield session
    finally:
        session.close()


def test_tenant_tablename() -> None:
    """Test that Tenant model has the expected table name."""
    assert Tenant.__tablename__ == "tenant"


def test_tenant_creation_defaults(test_db_session: Session) -> None:
    """Test creating a Tenant record with default UUID and created_at values."""
    tenant = Tenant(name="Acme Corp")
    test_db_session.add(tenant)
    test_db_session.commit()
    test_db_session.refresh(tenant)

    assert isinstance(tenant.tenant_id, uuid.UUID)
    assert tenant.name == "Acme Corp"
    assert isinstance(tenant.created_at, datetime)
    assert tenant.created_at.tzinfo is not None or tenant.created_at.time() is not None


def test_tenant_custom_attributes(test_db_session: Session) -> None:
    """Test creating a Tenant record with explicit tenant_id and created_at."""
    custom_id = uuid.uuid4()
    custom_time = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    tenant = Tenant(
        tenant_id=custom_id,
        name="Beta Inc",
        created_at=custom_time,
    )
    test_db_session.add(tenant)
    test_db_session.commit()
    test_db_session.refresh(tenant)

    assert tenant.tenant_id == custom_id
    assert tenant.name == "Beta Inc"
    assert tenant.created_at == custom_time


def test_tenant_repr() -> None:
    """Test string representation of Tenant model."""
    tenant_id = uuid.uuid4()
    tenant = Tenant(tenant_id=tenant_id, name="Test Tenant")
    expected_repr = f"<Tenant(tenant_id={tenant_id!r}, name='Test Tenant')>"
    assert repr(tenant) == expected_repr


def test_tenant_name_uniqueness_constraint(test_db_session: Session) -> None:
    """Test that creating two tenants with the same name raises IntegrityError."""
    tenant1 = Tenant(name="Unique Name")
    test_db_session.add(tenant1)
    test_db_session.commit()

    tenant2 = Tenant(name="Unique Name")
    test_db_session.add(tenant2)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_tenant_name_non_nullable(test_db_session: Session) -> None:
    """Test that creating a tenant without a name raises IntegrityError."""
    tenant = Tenant(name=None)  # type: ignore[arg-type]
    test_db_session.add(tenant)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()
