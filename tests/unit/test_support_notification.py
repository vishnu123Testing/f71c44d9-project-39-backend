"""Unit tests for support operations notification connector adapter [FR-5, FR-6, NFR-3, NFR-4, NFR-5]."""

from datetime import datetime, timezone
import json
from uuid import uuid4
import pytest

from src.connectors.notification import (
    InAppNotificationAdapter,
    NotificationChannel,
    NotificationConnector,
    NotificationPayload,
    NotificationResult,
    NotificationType,
    WebhookNotificationAdapter,
    get_notification_connector,
)


def test_in_app_notification_adapter_dispatch() -> None:
    """Verify in-app notification adapter dispatches, formats, and stores alerts [FR-5, NFR-3]."""
    adapter = InAppNotificationAdapter()
    ticket_id = uuid4()
    client_id = "client-alpha-123"
    target_due_at = datetime.now(timezone.utc)

    result = adapter.send_breach_warning(
        ticket_id=ticket_id,
        client_id=client_id,
        priority="P1",
        target_type="first_response",
        target_due_at=target_due_at,
        remaining_minutes=12.5,
    )

    assert result.success is True
    assert result.channel == NotificationChannel.IN_APP.value
    assert len(adapter.sent_notifications) == 1

    record = adapter.sent_notifications[0]
    assert record["ticket_id"] == str(ticket_id)
    assert record["client_id"] == client_id
    assert record["priority"] == "P1"
    assert record["notification_type"] == NotificationType.BREACH_WARNING.value


def test_in_app_notification_breach_alert() -> None:
    """Verify breach alert dispatch [FR-5]."""
    adapter = InAppNotificationAdapter()
    ticket_id = uuid4()
    client_id = "client-beta-456"
    breached_at = datetime.now(timezone.utc)

    result = adapter.send_breach_alert(
        ticket_id=ticket_id,
        client_id=client_id,
        priority="P2",
        target_type="resolution",
        breached_at=breached_at,
    )

    assert result.success is True
    assert len(adapter.sent_notifications) == 1
    record = adapter.sent_notifications[0]
    assert record["notification_type"] == NotificationType.BREACH_ALERT.value
    assert "SLA BREACH" in record["subject"]


def test_escalation_alert_dispatch() -> None:
    """Verify escalation pathway alert dispatch [FR-6]."""
    adapter = InAppNotificationAdapter()
    ticket_id = uuid4()
    client_id = "client-gamma-789"

    result = adapter.send_escalation_alert(
        ticket_id=ticket_id,
        client_id=client_id,
        priority="P1",
        source_tier="Tier 1",
        target_tier="Tier 2",
        reason="80% resolution SLA elapsed without resolution",
    )

    assert result.success is True
    assert len(adapter.sent_notifications) == 1
    record = adapter.sent_notifications[0]
    assert record["notification_type"] == NotificationType.ESCALATION_ALERT.value
    assert record["source_tier"] == "Tier 1"
    assert record["target_tier"] == "Tier 2"


def test_webhook_notification_adapter_simulation() -> None:
    """Verify webhook notification adapter operates safely without configured webhook URL."""
    adapter = WebhookNotificationAdapter(webhook_url=None)
    payload = NotificationPayload(
        ticket_id=uuid4(),
        client_id="client-wh-01",
        notification_type=NotificationType.BREACH_ALERT,
        priority="P3",
        subject="Test Webhook Alert",
        message="Simulated webhook delivery test.",
    )

    result = adapter.dispatch(payload)
    assert result.success is True
    assert result.channel == NotificationChannel.WEBHOOK.value
    assert len(adapter.sent_notifications) == 1


def test_notification_connector_disabled_master_toggle() -> None:
    """Verify master notification toggle suppresses dispatch when disabled [NFR-5]."""
    connector = NotificationConnector(notification_enabled=False)
    payload = NotificationPayload(
        ticket_id=uuid4(),
        client_id="client-toggle-01",
        notification_type=NotificationType.BREACH_WARNING,
        priority="P1",
        subject="Suppressed Alert",
        message="This alert should be suppressed.",
    )

    result = connector.dispatch(payload)
    assert result.success is True
    assert result.channel == "disabled"
    assert result.details.get("suppressed") is True


def test_get_notification_connector_factory() -> None:
    """Verify factory helper returns configured NotificationConnector instance [NFR-4]."""
    connector = get_notification_connector(channels=["in_app", "webhook"])
    assert isinstance(connector, NotificationConnector)
    assert connector.channels == ["in_app", "webhook"]
