"""Unit tests for InAppPersistenceConnector [FR-5, FR-8, NFR-2, NFR-3]."""

from unittest.mock import MagicMock
import uuid

import pytest

from src.connectors.in_app import (
    InAppAdapter,
    InAppConnector,
    InAppPersistenceAdapter,
    InAppPersistenceConnector,
)
from src.models.alerting import AlertUrgency, NotificationChannel, NotificationDeliveryStatus
from src.services.alerting.ports import InAppDispatchPayload


def test_in_app_connector_aliases():
    """Verify class aliases match InAppPersistenceConnector."""
    assert InAppConnector is InAppPersistenceConnector
    assert InAppAdapter is InAppPersistenceConnector
    assert InAppPersistenceAdapter is InAppPersistenceConnector


def test_send_in_app_success_in_memory():
    """Test successful in-app notification dispatch using in-memory fallback."""
    connector = InAppPersistenceConnector(in_memory_fallback=True)

    recipient_uuid = uuid.uuid4()
    trigger_uuid = uuid.uuid4()

    payload = InAppDispatchPayload(
        recipient_id=recipient_uuid,
        title="Payment Reminder",
        message="Invoice #2002 is due tomorrow.",
        urgency=AlertUrgency.MEDIUM,
        trigger_id=trigger_uuid,
        correlation_id="corr-inapp-101",
    )

    result = connector.send_in_app(payload)

    assert result.success is True
    assert result.recipient_id == str(recipient_uuid)
    assert result.channel == NotificationChannel.IN_APP.value
    assert result.delivery_status == NotificationDeliveryStatus.SENT.value
    assert result.notification_id is not None
    assert result.error_detail is None
    assert result.metadata["correlation_id"] == "corr-inapp-101"
    assert result.metadata["urgency"] == AlertUrgency.MEDIUM.value

    # Verify log entry in memory
    notifications = connector.get_in_app_notifications(recipient_id=recipient_uuid)
    assert len(notifications) == 1
    assert notifications[0].message == "Payment Reminder: Invoice #2002 is due tomorrow."
    assert notifications[0].read_status is False


def test_send_in_app_with_db_session():
    """Test successful in-app notification dispatch using active DB session."""
    mock_db = MagicMock()

    connector = InAppPersistenceConnector(db=mock_db)

    recipient_uuid = uuid.uuid4()
    payload = InAppDispatchPayload(
        recipient_id=recipient_uuid,
        title="High Balance Alert",
        message="Past due balance exceeds $10,000",
        urgency=AlertUrgency.HIGH,
        correlation_id="corr-inapp-102",
    )

    result = connector.send_in_app(payload)

    assert result.success is True
    assert mock_db.add.called
    assert mock_db.commit.called
    assert result.delivery_status == NotificationDeliveryStatus.SENT.value


def test_send_in_app_simulate_failure():
    """Test simulated failure during in-app notification persistence."""
    connector = InAppPersistenceConnector(simulate_failure=True)

    payload = InAppDispatchPayload(
        recipient_id=uuid.uuid4(),
        title="System Notice",
        message="Maintenance window scheduled",
    )

    result = connector.send_in_app(payload)

    assert result.success is False
    assert result.delivery_status == NotificationDeliveryStatus.FAILED.value
    assert result.error_detail == "Simulated in-app persistence failure"


def test_send_in_app_db_error_fallback():
    """Test fallback to in-memory store when database commit fails."""
    mock_db = MagicMock()
    mock_db.commit.side_effect = Exception("Database connection lost")

    connector = InAppPersistenceConnector(db=mock_db, in_memory_fallback=True)

    recipient_uuid = uuid.uuid4()
    payload = InAppDispatchPayload(
        recipient_id=recipient_uuid,
        title="Fallback Notice",
        message="Testing fallback behavior",
    )

    result = connector.send_in_app(payload)

    assert mock_db.rollback.called
    assert result.success is True  # Persisted to in-memory fallback
    assert len(connector._in_memory_logs) == 1


def test_send_in_app_db_error_no_fallback():
    """Test failure response when database commit fails and fallback is disabled."""
    mock_db = MagicMock()
    mock_db.commit.side_effect = Exception("Database connection lost")

    connector = InAppPersistenceConnector(db=mock_db, in_memory_fallback=False)

    payload = InAppDispatchPayload(
        recipient_id=uuid.uuid4(),
        title="No Fallback Notice",
        message="Testing no fallback behavior",
    )

    result = connector.send_in_app(payload)

    assert mock_db.rollback.called
    assert result.success is False
    assert result.delivery_status == NotificationDeliveryStatus.FAILED.value
    assert "Database persistence error" in result.error_detail


def test_mark_as_read_in_memory():
    """Test marking an in-memory in-app notification as read."""
    connector = InAppPersistenceConnector(in_memory_fallback=True)

    payload = InAppDispatchPayload(
        recipient_id=uuid.uuid4(),
        title="Unread Notice",
        message="Please review invoice.",
    )

    result = connector.send_in_app(payload)
    notif_id = result.notification_id

    # Initially unread
    unread_logs = connector.get_in_app_notifications(unread_only=True)
    assert len(unread_logs) == 1

    # Mark as read
    marked = connector.mark_as_read(notif_id)
    assert marked is True

    # No longer in unread list
    unread_logs_after = connector.get_in_app_notifications(unread_only=True)
    assert len(unread_logs_after) == 0


def test_dispatch_convenience_method():
    """Test convenience dispatch method inherited from InAppDispatcherPort."""
    connector = InAppPersistenceConnector(in_memory_fallback=True)

    recipient_uuid = uuid.uuid4()
    result = connector.dispatch(
        recipient_id=recipient_uuid,
        title="Convenience Dispatch",
        message="Testing convenience method",
        urgency=AlertUrgency.LOW,
        correlation_id="corr-conv-1",
    )

    assert result.success is True
    assert result.recipient_id == str(recipient_uuid)
    assert result.metadata["urgency"] == AlertUrgency.LOW.value
