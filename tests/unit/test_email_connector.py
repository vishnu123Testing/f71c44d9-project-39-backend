"""Unit tests for SMTPEmailConnector [FR-8, NFR-3]."""

from unittest.mock import MagicMock, patch

import pytest

from src.connectors.email import EmailConnector, SMTPEmailAdapter, SMTPEmailConnector
from src.models.alerting import AlertUrgency, NotificationChannel, NotificationDeliveryStatus
from src.services.alerting.ports import EmailDispatchPayload


def test_email_connector_aliases():
    """Verify class aliases match SMTPEmailConnector."""
    assert EmailConnector is SMTPEmailConnector
    assert SMTPEmailAdapter is SMTPEmailConnector


def test_send_email_success_with_callable_client():
    """Test successful email dispatch using callable smtp_client dependency injection."""
    dispatched_messages = []

    def mock_client(host, port, user, password, sender, recipient, message):
        dispatched_messages.append(
            {
                "host": host,
                "port": port,
                "user": user,
                "password": password,
                "sender": sender,
                "recipient": recipient,
                "message": message,
            }
        )

    connector = SMTPEmailConnector(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="user@example.com",
        smtp_password="secretpassword",
        default_sender="alerts@example.com",
        smtp_client=mock_client,
    )

    payload = EmailDispatchPayload(
        recipient_email="finance@example.com",
        subject="Past Due Invoice Alert",
        body_text="Invoice #1001 is past due.",
        body_html="<p>Invoice #1001 is past due.</p>",
        urgency=AlertUrgency.HIGH,
        correlation_id="corr-12345",
    )

    result = connector.send_email(payload)

    assert result.success is True
    assert result.recipient_email == "finance@example.com"
    assert result.channel == NotificationChannel.EMAIL.value
    assert result.delivery_status == NotificationDeliveryStatus.SENT.value
    assert result.message_id is not None
    assert result.error_detail is None
    assert result.metadata["sender"] == "alerts@example.com"
    assert result.metadata["urgency"] == AlertUrgency.HIGH.value

    assert len(dispatched_messages) == 1
    sent = dispatched_messages[0]
    assert sent["host"] == "smtp.example.com"
    assert sent["sender"] == "alerts@example.com"
    assert sent["recipient"] == "finance@example.com"
    assert "Subject: Past Due Invoice Alert" in sent["message"]
    assert "X-Correlation-ID: corr-12345" in sent["message"]


def test_send_email_success_with_object_client():
    """Test successful email dispatch using object with send_message method."""
    mock_smtp_obj = MagicMock()
    connector = SMTPEmailConnector(
        smtp_host="mail.example.com",
        default_sender="sender@example.com",
        smtp_client=mock_smtp_obj,
    )

    payload = EmailDispatchPayload(
        recipient_email="user@example.com",
        subject="Test Notification",
        body_text="Hello World",
    )

    result = connector.send_email(payload)

    assert result.success is True
    assert mock_smtp_obj.send_message.called
    assert result.delivery_status == NotificationDeliveryStatus.SENT.value


def test_send_email_missing_smtp_host():
    """Test failure when SMTP_HOST is not configured (NFR-3)."""
    connector = SMTPEmailConnector(
        smtp_host=None,
        default_sender="sender@example.com",
    )

    payload = EmailDispatchPayload(
        recipient_email="user@example.com",
        subject="Test",
        body_text="Test Body",
    )

    result = connector.send_email(payload)

    assert result.success is False
    assert result.delivery_status == NotificationDeliveryStatus.FAILED.value
    assert "SMTP_HOST is not configured" in (result.error_detail or "")


def test_send_email_missing_sender():
    """Test failure when sender email cannot be determined."""
    connector = SMTPEmailConnector(
        smtp_host="smtp.example.com",
        smtp_user=None,
        default_sender=None,
    )

    payload = EmailDispatchPayload(
        recipient_email="user@example.com",
        subject="Test",
        body_text="Test Body",
        sender_email=None,
    )

    result = connector.send_email(payload)

    assert result.success is False
    assert result.delivery_status == NotificationDeliveryStatus.FAILED.value
    assert "Sender email address is not specified" in (result.error_detail or "")


def test_send_email_empty_recipient():
    """Test failure when recipient email is empty or invalid."""
    connector = SMTPEmailConnector(
        smtp_host="smtp.example.com",
        default_sender="sender@example.com",
    )

    payload = EmailDispatchPayload(
        recipient_email="   ",
        subject="Test",
        body_text="Test Body",
    )

    result = connector.send_email(payload)

    assert result.success is False
    assert result.delivery_status == NotificationDeliveryStatus.FAILED.value
    assert "Recipient email address is required" in (result.error_detail or "")


def test_send_email_simulated_failure():
    """Test connector behavior when simulate_failure is True."""
    connector = SMTPEmailConnector(
        smtp_host="smtp.example.com",
        default_sender="sender@example.com",
        simulate_failure=True,
    )

    payload = EmailDispatchPayload(
        recipient_email="user@example.com",
        subject="Test",
        body_text="Test Body",
    )

    result = connector.send_email(payload)

    assert result.success is False
    assert result.delivery_status == NotificationDeliveryStatus.FAILED.value
    assert result.error_detail == "Simulated SMTP failure"


def test_send_email_smtp_exception_handling():
    """Test handling of SMTP exceptions during message dispatch."""

    def failing_client(*args, **kwargs):
        raise RuntimeError("Connection refused by SMTP server")

    connector = SMTPEmailConnector(
        smtp_host="smtp.example.com",
        default_sender="sender@example.com",
        smtp_client=failing_client,
    )

    payload = EmailDispatchPayload(
        recipient_email="user@example.com",
        subject="Test Error",
        body_text="Test Body",
    )

    result = connector.send_email(payload)

    assert result.success is False
    assert result.delivery_status == NotificationDeliveryStatus.FAILED.value
    assert "Connection refused" in (result.error_detail or "")


def test_convenience_dispatch_method():
    """Test convenience dispatch method inherited from EmailDispatcherPort."""
    mock_smtp_obj = MagicMock()
    connector = SMTPEmailConnector(
        smtp_host="smtp.example.com",
        default_sender="alerts@example.com",
        smtp_client=mock_smtp_obj,
    )

    result = connector.dispatch(
        recipient_email="user@example.com",
        subject="Convenience Method Test",
        body_text="Body content",
        urgency=AlertUrgency.LOW,
    )

    assert result.success is True
    assert result.recipient_email == "user@example.com"
    assert mock_smtp_obj.send_message.called
