from datetime import datetime, timezone
import io
import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.lib.logging import reset_correlation_id, set_correlation_id
from src.models.audit import AuditLog
from src.models.database import Base
from src.services.access_control.audit import (
    AuditAction,
    AuditEvent,
    AuditOutcome,
    AuditService,
    get_tenant_audit_logs,
    log_audit_event,
    parse_uuid,
    sanitize_details,
)


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


def test_parse_uuid_valid() -> None:
    """Test parse_uuid helper with various valid and invalid inputs."""
    u = uuid.uuid4()
    assert parse_uuid(u) == u
    assert parse_uuid(str(u)) == u
    assert parse_uuid("not-a-uuid") is None
    assert parse_uuid(None) is None


def test_sanitize_details_masking() -> None:
    """Test that sanitize_details masks sensitive keys recursively."""
    unmasked = {
        "user_email": "user@example.com",
        "password": "secret_password_123",
        "access_token": "bearer_xyz_token",
        "nested": {
            "api_key": "key_9999",
            "normal_field": "visible",
        },
    }
    sanitized = sanitize_details(unmasked)
    assert sanitized is not None
    assert sanitized["user_email"] == "user@example.com"
    assert sanitized["password"] == "***"
    assert sanitized["access_token"] == "***"
    assert sanitized["nested"]["api_key"] == "***"
    assert sanitized["nested"]["normal_field"] == "visible"


def test_audit_event_serialization() -> None:
    """Test AuditEvent to_dict and to_json methods."""
    event_id = uuid.uuid4()
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    corr_id = uuid.uuid4()

    event = AuditEvent(
        event_id=event_id,
        action=AuditAction.AUTH_LOGIN.value,
        outcome_status=AuditOutcome.SUCCESS.value,
        user_id=user_id,
        assigned_role="Admin",
        tenant_id=tenant_id,
        resource_id="res_01",
        client_ip="127.0.0.1",
        correlation_id=corr_id,
        details={"ip": "127.0.0.1"},
    )

    data = event.to_dict()
    assert data["event_id"] == str(event_id)
    assert data["action"] == "auth.login"
    assert data["outcome_status"] == "SUCCESS"
    assert data["user_id"] == str(user_id)
    assert data["assigned_role"] == "Admin"
    assert data["tenant_id"] == str(tenant_id)
    assert data["correlation_id"] == str(corr_id)

    json_str = event.to_json()
    parsed = json.loads(json_str)
    assert parsed["event_id"] == str(event_id)
    assert parsed["action"] == "auth.login"


def test_log_audit_event_stdout_emission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that log_audit_event writes formatted JSON to stdout stream."""
    captured_output = io.StringIO()
    monkeypatch.setattr("sys.stdout", captured_output)

    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    event = log_audit_event(
        action=AuditAction.ACCESS_EVALUATE.value,
        outcome_status=AuditOutcome.ALLOWED.value,
        user_id=user_id,
        assigned_role="ClientStandard",
        tenant_id=tenant_id,
        resource_id="contracts",
        client_ip="10.0.0.1",
        details={"secret_token": "should_be_masked", "section": "contracts"},
    )

    assert event is not None
    stdout_content = captured_output.getvalue()
    assert stdout_content.endswith("\n")

    log_entry = json.loads(stdout_content.strip())
    assert log_entry["action"] == "access.evaluate"
    assert log_entry["outcome_status"] == "ALLOWED"
    assert log_entry["user_id"] == str(user_id)
    assert log_entry["tenant_id"] == str(tenant_id)
    assert log_entry["details"]["secret_token"] == "***"
    assert log_entry["details"]["section"] == "contracts"


def test_log_audit_event_context_correlation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that correlation ID is retrieved from context if not explicitly passed."""
    captured_output = io.StringIO()
    monkeypatch.setattr("sys.stdout", captured_output)

    context_corr_id = uuid.uuid4()
    token = set_correlation_id(str(context_corr_id))
    try:
        event = log_audit_event(
            action=AuditAction.AUTH_REGISTER.value,
            outcome_status=AuditOutcome.SUCCESS.value,
        )
        assert event is not None
        assert event.correlation_id == context_corr_id

        stdout_content = captured_output.getvalue().strip()
        log_entry = json.loads(stdout_content)
        assert log_entry["correlation_id"] == str(context_corr_id)
    finally:
        reset_correlation_id(token)


def test_log_audit_event_database_persistence(
    test_db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test persisting audit events to database when db session is supplied."""
    monkeypatch.setattr("sys.stdout", io.StringIO())

    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    event = AuditService.log_event(
        action=AuditAction.RECORD_CREATE.value,
        outcome_status=AuditOutcome.SUCCESS.value,
        user_id=user_id,
        assigned_role="Admin",
        tenant_id=tenant_id,
        resource_id="rec_99",
        client_ip="192.168.1.1",
        db=test_db_session,
    )

    assert event is not None

    db_log = (
        test_db_session.query(AuditLog)
        .filter(AuditLog.audit_id == event.event_id)
        .first()
    )
    assert db_log is not None
    assert db_log.action == "record.create"
    assert db_log.outcome_status == "SUCCESS"
    assert db_log.user_id == user_id
    assert db_log.tenant_id == tenant_id
    assert db_log.assigned_role == "Admin"
    assert db_log.resource_id == "rec_99"


def test_log_audit_event_failsafe_on_db_exception(
    test_db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test non-blocking fail-safe trap when DB commit raises an exception."""
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    # Cause db.commit to raise an exception
    def failing_commit() -> None:
        raise Exception("Database disk full or connection lost")

    monkeypatch.setattr(test_db_session, "commit", failing_commit)

    event = log_audit_event(
        action=AuditAction.RECORD_UPDATE.value,
        outcome_status=AuditOutcome.ERROR.value,
        db=test_db_session,
    )

    # Event object is still returned without raising an exception to caller
    assert event is not None
    assert event.action == "record.update"


def test_log_audit_event_failsafe_on_stdout_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test non-blocking fail-safe trap when stdout write raises an exception."""

    def failing_write(data: str) -> int:
        raise OSError("stdout stream closed")

    monkeypatch.setattr("sys.stdout.write", failing_write)
    monkeypatch.setattr("sys.stderr", io.StringIO())

    # Must catch OSError and return None without propagating exception
    event = log_audit_event(
        action=AuditAction.AUTH_LOGOUT.value,
        outcome_status=AuditOutcome.SUCCESS.value,
    )
    assert event is None


def test_get_tenant_audit_logs(test_db_session: Session) -> None:
    """Test querying audit logs filtered strictly by tenant_id."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    now = datetime.now(timezone.utc)

    # Create logs for tenant_a
    log1 = AuditLog(
        tenant_id=tenant_a,
        action="action_a1",
        outcome_status="SUCCESS",
        logged_at=now,
    )
    log2 = AuditLog(
        tenant_id=tenant_a,
        action="action_a2",
        outcome_status="DENIED",
        logged_at=now,
    )
    # Create log for tenant_b
    log3 = AuditLog(
        tenant_id=tenant_b,
        action="action_b1",
        outcome_status="SUCCESS",
        logged_at=now,
    )

    test_db_session.add_all([log1, log2, log3])
    test_db_session.commit()

    logs, total = AuditService.get_logs(test_db_session, tenant_id=tenant_a)
    assert total == 2
    assert len(logs) == 2
    assert all(l.tenant_id == tenant_a for l in logs)

    logs_b, total_b = AuditService.get_logs(test_db_session, tenant_id=tenant_b)
    assert total_b == 1
    assert len(logs_b) == 1
    assert logs_b[0].action == "action_b1"


def test_get_tenant_audit_logs_invalid_tenant_id(
    test_db_session: Session,
) -> None:
    """Test get_tenant_audit_logs handles invalid tenant_id gracefully."""
    logs, total = get_tenant_audit_logs(test_db_session, tenant_id="invalid-uuid")
    assert logs == []
    assert total == 0
