"""Pydantic request query and response schemas with strict boundary validation for the executive dashboard API.

This module defines validation models for request parameters (query and path parameters),
sanitized response schemas, and route handlers for the executive dashboard endpoints.
"""

import html
import logging
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from src.api.deps import CurrentUser, get_current_user
from src.connectors.financials import FinancialsConnector
from src.connectors.lead_gen import LeadGenConnector
from src.connectors.sales_pipeline import SalesPipelineConnector
from src.connectors.support_tickets import SupportTicketsConnector
from src.lib.logging import get_correlation_id
from src.models.database import get_db
from src.services.dashboard.aggregator import (
    DashboardAggregationService,
    _sanitize_error_message,
)
from src.services.dashboard.domain_ports import (
    AggregationStatus,
    BreakdownItemDTO,
    DomainConnectorPort,
    DomainIdentifier,
    DomainMetricsResult,
    MetricResult,
    MetricStatus,
    MetricUnit,
    SummaryAggregationResult,
)

logger = logging.getLogger(__name__)


class TimeframeEnum(str, Enum):
    """Allowed query timeframe durations."""

    SEVEN_DAYS = "7d"
    THIRTY_DAYS = "30d"
    NINETY_DAYS = "90d"


class DomainIdentifierEnum(str, Enum):
    """Supported core domain identifiers for drilldown."""

    LEAD_GEN = "lead_gen"
    SALES_PIPELINE = "sales_pipeline"
    SUPPORT = "support"
    FINANCIALS = "financials"


class MetricStatusEnum(str, Enum):
    """Operational metric classification states."""

    VALID = "VALID"
    ZERO = "ZERO"
    UNAVAILABLE = "UNAVAILABLE"


class AggregationStatusEnum(str, Enum):
    """Consolidated summary aggregation operational status."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class DomainStatusEnum(str, Enum):
    """Domain quick-view operational status."""

    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


class MetricUnitEnum(str, Enum):
    """Measurement units for metric formatting."""

    CURRENCY = "currency"
    COUNT = "count"
    PERCENTAGE = "percentage"
    RATIO = "ratio"


def sanitize_input_string(v: str | None) -> str | None:
    """Sanitize and clean raw user string inputs or error details.

    Strips leading/trailing whitespace, strips script/HTML markup tags,
    un-escapes or normalizes unsafe characters, and removes harmful control characters.
    """
    if v is None:
        return None

    cleaned = str(v).strip()
    if not cleaned:
        return cleaned

    # Remove HTML/script tags
    cleaned = re.sub(r"<[^>]*>", "", cleaned)
    # Escape remaining special HTML characters to prevent XSS injection
    cleaned = html.escape(cleaned)
    # Remove unexpected control characters except standard space/tab/newline
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", cleaned)
    return cleaned


class DashboardSummaryQueryParams(BaseModel):
    """Query parameters model for GET /api/v1/dashboard/summary."""

    timeframe: TimeframeEnum = Field(
        default=TimeframeEnum.THIRTY_DAYS,
        description="Comparative period duration ('7d', '30d', '90d'). Default is '30d'.",
    )

    @field_validator("timeframe", mode="before")
    @classmethod
    def validate_timeframe(cls, v: Any) -> TimeframeEnum:
        """Validate and sanitize timeframe input parameter."""
        if isinstance(v, TimeframeEnum):
            return v
        if isinstance(v, str):
            cleaned = v.strip().lower()
            for tf in TimeframeEnum:
                if tf.value == cleaned:
                    return tf
            raise ValueError(
                f"Invalid timeframe parameter '{v}'. Allowed values are '7d', '30d', '90d'."
            )
        raise ValueError(f"Invalid timeframe type: {type(v).__name__}")


class DomainDrilldownPathParams(BaseModel):
    """Path parameters model for GET /api/v1/dashboard/domains/{domain_id}."""

    domain_id: DomainIdentifierEnum = Field(
        ...,
        description="Target domain identifier key ('lead_gen', 'sales_pipeline', 'support', 'financials').",
    )

    @field_validator("domain_id", mode="before")
    @classmethod
    def validate_domain_id(cls, v: Any) -> DomainIdentifierEnum:
        """Validate and sanitize domain_id path parameter."""
        if isinstance(v, DomainIdentifierEnum):
            return v
        if isinstance(v, str):
            cleaned = v.strip().lower()
            for domain in DomainIdentifierEnum:
                if domain.value == cleaned:
                    return domain
            raise ValueError(
                f"Invalid domain_id parameter '{v}'. Allowed domains: 'lead_gen', 'sales_pipeline', 'support', 'financials'."
            )
        raise ValueError(f"Invalid domain_id type: {type(v).__name__}")


class DomainDrilldownQueryParams(BaseModel):
    """Query parameters model for GET /api/v1/dashboard/domains/{domain_id}."""

    timeframe: TimeframeEnum = Field(
        default=TimeframeEnum.THIRTY_DAYS,
        description="Comparative period duration ('7d', '30d', '90d'). Default is '30d'.",
    )

    @field_validator("timeframe", mode="before")
    @classmethod
    def validate_timeframe(cls, v: Any) -> TimeframeEnum:
        """Validate and sanitize timeframe input parameter."""
        return DashboardSummaryQueryParams.validate_timeframe(v)


class MetricResponse(BaseModel):
    """Schema representing an individual KPI metric card in response payloads."""

    domain: str = Field(
        ...,
        description="Domain key identifier (e.g. 'lead_gen', 'sales_pipeline')",
        min_length=1,
        max_length=50,
    )
    metric_key: str = Field(
        ...,
        description="Unique metric key identifier (e.g. 'recurring_revenue')",
        min_length=1,
        max_length=100,
    )
    display_name: str = Field(
        ...,
        description="Human-readable UI card label",
        min_length=1,
        max_length=150,
    )
    formatted_value: str = Field(
        ...,
        description="Formatted value string (e.g., '$12,500', '0', '$0', 'Unavailable')",
        max_length=100,
    )
    raw_value: float | None = Field(
        default=None,
        description="Unformatted numerical metric value",
    )
    baseline_value: float | None = Field(
        default=None,
        description="Comparative period numerical baseline value",
    )
    percentage_change: float | None = Field(
        default=None,
        description="Calculated percentage change relative to baseline",
    )
    status: MetricStatusEnum = Field(
        ...,
        description="Metric state ('VALID', 'ZERO', 'UNAVAILABLE')",
    )
    unit: str = Field(
        default=MetricUnitEnum.COUNT.value,
        description="Display unit type ('currency', 'count', 'percentage', 'ratio')",
        max_length=30,
    )
    error_detail: str | None = Field(
        default=None,
        description="Sanitized error details if metric retrieval failed",
        max_length=255,
    )

    @field_validator("display_name", "formatted_value", "unit", mode="before")
    @classmethod
    def sanitize_strings(cls, v: Any) -> str:
        """Sanitize string values."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""

    @field_validator("error_detail", mode="before")
    @classmethod
    def sanitize_optional_error(cls, v: Any) -> str | None:
        """Sanitize optional error detail string."""
        if v is None:
            return None
        return sanitize_input_string(str(v))

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v: Any) -> MetricStatusEnum:
        """Validate status enum conversion."""
        if isinstance(v, MetricStatusEnum):
            return v
        if isinstance(v, MetricStatus):
            return MetricStatusEnum(v.value)
        if isinstance(v, str):
            v_str = v.strip().upper()
            for s in MetricStatusEnum:
                if s.value == v_str:
                    return s
        return MetricStatusEnum.UNAVAILABLE

    @classmethod
    def from_dto(cls, dto: MetricResult) -> "MetricResponse":
        """Convert MetricResult DTO into validated MetricResponse model."""
        status_enum = MetricStatusEnum.UNAVAILABLE
        if isinstance(dto.status, MetricStatusEnum):
            status_enum = dto.status
        elif isinstance(dto.status, MetricStatus):
            status_enum = MetricStatusEnum(dto.status.value)
        elif isinstance(dto.status, str):
            try:
                status_enum = MetricStatusEnum(dto.status.upper())
            except ValueError:
                status_enum = MetricStatusEnum.UNAVAILABLE

        unit_str = dto.unit if isinstance(dto.unit, str) else dto.unit.value

        return cls(
            domain=dto.domain,
            metric_key=dto.metric_key,
            display_name=dto.display_name,
            formatted_value=dto.formatted_value,
            raw_value=dto.raw_value,
            baseline_value=dto.baseline_value,
            percentage_change=dto.percentage_change,
            status=status_enum,
            unit=unit_str,
            error_detail=dto.error_detail,
        )


class BreakdownItemResponse(BaseModel):
    """Schema representing a distribution segment breakdown item."""

    label: str = Field(
        ...,
        description="Distribution segment name (e.g., 'Lead Source: Organic')",
        min_length=1,
        max_length=150,
    )
    value: float = Field(
        ...,
        description="Segment numerical total",
    )

    @field_validator("label", mode="before")
    @classmethod
    def sanitize_label(cls, v: Any) -> str:
        """Sanitize breakdown label string."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""

    @classmethod
    def from_dto(cls, dto: BreakdownItemDTO) -> "BreakdownItemResponse":
        """Convert BreakdownItemDTO into validated BreakdownItemResponse model."""
        return cls(
            label=dto.label,
            value=dto.value,
        )


class SummaryAggregationResponse(BaseModel):
    """Response schema for consolidated multi-domain dashboard summary."""

    status: AggregationStatusEnum = Field(
        ...,
        description="Overall aggregation state ('COMPLETE', 'PARTIAL', 'FAILED')",
    )
    generated_at: str = Field(
        ...,
        description="ISO 8601 timestamp string of response generation",
    )
    metrics: list[MetricResponse] = Field(
        default_factory=list,
        description="List of aggregated KPI metric objects",
    )

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v: Any) -> AggregationStatusEnum:
        """Validate aggregation status enum conversion."""
        if isinstance(v, AggregationStatusEnum):
            return v
        if isinstance(v, AggregationStatus):
            return AggregationStatusEnum(v.value)
        if isinstance(v, str):
            v_str = v.strip().upper()
            for s in AggregationStatusEnum:
                if s.value == v_str:
                    return s
        return AggregationStatusEnum.FAILED

    @field_validator("generated_at", mode="before")
    @classmethod
    def sanitize_and_validate_timestamp(cls, v: Any) -> str:
        """Sanitize and validate timestamp string."""
        if v is None:
            return datetime.now(timezone.utc).isoformat()
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized else datetime.now(timezone.utc).isoformat()

    @classmethod
    def from_dto(cls, dto: SummaryAggregationResult) -> "SummaryAggregationResponse":
        """Convert SummaryAggregationResult DTO into validated SummaryAggregationResponse model."""
        status_enum = AggregationStatusEnum.FAILED
        if isinstance(dto.status, AggregationStatusEnum):
            status_enum = dto.status
        elif isinstance(dto.status, AggregationStatus):
            status_enum = AggregationStatusEnum(dto.status.value)
        elif isinstance(dto.status, str):
            try:
                status_enum = AggregationStatusEnum(dto.status.upper())
            except ValueError:
                status_enum = AggregationStatusEnum.FAILED

        metric_responses = [MetricResponse.from_dto(m) for m in dto.metrics]

        return cls(
            status=status_enum,
            generated_at=dto.generated_at,
            metrics=metric_responses,
        )


class DomainQuickViewResponse(BaseModel):
    """Response schema for domain quick-view drilldown panel."""

    domain: str = Field(
        ...,
        description="Target domain key identifier",
        min_length=1,
        max_length=50,
    )
    status: DomainStatusEnum = Field(
        ...,
        description="Domain operational status ('COMPLETE', 'UNAVAILABLE')",
    )
    detailed_metrics: list[MetricResponse] = Field(
        default_factory=list,
        description="Array of detailed KPI metric objects matching domain",
    )
    breakdown_items: list[BreakdownItemResponse] = Field(
        default_factory=list,
        description="Array of distribution breakdown objects",
    )
    error_detail: str | None = Field(
        default=None,
        description="Sanitized error detail if domain fetch failed",
        max_length=255,
    )

    @field_validator("domain", mode="before")
    @classmethod
    def sanitize_domain(cls, v: Any) -> str:
        """Sanitize domain string."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v: Any) -> DomainStatusEnum:
        """Validate domain status enum conversion."""
        if isinstance(v, DomainStatusEnum):
            return v
        if isinstance(v, str):
            v_str = v.strip().upper()
            for s in DomainStatusEnum:
                if s.value == v_str:
                    return s
        return DomainStatusEnum.UNAVAILABLE

    @field_validator("error_detail", mode="before")
    @classmethod
    def sanitize_error(cls, v: Any) -> str | None:
        """Sanitize error detail string."""
        if v is None:
            return None
        return sanitize_input_string(str(v))

    @classmethod
    def from_dto(cls, dto: DomainMetricsResult) -> "DomainQuickViewResponse":
        """Convert DomainMetricsResult DTO into validated DomainQuickViewResponse model."""
        status_enum = DomainStatusEnum.UNAVAILABLE
        if isinstance(dto.status, DomainStatusEnum):
            status_enum = dto.status
        elif isinstance(dto.status, str):
            try:
                status_enum = DomainStatusEnum(dto.status.upper())
            except ValueError:
                status_enum = DomainStatusEnum.UNAVAILABLE

        detailed_metrics = [MetricResponse.from_dto(m) for m in dto.metrics]
        breakdown_items = [BreakdownItemResponse.from_dto(b) for b in dto.breakdown_items]

        return cls(
            domain=dto.domain,
            status=status_enum,
            detailed_metrics=detailed_metrics,
            breakdown_items=breakdown_items,
            error_detail=dto.error_detail,
        )


router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def get_connectors() -> list[DomainConnectorPort]:
    """Dependency providing default domain connector adapter instances."""
    return [
        LeadGenConnector(),
        SalesPipelineConnector(),
        SupportTicketsConnector(),
        FinancialsConnector(),
    ]


@router.get(
    "/summary",
    response_model=SummaryAggregationResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve consolidated KPI summary cards",
    description=(
        "Aggregate operational status metrics across lead gen, sales, support, "
        "and financials on a single consolidated view."
    ),
)
def get_dashboard_summary(
    timeframe: str = Query(
        default="30d",
        description="Comparative period duration ('7d', '30d', '90d'). Default is '30d'.",
    ),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    connectors: list[DomainConnectorPort] = Depends(get_connectors),
) -> SummaryAggregationResponse:
    """Retrieve consolidated KPI summary across all operational domains.

    Aggregates operational metrics across Lead Gen, Sales Pipeline, Support Ticketing,
    and Financial Summaries into a consolidated view, evaluating metric status
    classifications (VALID, ZERO, UNAVAILABLE) and comparative baselines.
    """
    try:
        query_params = DashboardSummaryQueryParams(timeframe=timeframe)
    except ValidationError as exc:
        logger.warning(
            "Invalid timeframe parameter provided to dashboard summary",
            extra={
                "correlation_id": get_correlation_id(),
                "timeframe": timeframe,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid timeframe parameter '{timeframe}'. Allowed values are '7d', '30d', '90d'.",
        ) from exc

    timeframe_str = query_params.timeframe.value

    logger.info(
        "Fetching executive dashboard summary metrics",
        extra={
            "correlation_id": get_correlation_id(),
            "timeframe": timeframe_str,
            "user_id": str(current_user.user_id),
            "tenant_id": str(current_user.tenant_id),
        },
    )

    try:
        service = DashboardAggregationService(
            connectors=connectors,
            db_session=db,
        )
        summary_result = service.aggregate_summary(timeframe=timeframe_str)
        return SummaryAggregationResponse.from_dto(summary_result)
    except HTTPException:
        raise
    except Exception as exc:
        sanitized_err = _sanitize_error_message(str(exc))
        logger.error(
            "Failed to aggregate executive dashboard summary",
            extra={
                "correlation_id": get_correlation_id(),
                "timeframe": timeframe_str,
                "error_detail": sanitized_err,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while generating the dashboard summary.",
        ) from exc


@router.get(
    "/domains/{domain_id}",
    response_model=DomainQuickViewResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve domain quick-view drilldown metrics",
    description=(
        "Retrieve detailed operational metrics and distribution breakdowns for a "
        "specific domain ('lead_gen', 'sales_pipeline', 'support', 'financials')."
    ),
)
def get_domain_drilldown(
    domain_id: str,
    timeframe: str = Query(
        default="30d",
        description="Comparative period duration ('7d', '30d', '90d'). Default is '30d'.",
    ),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    connectors: list[DomainConnectorPort] = Depends(get_connectors),
) -> DomainQuickViewResponse:
    """Retrieve quick-view drilldown metrics and distribution breakdowns for a specific domain.

    Supports domain quick-view sections for Lead Generation (volume, conversion),
    Sales Pipeline (deal values, stage distribution), Support Ticketing (open tickets,
    resolution times, SLA compliance), and Financial Summaries (recurring revenue,
    outstanding invoices).
    """
    try:
        path_params = DomainDrilldownPathParams(domain_id=domain_id)
    except ValidationError as exc:
        logger.warning(
            "Invalid domain_id path parameter provided to domain drilldown endpoint",
            extra={
                "correlation_id": get_correlation_id(),
                "domain_id": domain_id,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid domain_id parameter '{domain_id}'. Allowed domains: 'lead_gen', 'sales_pipeline', 'support', 'financials'.",
        ) from exc

    try:
        query_params = DomainDrilldownQueryParams(timeframe=timeframe)
    except ValidationError as exc:
        logger.warning(
            "Invalid timeframe query parameter provided to domain drilldown endpoint",
            extra={
                "correlation_id": get_correlation_id(),
                "timeframe": timeframe,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid timeframe parameter '{timeframe}'. Allowed values are '7d', '30d', '90d'.",
        ) from exc

    target_domain = path_params.domain_id.value
    timeframe_str = query_params.timeframe.value

    logger.info(
        "Fetching executive dashboard domain drilldown metrics",
        extra={
            "correlation_id": get_correlation_id(),
            "domain_id": target_domain,
            "timeframe": timeframe_str,
            "user_id": str(current_user.user_id),
            "tenant_id": str(current_user.tenant_id),
        },
    )

    try:
        service = DashboardAggregationService(
            connectors=connectors,
            db_session=db,
        )
        domain_result = service.aggregate_domain_detail(
            domain_id=target_domain,
            timeframe=timeframe_str,
        )
        return DomainQuickViewResponse.from_dto(domain_result)
    except HTTPException:
        raise
    except Exception as exc:
        sanitized_err = _sanitize_error_message(str(exc))
        logger.error(
            "Failed to retrieve executive dashboard domain drilldown metrics",
            extra={
                "correlation_id": get_correlation_id(),
                "domain_id": target_domain,
                "timeframe": timeframe_str,
                "error_detail": sanitized_err,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while generating the domain drilldown metrics.",
        ) from exc
