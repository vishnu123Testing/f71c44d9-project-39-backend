"""SMTP email delivery connector conforming to Email port interface [FR-8, NFR-3].

This module provides the concrete SMTPEmailConnector implementation conforming to EmailDispatcherPort [FR-8].
Dispatches financial transparency and alert notifications via SMTP protocol using configuration parameters
retrieved strictly from environment variables or settings without hardcoded fallback credentials [NFR-3].
"""

from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import logging
import os
import smtplib
from typing import Any
from uuid import uuid4

from src.lib.config import settings
from src.lib.logging import get_correlation_id
from src.models.alerting import AlertUrgency, NotificationChannel, NotificationDeliveryStatus
from src.services.alerting.ports import (
    EmailDispatcherPort,
    EmailDispatchPayload,
    EmailDispatchResult,
)

logger = logging.getLogger(__name__)


def _sanitize_error_message(msg: str) -> str:
    """Sanitize error messages to prevent credential, password, or token exposure [NFR-3]."""
    if not msg:
        return "Unknown error occurred"
    cleaned = str(msg).split("?")[0]
    cleaned = cleaned.replace("\n", " ").strip()
    if len(cleaned) > 200:
        cleaned = cleaned[:197] + "..."
    return cleaned


class SMTPEmailConnector(EmailDispatcherPort):
    """SMTP email delivery connector conforming to Email port interface [FR-8, NFR-3]."""

    def __init__(
        self,
        smtp_host: str | None = None,
        smtp_port: int | None = None,
        smtp_user: str | None = None,
        smtp_password: str | None = None,
        default_sender: str | None = None,
        use_tls: bool = True,
        use_ssl: bool = False,
        timeout: float = 10.0,
        smtp_client: Any | None = None,
        simulate_failure: bool = False,
    ) -> None:
        """Initialize SMTPEmailConnector adapter [FR-8, NFR-3].

        Credentials and configuration are retrieved strictly from environment variables or settings
        without fallback defaults [NFR-3].

        Args:
            smtp_host: Optional SMTP server hostname or IP address.
            smtp_port: Optional SMTP server port (defaults to 587 if not set).
            smtp_user: Optional SMTP username / sender credential.
            smtp_password: Optional SMTP password credential.
            default_sender: Optional default sender email address.
            use_tls: Flag indicating whether to use STARTTLS (default True).
            use_ssl: Flag indicating whether to use SSL (e.g., port 465).
            timeout: Socket timeout in seconds for SMTP connection.
            smtp_client: Optional mock or custom SMTP client callable/object for testing.
            simulate_failure: Flag to simulate SMTP connection failure for testing.
        """
        self.smtp_host = (
            smtp_host
            if smtp_host is not None
            else getattr(settings, "SMTP_HOST", None) or os.getenv("SMTP_HOST")
        )
        self.smtp_port = (
            smtp_port
            if smtp_port is not None
            else getattr(settings, "SMTP_PORT", 587)
        )
        self.smtp_user = (
            smtp_user
            if smtp_user is not None
            else getattr(settings, "SMTP_USER", None) or os.getenv("SMTP_USER")
        )
        self.smtp_password = (
            smtp_password
            if smtp_password is not None
            else getattr(settings, "SMTP_PASSWORD", None) or os.getenv("SMTP_PASSWORD")
        )
        self.default_sender = (
            default_sender
            if default_sender is not None
            else getattr(settings, "DEFAULT_SENDER", None)
            or os.getenv("DEFAULT_SENDER")
            or self.smtp_user
        )
        self.use_tls = use_tls
        self.use_ssl = use_ssl
        self.timeout = timeout
        self.smtp_client = smtp_client
        self.simulate_failure = simulate_failure

    def send_email(self, payload: EmailDispatchPayload) -> EmailDispatchResult:
        """Dispatch an email notification to a recipient via SMTP [FR-8, NFR-3].

        Args:
            payload: EmailDispatchPayload containing recipient, subject, body, and metadata.

        Returns:
            EmailDispatchResult representing the outcome of the email dispatch attempt.
        """
        correlation_id = payload.correlation_id or get_correlation_id()
        urgency_val = (
            payload.urgency.value
            if isinstance(payload.urgency, AlertUrgency)
            else str(payload.urgency)
        )

        if self.simulate_failure:
            logger.warning(
                f"Simulated SMTP dispatch failure for recipient: {payload.recipient_email}",
                extra={"correlation_id": correlation_id},
            )
            return EmailDispatchResult(
                success=False,
                recipient_email=payload.recipient_email,
                channel=NotificationChannel.EMAIL.value,
                delivery_status=NotificationDeliveryStatus.FAILED.value,
                message_id=None,
                dispatched_at=datetime.now(timezone.utc),
                error_detail="Simulated SMTP failure",
                metadata={
                    "urgency": urgency_val,
                    "correlation_id": correlation_id,
                    **(payload.metadata or {}),
                },
            )

        if not payload.recipient_email or not payload.recipient_email.strip():
            logger.warning(
                "Email dispatch rejected: recipient_email is missing or empty",
                extra={"correlation_id": correlation_id},
            )
            return EmailDispatchResult(
                success=False,
                recipient_email=payload.recipient_email or "",
                channel=NotificationChannel.EMAIL.value,
                delivery_status=NotificationDeliveryStatus.FAILED.value,
                message_id=None,
                dispatched_at=datetime.now(timezone.utc),
                error_detail="Recipient email address is required",
                metadata={
                    "urgency": urgency_val,
                    "correlation_id": correlation_id,
                    **(payload.metadata or {}),
                },
            )

        if not self.smtp_host:
            error_msg = "SMTP_HOST is not configured in environment variables (NFR-3)"
            logger.warning(
                f"Email dispatch failed for {payload.recipient_email}: {error_msg}",
                extra={"correlation_id": correlation_id},
            )
            return EmailDispatchResult(
                success=False,
                recipient_email=payload.recipient_email,
                channel=NotificationChannel.EMAIL.value,
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

        sender_email = (
            payload.sender_email
            or self.default_sender
            or self.smtp_user
        )
        if not sender_email:
            error_msg = "Sender email address is not specified and DEFAULT_SENDER / SMTP_USER is not configured"
            logger.warning(
                f"Email dispatch failed for {payload.recipient_email}: {error_msg}",
                extra={"correlation_id": correlation_id},
            )
            return EmailDispatchResult(
                success=False,
                recipient_email=payload.recipient_email,
                channel=NotificationChannel.EMAIL.value,
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

        message_id = f"email-{uuid4()}"

        # Construct MIME email message
        msg = MIMEMultipart("alternative") if payload.body_html else MIMEMultipart()
        msg["Subject"] = payload.subject or ""
        msg["From"] = sender_email
        msg["To"] = payload.recipient_email
        msg["Message-ID"] = f"<{message_id}@{self.smtp_host}>"
        if correlation_id:
            msg["X-Correlation-ID"] = correlation_id

        msg.attach(MIMEText(payload.body_text or "", "plain", "utf-8"))
        if payload.body_html:
            msg.attach(MIMEText(payload.body_html, "html", "utf-8"))

        # Send via injected custom client or standard smtplib
        try:
            if self.smtp_client is not None:
                if callable(self.smtp_client):
                    self.smtp_client(
                        host=self.smtp_host,
                        port=self.smtp_port,
                        user=self.smtp_user,
                        password=self.smtp_password,
                        sender=sender_email,
                        recipient=payload.recipient_email,
                        message=msg.as_string(),
                    )
                elif hasattr(self.smtp_client, "send_message"):
                    self.smtp_client.send_message(msg)
                elif hasattr(self.smtp_client, "sendmail"):
                    self.smtp_client.sendmail(
                        sender_email, [payload.recipient_email], msg.as_string()
                    )
            else:
                if self.use_ssl or self.smtp_port == 465:
                    smtp_server = smtplib.SMTP_SSL(
                        self.smtp_host, self.smtp_port, timeout=self.timeout
                    )
                else:
                    smtp_server = smtplib.SMTP(
                        self.smtp_host, self.smtp_port, timeout=self.timeout
                    )
                    if self.use_tls:
                        smtp_server.starttls()

                with smtp_server:
                    if self.smtp_user and self.smtp_password:
                        smtp_server.login(self.smtp_user, self.smtp_password)
                    smtp_server.send_message(msg)

            logger.info(
                f"Successfully dispatched email to {payload.recipient_email}",
                extra={
                    "recipient": payload.recipient_email,
                    "subject": payload.subject,
                    "message_id": message_id,
                    "urgency": urgency_val,
                    "correlation_id": correlation_id,
                },
            )

            return EmailDispatchResult(
                success=True,
                recipient_email=payload.recipient_email,
                channel=NotificationChannel.EMAIL.value,
                delivery_status=NotificationDeliveryStatus.SENT.value,
                message_id=message_id,
                dispatched_at=datetime.now(timezone.utc),
                error_detail=None,
                metadata={
                    "sender": sender_email,
                    "subject": payload.subject,
                    "urgency": urgency_val,
                    "correlation_id": correlation_id,
                    **(payload.metadata or {}),
                },
            )
        except Exception as e:
            sanitized_err = _sanitize_error_message(str(e))
            logger.warning(
                f"SMTP email dispatch to {payload.recipient_email} failed: {sanitized_err}",
                extra={"correlation_id": correlation_id},
            )
            return EmailDispatchResult(
                success=False,
                recipient_email=payload.recipient_email,
                channel=NotificationChannel.EMAIL.value,
                delivery_status=NotificationDeliveryStatus.FAILED.value,
                message_id=None,
                dispatched_at=datetime.now(timezone.utc),
                error_detail=sanitized_err,
                metadata={
                    "sender": sender_email,
                    "subject": payload.subject,
                    "urgency": urgency_val,
                    "correlation_id": correlation_id,
                    **(payload.metadata or {}),
                },
            )


# Aliases for flexible connector instantiation
EmailConnector = SMTPEmailConnector
SMTPEmailAdapter = SMTPEmailConnector
