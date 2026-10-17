"""Unit tests for alerting ORM models in backend/src/models/alerting.py."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.models.alerting import (
    AlertSuppression,
    AlertTrigger,
    AlertUrgency,
    NotificationChannel,
    NotificationDeliveryStatus,
    NotificationLog,
    NotificationPreference,
)
from src.models.database import Base


@pytest.fixture
def test_db_session() -> Session:
    """Fixture providing an isolated in-memory SQLite database session with foreign keys enabled."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
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


def test_alerting_tablenames() -> None:
    """Test table names for alerting ORM models."""
    assert AlertTrigger.__tablename__ == "alert_triggers"
    assert NotificationPreference.__tablename__ == "notification_preferences"
    assert NotificationLog.__tablename__ == "notification_logs"
    assert AlertSuppression.__tablename__ == "alert_suppressions"


def test_alert_urgency_enum() -> None:
    """Test AlertUrgency enum values."""
    assert AlertUrgency.LOW.value == "Low"
    assert AlertUrgency.MEDIUM.value == "Medium"
    assert AlertUrgency.HIGH.value == "High"


def test_notification_channel_enum() -> None:
    """Test NotificationChannel enum values."""
    assert NotificationChannel.IN_APP.value == "InApp"
    assert NotificationChannel.EMAIL.value == "Email"
    assert NotificationChannel.SMS.value == "SMS"


def test_notification_delivery_status_enum() -> None:
    """Test NotificationDeliveryStatus enum values."""
    assert NotificationDeliveryStatus.SENT.value == "Sent"
    assert NotificationDeliveryStatus.FAILED.value == "Failed"
    assert NotificationDeliveryStatus.FALLBACK.value == "Fallback"


def test_alert_trigger_creation(test_db_session: Session) -> None:
    """Test creating an AlertTrigger entity."""
    trigger = AlertTrigger(
        rule_name="Past Due Invoices Exceed 5000",
        financial_criteria="past_due_balance_exceeded",
        threshold_amount=5000.0,
        urgency=AlertUrgency.HIGH.value,
        is_active=True,
    )
    test_db_session.add(trigger)
    test_db_session.commit()
    test_db_session.refresh(trigger)

    assert isinstance(trigger.trigger_id, uuid.UUID)
    assert trigger.rule_name == "Past Due Invoices Exceed 5000"
    assert trigger.financial_criteria == "past_due_balance_exceeded"
    assert trigger.threshold_amount == 5000.0
    assert trigger.urgency == "High"
    assert trigger.is_active is True
    assert isinstance(trigger.created_at, datetime)
    assert isinstance(trigger.updated_at, datetime)
    assert trigger.notification_logs == []
    assert "<AlertTrigger(" in repr(trigger)


def test_alert_trigger_rule_name_unique(test_db_session: Session) -> None:
    """Test unique constraint on rule_name column."""
    t1 = AlertTrigger(
        rule_name="Duplicate Rule",
        financial_criteria="past_due_balance_exceeded",
        threshold_amount=100.0,
    )
    t2 = AlertTrigger(
        rule_name="Duplicate Rule",
        financial_criteria="past_due_balance_exceeded",
        threshold_amount=200.0,
    )
    test_db_session.add(t1)
    test_db_session.commit()

    test_db_session.add(t2)
    with pytest.raises(IntegrityError):
        test_db_session.commit()


def test_notification_preference_creation(test_db_session: Session) -> None:
    """Test creating a NotificationPreference entity."""
    pref = NotificationPreference(
        role_name="financial_user",
        event_type="past_due_alert",
        urgency=AlertUrgency.MEDIUM.value,
        channels_json=["InApp", "Email"],
        version=1,
    )
    test_db_session.add(pref)
    test_db_session.commit()
    test_db_session.refresh(pref)

    assert isinstance(pref.preference_id, uuid.UUID)
    assert pref.role_name == "financial_user"
    assert pref.event_type == "past_due_alert"
    assert pref.urgency == "Medium"
    assert pref.channels_json == ["InApp", "Email"]
    assert pref.version == 1
    assert "<NotificationPreference(" in repr(pref)


def test_notification_log_creation_and_relationship(test_db_session: Session) -> None:
    """Test creating a NotificationLog linked to an AlertTrigger."""
    trigger = AlertTrigger(
        rule_name="High Balance Warning",
        financial_criteria="past_due_balance_exceeded",
        threshold_amount=10000.0,
        urgency=AlertUrgency.HIGH.value,
    )
    test_db_session.add(trigger)
    test_db_session.commit()
    test_db_session.refresh(trigger)

    recipient = uuid.uuid4()
    corr_id = str(uuid.uuid4())
    log = NotificationLog(
        recipient_id=recipient,
        trigger_id=trigger.trigger_id,
        urgency=AlertUrgency.HIGH.value,
        channel=NotificationChannel.EMAIL.value,
        delivery_status=NotificationDeliveryStatus.SENT.value,
        message="Invoice #1001 past due threshold exceeded.",
        read_status=False,
        correlation_id=corr_id,
    )
    test_db_session.add(log)
    test_db_session.commit()
    test_db_session.refresh(log)

    assert isinstance(log.notification_id, uuid.UUID)
    assert log.recipient_id == recipient
    assert log.trigger_id == trigger.trigger_id
    assert log.urgency == "High"
    assert log.channel == "Email"
    assert log.delivery_status == "Sent"
    assert log.message == "Invoice #1001 past due threshold exceeded."
    assert log.read_status is False
    assert log.correlation_id == corr_id
    assert log.trigger == trigger
    assert trigger.notification_logs == [log]
    assert "<NotificationLog(" in repr(log)


def test_alert_suppression_creation_and_unique_hash(test_db_session: Session) -> None:
    """Test creating AlertSuppression and enforcing unique dedup_hash."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=15)
    hash_val = "abc123hashvalue"

    suppression = AlertSuppression(
        dedup_hash=hash_val,
        last_triggered_at=now,
        expires_at=expires,
    )
    test_db_session.add(suppression)
    test_db_session.commit()
    test_db_session.refresh(suppression)

    assert isinstance(suppression.suppression_id, uuid.UUID)
    assert suppression.dedup_hash == hash_val
    assert suppression.last_triggered_at == now
    assert suppression.expires_at == expires
    assert "<AlertSuppression(" in repr(suppression)

    # Attempt to add duplicate hash
    duplicate_suppression = AlertSuppression(
        dedup_hash=hash_val,
        last_triggered_at=now,
        expires_at=expires,
    )
    test_db_session.add(duplicate_suppression)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
