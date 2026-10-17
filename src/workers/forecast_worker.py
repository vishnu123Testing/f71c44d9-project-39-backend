"""Background processing worker for AI forecast recalculations on deal changes [FR-010, NFR-002, NFR-004].

This module provides the Celery background task and execution functions to trigger asynchronous
AI completion timeline predictions and confidence score recalculations when deal stage, value,
or milestone activity changes.
"""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from src.lib.logging import get_correlation_id
from src.models.database import SessionLocal
from src.services.forecasting.crm_port import CRMDataPort
from src.services.forecasting.forecast_port import ForecastProviderPort
from src.services.forecasting.service import (
    DealNotFoundError,
    ForecastDomain,
    ForecastingService,
    InvalidForecastDataError,
)
from src.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def run_forecast_recalculation(
    db: Session,
    deal_id: UUID | str,
    crm_port: CRMDataPort | None = None,
    forecast_port: ForecastProviderPort | None = None,
    trigger_source: str = "deal_update",
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Execute AI forecast recalculation for a deal and persist/log results [FR-010, NFR-002, NFR-004].

    Args:
        db: Synchronous SQLAlchemy database session.
        deal_id: Unique string or UUID identifier of the target deal.
        crm_port: Optional explicitly injected CRMDataPort instance.
        forecast_port: Optional explicitly injected ForecastProviderPort instance.
        trigger_source: Contextual trigger reason (e.g., 'stage_update', 'value_update').
        correlation_id: Optional correlation identifier for log tracking.

    Returns:
        Summary dictionary containing calculated forecast status and details.
    """
    corr_id = correlation_id or get_correlation_id()
    deal_id_str = str(deal_id)

    service = ForecastingService(crm_port=crm_port, forecast_port=forecast_port)

    try:
        forecast: ForecastDomain = service.recalculate_forecast(
            deal_id=deal_id,
            db_session=db,
            crm_port=crm_port,
            forecast_port=forecast_port,
            trigger_source=trigger_source,
            correlation_id=corr_id,
        )
    except (DealNotFoundError, InvalidForecastDataError) as exc:
        logger.warning(
            "AI forecast recalculation skipped/failed for deal %s: %s [correlation_id=%s]",
            deal_id_str,
            exc,
            corr_id,
            extra={
                "event_type": "ai_forecast_recalculation_failed",
                "deal_id": deal_id_str,
                "trigger_source": trigger_source,
                "correlation_id": corr_id,
                "error": str(exc),
            },
        )
        return {
            "deal_id": deal_id_str,
            "estimated_completion_date": None,
            "confidence_score_pct": None,
            "status_code": "FAILED",
            "calculated_on": None,
            "trigger_source": trigger_source,
            "correlation_id": corr_id,
            "error_detail": str(exc),
        }
    except Exception as exc:
        logger.error(
            "Unexpected error during AI forecast recalculation for deal %s: %s [correlation_id=%s]",
            deal_id_str,
            exc,
            corr_id,
            extra={
                "event_type": "ai_forecast_recalculation_error",
                "deal_id": deal_id_str,
                "trigger_source": trigger_source,
                "correlation_id": corr_id,
                "error": str(exc),
            },
        )
        return {
            "deal_id": deal_id_str,
            "estimated_completion_date": None,
            "confidence_score_pct": None,
            "status_code": "FAILED",
            "calculated_on": None,
            "trigger_source": trigger_source,
            "correlation_id": corr_id,
            "error_detail": str(exc),
        }

    est_date_str = (
        forecast.estimated_completion_date.isoformat()
        if forecast.estimated_completion_date
        else None
    )
    calc_on_str = (
        forecast.calculated_on.isoformat()
        if forecast.calculated_on
        else None
    )

    summary = {
        "deal_id": str(forecast.deal_id),
        "estimated_completion_date": est_date_str,
        "confidence_score_pct": forecast.confidence_score_pct,
        "status_code": forecast.status_code,
        "calculated_on": calc_on_str,
        "trigger_source": forecast.trigger_source,
        "correlation_id": corr_id,
        "error_detail": forecast.error_detail,
    }

    logger.info(
        "AI forecast recalculation worker task completed for deal %s [status=%s, correlation_id=%s]",
        str(forecast.deal_id),
        forecast.status_code,
        corr_id,
        extra={
            "event_type": "ai_forecast_recalculation_completed",
            "deal_id": str(forecast.deal_id),
            "status_code": forecast.status_code,
            "confidence_score_pct": forecast.confidence_score_pct,
            "estimated_completion_date": est_date_str,
            "trigger_source": trigger_source,
            "correlation_id": corr_id,
        },
    )

    return summary


# Helper alias for consistent function naming conventions
recalculate_deal_forecast = run_forecast_recalculation


@celery_app.task(name="src.workers.forecast_worker.recalculate_deal_forecast_task")
def recalculate_deal_forecast_task(
    deal_id: str,
    trigger_source: str = "deal_update",
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Celery background worker task for asynchronous AI forecast recalculations [FR-010, NFR-002, NFR-004].

    Args:
        deal_id: String representation of the deal UUID.
        trigger_source: Reason for recalculation (e.g., 'stage_update', 'milestone_update', 'value_update').
        correlation_id: Optional correlation identifier for log tracking.

    Returns:
        Summary dictionary containing calculated forecast status and details.
    """
    db = SessionLocal()
    try:
        return run_forecast_recalculation(
            db=db,
            deal_id=deal_id,
            trigger_source=trigger_source,
            correlation_id=correlation_id,
        )
    finally:
        db.close()


# Alias for alternative task name references
recalculate_forecast_task = recalculate_deal_forecast_task
