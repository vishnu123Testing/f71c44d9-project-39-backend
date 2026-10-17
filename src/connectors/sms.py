"""SMS gateway delivery connector conforming to SMS port interface [FR-8, NFR-2, NFR-3].

This module provides the concrete SMSGatewayConnector implementation conforming to SMSDispatcherPort [FR-8].
Dispatches financial transparency and alert notifications via SMS gateway HTTP service using configuration parameters
retrieved strictly from environment variables or settings without hardcoded fallback credentials [NFR-3].
Outputs structured warning logging on delivery failure containing contextual correlation IDs [NFR-2].
"""

from datetime import datetime, timezone
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any
from uuid import uuid4

from src.lib.config import settings
from src.lib.logging import get_correlation_id
from src.models.alerting import AlertUrgency, NotificationChannel, NotificationDeliveryStatus
from src.services.alerting.ports import (
    SMSDispatcherPort,
    SMSDispatchPayload,
    SMSDispatchResult,
)

logger = logging.getLogger(__name__)


def _sanitize_error_message(msg: str) -> str:
    """Sanitize error messages to prevent API key, token, or sensitive parameter exposure [NFR-3]."""
    if not msg:
        return "Unknown error occurred"
    cleaned = str(msg).split("?")[0]
    cleaned = cleaned.replace("\n", " ").strip()
    if len(cleaned) > 200:
        cleaned = cleaned[:197] + "..."
    return cleaned


class SMSGatewayConnector(SMSDispatcherPort):
    """SMS gateway delivery connector conforming to SMS port interface [FR-8, NFR-2, NFR-3]."""

    def __init__(
        self,
        sms_gateway_url: str | None = None,
        sms_api_key: str | None = None,
        timeout: float = 10.0,
        http_client: Any | None = None,
        simulate_failure: bool = False,
    ) -> None:
        """Initialize SMSGatewayConnector adapter [FR-8, NFR-3].

        Credentials and gateway endpoints are retrieved strictly from environment variables or settings
        without fallback defaults [NFR-3].

        Args:
            sms_gateway_url: Optional SMS gateway URL endpoint.
            sms_api_key: Optional SMS gateway API key / token credential.
            timeout: Socket timeout in seconds for HTTP gateway request.
            http_client: Optional mock or custom HTTP client callable/object for testing.
            simulate_failure: Flag to simulate SMS gateway delivery failure for testing.
        """
        self.sms_gateway_url = (
            sms_gateway_url
            if sms_gateway_url is not None
            else getattr(settings, "SMS_GATEWAY_URL", None) or os.getenv("SMS_GATEWAY_URL")
        )
        self.sms_api_key = (
            sms_api_key
            if sms_api_key is not None
            else getattr(settings, "SMS_API_KEY", None) or os.getenv("SMS_API_KEY")
        )
        self.timeout = timeout
        self.http_client = http_client
        self.simulate_failure = simulate_failure

    def send_sms(self, payload: SMSDispatchPayload) -> SMSDispatchResult:
        """Dispatch an SMS notification to a phone number via SMS gateway [FR-8, NFR-2, NFR-3].

        Args:
            payload: SMSDispatchPayload containing phone number, message content, urgency, and metadata.

        Returns:
            SMSDispatchResult representing the outcome of the SMS dispatch attempt.
        """
        correlation_id = payload.correlation_id or get_correlation_id()
        urgency_val = (
            payload.urgency.value
            if isinstance(payload.urgency, AlertUrgency)
            else str(payload.urgency)
        )

        if self.simulate_failure:
            logger.warning(
                f"Simulated SMS gateway dispatch failure for recipient: {payload.phone_number}",
                extra={"correlation_id": correlation_id},
            )
            return SMSDispatchResult(
                success=False,
                phone_number=payload.phone_number or "",
                channel=NotificationChannel.SMS.value,
                delivery_status=NotificationDeliveryStatus.FAILED.value,
                message_id=None,
                dispatched_at=datetime.now(timezone.utc),
                error_detail="Simulated SMS gateway failure",
                metadata={
                    "urgency": urgency_val,
                    "correlation_id": correlation_id,
                    **(payload.metadata or {}),
                },
            )

        if not payload.phone_number or not payload.phone_number.strip():
            logger.warning(
                "SMS dispatch rejected: phone_number is missing or empty",
                extra={"correlation_id": correlation_id},
            )
            return SMSDispatchResult(
                success=False,
                phone_number=payload.phone_number or "",
                channel=NotificationChannel.SMS.value,
                delivery_status=NotificationDeliveryStatus.FAILED.value,
                message_id=None,
                dispatched_at=datetime.now(timezone.utc),
                error_detail="Phone number is required",
                metadata={
                    "urgency": urgency_val,
                    "correlation_id": correlation_id,
                    **(payload.metadata or {}),
                },
            )

        if not self.sms_gateway_url:
            error_msg = "SMS_GATEWAY_URL is not configured in environment variables (NFR-3)"
            logger.warning(
                f"SMS dispatch failed for {payload.phone_number}: {error_msg}",
                extra={"correlation_id": correlation_id},
            )
            return SMSDispatchResult(
                success=False,
                phone_number=payload.phone_number,
                channel=NotificationChannel.SMS.value,
                delivery_status=NotificationDeliveryStatus.FAILED.value,
                message_id=None,
                dispatched_at=datetime.now(timezone.utc),
                error_detail=error_msg,
                metadata={
                    "urgency": urgency_val,
                    "correlation_id": correlation_id,
                    **(payload.metadata or {}),
                },
            )

        if not self.sms_api_key:
            error_msg = "SMS_API_KEY is not configured in environment variables (NFR-3)"
            logger.warning(
                f"SMS dispatch failed for {payload.phone_number}: {error_msg}",
                extra={"correlation_id": correlation_id},
            )
            return SMSDispatchResult(
                success=False,
                phone_number=payload.phone_number,
                channel=NotificationChannel.SMS.value,
                delivery_status=NotificationDeliveryStatus.FAILED.value,
                message_id=None,
                dispatched_at=datetime.now(timezone.utc),
                error_detail=error_msg,
                metadata={
                    "urgency": urgency_val,
                    "correlation_id": correlation_id,
                    **(payload.metadata or {}),
                },
            )

        message_id = f"sms-{uuid4()}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.sms_api_key}",
            "X-Api-Key": self.sms_api_key,
        }
        if correlation_id:
            headers["X-Correlation-ID"] = correlation_id

        req_payload = {
            "to": payload.phone_number,
            "message": payload.message,
            "urgency": urgency_val,
            "correlation_id": correlation_id,
        }

        try:
            if self.http_client is not None:
                if callable(self.http_client):
                    res = self.http_client(
                        url=self.sms_gateway_url,
                        headers=headers,
                        payload=req_payload,
                        timeout=self.timeout,
                    )
                    if isinstance(res, dict) and res.get("message_id"):
                        message_id = str(res["message_id"])
                elif hasattr(self.http_client, "post"):
                    resp = self.http_client.post(
                        self.sms_gateway_url,
                        headers=headers,
                        json=req_payload,
                        timeout=self.timeout,
                    )
                    if hasattr(resp, "status_code") and resp.status_code >= 400:
                        raise RuntimeError(f"HTTP {resp.status_code}: {getattr(resp, 'text', '')}")
                    if hasattr(resp, "json") and callable(resp.json):
                        res = resp.json()
                        if isinstance(res, dict) and res.get("message_id"):
                            message_id = str(res["message_id"])
                elif hasattr(self.http_client, "request"):
                    resp = self.http_client.request(
                        "POST",
                        self.sms_gateway_url,
                        headers=headers,
                        json=req_payload,
                        timeout=self.timeout,
                    )
                    if hasattr(resp, "status_code") and resp.status_code >= 400:
                        raise RuntimeError(f"HTTP {resp.status_code}: {getattr(resp, 'text', '')}")
                    if hasattr(resp, "json") and callable(resp.json):
                        res = resp.json()
                        if isinstance(res, dict) and res.get("message_id"):
                            message_id = str(res["message_id"])
                else:
                    raise ValueError(f"Unsupported http_client interface: {type(self.http_client)}")
            else:
                req_data = json.dumps(req_payload).encode("utf-8")
                req = urllib.request.Request(
                    url=self.sms_gateway_url,
                    data=req_data,
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    status_code = response.status
                    if status_code >= 400:
                        raise RuntimeError(f"SMS Gateway returned HTTP status {status_code}")
                    resp_text = response.read().decode("utf-8")
                    if resp_text:
                        try:
                            resp_data = json.loads(resp_text)
                            if isinstance(resp_data, dict) and resp_data.get("message_id"):
                                message_id = str(resp_data["message_id"])
                        except json.JSONDecodeError:
                            pass

            logger.info(
                f"Successfully dispatched SMS to {payload.phone_number}",
                extra={
                    "phone_number": payload.phone_number,
                    "message_id": message_id,
                    "urgency": urgency_val,
                    "correlation_id": correlation_id,
                },
            )

            return SMSDispatchResult(
                success=True,
                phone_number=payload.phone_number,
                channel=NotificationChannel.SMS.value,
                delivery_status=NotificationDeliveryStatus.SENT.value,
                message_id=message_id,
                dispatched_at=datetime.now(timezone.utc),
                error_detail=None,
                metadata={
                    "gateway_url": self.sms_gateway_url,
                    "urgency": urgency_val,
                    "correlation_id": correlation_id,
                    **(payload.metadata or {}),
                },
            )
        except Exception as e:
            sanitized_err = _sanitize_error_message(str(e))
            logger.warning(
                f"SMS gateway dispatch to {payload.phone_number} failed: {sanitized_err}",
                extra={"correlation_id": correlation_id},
            )
            return SMSDispatchResult(
                success=False,
                phone_number=payload.phone_number,
                channel=NotificationChannel.SMS.value,
                delivery_status=NotificationDeliveryStatus.FAILED.value,
                message_id=None,
                dispatched_at=datetime.now(timezone.utc),
                error_detail=sanitized_err,
                metadata={
                    "urgency": urgency_val,
                    "correlation_id": correlation_id,
                    **(payload.metadata or {}),
                },
            )


# Aliases for alternative naming conventions
SMSConnector = SMSGatewayConnector
SMSGatewayAdapter = SMSGatewayConnector
SMSAdapter = SMSGatewayConnector
