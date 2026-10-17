from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.models.audit import AuditLog
from src.models.database import Base


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


def test_audit_log_tablename() -> None:
    """Test that AuditLog model has the expected table name."""
    assert AuditLog.__tablename__ == "audit_log"


def test_audit_log_creation_defaults(test_db_session: Session) -> None:
    """Test creating an AuditLog record with minimal attributes and default values."""
    audit = AuditLog(
        action="user.login",
        outcome_status="SUCCESS",
    )
    test_db_session.add(audit)
    test_db_session.commit()
    test_db_session.refresh(audit)

    assert isinstance(audit.audit_id, uuid.UUID)
    assert isinstance(audit.logged_at, datetime)
    assert isinstance(audit.correlation_id, uuid.UUID)
    assert audit.action == "user.login"
    assert audit.outcome_status == "SUCCESS"
    assert audit.user_id is None
    assert audit.assigned_role is None
    assert audit.tenant_id is None
    assert audit.resource_id is None
    assert audit.client_ip is None


def test_audit_log_custom_attributes(test_db_session: Session) -> None:
    """Test creating an AuditLog record with explicit attributes for all columns."""
    custom_audit_id = uuid.uuid4()
    custom_logged_at = datetime(2025, 3, 1, 14, 30, 0, tzinfo=timezone.utc)
    custom_user_id = uuid.uuid4()
    custom_tenant_id = uuid.uuid4()
    custom_correlation_id = uuid.uuid4()

    audit = AuditLog(
        audit_id=custom_audit_id,
        logged_at=custom_logged_at,
        user_id=custom_user_id,
        assigned_role="ClientViewer",
        tenant_id=custom_tenant_id,
        action="document.read",
        resource_id="doc_12345",
        client_ip="192.168.1.100",
        outcome_status="ALLOWED",
        correlation_id=custom_correlation_id,
    )
    test_db_session.add(audit)
    test_db_session.commit()
    test_db_session.refresh(audit)

    assert audit.audit_id == custom_audit_id
    assert audit.logged_at == custom_logged_at
    assert audit.user_id == custom_user_id
    assert audit.assigned_role == "ClientViewer"
    assert audit.tenant_id == custom_tenant_id
    assert audit.action == "document.read"
    assert audit.resource_id == "doc_12345"
    assert audit.client_ip == "192.168.1.100"
    assert audit.outcome_status == "ALLOWED"
    assert audit.correlation_id == custom_correlation_id


def test_audit_log_repr() -> None:
    """Test string representation of AuditLog model."""
    audit_id = uuid.uuid4()
    logged_at = datetime(2025, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    audit = AuditLog(
        audit_id=audit_id,
        action="user.register",
        outcome_status="SUCCESS",
        logged_at=logged_at,
    )
    expected_repr = (
        f"<AuditLog(audit_id={audit_id!r}, "
        f"action='user.register', "
        f"outcome_status='SUCCESS', "
        f"logged_at={logged_at!r})>"
    )
    assert repr(audit) == expected_repr


def test_audit_log_non_nullable_action(test_db_session: Session) -> None:
    """Test that creating an AuditLog without an action raises IntegrityError."""
    audit = AuditLog(
        action=None,  # type: ignore[arg-type]
        outcome_status="SUCCESS",
    )
    test_db_session.add(audit)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_audit_log_non_nullable_outcome_status(test_db_session: Session) -> None:
    """Test that creating an AuditLog without outcome_status raises IntegrityError."""
    audit = AuditLog(
        action="user.login",
        outcome_status=None,  # type: ignore[arg-type]
    )
    test_db_session.add(audit)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_audit_log_indexes() -> None:
    """Test that tenant_id + logged_at composite index and correlation_id index exist."""
    index_names = [idx.name for idx in AuditLog.__table__.indexes]
    assert "ix_audit_log_tenant_id_logged_at" in index_names

    correlation_idx = [
        idx for idx in AuditLog.__table__.indexes if "correlation_id" in idx.columns
    ]
    assert len(correlation_idx) > 0


def test_audit_log_query_by_tenant_and_logged_at(test_db_session: Session) -> None:
    """Test querying audit logs filtered by tenant_id and ordered by logged_at."""
    tenant1_id = uuid.uuid4()
    tenant2_id = uuid.uuid4()

    t1_audit1 = AuditLog(
        tenant_id=tenant1_id,
        action="action1",
        outcome_status="SUCCESS",
        logged_at=datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
    )
    t1_audit2 = AuditLog(
        tenant_id=tenant1_id,
        action="action2",
        outcome_status="DENIED",
        logged_at=datetime(2025, 1, 1, 11, 0, 0, tzinfo=timezone.utc),
    )
    t2_audit = AuditLog(
        tenant_id=tenant2_id,
        action="action3",
        outcome_status="SUCCESS",
        logged_at=datetime(2025, 1, 1, 10, 30, 0, tzinfo=timezone.utc),
    )

    test_db_session.add_all([t1_audit1, t1_audit2, t2_audit])
    test_db_session.commit()

    results = (
        test_db_session.query(AuditLog)
        .filter(AuditLog.tenant_id == tenant1_id)
        .order_by(AuditLog.logged_at.asc())
        .all()
    )

    assert len(results) == 2
    assert results[0].action == "action1"
    assert results[1].action == "action2"
