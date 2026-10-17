"""Forecasting Service implementation for AI-driven deal completion timeline predictions and confidence scoring.

This module encapsulates business logic for calculating AI deal completion forecasts,
evaluating confidence percentages, handling sparse CRM data fallback states, persisting forecast
state and evaluation logs, and supporting explicit dependency injection for CRM and forecast ports
(FR-008, FR-009, FR-010, NFR-002, NFR-005).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any
import uuid
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.lib.logging import get_correlation_id
from src.models.deal import Deal, DealForecast
from src.models.forecast import ForecastEvaluationLog
from src.services.forecasting.crm_port import CRMDataPort
from src.services.forecasting.forecast_port import ForecastPredictionResult, ForecastProviderPort

logger = logging.getLogger(__name__)


class ForecastError(Exception):
    """Base exception for forecasting service operations."""

    pass


class ForecastNotFoundError(ValueError, ForecastError):
    """Raised when a requested deal forecast is not found in the database."""

    pass


class InvalidForecastDataError(ValueError, ForecastError):
    """Raised when forecast input parameters fail domain validation rules."""

    pass


class DealNotFoundError(ValueError, ForecastError):
    """Raised when the specified deal for a forecast does not exist."""

    pass


@dataclass
class ForecastDomain:
    """Pure domain entity representation of a Deal Forecast."""

    id: UUID | None
    deal_id: UUID
    estimated_completion_date: datetime | None
    confidence_score_pct: float | None
    status_code: str  # READY, INSUFFICIENT_DATA, COMPUTING, FAILED
    calculated_on: datetime
    error_detail: str | None = None
    trigger_source: str | None = "deal_update"
    model_version: str | None = "v1.0"
    is_sparse: bool = False


@dataclass
class ForecastEvaluationLogDomain:
    """Pure domain entity representation of a Forecast Evaluation/Recalculation Log."""

    id: UUID
    deal_id: UUID
    estimated_completion_date: datetime | None
    confidence_score_pct: float | None
    status_code: str
    trigger_source: str
    correlation_id: str | None
    error_detail: str | None
    recalculated_on: datetime


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime instance is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_uuid(val: UUID | str) -> UUID:
    """Convert input string or UUID to a valid UUID object deterministically."""
    if isinstance(val, UUID):
        return val
    if not val or not isinstance(val, str) or not val.strip():
        raise InvalidForecastDataError("deal_id must be a non-empty string or UUID")
    s = val.strip()
    try:
        return UUID(s)
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_DNS, s)


def _get_default_crm_port() -> CRMDataPort:
    """Lazily import and instantiate default ExternalCRMConnector."""
    from src.connectors.crm_connector import ExternalCRMConnector

    return ExternalCRMConnector()


def _get_default_forecast_port(crm_port: CRMDataPort | None = None) -> ForecastProviderPort:
    """Lazily import and instantiate default AIForecastConnector."""
    from src.connectors.forecast_connector import AIForecastConnector

    return AIForecastConnector(crm_port=crm_port)


class ForecastingService:
    """Domain service orchestrating AI completion timeline forecasting and confidence evaluations."""

    def __init__(
        self,
        crm_port: CRMDataPort | None = None,
        forecast_port: ForecastProviderPort | None = None,
    ) -> None:
        """Initialize ForecastingService with explicit port dependency injection (NFR-005).

        Args:
            crm_port: Optional explicitly injected CRMDataPort instance.
            forecast_port: Optional explicitly injected ForecastProviderPort instance.
        """
        self._crm_port = crm_port
        self._forecast_port = forecast_port

    @property
    def crm_port(self) -> CRMDataPort:
        """Get effective CRM data port, lazily instantiating default if unassigned."""
        if self._crm_port is None:
            self._crm_port = _get_default_crm_port()
        return self._crm_port

    @property
    def forecast_port(self) -> ForecastProviderPort:
        """Get effective forecast provider port, lazily instantiating default if unassigned."""
        if self._forecast_port is None:
            self._forecast_port = _get_default_forecast_port(self.crm_port)
        return self._forecast_port

    def calculate_forecast(
        self,
        deal_id: UUID | str,
        db_session: Session | None = None,
        crm_port: CRMDataPort | None = None,
        forecast_port: ForecastProviderPort | None = None,
        trigger_source: str = "deal_update",
        correlation_id: str | None = None,
    ) -> ForecastDomain:
        """Calculate or recalculate AI completion timeline forecast for a deal.

        Retrieves prediction from forecast provider using explicitly injected ports,
        handles sparse data fallbacks gracefully, persists DealForecast state if session provided,
        records structured evaluation log, and returns ForecastDomain (FR-008, FR-009, FR-010, NFR-005).

        Args:
            deal_id: Unique string or UUID identifier of the deal.
            db_session: Optional database session for persistence and evaluation logging.
            crm_port: Optional explicitly injected CRMDataPort instance.
            forecast_port: Optional explicitly injected ForecastProviderPort instance.
            trigger_source: Contextual trigger reason (e.g., 'deal_update', 'stage_change').
            correlation_id: Optional correlation identifier for observability logs.

        Returns:
            ForecastDomain instance representing calculated forecast state.
        """
        deal_uuid = _to_uuid(deal_id)
        deal_id_str = str(deal_id).strip()
        corr_id = correlation_id or get_correlation_id()
        now = datetime.now(timezone.utc)

        deal_model = None
        if db_session is not None:
            deal_model = db_session.get(Deal, deal_uuid)
            if deal_model is None:
                deal_model = db_session.scalar(select(Deal).where(Deal.id == deal_uuid))
            if deal_model is None:
                raise DealNotFoundError(f"Deal with ID {deal_id} not found in database.")

        effective_crm = crm_port or self._crm_port or _get_default_crm_port()
        effective_forecast = forecast_port or self._forecast_port or _get_default_forecast_port(effective_crm)

        prediction: ForecastPredictionResult
        try:
            prediction = effective_forecast.predict_forecast(
                deal_id=deal_id_str,
                crm_port=effective_crm,
            )
        except Exception as exc:
            logger.warning(
                f"Forecast calculation failed for deal {deal_id_str}: {exc}",
                extra={
                    "correlation_id": corr_id,
                    "deal_id": deal_id_str,
                    "trigger_source": trigger_source,
                },
            )
            prediction = ForecastPredictionResult(
                deal_id=deal_id_str,
                estimated_completion_date=None,
                confidence_score_pct=None,
                status_code="FAILED",
                calculated_on=now,
                is_sparse=True,
                error_detail=str(exc),
            )

        est_date = _ensure_utc(prediction.estimated_completion_date)
        calc_on = _ensure_utc(prediction.calculated_on) or now
        status_code = prediction.status_code or ("READY" if est_date else "INSUFFICIENT_DATA")
        confidence = prediction.confidence_score_pct
        error_detail = prediction.error_detail

        logger.info(
            f"AI forecast calculation completed for deal {deal_id_str}",
            extra={
                "correlation_id": corr_id,
                "deal_id": deal_id_str,
                "status_code": status_code,
                "confidence_score_pct": confidence,
                "estimated_completion_date": est_date.isoformat() if est_date else None,
                "trigger_source": trigger_source,
            },
        )

        forecast_model = None
        if db_session is not None:
            forecast_model = db_session.scalar(
                select(DealForecast).where(DealForecast.deal_id == deal_uuid)
            )
            if forecast_model is None:
                forecast_model = DealForecast(
                    deal_id=deal_uuid,
                    estimated_completion_date=est_date,
                    confidence_score_pct=confidence,
                    status_code=status_code,
                    calculated_on=calc_on,
                )
                db_session.add(forecast_model)
            else:
                forecast_model.estimated_completion_date = est_date
                forecast_model.confidence_score_pct = confidence
                forecast_model.status_code = status_code
                forecast_model.calculated_on = calc_on

            eval_log = ForecastEvaluationLog(
                deal_id=deal_uuid,
                estimated_completion_date=est_date,
                confidence_score_pct=confidence,
                status_code=status_code,
                trigger_source=trigger_source,
                correlation_id=corr_id,
                error_detail=error_detail,
                recalculated_on=calc_on,
            )
            db_session.add(eval_log)
            db_session.commit()
            db_session.refresh(forecast_model)

        return ForecastDomain(
            id=forecast_model.id if forecast_model else None,
            deal_id=deal_uuid,
            estimated_completion_date=est_date,
            confidence_score_pct=confidence,
            status_code=status_code,
            calculated_on=calc_on,
            error_detail=error_detail,
            trigger_source=trigger_source,
            model_version=prediction.model_version,
            is_sparse=prediction.is_sparse,
        )

    def recalculate_forecast(
        self,
        deal_id: UUID | str,
        db_session: Session | None = None,
        crm_port: CRMDataPort | None = None,
        forecast_port: ForecastProviderPort | None = None,
        trigger_source: str = "deal_update",
        correlation_id: str | None = None,
    ) -> ForecastDomain:
        """Recalculate AI forecast for a deal when stage, value, or activity changes (FR-010)."""
        return self.calculate_forecast(
            deal_id=deal_id,
            db_session=db_session,
            crm_port=crm_port,
            forecast_port=forecast_port,
            trigger_source=trigger_source,
            correlation_id=correlation_id,
        )

    def get_forecast_by_deal_id(
        self,
        deal_id: UUID | str,
        db_session: Session,
        crm_port: CRMDataPort | None = None,
        forecast_port: ForecastProviderPort | None = None,
        auto_calculate: bool = True,
    ) -> ForecastDomain:
        """Retrieve current deal forecast from database or calculate on demand."""
        deal_uuid = _to_uuid(deal_id)

        deal_model = db_session.get(Deal, deal_uuid)
        if deal_model is None:
            deal_model = db_session.scalar(select(Deal).where(Deal.id == deal_uuid))
        if deal_model is None:
            raise DealNotFoundError(f"Deal with ID {deal_id} not found in database.")

        forecast_model = db_session.scalar(
            select(DealForecast).where(DealForecast.deal_id == deal_uuid)
        )

        if forecast_model is not None:
            return ForecastDomain(
                id=forecast_model.id,
                deal_id=forecast_model.deal_id,
                estimated_completion_date=_ensure_utc(forecast_model.estimated_completion_date),
                confidence_score_pct=forecast_model.confidence_score_pct,
                status_code=forecast_model.status_code,
                calculated_on=_ensure_utc(forecast_model.calculated_on) or datetime.now(timezone.utc),
                trigger_source="query",
            )

        if auto_calculate:
            return self.calculate_forecast(
                deal_id=deal_id,
                db_session=db_session,
                crm_port=crm_port,
                forecast_port=forecast_port,
                trigger_source="initial_query",
            )

        raise ForecastNotFoundError(f"Forecast for deal {deal_id} not found.")

    def get_forecast_evaluation_logs(
        self,
        deal_id: UUID | str,
        db_session: Session,
        limit: int = 50,
    ) -> list[ForecastEvaluationLogDomain]:
        """Retrieve historical forecast evaluation logs for a deal."""
        deal_uuid = _to_uuid(deal_id)

        logs = db_session.scalars(
            select(ForecastEvaluationLog)
            .where(ForecastEvaluationLog.deal_id == deal_uuid)
            .order_by(ForecastEvaluationLog.recalculated_on.desc())
            .limit(limit)
        ).all()

        return [
            ForecastEvaluationLogDomain(
                id=log.id,
                deal_id=log.deal_id,
                estimated_completion_date=_ensure_utc(log.estimated_completion_date),
                confidence_score_pct=log.confidence_score_pct,
                status_code=log.status_code,
                trigger_source=log.trigger_source,
                correlation_id=log.correlation_id,
                error_detail=log.error_detail,
                recalculated_on=_ensure_utc(log.recalculated_on) or datetime.now(timezone.utc),
            )
            for log in logs
        ]


def calculate_forecast(
    deal_id: UUID | str,
    db_session: Session | None = None,
    crm_port: CRMDataPort | None = None,
    forecast_port: ForecastProviderPort | None = None,
    trigger_source: str = "deal_update",
    correlation_id: str | None = None,
) -> ForecastDomain:
    """Calculate or recalculate AI forecast for a deal (functional entrypoint)."""
    service = ForecastingService(crm_port=crm_port, forecast_port=forecast_port)
    return service.calculate_forecast(
        deal_id=deal_id,
        db_session=db_session,
        crm_port=crm_port,
        forecast_port=forecast_port,
        trigger_source=trigger_source,
        correlation_id=correlation_id,
    )


def recalculate_forecast(
    deal_id: UUID | str,
    db_session: Session | None = None,
    crm_port: CRMDataPort | None = None,
    forecast_port: ForecastProviderPort | None = None,
    trigger_source: str = "deal_update",
    correlation_id: str | None = None,
) -> ForecastDomain:
    """Recalculate AI forecast for a deal (functional entrypoint)."""
    return calculate_forecast(
        deal_id=deal_id,
        db_session=db_session,
        crm_port=crm_port,
        forecast_port=forecast_port,
        trigger_source=trigger_source,
        correlation_id=correlation_id,
    )


def get_forecast_by_deal_id(
    deal_id: UUID | str,
    db_session: Session,
    crm_port: CRMDataPort | None = None,
    forecast_port: ForecastProviderPort | None = None,
    auto_calculate: bool = True,
) -> ForecastDomain:
    """Retrieve existing forecast by deal ID or calculate on demand (functional entrypoint)."""
    service = ForecastingService(crm_port=crm_port, forecast_port=forecast_port)
    return service.get_forecast_by_deal_id(
        deal_id=deal_id,
        db_session=db_session,
        crm_port=crm_port,
        forecast_port=forecast_port,
        auto_calculate=auto_calculate,
    )


def get_forecast_evaluation_logs(
    deal_id: UUID | str,
    db_session: Session,
    limit: int = 50,
) -> list[ForecastEvaluationLogDomain]:
    """Retrieve historical forecast evaluation logs for a deal (functional entrypoint)."""
    service = ForecastingService()
    return service.get_forecast_evaluation_logs(
        deal_id=deal_id,
        db_session=db_session,
        limit=limit,
    )
