"""Unit tests for SMSGatewayConnector [FR-8, NFR-2, NFR-3]."""

from unittest.mock import MagicMock, patch
import urllib.error

import pytest

from src.connectors.sms import SMSAdapter, SMSConnector, SMSGatewayAdapter, SMSGatewayConnector
from src.models.alerting import AlertUrgency, NotificationChannel, NotificationDeliveryStatus
from src.services.alerting.ports import SMSDispatchPayload


def test_sms_connector_aliases():
    """Verify class aliases match SMSGatewayConnector."""
    assert SMSConnector is SMSGatewayConnector
    assert SMSGatewayAdapter is SMSGatewayConnector
    assert SMSAdapter is SMSGatewayConnector


def test_send_sms_success_with_callable_client():
    """Test successful SMS dispatch using callable http_client dependency injection."""
    dispatched_requests = []

    def mock_client(url, headers, payload, timeout):
        dispatched_requests.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout": timeout,
            }
        )
        return {"status": "ok", "message_id": "gateway-sms-789"}

    connector = SMSGatewayConnector(
        sms_gateway_url="https://sms.example.com/api/v1/send",
        sms_api_key="secret-sms-key",
        http_client=mock_client,
    )

    payload = SMSDispatchPayload(
        phone_number="+15551234567",
        message="Critical financial alert: High balance due",
        urgency=AlertUrgency.HIGH,
        correlation_id="corr-sms-101",
    )

    result = connector.send_sms(payload)

    assert result.success is True
    assert result.phone_number == "+15551234567"
    assert result.channel == NotificationChannel.SMS.value
    assert result.delivery_status == NotificationDeliveryStatus.SENT.value
    assert result.message_id == "gateway-sms-789"
    assert result.error_detail is None
    assert result.metadata["urgency"] == AlertUrgency.HIGH.value
    assert result.metadata["correlation_id"] == "corr-sms-101"

    assert len(dispatched_requests) == 1
    sent = dispatched_requests[0]
    assert sent["url"] == "https://sms.example.com/api/v1/send"
    assert sent["headers"]["Authorization"] == "Bearer secret-sms-key"
    assert sent["headers"]["X-Correlation-ID"] == "corr-sms-101"
    assert sent["payload"]["to"] == "+15551234567"
    assert sent["payload"]["message"] == "Critical financial alert: High balance due"


def test_send_sms_success_with_object_post_client():
    """Test successful SMS dispatch using object with post method."""
    mock_post_obj = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"message_id": "sms-post-123"}
    mock_post_obj.post.return_value = mock_response

    connector = SMSGatewayConnector(
        sms_gateway_url="https://sms.example.com/send",
        sms_api_key="api-key-xyz",
        http_client=mock_post_obj,
    )

    payload = SMSDispatchPayload(
        phone_number="+15559876543",
        message="Payment confirmation notice",
        urgency=AlertUrgency.MEDIUM,
    )

    result = connector.send_sms(payload)

    assert result.success is True
    assert result.message_id == "sms-post-123"
    assert mock_post_obj.post.called


def test_send_sms_missing_gateway_url():
    """Test failure when SMS_GATEWAY_URL is not configured (NFR-3)."""
    connector = SMSGatewayConnector(
        sms_gateway_url=None,
        sms_api_key="valid-api-key",
    )

    payload = SMSDispatchPayload(
        phone_number="+15551112222",
        message="Test alert",
    )

    result = connector.send_sms(payload)

    assert result.success is False
    assert result.delivery_status == NotificationDeliveryStatus.FAILED.value
    assert "SMS_GATEWAY_URL is not configured" in result.error_detail


def test_send_sms_missing_api_key():
    """Test failure when SMS_API_KEY is not configured (NFR-3)."""
    connector = SMSGatewayConnector(
        sms_gateway_url="https://sms.example.com/send",
        sms_api_key=None,
    )

    payload = SMSDispatchPayload(
        phone_number="+15551112222",
        message="Test alert",
    )

    result = connector.send_sms(payload)

    assert result.success is False
    assert result.delivery_status == NotificationDeliveryStatus.FAILED.value
    assert "SMS_API_KEY is not configured" in result.error_detail


def test_send_sms_empty_phone_number():
    """Test rejection when phone_number is empty."""
    connector = SMSGatewayConnector(
        sms_gateway_url="https://sms.example.com/send",
        sms_api_key="valid-key",
    )

    payload = SMSDispatchPayload(
        phone_number="  ",
        message="Test alert",
    )

    result = connector.send_sms(payload)

    assert result.success is False
    assert result.delivery_status == NotificationDeliveryStatus.FAILED.value
    assert "Phone number is required" in result.error_detail


def test_send_sms_simulated_failure():
    """Test simulated failure mode."""
    connector = SMSGatewayConnector(
        sms_gateway_url="https://sms.example.com/send",
        sms_api_key="valid-key",
        simulate_failure=True,
    )

    payload = SMSDispatchPayload(
        phone_number="+15551234567",
        message="Test alert",
        correlation_id="corr-sim-fail",
    )

    result = connector.send_sms(payload)

    assert result.success is False
    assert result.delivery_status == NotificationDeliveryStatus.FAILED.value
    assert result.error_detail == "Simulated SMS gateway failure"


def test_send_sms_gateway_exception_handling():
    """Test graceful handling and warning logging on HTTP exception [NFR-2, NFR-3]."""

    def failing_client(url, headers, payload, timeout):
        raise RuntimeError("Gateway connection refused?secret_token=abc123secret")

    connector = SMSGatewayConnector(
        sms_gateway_url="https://sms.example.com/send",
        sms_api_key="secret-key",
        http_client=failing_client,
    )

    payload = SMSDispatchPayload(
        phone_number="+15553334444",
        message="Alert message",
        correlation_id="corr-err-001",
    )

    result = connector.send_sms(payload)

    assert result.success is False
    assert result.delivery_status == NotificationDeliveryStatus.FAILED.value
    assert "Gateway connection refused" in result.error_detail
    # Sensitive URL params must be sanitized
    assert "secret_token" not in result.error_detail


def test_dispatch_convenience_method():
    """Test the inherited convenience dispatch method from SMSDispatcherPort."""
    mock_post_obj = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"message_id": "sms-convenience-1"}
    mock_post_obj.post.return_value = mock_response

    connector = SMSGatewayConnector(
        sms_gateway_url="https://sms.example.com/send",
        sms_api_key="key-123",
        http_client=mock_post_obj,
    )

    result = connector.dispatch(
        phone_number="+15557778888",
        message="Convenience dispatch test",
        urgency=AlertUrgency.HIGH,
        correlation_id="corr-conv-1",
    )

    assert result.success is True
    assert result.phone_number == "+15557778888"
    assert result.message_id == "sms-convenience-1"
