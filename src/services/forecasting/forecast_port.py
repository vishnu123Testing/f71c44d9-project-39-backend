"""Abstract forecast provider port definition for completion timeline and confidence estimations.

This module defines the abstract port interface (ForecastProviderPort) and associated DTOs
that decouple external AI forecasting providers from core domain services (NFR-005).
Supports computing AI completion dates (FR-008) and confidence percentages (FR-009).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from src.services.forecasting.crm_port import CRMDataPort, CRMDataResult


@dataclass
class ForecastPredictionResult:
    """DTO encapsulating AI completion timeline estimation and confidence score."""

    deal_id: str
    estimated_completion_date: datetime | None = None
    confidence_score_pct: float | None = None
    status_code: str = "READY"  # READY, INSUFFICIENT_DATA, COMPUTING, FAILED
    calculated_on: datetime | None = None
    is_sparse: bool = False
    error_detail: str | None = None
    model_version: str | None = "v1.0"


class ForecastProviderPort(ABC):
    """Abstract port interface for AI forecast timeline and confidence predictions."""

    @abstractmethod
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
            crm_port: Optional explicitly injected CRMDataPort instance.

        Returns:
            ForecastPredictionResult containing close date estimation and confidence score.
        """
        ...

    def compute_forecast(
        self,
        deal_id: str,
        crm_data: CRMDataResult | None = None,
        crm_port: CRMDataPort | None = None,
    ) -> ForecastPredictionResult:
        """Alias for predict_forecast to support uniform domain service invocation."""
        return self.predict_forecast(
            deal_id=deal_id,
            crm_data=crm_data,
            crm_port=crm_port,
        )
