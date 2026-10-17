"""In-App persistence connector conforming to In-App port interface [FR-5, FR-8, NFR-2, NFR-3].

This module provides the concrete InAppPersistenceConnector implementation conforming to InAppDispatcherPort [FR-8].
Persists in-app alert notifications to the central database or in-memory store for display in the notification hub [FR-5].
Emits structured logs with correlation metadata and handles failures gracefully [NFR-2, NFR-3].
"""

from datetime import datetime, timezone
import logging
from typing import Any, Callable
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.lib.logging import get_correlation_id
from src.models.alerting import (
    AlertUrgency,
    NotificationChannel,
    NotificationDeliveryStatus,
    NotificationLog,
)
from src.services.alerting.ports import (
    InAppDispatcherPort,
    InAppDispatchPayload,
    InAppDispatchResult,
)

logger = logging.getLogger(__name__)


def _sanitize_error_message(msg: str) -> str:
    """Sanitize error messages to prevent sensitive data exposure [NFR-3]."""
    if not msg:
        return "Unknown error occurred"
    cleaned = str(msg).split("?")[0]
    cleaned = cleaned.replace("\n", " ").strip()
    if len(cleaned) > 200:
        cleaned = cleaned[:197] + "..."
    return cleaned


class InAppPersistenceConnector(InAppDispatcherPort):
    """In-App persistence connector implementing InAppDispatcherPort [FR-5, FR-8, NFR-2, NFR-3]."""

    def __init__(
        self,
        db: Session | None = None,
        session_factory: Callable[[], Session] | None = None,
        simulate_failure: bool = False,
        in_memory_fallback: bool = True,
    ) -> None:
        """Initialize InAppPersistenceConnector adapter [FR-5, FR-8, NFR-3].

        Args:
            db: Optional active SQLAlchemy Session for persisting in-app notifications.
            session_factory: Optional callable returning a new SQLAlchemy Session.
            simulate_failure: Flag to simulate persistence failure for testing.
            in_memory_fallback: Flag to store notifications in memory when DB is absent or fails.
        """
        self.db = db
        self.session_factory = session_factory
        self.simulate_failure = simulate_failure
        self.in_memory_fallback = in_memory_fallback
        self._in_memory_logs: list[NotificationLog] = []

    def send_in_app(self, payload: InAppDispatchPayload) -> InAppDispatchResult:
        """Persist an in-app notification for a recipient or role [FR-5, FR-8, NFR-2, NFR-3].

        Args:
            payload: InAppDispatchPayload containing recipient_id, title, message, urgency, and metadata.

        Returns:
            InAppDispatchResult representing the outcome of the in-app persistence attempt.
        """
        correlation_id = payload.correlation_id or get_correlation_id()
        urgency_val = (
            payload.urgency.value
            if isinstance(payload.urgency, AlertUrgency)
            else str(payload.urgency)
        )

        recipient_str = str(payload.recipient_id) if payload.recipient_id is not None else None

        if self.simulate_failure:
            logger.warning(
                f"Simulated in-app notification persistence failure for recipient: {recipient_str}",
                extra={"correlation_id": correlation_id},
            )
            return InAppDispatchResult(
                success=False,
                recipient_id=recipient_str,
                channel=NotificationChannel.IN_APP.value,
                delivery_status=NotificationDeliveryStatus.FAILED.value,
                dispatched_at=datetime.now(timezone.utc),
                error_detail="Simulated in-app persistence failure",
                metadata={"correlation_id": correlation_id, "urgency": urgency_val},
            )

        recipient_uuid: uuid.UUID | None = None
        if payload.recipient_id is not None:
            if isinstance(payload.recipient_id, uuid.UUID):
                recipient_uuid = payload.recipient_id
            else:
                try:
                    recipient_uuid = uuid.UUID(str(payload.recipient_id))
                except ValueError:
                    recipient_uuid = None

        trigger_uuid: uuid.UUID | None = None
        if payload.trigger_id is not None:
            if isinstance(payload.trigger_id, uuid.UUID):
                trigger_uuid = payload.trigger_id
            else:
                try:
                    trigger_uuid = uuid.UUID(str(payload.trigger_id))
                except ValueError:
                    trigger_uuid = None

        notification_id = uuid.uuid4()
        now_utc = datetime.now(timezone.utc)

        if payload.title and payload.title.strip() and payload.title != payload.message:
            formatted_message = f"{payload.title}: {payload.message}"
        else:
            formatted_message = payload.message

        log_entry = NotificationLog(
            notification_id=notification_id,
            recipient_id=recipient_uuid,
            trigger_id=trigger_uuid,
            urgency=urgency_val,
            channel=NotificationChannel.IN_APP.value,
            delivery_status=NotificationDeliveryStatus.SENT.value,
            message=formatted_message,
            read_status=False,
            dispatched_at=now_utc,
            correlation_id=correlation_id,
            created_at=now_utc,
        )

        db_session = self.db
        created_session = False

        if db_session is None and self.session_factory is not None:
            try:
                db_session = self.session_factory()
                created_session = True
            except Exception as e:
                logger.error(
                    f"Failed to create session from factory: {_sanitize_error_message(str(e))}",
                    extra={"correlation_id": correlation_id},
                )
                db_session = None

        persisted = False
        if db_session is not None:
            try:
                db_session.add(log_entry)
                db_session.commit()
                persisted = True
                logger.info(
                    f"Successfully persisted in-app notification {notification_id} for recipient: {recipient_str}",
                    extra={"correlation_id": correlation_id},
                )
            except Exception as e:
                db_session.rollback()
                sanitized_err = _sanitize_error_message(str(e))
                logger.error(
                    f"Database error persisting in-app notification: {sanitized_err}",
                    extra={"correlation_id": correlation_id},
                )
                if not self.in_memory_fallback:
                    return InAppDispatchResult(
                        success=False,
                        recipient_id=recipient_str,
                        channel=NotificationChannel.IN_APP.value,
                        delivery_status=NotificationDeliveryStatus.FAILED.value,
                        dispatched_at=now_utc,
                        error_detail=f"Database persistence error: {sanitized_err}",
                        metadata={"correlation_id": correlation_id, "urgency": urgency_val},
                    )
            finally:
                if created_session and db_session is not None:
                    db_session.close()

        if not persisted:
            if self.in_memory_fallback:
                self._in_memory_logs.append(log_entry)
                logger.info(
                    f"Persisted in-app notification {notification_id} in-memory fallback for recipient: {recipient_str}",
                    extra={"correlation_id": correlation_id},
                )
            else:
                return InAppDispatchResult(
                    success=False,
                    recipient_id=recipient_str,
                    channel=NotificationChannel.IN_APP.value,
                    delivery_status=NotificationDeliveryStatus.FAILED.value,
                    dispatched_at=now_utc,
                    error_detail="No database session available for in-app persistence",
                    metadata={"correlation_id": correlation_id, "urgency": urgency_val},
                )

        result_metadata = {
            "correlation_id": correlation_id,
            "urgency": urgency_val,
            "title": payload.title,
            "read_status": False,
        }
        if payload.metadata:
            result_metadata.update(payload.metadata)

        return InAppDispatchResult(
            success=True,
            recipient_id=recipient_str,
            channel=NotificationChannel.IN_APP.value,
            delivery_status=NotificationDeliveryStatus.SENT.value,
            notification_id=str(notification_id),
            dispatched_at=now_utc,
            error_detail=None,
            metadata=result_metadata,
        )

    def get_in_app_notifications(
        self,
        recipient_id: uuid.UUID | str | None = None,
        unread_only: bool = False,
        limit: int = 50,
    ) -> list[NotificationLog]:
        """Fetch in-app notification records for a recipient [FR-5].

        Args:
            recipient_id: Optional recipient UUID or string to filter by.
            unread_only: Flag to filter only unread notifications.
            limit: Maximum number of notification records to return.

        Returns:
            List of NotificationLog records sorted by dispatched_at descending.
        """
        recipient_uuid: uuid.UUID | None = None
        if recipient_id is not None:
            if isinstance(recipient_id, uuid.UUID):
                recipient_uuid = recipient_id
            else:
                try:
                    recipient_uuid = uuid.UUID(str(recipient_id))
                except ValueError:
                    recipient_uuid = None

        if self.db is not None:
            stmt = select(NotificationLog).where(
                NotificationLog.channel == NotificationChannel.IN_APP.value
            )
            if recipient_uuid is not None:
                stmt = stmt.where(NotificationLog.recipient_id == recipient_uuid)
            if unread_only:
                stmt = stmt.where(NotificationLog.read_status == False)
            stmt = stmt.order_by(NotificationLog.dispatched_at.desc()).limit(limit)

            return list(self.db.scalars(stmt).all())

        # Fallback to in-memory logs
        results = [
            log
            for log in self._in_memory_logs
            if log.channel == NotificationChannel.IN_APP.value
        ]
        if recipient_uuid is not None:
            results = [log for log in results if log.recipient_id == recipient_uuid]
        elif recipient_id is not None:
            results = [
                log for log in results if str(log.recipient_id) == str(recipient_id)
            ]
        if unread_only:
            results = [log for log in results if not log.read_status]

        results.sort(key=lambda x: x.dispatched_at, reverse=True)
        return results[:limit]

    def mark_as_read(self, notification_id: uuid.UUID | str) -> bool:
        """Mark an in-app notification as read [FR-5].

        Args:
            notification_id: UUID or string ID of the notification to mark as read.

        Returns:
            True if the record was found and updated; False otherwise.
        """
        notif_uuid: uuid.UUID
        if isinstance(notification_id, uuid.UUID):
            notif_uuid = notification_id
        else:
            try:
                notif_uuid = uuid.UUID(str(notification_id))
            except ValueError:
                return False

        if self.db is not None:
            stmt = select(NotificationLog).where(
                NotificationLog.notification_id == notif_uuid
            )
            log_entry = self.db.scalars(stmt).first()
            if log_entry is not None:
                log_entry.read_status = True
                self.db.commit()
                return True
            return False

        # In-memory search
        for log in self._in_memory_logs:
            if log.notification_id == notif_uuid:
                log.read_status = True
                return True
        return False


# Class aliases for consistency across imports
InAppConnector = InAppPersistenceConnector
InAppAdapter = InAppPersistenceConnector
InAppPersistenceAdapter = InAppPersistenceConnector
