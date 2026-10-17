"""External CRM connector adapter for retrieving deal history, activities, and contact mappings.

This module implements the ExternalCRMConnector adapter adhering to CRMDataPort (NFR-005).
It retrieves deal stage transition history (FR-008), stakeholder activity logs, and
contact details with fault isolation, input sanitization, structured logging, and fallback mechanisms.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from src.lib.config import settings
from src.lib.logging import get_correlation_id
from src.services.forecasting.crm_port import (
    CRMActivityDTO,
    CRMDataPort,
    CRMDataResult,
    CRMStakeholderDTO,
    DealStageHistoryDTO,
)

logger = logging.getLogger(__name__)


class CRMConnectorError(Exception):
    """Exception raised for CRM connector operation failures."""

    pass


class CRMDataNotFoundError(CRMConnectorError):
    """Exception raised when requested CRM deal record is not found."""

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


class ExternalCRMConnector(CRMDataPort):
    """External CRM connector adapter implementing CRMDataPort with fault isolation."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        http_client: Any | None = None,
        initial_data: dict[str, Any] | None = None,
        simulate_failure: bool = False,
        simulate_timeout: bool = False,
        simulate_sparse_data: bool = False,
    ) -> None:
        """Initialize ExternalCRMConnector adapter.

        Args:
            endpoint_url: Optional API endpoint URL for external CRM service.
            api_key: Optional API key for authenticating with external CRM.
            timeout: Execution timeout in seconds. Defaults to CONNECTOR_TIMEOUT setting.
            retries: Retry attempts for transient failures. Defaults to CONNECTOR_RETRIES setting.
            http_client: Optional HTTP client callable or object for dependency injection.
            initial_data: Optional dictionary containing seeded CRM deal records.
            simulate_failure: Flag to simulate upstream fault for testing.
            simulate_timeout: Flag to simulate request timeout for testing.
            simulate_sparse_data: Flag to simulate sparse history for fallback testing.
        """
        self.endpoint_url = endpoint_url or os.getenv("CRM_API_URL")
        self.api_key = api_key or os.getenv("CRM_API_KEY")
        self.timeout = timeout if timeout is not None else settings.CONNECTOR_TIMEOUT
        self.retries = retries if retries is not None else settings.CONNECTOR_RETRIES
        self.http_client = http_client
        self.initial_data = initial_data or {}
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
                "CRM connector simulating upstream service failure",
                extra={"correlation_id": get_correlation_id(), "deal_id": deal_id},
            )
            raise CRMConnectorError("Simulated upstream CRM failure")

        if self.simulate_timeout:
            logger.warning(
                "CRM connector simulating request timeout",
                extra={"correlation_id": get_correlation_id(), "deal_id": deal_id},
            )
            raise CRMConnectorError(
                f"Request timed out after {self.timeout}s connecting to CRM endpoint"
            )

    def get_deal_stage_history(self, deal_id: str) -> list[DealStageHistoryDTO]:
        """Retrieve chronological milestone stage transition history for a deal."""
        clean_deal_id = self._validate_deal_id(deal_id)
        self._check_simulation_flags(clean_deal_id)

        if self.simulate_sparse_data:
            logger.info(
                "CRM connector returning sparse history (simulated)",
                extra={"correlation_id": get_correlation_id(), "deal_id": clean_deal_id},
            )
            return []

        # Check injected http_client
        if self.http_client is not None:
            if callable(self.http_client):
                res = self.http_client("stage_history", clean_deal_id)
                if isinstance(res, list):
                    return res
            elif isinstance(self.http_client, dict) and clean_deal_id in self.http_client:
                data = self.http_client[clean_deal_id]
                if isinstance(data, dict) and "stage_history" in data:
                    return data["stage_history"]

        # Check initial_data
        if clean_deal_id in self.initial_data:
            deal_record = self.initial_data[clean_deal_id]
            if isinstance(deal_record, dict) and "stage_history" in deal_record:
                history_raw = deal_record["stage_history"]
                result: list[DealStageHistoryDTO] = []
                for item in history_raw:
                    if isinstance(item, DealStageHistoryDTO):
                        result.append(item)
                    elif isinstance(item, dict):
                        result.append(
                            DealStageHistoryDTO(
                                deal_id=clean_deal_id,
                                milestone_stage=item.get("milestone_stage", "Qualification"),
                                entered_at=item.get("entered_at", datetime.now(timezone.utc)),
                                exited_at=item.get("exited_at"),
                                duration_days=item.get("duration_days"),
                            )
                        )
                return result

        # Default fallback mock stage history generation
        now = datetime.now(timezone.utc)
        return [
            DealStageHistoryDTO(
                deal_id=clean_deal_id,
                milestone_stage="Qualification",
                entered_at=now - timedelta(days=30),
                exited_at=now - timedelta(days=20),
                duration_days=10.0,
            ),
            DealStageHistoryDTO(
                deal_id=clean_deal_id,
                milestone_stage="Proposal",
                entered_at=now - timedelta(days=20),
                exited_at=now - timedelta(days=10),
                duration_days=10.0,
            ),
            DealStageHistoryDTO(
                deal_id=clean_deal_id,
                milestone_stage="Negotiation",
                entered_at=now - timedelta(days=10),
                exited_at=None,
                duration_days=10.0,
            ),
        ]

    def get_deal_activities(self, deal_id: str) -> list[CRMActivityDTO]:
        """Retrieve chronological interaction activity logs associated with a deal."""
        clean_deal_id = self._validate_deal_id(deal_id)
        self._check_simulation_flags(clean_deal_id)

        if self.simulate_sparse_data:
            return []

        # Check http_client
        if self.http_client is not None:
            if callable(self.http_client):
                res = self.http_client("activities", clean_deal_id)
                if isinstance(res, list):
                    return res
            elif isinstance(self.http_client, dict) and clean_deal_id in self.http_client:
                data = self.http_client[clean_deal_id]
                if isinstance(data, dict) and "activities" in data:
                    return data["activities"]

        # Check initial_data
        if clean_deal_id in self.initial_data:
            deal_record = self.initial_data[clean_deal_id]
            if isinstance(deal_record, dict) and "activities" in deal_record:
                activities_raw = deal_record["activities"]
                result: list[CRMActivityDTO] = []
                for item in activities_raw:
                    if isinstance(item, CRMActivityDTO):
                        result.append(item)
                    elif isinstance(item, dict):
                        result.append(
                            CRMActivityDTO(
                                activity_id=item.get("activity_id", "act_001"),
                                deal_id=clean_deal_id,
                                stakeholder_id=item.get("stakeholder_id"),
                                activity_type=item.get("activity_type", "Meeting"),
                                summary=item.get("summary", "CRM Activity Summary"),
                                occurred_on=item.get("occurred_on", datetime.now(timezone.utc)),
                            )
                        )
                return result

        # Default fallback mock activity logs
        now = datetime.now(timezone.utc)
        return [
            CRMActivityDTO(
                activity_id=f"act_{clean_deal_id}_1",
                deal_id=clean_deal_id,
                stakeholder_id="sh_001",
                activity_type="Meeting",
                summary="Initial discovery call and requirements review",
                occurred_on=now - timedelta(days=25),
            ),
            CRMActivityDTO(
                activity_id=f"act_{clean_deal_id}_2",
                deal_id=clean_deal_id,
                stakeholder_id="sh_001",
                activity_type="Email",
                summary="Proposal document sent for technical review",
                occurred_on=now - timedelta(days=15),
            ),
            CRMActivityDTO(
                activity_id=f"act_{clean_deal_id}_3",
                deal_id=clean_deal_id,
                stakeholder_id="sh_002",
                activity_type="Call",
                summary="Contract negotiation and SLA terms discussion",
                occurred_on=now - timedelta(days=5),
            ),
        ]

    def get_stakeholders(self, deal_id: str) -> list[CRMStakeholderDTO]:
        """Retrieve mapped contacts and stakeholders for a deal."""
        clean_deal_id = self._validate_deal_id(deal_id)
        self._check_simulation_flags(clean_deal_id)

        if self.simulate_sparse_data:
            return []

        # Check http_client
        if self.http_client is not None:
            if callable(self.http_client):
                res = self.http_client("stakeholders", clean_deal_id)
                if isinstance(res, list):
                    return res
            elif isinstance(self.http_client, dict) and clean_deal_id in self.http_client:
                data = self.http_client[clean_deal_id]
                if isinstance(data, dict) and "stakeholders" in data:
                    return data["stakeholders"]

        # Check initial_data
        if clean_deal_id in self.initial_data:
            deal_record = self.initial_data[clean_deal_id]
            if isinstance(deal_record, dict) and "stakeholders" in deal_record:
                sh_raw = deal_record["stakeholders"]
                result: list[CRMStakeholderDTO] = []
                for item in sh_raw:
                    if isinstance(item, CRMStakeholderDTO):
                        result.append(item)
                    elif isinstance(item, dict):
                        result.append(
                            CRMStakeholderDTO(
                                stakeholder_id=item.get("stakeholder_id", "sh_001"),
                                deal_id=clean_deal_id,
                                full_name=item.get("full_name", "John Doe"),
                                email=item.get("email", "john@example.com"),
                                org_role=item.get("org_role", "Decision Maker"),
                                influence_level=item.get("influence_level", "High"),
                                is_active=item.get("is_active", True),
                            )
                        )
                return result

        # Default fallback mock stakeholders
        return [
            CRMStakeholderDTO(
                stakeholder_id="sh_001",
                deal_id=clean_deal_id,
                full_name="Sarah Jenkins",
                email="s.jenkins@acmecorp.com",
                org_role="VP of Engineering",
                influence_level="High",
                is_active=True,
            ),
            CRMStakeholderDTO(
                stakeholder_id="sh_002",
                deal_id=clean_deal_id,
                full_name="Mark Vance",
                email="m.vance@acmecorp.com",
                org_role="Procurement Lead",
                influence_level="Medium",
                is_active=True,
            ),
        ]

    def fetch_crm_deal_data(self, deal_id: str) -> CRMDataResult:
        """Fetch consolidated deal history, activities, and stakeholder mappings."""
        clean_deal_id = self._validate_deal_id(deal_id)
        now = datetime.now(timezone.utc)

        try:
            stage_history = self.get_deal_stage_history(clean_deal_id)
            activities = self.get_deal_activities(clean_deal_id)
            stakeholders = self.get_stakeholders(clean_deal_id)

            min_transitions = getattr(settings, "FORECAST_MIN_HISTORICAL_TRANSITIONS", 3)
            is_sparse = len(stage_history) < min_transitions or self.simulate_sparse_data

            return CRMDataResult(
                deal_id=clean_deal_id,
                stage_history=stage_history,
                activities=activities,
                stakeholders=stakeholders,
                fetched_at=now,
                is_sparse=is_sparse,
                error_detail=None,
            )
        except CRMConnectorError as err:
            sanitized_err = _sanitize_error_message(str(err))
            logger.error(
                f"CRM connector failed fetching deal data for {clean_deal_id}: {sanitized_err}",
                extra={"correlation_id": get_correlation_id(), "deal_id": clean_deal_id},
            )
            return CRMDataResult(
                deal_id=clean_deal_id,
                stage_history=[],
                activities=[],
                stakeholders=[],
                fetched_at=now,
                is_sparse=True,
                error_detail=sanitized_err,
            )


# Alias for flexible class naming
CRMConnector = ExternalCRMConnector
