"""Celery background worker tasks for multi-channel alert dispatching, audit log recording, and SMS fallback execution [FR-8, FR-12, NFR-2, NFR-4].

This module provides Celery worker tasks and execution functions to:
1. Dispatch financial transparency and alert notifications across In-App, Email, and SMS channels based on urgency and role preferences [FR-8].
2. Record structured audit logs in the notification_logs database table for all dispatch attempts and channel outcomes [FR-12].
3. Handle SMS gateway failures gracefully by emitting structured warning logs with correlation IDs and executing fallback dispatches to Email and In-App channels [FR-8, NFR-2, NFR-4].
"""

from datetime import datetime, timezone
import logging
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.connectors.email import SMTPEmailConnector
from src.connectors.in_app import InAppPersistenceConnector
from src.connectors.sms import SMSGatewayConnector
from src.lib.logging import get_correlation_id
from src.models.alerting import (
    AlertUrgency,
    NotificationChannel,
    NotificationDeliveryStatus,
    NotificationLog,
)
from src.models.database import SessionLocal
from src.services.alerting.evaluator import route_notification_channels
from src.services.alerting.ports import (
    EmailDispatcherPort,
    EmailDispatchPayload,
    EmailDispatchResult,
    InAppDispatcherPort,
    InAppDispatchPayload,
    InAppDispatchResult,
    SMSDispatcherPort,
    SMSDispatchPayload,
    SMSDispatchResult,
)
from src.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _parse_uuid(val: Any) -> uuid.UUID | None:
    """Safely convert a string or UUID object to a UUID, returning None if invalid."""
    if val is None:
        return None
    if isinstance(val, uuid.UUID):
        return val
    try:
        return uuid.UUID(str(val))
    except (ValueError, TypeError):
        return None


def run_notification_dispatch(
    db: Session | None = None,
    recipient_id: str | uuid.UUID | None = None,
    recipient_email: str | None = None,
    recipient_phone: str | None = None,
    title: str = "Financial Alert",
    message: str = "",
    urgency: str | AlertUrgency = AlertUrgency.MEDIUM.value,
    channels: list[str] | None = None,
    trigger_id: str | uuid.UUID | None = None,
    role_name: str | None = None,
    event_type: str | None = None,
    correlation_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    email_connector: EmailDispatcherPort | None = None,
    sms_connector: SMSDispatcherPort | None = None,
    in_app_connector: InAppDispatcherPort | None = None,
) -> dict[str, Any]:
    """Execute multi-channel notification dispatching, audit logging, and SMS fallback handling [FR-8, FR-12, NFR-2, NFR-4].

    Args:
        db: Optional active synchronous SQLAlchemy session. If None, SessionLocal is used.
        recipient_id: Optional UUID or string identifier for the target recipient.
        recipient_email: Optional target email address.
        recipient_phone: Optional target phone number for SMS.
        title: Notification header / subject title.
        message: Primary notification message body.
        urgency: Urgency level ('Low', 'Medium', 'High').
        channels: Optional explicit list of target channels (e.g. ['InApp', 'Email', 'SMS']).
        trigger_id: Optional associated AlertTrigger UUID.
        role_name: Optional recipient role name for preference routing.
        event_type: Optional event criteria name for preference routing.
        correlation_id: Optional contextual correlation ID string.
        metadata: Optional additional metadata dictionary.
        email_connector: Optional custom EmailDispatcherPort implementation.
        sms_connector: Optional custom SMSDispatcherPort implementation.
        in_app_connector: Optional custom InAppDispatcherPort implementation.

    Returns:
        Summary dictionary detailing dispatch outcomes across all channels.
    """
    corr_id = correlation_id or get_correlation_id()
    urgency_val = urgency.value if isinstance(urgency, AlertUrgency) else str(urgency)
    recipient_uuid = _parse_uuid(recipient_id)
    trigger_uuid = _parse_uuid(trigger_id)
    meta = metadata or {}

    db_session = db
    created_session = False
    if db_session is None:
        db_session = SessionLocal()
        created_session = True

    try:
        # Determine notification channels using explicit input or role/urgency routing matrix
        if channels and len(channels) > 0:
            target_channels = [str(c) for c in channels]
        else:
            target_channels = route_notification_channels(
                db=db_session,
                urgency=urgency_val,
                role_name=role_name,
                event_type=event_type,
            )

        # Initialize connectors if not injected
        email_conn = email_connector or SMTPEmailConnector()
        sms_conn = sms_connector or SMSGatewayConnector()
        in_app_conn = in_app_connector or InAppPersistenceConnector(db=db_session)

        dispatched_logs: list[dict[str, Any]] = []
        executed_channels: set[str] = set()

        # 1. In-App Notification Dispatch [FR-8, FR-12]
        if NotificationChannel.IN_APP.value in target_channels:
            executed_channels.add(NotificationChannel.IN_APP.value)
            in_app_payload = InAppDispatchPayload(
                recipient_id=recipient_id,
                title=title,
                message=message,
                urgency=urgency_val,
                trigger_id=trigger_id,
                correlation_id=corr_id,
                metadata=meta,
            )

            try:
                in_app_res: InAppDispatchResult = in_app_conn.send_in_app(in_app_payload)
                in_app_success = in_app_res.success
                delivery_status = (
                    NotificationDeliveryStatus.SENT.value
                    if in_app_success
                    else NotificationDeliveryStatus.FAILED.value
                )
            except Exception as exc:
                in_app_success = False
                delivery_status = NotificationDeliveryStatus.FAILED.value
                logger.error(
                    f"Error dispatching in-app notification for recipient {recipient_id}: {exc}",
                    extra={"correlation_id": corr_id, "event_type": "in_app_dispatch_error"},
                )

            # Ensure NotificationLog is recorded if not already saved by connector
            if not isinstance(in_app_conn, InAppPersistenceConnector) or not in_app_success:
                formatted_msg = f"{title}: {message}" if title and title != message else message
                in_app_log = NotificationLog(
                    notification_id=uuid.uuid4(),
                    recipient_id=recipient_uuid,
                    trigger_id=trigger_uuid,
                    urgency=urgency_val,
                    channel=NotificationChannel.IN_APP.value,
                    delivery_status=delivery_status,
                    message=formatted_msg,
                    read_status=False,
                    dispatched_at=datetime.now(timezone.utc),
                    correlation_id=corr_id,
                )
                db_session.add(in_app_log)
                try:
                    db_session.commit()
                except Exception as commit_exc:
                    db_session.rollback()
                    logger.error(f"Failed to record in-app log: {commit_exc}", extra={"correlation_id": corr_id})

            dispatched_logs.append({
                "channel": NotificationChannel.IN_APP.value,
                "status": delivery_status,
                "success": in_app_success,
            })

        # 2. Email Notification Dispatch [FR-8, FR-12]
        if NotificationChannel.EMAIL.value in target_channels:
            executed_channels.add(NotificationChannel.EMAIL.value)
            if recipient_email:
                email_payload = EmailDispatchPayload(
                    recipient_email=recipient_email,
                    subject=title,
                    body_text=message,
                    urgency=urgency_val,
                    correlation_id=corr_id,
                    metadata=meta,
                )

                try:
                    email_res: EmailDispatchResult = email_conn.send_email(email_payload)
                    email_success = email_res.success
                    email_status = (
                        NotificationDeliveryStatus.SENT.value
                        if email_success
                        else NotificationDeliveryStatus.FAILED.value
                    )
                except Exception as exc:
                    email_success = False
                    email_status = NotificationDeliveryStatus.FAILED.value
                    logger.error(
                        f"Error dispatching email notification to {recipient_email}: {exc}",
                        extra={"correlation_id": corr_id, "event_type": "email_dispatch_error"},
                    )

                email_log = NotificationLog(
                    notification_id=uuid.uuid4(),
                    recipient_id=recipient_uuid,
                    trigger_id=trigger_uuid,
                    urgency=urgency_val,
                    channel=NotificationChannel.EMAIL.value,
                    delivery_status=email_status,
                    message=message,
                    read_status=False,
                    dispatched_at=datetime.now(timezone.utc),
                    correlation_id=corr_id,
                )
                db_session.add(email_log)
                try:
                    db_session.commit()
                except Exception as commit_exc:
                    db_session.rollback()
                    logger.error(f"Failed to record email log: {commit_exc}", extra={"correlation_id": corr_id})

                dispatched_logs.append({
                    "channel": NotificationChannel.EMAIL.value,
                    "status": email_status,
                    "success": email_success,
                })
            else:
                logger.warning(
                    f"Email channel requested but recipient_email missing for recipient: {recipient_id}",
                    extra={"correlation_id": corr_id, "event_type": "missing_recipient_email"},
                )
                email_log = NotificationLog(
                    notification_id=uuid.uuid4(),
                    recipient_id=recipient_uuid,
                    trigger_id=trigger_uuid,
                    urgency=urgency_val,
                    channel=NotificationChannel.EMAIL.value,
                    delivery_status=NotificationDeliveryStatus.FAILED.value,
                    message=f"Missing recipient email for: {title}",
                    read_status=False,
                    dispatched_at=datetime.now(timezone.utc),
                    correlation_id=corr_id,
                )
                db_session.add(email_log)
                try:
                    db_session.commit()
                except Exception as commit_exc:
                    db_session.rollback()

                dispatched_logs.append({
                    "channel": NotificationChannel.EMAIL.value,
                    "status": NotificationDeliveryStatus.FAILED.value,
                    "success": False,
                    "error": "Missing recipient email",
                })

        # 3. SMS Notification Dispatch & Fallback Handling [FR-8, FR-12, NFR-2, NFR-4]
        if NotificationChannel.SMS.value in target_channels:
            sms_attempted = True
            sms_success = False
            sms_error_detail = None

            if recipient_phone:
                sms_payload = SMSDispatchPayload(
                    phone_number=recipient_phone,
                    message=message,
                    urgency=urgency_val,
                    correlation_id=corr_id,
                    metadata=meta,
                )
                try:
                    sms_res: SMSDispatchResult = sms_conn.send_sms(sms_payload)
                    sms_success = sms_res.success
                    sms_error_detail = sms_res.error_detail
                except Exception as exc:
                    sms_success = False
                    sms_error_detail = str(exc)
            else:
                sms_error_detail = "Missing recipient phone number"

            if sms_success:
                sms_log = NotificationLog(
                    notification_id=uuid.uuid4(),
                    recipient_id=recipient_uuid,
                    trigger_id=trigger_uuid,
                    urgency=urgency_val,
                    channel=NotificationChannel.SMS.value,
                    delivery_status=NotificationDeliveryStatus.SENT.value,
                    message=message,
                    read_status=False,
                    dispatched_at=datetime.now(timezone.utc),
                    correlation_id=corr_id,
                )
                db_session.add(sms_log)
                try:
                    db_session.commit()
                except Exception as commit_exc:
                    db_session.rollback()

                dispatched_logs.append({
                    "channel": NotificationChannel.SMS.value,
                    "status": NotificationDeliveryStatus.SENT.value,
                    "success": True,
                })
            else:
                # SMS Delivery Failed: Output structured warning log [NFR-2] and execute fallback [FR-8, NFR-4]
                logger.warning(
                    f"SMS gateway delivery failed for recipient {recipient_phone or recipient_id}: {sms_error_detail}; executing fallback to Email and InApp",
                    extra={
                        "event_type": "sms_dispatch_failed_fallback",
                        "recipient_id": str(recipient_id),
                        "recipient_phone": recipient_phone,
                        "correlation_id": corr_id,
                        "error": sms_error_detail,
                    },
                )

                # Record SMS log entry with Fallback status [FR-12]
                sms_log = NotificationLog(
                    notification_id=uuid.uuid4(),
                    recipient_id=recipient_uuid,
                    trigger_id=trigger_uuid,
                    urgency=urgency_val,
                    channel=NotificationChannel.SMS.value,
                    delivery_status=NotificationDeliveryStatus.FALLBACK.value,
                    message=f"SMS dispatch failed ({sms_error_detail}); fallback executed",
                    read_status=False,
                    dispatched_at=datetime.now(timezone.utc),
                    correlation_id=corr_id,
                )
                db_session.add(sms_log)
                try:
                    db_session.commit()
                except Exception as commit_exc:
                    db_session.rollback()

                dispatched_logs.append({
                    "channel": NotificationChannel.SMS.value,
                    "status": NotificationDeliveryStatus.FALLBACK.value,
                    "success": False,
                    "error": sms_error_detail,
                })

                # Fallback Channel 1: Email (if not already executed successfully)
                if NotificationChannel.EMAIL.value not in executed_channels:
                    executed_channels.add(NotificationChannel.EMAIL.value)
                    if recipient_email:
                        fallback_email_payload = EmailDispatchPayload(
                            recipient_email=recipient_email,
                            subject=f"[FALLBACK] {title}",
                            body_text=message,
                            urgency=urgency_val,
                            correlation_id=corr_id,
                            metadata=meta,
                        )
                        try:
                            fb_email_res = email_conn.send_email(fallback_email_payload)
                            fb_email_status = (
                                NotificationDeliveryStatus.FALLBACK.value
                                if fb_email_res.success
                                else NotificationDeliveryStatus.FAILED.value
                            )
                        except Exception as exc:
                            fb_email_status = NotificationDeliveryStatus.FAILED.value
                            logger.error(
                                f"SMS fallback email dispatch failed: {exc}",
                                extra={"correlation_id": corr_id},
                            )

                        fb_email_log = NotificationLog(
                            notification_id=uuid.uuid4(),
                            recipient_id=recipient_uuid,
                            trigger_id=trigger_uuid,
                            urgency=urgency_val,
                            channel=NotificationChannel.EMAIL.value,
                            delivery_status=fb_email_status,
                            message=f"[SMS Fallback] {message}",
                            read_status=False,
                            dispatched_at=datetime.now(timezone.utc),
                            correlation_id=corr_id,
                        )
                        db_session.add(fb_email_log)
                        try:
                            db_session.commit()
                        except Exception as commit_exc:
                            db_session.rollback()

                        dispatched_logs.append({
                            "channel": f"{NotificationChannel.EMAIL.value}_fallback",
                            "status": fb_email_status,
                            "success": fb_email_status == NotificationDeliveryStatus.FALLBACK.value,
                        })

                # Fallback Channel 2: InApp (if not already executed)
                if NotificationChannel.IN_APP.value not in executed_channels:
                    executed_channels.add(NotificationChannel.IN_APP.value)
                    fallback_in_app_payload = InAppDispatchPayload(
                        recipient_id=recipient_id,
                        title=f"[FALLBACK] {title}",
                        message=message,
                        urgency=urgency_val,
                        trigger_id=trigger_id,
                        correlation_id=corr_id,
                        metadata=meta,
                    )
                    try:
                        fb_in_app_res = in_app_conn.send_in_app(fallback_in_app_payload)
                        fb_in_app_status = (
                            NotificationDeliveryStatus.FALLBACK.value
                            if fb_in_app_res.success
                            else NotificationDeliveryStatus.FAILED.value
                        )
                    except Exception as exc:
                        fb_in_app_status = NotificationDeliveryStatus.FAILED.value
                        logger.error(
                            f"SMS fallback in-app dispatch failed: {exc}",
                            extra={"correlation_id": corr_id},
                        )

                    dispatched_logs.append({
                        "channel": f"{NotificationChannel.IN_APP.value}_fallback",
                        "status": fb_in_app_status,
                        "success": fb_in_app_status == NotificationDeliveryStatus.FALLBACK.value,
                    })

        summary = {
            "recipient_id": str(recipient_id) if recipient_id else None,
            "title": title,
            "urgency": urgency_val,
            "dispatched_logs": dispatched_logs,
            "correlation_id": corr_id,
        }

        logger.info(
            f"Notification dispatch completed for recipient {recipient_id or recipient_email}: {len(dispatched_logs)} channel dispatches recorded [correlation_id={corr_id}]",
            extra={
                "event_type": "notification_dispatch_completed",
                "recipient_id": str(recipient_id) if recipient_id else None,
                "dispatched_count": len(dispatched_logs),
                "correlation_id": corr_id,
            },
        )
        return summary

    finally:
        if created_session and db_session is not None:
            db_session.close()


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=5,
    name="src.workers.notification_tasks.dispatch_alert_notification",
)
def dispatch_alert_notification(
    self,
    recipient_id: str | None = None,
    recipient_email: str | None = None,
    recipient_phone: str | None = None,
    title: str = "Financial Alert",
    message: str = "",
    urgency: str = "Medium",
    channels: list[str] | None = None,
    trigger_id: str | None = None,
    role_name: str | None = None,
    event_type: str | None = None,
    correlation_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Celery task executing background multi-channel notification dispatching and audit log recording [FR-8, FR-12, NFR-2, NFR-4].

    Args:
        recipient_id: Optional string/UUID of target recipient.
        recipient_email: Optional target email address.
        recipient_phone: Optional target phone number.
        title: Notification title header.
        message: Notification message text.
        urgency: Urgency string ('Low', 'Medium', 'High').
        channels: Optional target channels list.
        trigger_id: Optional trigger rule UUID string.
        role_name: Optional user role name.
        event_type: Optional event criteria name.
        correlation_id: Optional correlation tracking ID.
        metadata: Optional metadata dict.

    Returns:
        Summary dict containing dispatch details across channels.
    """
    corr_id = correlation_id or get_correlation_id()
    logger.info(
        f"Starting background notification dispatch task for recipient {recipient_id or recipient_email or recipient_phone or 'unknown'} [correlation_id={corr_id}]",
        extra={
            "event_type": "background_notification_dispatch_started",
            "recipient_id": recipient_id,
            "urgency": urgency,
            "correlation_id": corr_id,
        },
    )

    try:
        return run_notification_dispatch(
            db=None,
            recipient_id=recipient_id,
            recipient_email=recipient_email,
            recipient_phone=recipient_phone,
            title=title,
            message=message,
            urgency=urgency,
            channels=channels,
            trigger_id=trigger_id,
            role_name=role_name,
            event_type=event_type,
            correlation_id=corr_id,
            metadata=metadata,
        )
    except Exception as exc:
        sanitized_err = str(exc)
        logger.error(
            f"Error executing background notification dispatch: {sanitized_err} [correlation_id={corr_id}]",
            extra={
                "event_type": "background_notification_dispatch_error",
                "correlation_id": corr_id,
                "error": sanitized_err,
            },
        )
        if hasattr(self, "request") and getattr(self.request, "retries", 0) < getattr(self, "max_retries", 3):
            raise self.retry(exc=exc)
        return {
            "success": False,
            "error": sanitized_err,
            "correlation_id": corr_id,
        }


# Module aliases for convenience / backwards compatibility
process_notification_dispatch = dispatch_alert_notification
dispatch_multi_channel_notification = dispatch_alert_notification
