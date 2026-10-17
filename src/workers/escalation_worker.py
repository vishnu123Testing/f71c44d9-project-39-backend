"""Background processing worker for SLA breach detection, escalation alerts, and alert suppression [FR-005, NFR-004].

This module provides the Celery background task and execution functions to scan uncompleted
action items, identify SLA breaches, trigger management escalation notifications, suppress duplicate
alerts for already-escalated items, and emit structured context-correlated JSON logs.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

from src.lib.logging import get_correlation_id
from src.models.database import SessionLocal
from src.services.action_items.service import (
    SLABreachScanResultDTO,
    scan_and_escalate_sla_breaches,
)
from src.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime instance is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def run_escalation_scan(
    db: Session,
    reference_time: datetime | str | None = None,
    notification_dispatcher: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Execute SLA breach scan and trigger escalation alerts for breached action items [FR-005, NFR-004].

    Args:
        db: Synchronous SQLAlchemy session.
        reference_time: Optional reference datetime or ISO format string for evaluation time.
        notification_dispatcher: Optional callback to dispatch external escalation notifications.

    Returns:
        Summary dictionary with scan metrics and escalated item details.
    """
    if reference_time is None:
        ref_dt = datetime.now(timezone.utc)
    elif isinstance(reference_time, str):
        ref_dt = datetime.fromisoformat(reference_time)
        ref_dt = _ensure_utc(ref_dt) or datetime.now(timezone.utc)
    else:
        ref_dt = _ensure_utc(reference_time) or datetime.now(timezone.utc)

    correlation_id = get_correlation_id()

    scan_result: SLABreachScanResultDTO = scan_and_escalate_sla_breaches(
        session=db,
        reference_time=ref_dt,
        notification_dispatcher=notification_dispatcher,
    )

    escalated_details = [
        {
            "id": str(item.id),
            "deal_id": str(item.deal_id),
            "title": item.title,
            "assigned_owner_id": item.assigned_owner_id,
            "target_due_date": item.target_due_date.isoformat()
            if item.target_due_date
            else None,
            "sla_threshold_hours": item.sla_threshold_hours,
            "escalated_on": item.escalated_on.isoformat()
            if item.escalated_on
            else None,
        }
        for item in scan_result.escalated_items
    ]

    summary = {
        "scanned_count": scan_result.scanned_count,
        "newly_escalated_count": scan_result.newly_escalated_count,
        "already_escalated_count": scan_result.already_escalated_count,
        "escalated_items": escalated_details,
        "scanned_at": ref_dt.isoformat(),
        "correlation_id": correlation_id,
    }

    logger.info(
        "SLA escalation scan completed: %d items scanned, %d newly escalated, %d suppressed [correlation_id=%s]",
        scan_result.scanned_count,
        scan_result.newly_escalated_count,
        scan_result.already_escalated_count,
        correlation_id,
        extra={
            "event_type": "sla_escalation_scan_completed",
            "scanned_count": scan_result.scanned_count,
            "newly_escalated_count": scan_result.newly_escalated_count,
            "already_escalated_count": scan_result.already_escalated_count,
            "correlation_id": correlation_id,
        },
    )

    return summary


@celery_app.task(name="src.workers.escalation_worker.process_sla_escalations")
def process_sla_escalations(
    reference_time_str: str | None = None,
) -> dict[str, Any]:
    """Celery task running background SLA breach scan and dispatching escalation notifications [FR-005, NFR-004].

    Args:
        reference_time_str: Optional ISO format string for evaluation reference timestamp.

    Returns:
        Summary dictionary containing result metrics from SLA escalation scan.
    """
    db = SessionLocal()
    try:
        return run_escalation_scan(db=db, reference_time=reference_time_str)
    finally:
        db.close()
