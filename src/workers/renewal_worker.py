"""Scheduled renewal worker task for calculating contract expiration thresholds and triggering alerts [FR-9].

This module implements the scheduled task and Celery background worker for querying active
contracts near expiration, calculating UTC expiration thresholds at 90, 60, and 30 days,
and triggering renewal notification alerts [FR-9].

Alerts are suppressed if contracts are terminated, expired, draft, or if an alert for
the given threshold has already been dispatched.
"""

from datetime import date as date_type, datetime, timezone
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from src.connectors.notification_connector import NotificationConnector
from src.lib.config import settings
from src.lib.logging import get_correlation_id
from src.models.contract import Contract
from src.models.database import SessionLocal
from src.services.contract.lifecycle_service import ContractLifecycleService
from src.services.contract.models import ContractEventType, ContractStatus
from src.services.contract.notification_port import (
    RENEWAL_ALERT_THRESHOLDS,
    NotificationPort,
    RenewalAlertDispatchResult,
)
from src.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def calculate_threshold_alerts(
    db: Session,
    reference_date: date_type | datetime | None = None,
    notification_port: NotificationPort | None = None,
    thresholds: list[int] | tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """Calculate contract expiration thresholds and dispatch renewal notifications [FR-9].

    Args:
        db: Synchronous SQLAlchemy Session for database queries and alert persistence.
        reference_date: Optional reference UTC date or datetime (defaults to current UTC date).
        notification_port: Optional NotificationPort adapter (defaults to NotificationConnector).
        thresholds: Optional list/tuple of day thresholds (defaults to settings.RENEWAL_ALERT_THRESHOLDS or (90, 60, 30)).

    Returns:
        Dictionary summarizing evaluation details, contract counts, and dispatch outcomes.
    """
    if reference_date is None:
        eval_date = datetime.now(timezone.utc).date()
    elif isinstance(reference_date, datetime):
        eval_date = reference_date.date()
    else:
        eval_date = reference_date

    eval_thresholds = tuple(
        sorted(
            thresholds or settings.RENEWAL_ALERT_THRESHOLDS or RENEWAL_ALERT_THRESHOLDS,
            reverse=True,
        )
    )

    connector = notification_port or NotificationConnector(db=db)
    lifecycle_service = ContractLifecycleService(db=db, notification_port=connector)

    # Fetch contracts with configured expiration dates
    contracts = (
        db.query(Contract)
        .filter(Contract.expires_on.isnot(None))
        .all()
    )

    dispatched_results: list[RenewalAlertDispatchResult] = []
    suppressed_count = 0
    dispatched_count = 0
    processed_contracts_count = 0

    for contract in contracts:
        if not contract.expires_on:
            continue

        processed_contracts_count += 1
        contract_status = contract.status

        # Suppress alerts if contract status is terminated, expired, or draft
        if connector.should_suppress_alert(status=contract_status):
            suppressed_count += 1
            continue

        days_until_exp = connector.calculate_days_until_expiration(
            expires_on=contract.expires_on,
            reference_date=eval_date,
        )

        # Do not dispatch renewal alerts for contracts already past expiration
        if days_until_exp < 0:
            continue

        existing_alerts = connector.get_dispatched_alerts(contract.id)
        already_dispatched = {a.threshold_days for a in existing_alerts}

        for threshold in eval_thresholds:
            if days_until_exp <= threshold:
                if threshold in already_dispatched:
                    continue

                dispatch_res = connector.dispatch_renewal_alert(
                    contract_id=contract.id,
                    threshold_days=threshold,
                    title=contract.title,
                    counterparty=contract.counterparty,
                    expires_on=contract.expires_on,
                    dispatched_at=datetime.now(timezone.utc),
                )

                dispatched_results.append(dispatch_res)

                if dispatch_res.success and not dispatch_res.suppressed:
                    dispatched_count += 1
                    already_dispatched.add(threshold)
                    # Log lifecycle event for dispatched alert
                    lifecycle_service._log_lifecycle_event(
                        event_type=ContractEventType.RENEWAL_ALERT_DISPATCHED,
                        contract_id=contract.id,
                        metadata={
                            "threshold_days": threshold,
                            "expires_on": contract.expires_on.isoformat() if contract.expires_on else None,
                            "days_remaining": days_until_exp,
                            "alert_id": str(dispatch_res.alert_id),
                        },
                    )
                elif dispatch_res.suppressed:
                    suppressed_count += 1

    summary = {
        "processed_contracts": processed_contracts_count,
        "alerts_dispatched": dispatched_count,
        "alerts_suppressed": suppressed_count,
        "thresholds_evaluated": list(eval_thresholds),
        "reference_date": eval_date.isoformat(),
        "dispatched_details": [
            {
                "alert_id": str(r.alert_id),
                "contract_id": str(r.contract_id),
                "threshold_days": r.threshold_days,
                "dispatched_at": r.dispatched_at.isoformat(),
                "success": r.success,
                "suppressed": r.suppressed,
            }
            for r in dispatched_results
        ],
    }

    logger.info(
        "Renewal alert calculation completed: %d contracts evaluated, %d alerts dispatched, %d suppressed [correlation_id=%s]",
        processed_contracts_count,
        dispatched_count,
        suppressed_count,
        get_correlation_id(),
    )

    return summary


@celery_app.task(name="src.workers.renewal_worker.process_renewal_alerts")
def process_renewal_alerts(
    reference_date_str: str | None = None,
) -> dict[str, Any]:
    """Scheduled Celery task executing contract renewal alert calculations [FR-9].

    Args:
        reference_date_str: Optional ISO format date string (e.g., "2026-09-01") for evaluation date.

    Returns:
        Summary dictionary containing renewal alert calculation metrics.
    """
    ref_date: date_type | None = None
    if reference_date_str:
        ref_date = date_type.fromisoformat(reference_date_str)

    db = SessionLocal()
    try:
        return calculate_threshold_alerts(db=db, reference_date=ref_date)
    finally:
        db.close()
