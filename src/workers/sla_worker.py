"""Background worker task for periodic SLA countdown evaluation, breach warnings, and tier escalation execution [FR-4, FR-5, FR-6, NFR-3, NFR-4].

This module implements the Celery background worker task for scanning active support tickets,
evaluating first response and resolution SLA countdown timers, issuing proactive breach warning alerts,
detecting SLA breaches, and executing automated escalation pathways across support tiers.
"""

from datetime import datetime, timezone
import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from src.connectors.notification import NotificationConnector, get_notification_connector
from src.lib.logging import get_correlation_id
from src.models.database import SessionLocal
from src.services.support.sla_service import SLAService, evaluate_all_active_slas
from src.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _ensure_utc(dt: datetime | str | None) -> datetime | None:
    """Ensure a datetime instance or ISO format string is timezone-aware in UTC.

    Args:
        dt: Optional datetime instance or ISO 8601 formatted string.

    Returns:
        Timezone-aware datetime in UTC, or None if input was None or invalid.
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            parsed = datetime.fromisoformat(dt)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def run_sla_evaluation_scan(
    db: Session,
    reference_time: datetime | str | None = None,
    notification_port: Any | None = None,
) -> dict[str, Any]:
    """Execute batch SLA countdown evaluation and escalation processing [FR-4, FR-5, FR-6, NFR-3].

    Args:
        db: Synchronous SQLAlchemy database session.
        reference_time: Optional reference UTC datetime or ISO string for evaluation timestamp.
        notification_port: Optional notification port adapter (defaults to NotificationConnector).

    Returns:
        Summary dictionary containing evaluation counts, warnings, breaches, and escalation details.
    """
    ref_dt = _ensure_utc(reference_time) or datetime.now(timezone.utc)
    connector = notification_port or get_notification_connector(db=db)
    sla_service = SLAService(session=db, notification_port=connector)

    correlation_id = get_correlation_id()

    eval_result = sla_service.evaluate_all_active(now=ref_dt)

    db.commit()

    summary = {
        "evaluated_count": eval_result.get("evaluated_count", 0),
        "warnings_triggered_count": eval_result.get("warnings_triggered_count", 0),
        "breaches_triggered_count": eval_result.get("breaches_triggered_count", 0),
        "escalations_triggered_count": eval_result.get("escalations_triggered_count", 0),
        "details": eval_result.get("details", []),
        "evaluated_at": ref_dt.isoformat(),
        "correlation_id": correlation_id,
    }

    logger.info(
        "SLA worker evaluation scan completed: %d active tickets evaluated, %d warnings, %d breaches, %d escalations [correlation_id=%s]",
        summary["evaluated_count"],
        summary["warnings_triggered_count"],
        summary["breaches_triggered_count"],
        summary["escalations_triggered_count"],
        correlation_id,
        extra={
            "event_type": "sla_worker_evaluation_completed",
            "evaluated_count": summary["evaluated_count"],
            "warnings_triggered_count": summary["warnings_triggered_count"],
            "breaches_triggered_count": summary["breaches_triggered_count"],
            "escalations_triggered_count": summary["escalations_triggered_count"],
            "correlation_id": correlation_id,
        },
    )

    return summary


@celery_app.task(name="src.workers.sla_worker.process_sla_evaluations")
def process_sla_evaluations(
    reference_time_str: str | None = None,
) -> dict[str, Any]:
    """Scheduled Celery task executing SLA countdown evaluations and escalation pathways [FR-4, FR-5, FR-6].

    Args:
        reference_time_str: Optional ISO format timestamp string for evaluation time.

    Returns:
        Summary dictionary containing evaluation scan metrics.
    """
    db = SessionLocal()
    try:
        return run_sla_evaluation_scan(db=db, reference_time=reference_time_str)
    finally:
        db.close()


# Functional aliases for flexible caller integration
run_sla_evaluation = run_sla_evaluation_scan
evaluate_active_slas = process_sla_evaluations
process_sla_countdown_and_escalations = process_sla_evaluations
