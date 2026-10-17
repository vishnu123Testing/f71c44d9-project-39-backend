"""Forecast provider connector adapter for date estimations and confidence scores.

This module implements the AIForecastConnector adapter adhering to ForecastProviderPort (NFR-005).
It computes AI-driven estimated deal completion timelines (FR-008) and percentage-based
confidence scores (FR-009) using historical and real-time CRM data, with explicit parameter
dependency injection, fault isolation, input sanitization, and sparse data fallbacks.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from src.connectors.crm_connector import ExternalCRMConnector
from src.lib.config import settings
from src.lib.logging import get_correlation_id
from src.services.forecasting.crm_port import CRMDataPort, CRMDataResult
from src.services.forecasting.forecast_port import (
    ForecastPredictionResult,
    ForecastProviderPort,
)

logger = logging.getLogger(__name__)


class ForecastConnectorError(Exception):
    """Exception raised for forecast provider connector operation failures."""

    pass


class ForecastProviderError(ForecastConnectorError):
    """Exception raised for upstream AI forecast engine errors."""

    pass


class ForecastInsufficientDataError(ForecastConnectorError):
    """Exception raised when CRM deal history is sparse or insufficient for prediction."""

    pass


def _sanitize_error_message(msg: str) -> str:
    """Sanitize error messages to prevent credential or token exposure."""
    if not msg:
        return "Unknown error occurred"
    cleaned = str(msg).split("?")[0]
    cleaned = cleaned.replace("\n", " ").strip()
    if len(cleaned) > 200:
        cleaned = cleaned[:197] + "..."
    return cleaned


class AIForecastConnector(ForecastProviderPort):
    """Forecast provider connector adapter implementing ForecastProviderPort."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        http_client: Any | None = None,
        min_historical_transitions: int | None = None,
        default_confidence_pct: float | None = None,
        crm_port: CRMDataPort | None = None,
        initial_forecasts: dict[str, Any] | None = None,
        simulate_failure: bool = False,
        simulate_timeout: bool = False,
        simulate_sparse_data: bool = False,
    ) -> None:
        """Initialize AIForecastConnector adapter.

        Args:
            endpoint_url: Optional API endpoint URL for external forecast provider.
            api_key: Optional API key for authenticating with forecast provider.
            timeout: Execution timeout in seconds.
            retries: Retry attempts for transient failures.
            http_client: Optional HTTP client callable or object for dependency injection.
            min_historical_transitions: Minimum stage transitions required for prediction.
            default_confidence_pct: Default baseline confidence percentage score.
            crm_port: Explicitly injected CRM data port instance (NFR-005).
            initial_forecasts: Optional dictionary containing seeded forecast responses.
            simulate_failure: Flag to simulate upstream provider failure for testing.
            simulate_timeout: Flag to simulate request timeout for testing.
            simulate_sparse_data: Flag to simulate sparse CRM history for fallback testing.
        """
        self.endpoint_url = endpoint_url or os.getenv("FORECAST_PROVIDER_URL") or getattr(
            settings, "FORECAST_PROVIDER_URL", None
        )
        self.api_key = api_key or os.getenv("FORECAST_PROVIDER_API_KEY") or getattr(
            settings, "FORECAST_PROVIDER_API_KEY", None
        )
        self.timeout = timeout if timeout is not None else getattr(settings, "CONNECTOR_TIMEOUT", 5.0)
        self.retries = retries if retries is not None else getattr(settings, "CONNECTOR_RETRIES", 3)
        self.http_client = http_client
        self.min_historical_transitions = (
            min_historical_transitions
            if min_historical_transitions is not None
            else getattr(settings, "FORECAST_MIN_HISTORICAL_TRANSITIONS", 3)
        )
        self.default_confidence_pct = (
            default_confidence_pct
            if default_confidence_pct is not None
            else getattr(settings, "FORECAST_DEFAULT_CONFIDENCE_PCT", 75.0)
        )
        self.crm_port = crm_port
        self.initial_forecasts = initial_forecasts or {}
        self.simulate_failure = simulate_failure
        self.simulate_timeout = simulate_timeout
        self.simulate_sparse_data = simulate_sparse_data

    def _validate_deal_id(self, deal_id: str) -> str:
        """Sanitize and validate deal_id parameter."""
        if not deal_id or not isinstance(deal_id, str) or not deal_id.strip():
            raise ValueError("deal_id must be a non-empty string")
        return deal_id.strip()

    def _check_simulation_flags(self, deal_id: str) -> None:
        """Check simulation flags and raise appropriate exceptions if enabled."""
        if self.simulate_failure:
            logger.warning(
                "Forecast connector simulating upstream provider failure",
                extra={"correlation_id": get_correlation_id(), "deal_id": deal_id},
            )
            raise ForecastProviderError("Simulated upstream AI forecast provider failure")

        if self.simulate_timeout:
            logger.warning(
                "Forecast connector simulating request timeout",
                extra={"correlation_id": get_correlation_id(), "deal_id": deal_id},
            )
            raise ForecastConnectorError(
                f"Request timed out after {self.timeout}s connecting to forecast provider endpoint"
            )

    def predict_forecast(
        self,
        deal_id: str,
        crm_data: CRMDataResult | None = None,
        crm_port: CRMDataPort | None = None,
    ) -> ForecastPredictionResult:
        """Compute estimated completion date and percentage confidence score.

        Args:
            deal_id: Unique string identifier of the deal.
            crm_data: Optional pre-fetched CRMDataResult.
            crm_port: Optional explicitly injected CRMDataPort instance (NFR-005).

        Returns:
            ForecastPredictionResult containing estimated close date and confidence score.
        """
        clean_deal_id = self._validate_deal_id(deal_id)
        now = datetime.now(timezone.utc)

        try:
            self._check_simulation_flags(clean_deal_id)
        except ForecastConnectorError as err:
            sanitized_err = _sanitize_error_message(str(err))
            logger.error(
                f"Forecast connector error for deal {clean_deal_id}: {sanitized_err}",
                extra={"correlation_id": get_correlation_id(), "deal_id": clean_deal_id},
            )
            return ForecastPredictionResult(
                deal_id=clean_deal_id,
                estimated_completion_date=None,
                confidence_score_pct=None,
                status_code="FAILED",
                calculated_on=now,
                is_sparse=True,
                error_detail=sanitized_err,
            )

        # Handle simulation of sparse data directly
        if self.simulate_sparse_data:
            logger.info(
                "Forecast connector returning INSUFFICIENT_DATA status (simulated)",
                extra={"correlation_id": get_correlation_id(), "deal_id": clean_deal_id},
            )
            return ForecastPredictionResult(
                deal_id=clean_deal_id,
                estimated_completion_date=None,
                confidence_score_pct=None,
                status_code="INSUFFICIENT_DATA",
                calculated_on=now,
                is_sparse=True,
                error_detail="Sparse CRM historical stage transitions",
            )

        # Resolve CRM data using explicitly injected parameter, instance property, or fallback
        resolved_crm_port = crm_port or self.crm_port
        resolved_crm_data = crm_data

        if resolved_crm_data is None:
            if resolved_crm_port is not None:
                resolved_crm_data = resolved_crm_port.fetch_crm_deal_data(clean_deal_id)
            else:
                default_crm = ExternalCRMConnector()
                resolved_crm_data = default_crm.fetch_crm_deal_data(clean_deal_id)

        # Check for sparse or insufficient CRM data (FR-009 fallback handling)
        if (
            resolved_crm_data is None
            or resolved_crm_data.is_sparse
            or len(resolved_crm_data.stage_history) < self.min_historical_transitions
            or resolved_crm_data.error_detail is not None
        ):
            err_msg = (
                resolved_crm_data.error_detail
                if resolved_crm_data and resolved_crm_data.error_detail
                else "Sparse CRM historical stage transitions"
            )
            logger.info(
                f"Sparse CRM data for deal {clean_deal_id}, returning INSUFFICIENT_DATA forecast status",
                extra={"correlation_id": get_correlation_id(), "deal_id": clean_deal_id},
            )
            return ForecastPredictionResult(
                deal_id=clean_deal_id,
                estimated_completion_date=None,
                confidence_score_pct=None,
                status_code="INSUFFICIENT_DATA",
                calculated_on=now,
                is_sparse=True,
                error_detail=err_msg,
            )

        # Check injected http_client
        if self.http_client is not None:
            if callable(self.http_client):
                res = self.http_client("predict", clean_deal_id)
                if isinstance(res, ForecastPredictionResult):
                    return res
            elif isinstance(self.http_client, dict) and clean_deal_id in self.http_client:
                data = self.http_client[clean_deal_id]
                if isinstance(data, ForecastPredictionResult):
                    return data

        # Check initial_forecasts
        if clean_deal_id in self.initial_forecasts:
            forecast_raw = self.initial_forecasts[clean_deal_id]
            if isinstance(forecast_raw, ForecastPredictionResult):
                return forecast_raw
            if isinstance(forecast_raw, dict):
                return ForecastPredictionResult(
                    deal_id=clean_deal_id,
                    estimated_completion_date=forecast_raw.get("estimated_completion_date"),
                    confidence_score_pct=forecast_raw.get("confidence_score_pct", self.default_confidence_pct),
                    status_code=forecast_raw.get("status_code", "READY"),
                    calculated_on=now,
                    is_sparse=forecast_raw.get("is_sparse", False),
                    error_detail=forecast_raw.get("error_detail"),
                )

        # Algorithmic prediction logic using CRM history (FR-008, FR-009)
        stage_history = resolved_crm_data.stage_history
        activities = resolved_crm_data.activities
        stakeholders = resolved_crm_data.stakeholders

        # Calculate historical transition velocity
        durations = [h.duration_days for h in stage_history if h.duration_days is not None]
        avg_stage_duration_days = (sum(durations) / len(durations)) if durations else 14.0
        remaining_estimated_days = max(7.0, round(avg_stage_duration_days * 1.5, 1))

        est_completion_date = now + timedelta(days=remaining_estimated_days)

        # Calculate percentage-based confidence score
        base_confidence = self.default_confidence_pct
        transition_bonus = min(15.0, len(stage_history) * 3.0)
        activity_bonus = min(10.0, len(activities) * 2.0)
        stakeholder_bonus = min(5.0, len(stakeholders) * 2.5)

        calculated_confidence = min(
            99.0,
            max(10.0, round(base_confidence + transition_bonus + activity_bonus + stakeholder_bonus, 1)),
        )

        logger.info(
            f"Successfully computed forecast for deal {clean_deal_id}: "
            f"date={est_completion_date.isoformat()}, confidence={calculated_confidence}%",
            extra={"correlation_id": get_correlation_id(), "deal_id": clean_deal_id},
        )

        return ForecastPredictionResult(
            deal_id=clean_deal_id,
            estimated_completion_date=est_completion_date,
            confidence_score_pct=calculated_confidence,
            status_code="READY",
            calculated_on=now,
            is_sparse=False,
            error_detail=None,
        )


# Class Aliases for flexible naming conventions across domain components
ForecastConnector = AIForecastConnector
ForecastProviderConnector = AIForecastConnector
ExternalForecastConnector = AIForecastConnector
