"""FastAPI router and response schemas for lead acquisition and marketing analytics endpoints.

Provides GET /api/v1/analytics/acquisition with input sanitization, Pydantic validation,
and structured JSON logging with correlation IDs [FR-001, FR-002, FR-003, NFR-003, NFR-004].
"""

from datetime import datetime, timezone
import html
import logging
import re
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from src.api.deps import CurrentUser, get_current_user
from src.lib.config import settings
from src.lib.logging import get_correlation_id
from src.models.analytics import ReportAuditLog
from src.models.database import get_db
from src.services.analytics.funnel import (
    FunnelNotFoundError,
    FunnelStageDTO,
    InvalidFunnelDataError,
    analyze_conversion_funnel,
)
from src.services.analytics.reporting import (
    CampaignMetricsDTO,
    CampaignNotFoundError,
    CampaignTrendPointDTO,
    ChannelAcquisitionReportDTO,
    ChannelNotFoundError,
    ComparativeCampaignTrendDTO,
    ConfigurationNotFoundError,
    InvalidReportingDataError,
    create_report_audit_log,
    delete_report_configuration,
    get_campaign_trend_comparison,
    get_channel_acquisition_report,
)
from src.services.marketing.territory import (
    InvalidTerritoryFilterError,
    TerritoryAccessDeniedError,
    TerritoryDensityDTO,
    TerritoryHeatMapDTO,
    TerritoryNotFoundError,
    UnassignedDrawerDTO,
    UnassignedLeadDTO,
    get_territory_heatmap,
)
from src.services.support.ticket_service import TicketService
from src.workers.export_tasks import (
    generate_report_export_task,
    run_export_report_generation,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics"])
support_router = APIRouter(prefix="/support", tags=["analytics"])


def sanitize_input_string(v: str | None) -> str | None:
    """Sanitize and clean raw user string inputs.

    Strips leading/trailing whitespace, strips script/HTML markup tags,
    escapes special HTML characters, and removes unexpected control characters.
    """
    if v is None:
        return None

    cleaned = str(v).strip()
    if not cleaned:
        return cleaned

    # Remove script and HTML tags
    cleaned = re.sub(r"<[^>]*>", "", cleaned)
    # Escape special HTML characters
    cleaned = html.escape(cleaned)
    # Remove control characters except space, tab, newline
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", cleaned)
    return cleaned


class AcquisitionReportItemResponse(BaseModel):
    """Pydantic response schema representing channel acquisition performance metrics."""

    channel_id: UUID = Field(..., description="Unique identifier of the acquisition channel")
    channel_name: str = Field(..., description="Name of the marketing acquisition channel")
    lead_count: int = Field(..., description="Total number of attributed leads")
    quality_score: float = Field(..., description="Average lead quality score (0.0 to 100.0)")
    spend_amount: float = Field(..., description="Total marketing spend amount for the channel")
    revenue_amount: float = Field(..., description="Total attributed revenue amount")
    roi: str = Field(..., description="Calculated Return on Investment percentage string or 'N/A'")

    @field_validator("channel_name", "roi", mode="before")
    @classmethod
    def sanitize_strings(cls, v: Any) -> str:
        """Sanitize string attributes prior to model validation."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""

    @classmethod
    def from_dto(cls, dto: ChannelAcquisitionReportDTO) -> "AcquisitionReportItemResponse":
        """Convert a ChannelAcquisitionReportDTO domain object into a response schema."""
        return cls(
            channel_id=dto.channel_id,
            channel_name=dto.channel_name,
            lead_count=dto.lead_count,
            quality_score=dto.quality_score,
            spend_amount=dto.spend_amount,
            revenue_amount=dto.revenue_amount,
            roi=dto.roi,
        )


class FunnelStageResponse(BaseModel):
    """Pydantic response schema representing conversion funnel stage performance and bottleneck flags."""

    stage_name: str = Field(..., description="Name of the conversion funnel stage")
    count: int = Field(..., description="Total lead count at this stage")
    lead_count: int = Field(..., description="Total lead count at this stage")
    conversion_rate: float = Field(..., description="Step conversion rate percentage (0.0 to 100.0)")
    dropoff_rate: float = Field(..., description="Step drop-off rate percentage (0.0 to 100.0)")
    is_bottleneck: bool = Field(..., description="Whether drop-off rate exceeds configured threshold")
    threshold_percent: float = Field(..., description="Configured drop-off threshold percentage")
    overall_conversion_rate: float = Field(0.0, description="Overall conversion rate percentage relative to top stage")

    @field_validator("stage_name", mode="before")
    @classmethod
    def sanitize_stage_name(cls, v: Any) -> str:
        """Sanitize stage name string."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""

    @classmethod
    def from_dto(cls, dto: FunnelStageDTO) -> "FunnelStageResponse":
        """Convert a FunnelStageDTO domain object into a response schema."""
        return cls(
            stage_name=dto.stage_name,
            count=dto.lead_count,
            lead_count=dto.lead_count,
            conversion_rate=dto.conversion_rate,
            dropoff_rate=dto.dropoff_rate,
            is_bottleneck=dto.is_bottleneck,
            threshold_percent=dto.threshold_percent,
            overall_conversion_rate=dto.overall_conversion_rate,
        )


class CampaignTrendPointResponse(BaseModel):
    """Pydantic response schema for time-series trend point metrics."""

    period_val: str = Field(..., description="Date period string for the trend data point (YYYY-MM-DD)")
    lead_count: int = Field(..., description="Acquisition lead volume during this period")
    cost_per_lead: float = Field(..., description="Cost per lead (CPL) during this period")
    engagement_rate: float = Field(..., description="Engagement rate percentage during this period")

    @field_validator("period_val", mode="before")
    @classmethod
    def sanitize_period(cls, v: Any) -> str:
        """Sanitize period value string."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""

    @classmethod
    def from_dto(cls, dto: CampaignTrendPointDTO) -> "CampaignTrendPointResponse":
        """Convert a CampaignTrendPointDTO domain object into a response schema."""
        return cls(
            period_val=dto.period_val,
            lead_count=dto.lead_count,
            cost_per_lead=dto.cost_per_lead,
            engagement_rate=dto.engagement_rate,
        )


class CampaignMetricsResponse(BaseModel):
    """Pydantic response schema for summary and time-series metrics for an individual campaign."""

    campaign_id: UUID = Field(..., description="Unique identifier of the campaign")
    campaign_name: str = Field(..., description="Name of the campaign")
    budget: float = Field(..., description="Configured campaign budget")
    total_leads: int = Field(..., description="Total lead acquisition volume across the timeframe")
    overall_cpl: float = Field(..., description="Overall cost per lead (CPL)")
    overall_engagement_rate: float = Field(..., description="Overall engagement rate percentage")
    trend_points: list[CampaignTrendPointResponse] = Field(
        default_factory=list,
        description="Daily time-series trend metrics",
    )

    @field_validator("campaign_name", mode="before")
    @classmethod
    def sanitize_campaign_name(cls, v: Any) -> str:
        """Sanitize campaign name string."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""

    @classmethod
    def from_dto(cls, dto: CampaignMetricsDTO) -> "CampaignMetricsResponse":
        """Convert a CampaignMetricsDTO domain object into a response schema."""
        return cls(
            campaign_id=dto.campaign_id,
            campaign_name=dto.campaign_name,
            budget=dto.budget,
            total_leads=dto.total_leads,
            overall_cpl=dto.overall_cpl,
            overall_engagement_rate=dto.overall_engagement_rate,
            trend_points=[CampaignTrendPointResponse.from_dto(tp) for tp in dto.trend_points],
        )


class ComparativeCampaignTrendResponse(BaseModel):
    """Pydantic response schema for multi-campaign trend comparison results."""

    start_date_val: datetime | None = Field(None, description="Start date of the comparison timeframe")
    end_date_val: datetime | None = Field(None, description="End date of the comparison timeframe")
    campaigns: list[CampaignMetricsResponse] = Field(
        default_factory=list,
        description="List of compared campaigns and their metrics",
    )

    @classmethod
    def from_dto(cls, dto: ComparativeCampaignTrendDTO) -> "ComparativeCampaignTrendResponse":
        """Convert a ComparativeCampaignTrendDTO domain object into a response schema."""
        return cls(
            start_date_val=dto.start_date_val,
            end_date_val=dto.end_date_val,
            campaigns=[CampaignMetricsResponse.from_dto(cm) for cm in dto.campaigns],
        )


class UnassignedLeadResponse(BaseModel):
    """Pydantic response schema representing an unassigned lead or opportunity event."""

    lead_id: UUID = Field(..., description="Unique identifier of the lead")
    postal_code: str | None = Field(None, description="Raw postal code string if available")
    stage_name: str = Field(..., description="Stage name / lead status")
    deal_value: float | None = Field(None, description="Associated deal or opportunity value")
    event_timestamp: datetime = Field(..., description="UTC timestamp of the lead event")
    reason: str = Field(..., description="Reason why the lead was unassigned")

    @field_validator("postal_code", "stage_name", "reason", mode="before")
    @classmethod
    def sanitize_unassigned_lead_strings(cls, v: Any) -> str | None:
        """Sanitize string attributes prior to model validation."""
        if v is None:
            return None
        return sanitize_input_string(str(v))

    @classmethod
    def from_dto(cls, dto: UnassignedLeadDTO) -> "UnassignedLeadResponse":
        """Convert an UnassignedLeadDTO domain object into a response schema."""
        return cls(
            lead_id=dto.lead_id,
            postal_code=dto.postal_code,
            stage_name=dto.stage_name,
            deal_value=dto.deal_value,
            event_timestamp=dto.event_timestamp,
            reason=dto.reason,
        )


class UnassignedDrawerResponse(BaseModel):
    """Pydantic response schema for unassigned lead and opportunity breakdown drawer data."""

    total_unassigned_leads: int = Field(..., description="Total count of unassigned leads/opportunities")
    total_unassigned_value: float = Field(..., description="Total deal value of unassigned leads")
    unassigned_items: list[UnassignedLeadResponse] = Field(
        default_factory=list,
        description="List of unassigned lead items",
    )

    @classmethod
    def from_dto(cls, dto: UnassignedDrawerDTO) -> "UnassignedDrawerResponse":
        """Convert an UnassignedDrawerDTO domain object into a response schema."""
        return cls(
            total_unassigned_leads=dto.total_unassigned_leads,
            total_unassigned_value=dto.total_unassigned_value,
            unassigned_items=[UnassignedLeadResponse.from_dto(item) for item in dto.unassigned_items],
        )


class TerritoryDensityResponse(BaseModel):
    """Pydantic response schema representing lead and opportunity density metrics for a territory."""

    territory_id: UUID = Field(..., description="Unique identifier of the territory")
    territory_code: str = Field(..., description="Unique code identifier of the territory")
    territory_name: str = Field(..., description="Display name of the territory")
    lead_count: int = Field(..., description="Total lead count in this territory")
    opportunity_count: int = Field(..., description="Count of leads with positive deal value")
    total_deal_value: float = Field(..., description="Total deal value across leads in this territory")
    average_deal_value: float = Field(..., description="Average deal value per opportunity in this territory")
    postal_code_breakdown: dict[str, int] = Field(
        default_factory=dict,
        description="Map of postal codes to lead counts within the territory",
    )

    @field_validator("territory_code", "territory_name", mode="before")
    @classmethod
    def sanitize_territory_strings(cls, v: Any) -> str:
        """Sanitize territory string attributes."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""

    @classmethod
    def from_dto(cls, dto: TerritoryDensityDTO) -> "TerritoryDensityResponse":
        """Convert a TerritoryDensityDTO domain object into a response schema."""
        return cls(
            territory_id=dto.territory_id,
            territory_code=dto.territory_code,
            territory_name=dto.territory_name,
            lead_count=dto.lead_count,
            opportunity_count=dto.opportunity_count,
            total_deal_value=dto.total_deal_value,
            average_deal_value=dto.average_deal_value,
            postal_code_breakdown=dto.postal_code_breakdown,
        )


class TerritoryHeatMapResponse(BaseModel):
    """Pydantic response schema for geographic territory heat map visualization."""

    territories: list[TerritoryDensityResponse] = Field(
        default_factory=list,
        description="Density metrics for authorized territories",
    )
    unassigned_drawer: UnassignedDrawerResponse = Field(
        ...,
        description="Unassigned lead breakdown drawer data",
    )
    total_leads: int = Field(..., description="Total lead count across territories and unassigned drawer")
    total_value: float = Field(..., description="Total deal value across territories and unassigned drawer")

    @classmethod
    def from_dto(cls, dto: TerritoryHeatMapDTO) -> "TerritoryHeatMapResponse":
        """Convert a TerritoryHeatMapDTO domain object into a response schema."""
        return cls(
            territories=[TerritoryDensityResponse.from_dto(t) for t in dto.territories],
            unassigned_drawer=UnassignedDrawerResponse.from_dto(dto.unassigned_drawer),
            total_leads=dto.total_leads,
            total_value=dto.total_value,
        )


class AuditLogCreateRequest(BaseModel):
    """Pydantic request schema for creating a reporting configuration audit log entry."""

    action_type: str = Field(..., description="Action type or metric rule identifier")
    config_delta: str | None = Field(None, description="Delta or JSON description of configuration change")

    @field_validator("action_type", "config_delta", mode="before")
    @classmethod
    def sanitize_audit_strings(cls, v: Any) -> str | None:
        """Sanitize string inputs prior to model validation."""
        if v is None:
            return None
        return sanitize_input_string(str(v))

    @field_validator("action_type")
    @classmethod
    def validate_action_type(cls, v: str | None) -> str:
        """Ensure action_type is not empty after sanitization."""
        if not v or not v.strip():
            raise ValueError("action_type cannot be empty.")
        return v.strip()


class AuditLogResponse(BaseModel):
    """Pydantic response schema representing a report audit log record."""

    id: UUID = Field(..., description="Unique identifier of the audit log record")
    user_id: UUID | None = Field(None, description="UUID of the user performing the configuration change")
    action_type: str = Field(..., description="Action type or metric rule identifier")
    config_delta: str | None = Field(None, description="Configuration delta description")
    created_on: datetime = Field(..., description="UTC creation timestamp")

    @field_validator("action_type", "config_delta", mode="before")
    @classmethod
    def sanitize_response_strings(cls, v: Any) -> str | None:
        """Sanitize response string fields."""
        if v is None:
            return None
        return sanitize_input_string(str(v))

    @classmethod
    def from_model(cls, model: ReportAuditLog) -> "AuditLogResponse":
        """Convert a ReportAuditLog ORM model instance into a response schema."""
        return cls(
            id=model.id,
            user_id=model.user_id,
            action_type=model.action_type,
            config_delta=model.config_delta,
            created_on=model.created_on,
        )


@router.get(
    "/acquisition",
    response_model=list[AcquisitionReportItemResponse],
    status_code=status.HTTP_200_OK,
    summary="Retrieve channel acquisition performance metrics",
    description=(
        "Aggregate channel acquisition lead volume, quality scores, spend, revenue, "
        "and calculated ROI with optional date range, channel filter, and sorting."
    ),
)
def get_acquisition_report(
    start_date_val: datetime | None = Query(
        default=None,
        description="Filter events starting from this UTC timestamp (ISO 8601).",
    ),
    end_date_val: datetime | None = Query(
        default=None,
        description="Filter events up to this UTC timestamp (ISO 8601).",
    ),
    channel_id: str | None = Query(
        default=None,
        description="Filter by specific acquisition channel UUID or channel name.",
    ),
    sort_by: str | None = Query(
        default=None,
        description="Sort by field: 'quality_score', 'spend', 'revenue', 'lead_count', 'channel_name', or 'roi'.",
    ),
    sort_order: str = Query(
        default="desc",
        description="Sort direction: 'asc' or 'desc'. Default is 'desc'.",
    ),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> list[AcquisitionReportItemResponse]:
    """Retrieve acquisition channel performance metrics, quality scores, and safe ROI calculations."""
    sanitized_channel_id = sanitize_input_string(channel_id)
    sanitized_sort_by = sanitize_input_string(sort_by)
    sanitized_sort_order_raw = sanitize_input_string(sort_order) or "desc"
    sanitized_sort_order = (
        sanitized_sort_order_raw.lower()
        if sanitized_sort_order_raw.lower() in ("asc", "desc")
        else "desc"
    )

    logger.info(
        "Fetching acquisition channel report",
        extra={
            "correlation_id": get_correlation_id(),
            "user_id": str(current_user.user_id),
            "tenant_id": str(current_user.tenant_id),
            "start_date_val": start_date_val.isoformat() if start_date_val else None,
            "end_date_val": end_date_val.isoformat() if end_date_val else None,
            "channel_id": sanitized_channel_id,
            "sort_by": sanitized_sort_by,
            "sort_order": sanitized_sort_order,
        },
    )

    try:
        report_dtos = get_channel_acquisition_report(
            db=db,
            start_date_val=start_date_val,
            end_date_val=end_date_val,
            channel_id=sanitized_channel_id,
            sort_by=sanitized_sort_by,
            sort_order=sanitized_sort_order,
        )
        response_items = [AcquisitionReportItemResponse.from_dto(dto) for dto in report_dtos]

        logger.info(
            "Acquisition channel report generated successfully",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "tenant_id": str(current_user.tenant_id),
                "result_count": len(response_items),
            },
        )
        return response_items
    except ChannelNotFoundError as exc:
        logger.warning(
            f"Acquisition channel not found: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "channel_id": sanitized_channel_id,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except InvalidReportingDataError as exc:
        logger.warning(
            f"Invalid reporting parameters provided: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            f"Unexpected error generating channel acquisition report: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "error": str(exc),
            },
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while generating the acquisition report.",
        ) from exc


@router.get(
    "/funnel",
    response_model=list[FunnelStageResponse],
    status_code=status.HTTP_200_OK,
    summary="Retrieve conversion funnel stages and drop-off bottleneck flags",
    description=(
        "Analyze stage-by-stage lead counts, step conversion rates, drop-off rates, "
        "and bottleneck flags where drop-off rates exceed configured thresholds."
    ),
)
def get_conversion_funnel(
    funnel_id: str | None = Query(
        default=None,
        description="Optional funnel identifier or context name.",
    ),
    start_date_val: datetime | None = Query(
        default=None,
        description="Filter events starting from this UTC timestamp (ISO 8601).",
    ),
    end_date_val: datetime | None = Query(
        default=None,
        description="Filter events up to this UTC timestamp (ISO 8601).",
    ),
    channel_id: str | None = Query(
        default=None,
        description="Filter funnel by specific acquisition channel UUID.",
    ),
    campaign_id: str | None = Query(
        default=None,
        description="Filter funnel by specific campaign UUID.",
    ),
    stages: list[str] | None = Query(
        default=None,
        description="Optional ordered list of stage names to include in the funnel.",
    ),
    default_threshold: float | None = Query(
        default=None,
        ge=0.0,
        le=100.0,
        description="Override default drop-off bottleneck threshold percentage (0.0 to 100.0).",
    ),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> list[FunnelStageResponse]:
    """Retrieve conversion stage metrics, step conversion rates, and bottleneck flags [FR-004, FR-005, NFR-003, NFR-004]."""
    sanitized_funnel_id = sanitize_input_string(funnel_id)
    sanitized_channel_id = sanitize_input_string(channel_id)
    sanitized_campaign_id = sanitize_input_string(campaign_id)

    parsed_stages: list[str] | None = None
    if stages is not None:
        raw_stages: list[str] = []
        for stage_item in stages:
            if "," in stage_item:
                raw_stages.extend(stage_item.split(","))
            else:
                raw_stages.append(stage_item)
        clean_stages = [
            sanitized
            for s in raw_stages
            if (sanitized := sanitize_input_string(s))
        ]
        if clean_stages:
            parsed_stages = clean_stages

    logger.info(
        "Fetching conversion funnel analysis",
        extra={
            "correlation_id": get_correlation_id(),
            "user_id": str(current_user.user_id),
            "tenant_id": str(current_user.tenant_id),
            "funnel_id": sanitized_funnel_id,
            "start_date_val": start_date_val.isoformat() if start_date_val else None,
            "end_date_val": end_date_val.isoformat() if end_date_val else None,
            "channel_id": sanitized_channel_id,
            "campaign_id": sanitized_campaign_id,
            "stages": parsed_stages,
            "default_threshold": default_threshold,
        },
    )

    try:
        stage_dtos = analyze_conversion_funnel(
            db=db,
            start_date_val=start_date_val,
            end_date_val=end_date_val,
            channel_id=sanitized_channel_id,
            campaign_id=sanitized_campaign_id,
            stages=parsed_stages,
            default_threshold=default_threshold,
        )
        response_items = [FunnelStageResponse.from_dto(dto) for dto in stage_dtos]

        logger.info(
            "Conversion funnel analysis generated successfully",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "tenant_id": str(current_user.tenant_id),
                "stage_count": len(response_items),
                "bottleneck_count": sum(1 for item in response_items if item.is_bottleneck),
            },
        )
        return response_items
    except FunnelNotFoundError as exc:
        logger.warning(
            f"Funnel or stage configuration not found: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "funnel_id": sanitized_funnel_id,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except InvalidFunnelDataError as exc:
        logger.warning(
            f"Invalid funnel query parameters provided: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            f"Unexpected error generating conversion funnel analysis: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "error": str(exc),
            },
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while generating the conversion funnel analysis.",
        ) from exc


@router.get(
    "/campaigns/compare",
    response_model=ComparativeCampaignTrendResponse,
    status_code=status.HTTP_200_OK,
    summary="Compare trend metrics for 2 to 5 campaigns",
    description=(
        "Overlay time-series trend comparison for 2 to 5 campaigns simultaneously "
        "across custom date ranges displaying acquisition volume, cost per lead (CPL), "
        "and engagement rate [FR-006, NFR-003, NFR-004]."
    ),
)
def compare_campaign_trends(
    campaign_ids: list[str] | None = Query(
        default=None,
        description="CSV string or repeated query parameter for 2 to 5 campaign UUIDs or campaign names.",
    ),
    start_date_val: datetime | None = Query(
        default=None,
        description="Filter trend metrics starting from this UTC timestamp (ISO 8601).",
    ),
    end_date_val: datetime | None = Query(
        default=None,
        description="Filter trend metrics up to this UTC timestamp (ISO 8601).",
    ),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> ComparativeCampaignTrendResponse:
    """Compare performance trends across 2 to 5 campaigns with time-series metrics [FR-006, NFR-003, NFR-004]."""
    raw_campaign_ids: list[str] = []
    if campaign_ids is not None:
        for item in campaign_ids:
            if "," in item:
                raw_campaign_ids.extend(item.split(","))
            else:
                raw_campaign_ids.append(item)

    sanitized_campaign_ids = [
        sanitized
        for cid in raw_campaign_ids
        if (sanitized := sanitize_input_string(cid))
    ]

    logger.info(
        "Fetching campaign trend comparison",
        extra={
            "correlation_id": get_correlation_id(),
            "user_id": str(current_user.user_id),
            "tenant_id": str(current_user.tenant_id),
            "campaign_ids": sanitized_campaign_ids,
            "start_date_val": start_date_val.isoformat() if start_date_val else None,
            "end_date_val": end_date_val.isoformat() if end_date_val else None,
        },
    )

    try:
        trend_dto = get_campaign_trend_comparison(
            db=db,
            campaign_ids=sanitized_campaign_ids,
            start_date_val=start_date_val,
            end_date_val=end_date_val,
        )
        response_data = ComparativeCampaignTrendResponse.from_dto(trend_dto)

        logger.info(
            "Campaign trend comparison generated successfully",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "tenant_id": str(current_user.tenant_id),
                "compared_campaign_count": len(response_data.campaigns),
            },
        )
        return response_data
    except CampaignNotFoundError as exc:
        logger.warning(
            f"Campaign not found for comparison: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except InvalidReportingDataError as exc:
        logger.warning(
            f"Invalid campaign comparison query parameters provided: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            f"Unexpected error generating campaign trend comparison: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "error": str(exc),
            },
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while generating the campaign comparison.",
        ) from exc


@router.get(
    "/territory/heatmap",
    response_model=TerritoryHeatMapResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve geographic territory heat map data",
    description=(
        "Render interactive heat map metrics of lead and opportunity density by territory, "
        "enforcing territory authorization scopes and including unassigned drawer breakdown "
        "[FR-007, FR-008, NFR-002, NFR-004]."
    ),
)
def get_territory_heatmap_endpoint(
    lead_status: str | None = Query(
        default=None,
        description="Optional comma-separated lead statuses / stage names to filter by.",
    ),
    min_deal_value: float | None = Query(
        default=None,
        ge=0.0,
        description="Optional minimum deal value threshold.",
    ),
    max_deal_value: float | None = Query(
        default=None,
        ge=0.0,
        description="Optional maximum deal value threshold.",
    ),
    territory_id: str | None = Query(
        default=None,
        description="Optional territory UUID or territory code filter.",
    ),
    start_date_val: datetime | None = Query(
        default=None,
        description="Filter events starting from this UTC timestamp (ISO 8601).",
    ),
    end_date_val: datetime | None = Query(
        default=None,
        description="Filter events up to this UTC timestamp (ISO 8601).",
    ),
    user_authorized_territories: list[str] | None = Query(
        default=None,
        description="Optional comma-separated or repeated list of authorized territory UUIDs or territory codes.",
    ),
    include_unassigned: bool = Query(
        default=True,
        description="Whether to include unassigned drawer data.",
    ),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> TerritoryHeatMapResponse:
    """Retrieve geographic territory heat map metrics and unassigned drawer [FR-007, FR-008, NFR-002, NFR-004]."""
    sanitized_lead_status = sanitize_input_string(lead_status)
    sanitized_territory_id = sanitize_input_string(territory_id)

    parsed_auth_territories: list[str] | None = None
    if user_authorized_territories is not None:
        raw_auth: list[str] = []
        for item in user_authorized_territories:
            if "," in item:
                raw_auth.extend(item.split(","))
            else:
                raw_auth.append(item)
        clean_auth = [
            sanitized
            for t in raw_auth
            if (sanitized := sanitize_input_string(t))
        ]
        if clean_auth:
            parsed_auth_territories = clean_auth

    logger.info(
        "Fetching territory heat map analytics",
        extra={
            "correlation_id": get_correlation_id(),
            "user_id": str(current_user.user_id),
            "tenant_id": str(current_user.tenant_id),
            "lead_status": sanitized_lead_status,
            "min_deal_value": min_deal_value,
            "max_deal_value": max_deal_value,
            "territory_id": sanitized_territory_id,
            "start_date_val": start_date_val.isoformat() if start_date_val else None,
            "end_date_val": end_date_val.isoformat() if end_date_val else None,
            "user_authorized_territories": parsed_auth_territories,
            "include_unassigned": include_unassigned,
        },
    )

    try:
        dto = get_territory_heatmap(
            db=db,
            lead_status=sanitized_lead_status,
            min_deal_value=min_deal_value,
            max_deal_value=max_deal_value,
            territory_id=sanitized_territory_id,
            start_date_val=start_date_val,
            end_date_val=end_date_val,
            user_authorized_territories=parsed_auth_territories,
            include_unassigned=include_unassigned,
        )
        response_data = TerritoryHeatMapResponse.from_dto(dto)

        logger.info(
            "Territory heat map generated successfully",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "tenant_id": str(current_user.tenant_id),
                "territory_count": len(response_data.territories),
                "unassigned_count": response_data.unassigned_drawer.total_unassigned_leads,
            },
        )
        return response_data
    except TerritoryNotFoundError as exc:
        logger.warning(
            f"Territory not found: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "territory_id": sanitized_territory_id,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except TerritoryAccessDeniedError as exc:
        logger.warning(
            f"Territory access denied: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "territory_id": sanitized_territory_id,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except InvalidTerritoryFilterError as exc:
        logger.warning(
            f"Invalid territory filter parameters provided: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            f"Unexpected error generating territory heat map: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "error": str(exc),
            },
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while generating the territory heat map.",
        ) from exc


@router.post(
    "/config/audit",
    response_model=AuditLogResponse,
    status_code=status.HTTP_200_OK,
    summary="Create a reporting configuration audit log entry",
    description=(
        "Generate immutable audit log entry for administrative configuration changes "
        "to reporting definitions and metric rules [FR-011, NFR-003]."
    ),
)
def create_report_audit_log_endpoint(
    payload: AuditLogCreateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> AuditLogResponse:
    """Create a reporting configuration audit log record [FR-011, NFR-003]."""
    logger.info(
        "Creating reporting configuration audit log entry",
        extra={
            "correlation_id": get_correlation_id(),
            "user_id": str(current_user.user_id),
            "tenant_id": str(current_user.tenant_id),
            "action_type": payload.action_type,
        },
    )

    try:
        audit_rec = create_report_audit_log(
            db=db,
            action_type=payload.action_type,
            config_delta=payload.config_delta,
            user_id=current_user.user_id,
        )
        response_data = AuditLogResponse.from_model(audit_rec)

        logger.info(
            "Reporting configuration audit log entry created successfully",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "tenant_id": str(current_user.tenant_id),
                "audit_id": str(response_data.id),
            },
        )
        return response_data
    except InvalidReportingDataError as exc:
        logger.warning(
            f"Invalid audit log payload provided: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            f"Unexpected error creating reporting audit log entry: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "error": str(exc),
            },
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating the reporting audit log entry.",
        ) from exc


@router.delete(
    "/config/{config_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Delete a reporting configuration or threshold definition",
    description=(
        "Delete a reporting configuration definition or metric rule threshold "
        "by configuration UUID or identifier [FR-011, Architecture REST rules]."
    ),
)
def delete_report_configuration_endpoint(
    config_id: str,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> Response:
    """Delete a reporting configuration or threshold definition [FR-011, Architecture REST rules]."""
    sanitized_config_id = sanitize_input_string(config_id)
    if not sanitized_config_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configuration identifier cannot be empty.",
        )

    logger.info(
        "Deleting reporting configuration",
        extra={
            "correlation_id": get_correlation_id(),
            "user_id": str(current_user.user_id),
            "tenant_id": str(current_user.tenant_id),
            "config_id": sanitized_config_id,
        },
    )

    try:
        delete_report_configuration(db=db, config_id=sanitized_config_id)

        logger.info(
            "Reporting configuration deleted successfully",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "tenant_id": str(current_user.tenant_id),
                "config_id": sanitized_config_id,
            },
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except ConfigurationNotFoundError as exc:
        logger.warning(
            f"Reporting configuration not found: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "config_id": sanitized_config_id,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except InvalidReportingDataError as exc:
        logger.warning(
            f"Invalid configuration parameter: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            f"Unexpected error deleting reporting configuration: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": str(current_user.user_id),
                "error": str(exc),
            },
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while deleting the reporting configuration.",
        ) from exc


class ExportCreateRequest(BaseModel):
    """Pydantic request schema for submitting an async analytics report dataset export task [FR-012, NFR-004]."""

    report_type: str = Field(
        ...,
        description="Report domain type identifier (e.g. 'acquisition', 'funnel', 'campaigns', 'territory')",
    )
    export_format: str = Field(
        ...,
        description="Desired export file format ('csv' or 'pdf')",
    )
    start_date_val: datetime | None = Field(
        None,
        description="Filter report dataset starting from this UTC timestamp (ISO 8601)",
    )
    end_date_val: datetime | None = Field(
        None,
        description="Filter report dataset up to this UTC timestamp (ISO 8601)",
    )
    channel_id: str | None = Field(
        None,
        description="Optional acquisition channel UUID or channel name filter",
    )
    campaign_id: str | None = Field(
        None,
        description="Optional campaign UUID filter",
    )
    campaign_ids: list[str] | None = Field(
        None,
        description="Optional list of campaign UUIDs for campaign comparison export",
    )
    territory_id: str | None = Field(
        None,
        description="Optional territory UUID or code filter",
    )
    lead_status: str | None = Field(
        None,
        description="Optional lead status / stage name filter",
    )
    min_deal_value: float | None = Field(
        None,
        ge=0.0,
        description="Optional minimum deal value filter",
    )
    max_deal_value: float | None = Field(
        None,
        ge=0.0,
        description="Optional maximum deal value filter",
    )
    user_authorized_territories: list[str] | None = Field(
        None,
        description="Optional list of authorized territory UUIDs for territory heatmap export",
    )
    filters: dict[str, Any] | None = Field(
        None,
        description="Optional additional query filters dictionary",
    )
    output_dir: str | None = Field(
        None,
        description="Optional custom export file output directory path",
    )
    custom_filename: str | None = Field(
        None,
        description="Optional custom filename override for the exported report",
    )

    @field_validator(
        "report_type",
        "export_format",
        "channel_id",
        "campaign_id",
        "territory_id",
        "lead_status",
        "output_dir",
        "custom_filename",
        mode="before",
    )
    @classmethod
    def sanitize_export_strings(cls, v: Any) -> str | None:
        """Sanitize string attributes prior to model validation [NFR-004]."""
        if v is None:
            return None
        return sanitize_input_string(str(v))

    @field_validator("report_type")
    @classmethod
    def validate_report_type(cls, v: str | None) -> str:
        """Ensure report_type is not empty after sanitization."""
        if not v or not v.strip():
            raise ValueError("report_type cannot be empty.")
        return v.strip()

    @field_validator("export_format")
    @classmethod
    def validate_export_format(cls, v: str | None) -> str:
        """Ensure export_format is supported ('csv' or 'pdf') [FR-012]."""
        if not v or not v.strip():
            raise ValueError("export_format cannot be empty.")
        fmt = v.lower().strip()
        allowed = [f.lower().strip() for f in getattr(settings, "ALLOWED_EXPORT_FORMATS", ["csv", "pdf"])]
        if fmt not in allowed:
            raise ValueError(f"Unsupported export format '{v}'. Allowed formats: {allowed}")
        return fmt


class ExportTaskResponse(BaseModel):
    """Pydantic response schema representing an asynchronous dataset export task response [FR-012]."""

    task_id: str = Field(..., description="Unique task identifier for tracking async export generation")
    status: str = Field(..., description="Status of the export task (e.g. 'PENDING', 'ACCEPTED', 'COMPLETED', 'FAILED')")
    report_type: str = Field(..., description="Report domain type identifier")
    export_format: str = Field(..., description="Export file format ('csv' or 'pdf')")
    created_at: datetime = Field(..., description="UTC creation timestamp of the export task request")
    file_path: str | None = Field(None, description="Output file path if generated")
    file_name: str | None = Field(None, description="Output file name if generated")
    file_size_bytes: int = Field(0, description="Size of exported file in bytes")
    row_count: int = Field(0, description="Total exported data rows count")
    user_id: UUID | None = Field(None, description="UUID of the requesting user")
    correlation_id: str | None = Field(None, description="Correlation identifier for logging and tracking")
    error_detail: str | None = Field(None, description="Error detail if task generation failed")


@router.post(
    "/exports",
    response_model=ExportTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit an asynchronous dataset export task",
    description=(
        "Submit an asynchronous background task to export analytics, funnel, campaign, "
        "or territory heat map datasets to standard CSV or PDF document formats [FR-012, NFR-004]."
    ),
)
def create_dataset_export_endpoint(
    payload: ExportCreateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> ExportTaskResponse:
    """Submit an asynchronous report export task (CSV/PDF) [FR-012, NFR-004]."""
    corr_id = get_correlation_id()
    task_id = str(uuid4())
    created_at = datetime.now(timezone.utc)

    filters_dict: dict[str, Any] = payload.filters.copy() if payload.filters else {}
    if payload.channel_id and "channel_id" not in filters_dict:
        filters_dict["channel_id"] = payload.channel_id
    if payload.campaign_id and "campaign_id" not in filters_dict:
        filters_dict["campaign_id"] = payload.campaign_id
    if payload.campaign_ids and "campaign_ids" not in filters_dict:
        filters_dict["campaign_ids"] = payload.campaign_ids
    if payload.territory_id and "territory_id" not in filters_dict:
        filters_dict["territory_id"] = payload.territory_id
    if payload.lead_status and "lead_status" not in filters_dict:
        filters_dict["lead_status"] = payload.lead_status
    if payload.min_deal_value is not None and "min_deal_value" not in filters_dict:
        filters_dict["min_deal_value"] = payload.min_deal_value
    if payload.max_deal_value is not None and "max_deal_value" not in filters_dict:
        filters_dict["max_deal_value"] = payload.max_deal_value
    if payload.user_authorized_territories and "user_authorized_territories" not in filters_dict:
        filters_dict["user_authorized_territories"] = payload.user_authorized_territories

    logger.info(
        "Submitting asynchronous report export task",
        extra={
            "correlation_id": corr_id,
            "user_id": str(current_user.user_id),
            "tenant_id": str(current_user.tenant_id),
            "task_id": task_id,
            "report_type": payload.report_type,
            "export_format": payload.export_format,
        },
    )

    try:
        async_result = generate_report_export_task.apply_async(
            kwargs={
                "report_type": payload.report_type,
                "export_format": payload.export_format,
                "start_date_val": payload.start_date_val.isoformat() if payload.start_date_val else None,
                "end_date_val": payload.end_date_val.isoformat() if payload.end_date_val else None,
                "filters": filters_dict,
                "output_dir": payload.output_dir,
                "user_id": str(current_user.user_id),
                "custom_filename": payload.custom_filename,
                "correlation_id": corr_id,
            },
            task_id=task_id,
        )
        task_id_val = (
            async_result.id
            if async_result and getattr(async_result, "id", None)
            else task_id
        )
        response_data = ExportTaskResponse(
            task_id=str(task_id_val),
            status="PENDING",
            report_type=payload.report_type,
            export_format=payload.export_format,
            created_at=created_at,
            file_path=None,
            file_name=None,
            file_size_bytes=0,
            row_count=0,
            user_id=current_user.user_id,
            correlation_id=corr_id,
            error_detail=None,
        )
    except Exception as exc:
        logger.warning(
            f"Celery dispatch unavailable, falling back to synchronous export generation: {exc}",
            extra={
                "correlation_id": corr_id,
                "user_id": str(current_user.user_id),
                "error": str(exc),
            },
        )
        summary = run_export_report_generation(
            db=db,
            report_type=payload.report_type,
            export_format=payload.export_format,
            start_date_val=payload.start_date_val,
            end_date_val=payload.end_date_val,
            filters=filters_dict,
            output_dir=payload.output_dir,
            user_id=str(current_user.user_id),
            task_id=task_id,
            custom_filename=payload.custom_filename,
            correlation_id=corr_id,
        )
        response_data = ExportTaskResponse(
            task_id=summary.get("task_id", task_id),
            status=summary.get("status", "COMPLETED"),
            report_type=payload.report_type,
            export_format=payload.export_format,
            created_at=created_at,
            file_path=summary.get("file_path"),
            file_name=summary.get("file_name"),
            file_size_bytes=summary.get("file_size_bytes", 0),
            row_count=summary.get("row_count", 0),
            user_id=current_user.user_id,
            correlation_id=corr_id,
            error_detail=summary.get("error_detail"),
        )

    logger.info(
        "Report export task submitted successfully",
        extra={
            "correlation_id": corr_id,
            "user_id": str(current_user.user_id),
            "tenant_id": str(current_user.tenant_id),
            "task_id": response_data.task_id,
            "report_type": response_data.report_type,
            "export_format": response_data.export_format,
            "status": response_data.status,
        },
    )
    return response_data


# ---------------------------------------------------------------------------
# Operational Support Analytics Schemas and Endpoints [FR-8, NFR-2, NFR-3, NFR-4]
# ---------------------------------------------------------------------------


class PriorityBreakdown(BaseModel):
    """Pydantic schema representing ticket volume distribution across priorities."""

    P1: int = Field(0, description="Total count of P1 priority tickets")
    P2: int = Field(0, description="Total count of P2 priority tickets")
    P3: int = Field(0, description="Total count of P3 priority tickets")


class SupportAnalyticsResponse(BaseModel):
    """Pydantic response schema representing operational support performance analytics [FR-8]."""

    start_time: str | None = Field(None, description="ISO 8601 start timestamp of the analysis window")
    end_time: str | None = Field(None, description="ISO 8601 end timestamp of the analysis window")
    total_tickets: int = Field(..., description="Total ticket volume within the window")
    open_tickets: int = Field(..., description="Count of currently open tickets")
    pending_tickets: int = Field(..., description="Count of tickets pending agent/client action")
    resolved_tickets: int = Field(..., description="Count of resolved/closed tickets")
    average_first_response_time_seconds: float = Field(..., description="Average time in seconds from creation to first response")
    mean_time_to_resolution_seconds: float = Field(..., description="Mean time to resolution (MTTR) in seconds")
    total_sla_targets: int = Field(..., description="Total number of SLA targets evaluated")
    breached_sla_targets: int = Field(..., description="Number of breached SLA targets")
    sla_breach_rate_percent: float = Field(..., description="Percentage rate of SLA target breaches")
    average_csat_score: float = Field(..., description="Average Customer Satisfaction (CSAT) rating score (1.0 to 5.0)")
    csat_response_count: int = Field(..., description="Total number of CSAT survey responses received")
    priority_breakdown: PriorityBreakdown = Field(..., description="Breakdown of ticket counts by priority")


def get_support_analytics_endpoint(
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    priority: str | None = None,
    client_id: str | None = None,
    db: Session = None,  # type: ignore[assignment]
    current_user: CurrentUser = None,  # type: ignore[assignment]
) -> SupportAnalyticsResponse:
    """Get aggregated operational support performance analytics [FR-8, NFR-2, NFR-3, NFR-4]."""
    corr_id = get_correlation_id()

    clean_priority: str | None = None
    if priority is not None:
        p_str = sanitize_input_string(priority)
        if p_str:
            p_upper = p_str.upper()
            if p_upper not in ("P1", "P2", "P3"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid priority filter '{priority}'. Allowed values: P1, P2, P3",
                )
            clean_priority = p_upper

    clean_client_id = sanitize_input_string(client_id) if client_id else None

    if start_time and end_time and start_time > end_time:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_time cannot be after end_time",
        )

    logger.info(
        "Fetching operational support performance analytics",
        extra={
            "correlation_id": corr_id,
            "user_id": str(current_user.user_id) if current_user else None,
            "tenant_id": str(current_user.tenant_id) if current_user else None,
            "start_time": start_time.isoformat() if start_time else None,
            "end_time": end_time.isoformat() if end_time else None,
            "priority": clean_priority,
            "client_id": clean_client_id,
        },
    )

    ticket_service = TicketService(session=db)
    metrics_data = ticket_service.get_analytics(
        start_time=start_time,
        end_time=end_time,
        priority=clean_priority,
        client_id=clean_client_id,
    )

    return SupportAnalyticsResponse(
        start_time=metrics_data.get("start_time"),
        end_time=metrics_data.get("end_time"),
        total_tickets=metrics_data.get("total_tickets", 0),
        open_tickets=metrics_data.get("open_tickets", 0),
        pending_tickets=metrics_data.get("pending_tickets", 0),
        resolved_tickets=metrics_data.get("resolved_tickets", 0),
        average_first_response_time_seconds=metrics_data.get("average_first_response_time_seconds", 0.0),
        mean_time_to_resolution_seconds=metrics_data.get("mean_time_to_resolution_seconds", 0.0),
        total_sla_targets=metrics_data.get("total_sla_targets", 0),
        breached_sla_targets=metrics_data.get("breached_sla_targets", 0),
        sla_breach_rate_percent=metrics_data.get("sla_breach_rate_percent", 0.0),
        average_csat_score=metrics_data.get("average_csat_score", 0.0),
        csat_response_count=metrics_data.get("csat_response_count", 0),
        priority_breakdown=PriorityBreakdown(
            P1=metrics_data.get("priority_breakdown", {}).get("P1", 0),
            P2=metrics_data.get("priority_breakdown", {}).get("P2", 0),
            P3=metrics_data.get("priority_breakdown", {}).get("P3", 0),
        ),
    )


@support_router.get(
    "/analytics",
    response_model=SupportAnalyticsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get operational support performance metrics",
    description=(
        "Aggregate operational support analytics including average first response time, "
        "mean time to resolution (MTTR), SLA breach rates, CSAT scores, and priority breakdown [FR-8]."
    ),
)
def get_support_analytics_v1(
    start_time: datetime | None = Query(None, description="Window start UTC timestamp"),
    end_time: datetime | None = Query(None, description="Window end UTC timestamp"),
    priority: str | None = Query(None, description="Priority filter ('P1', 'P2', 'P3')"),
    client_id: str | None = Query(None, description="Client identifier filter"),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> SupportAnalyticsResponse:
    """Get operational support metrics endpoint for GET /api/v1/support/analytics [FR-8]."""
    return get_support_analytics_endpoint(
        start_time=start_time,
        end_time=end_time,
        priority=priority,
        client_id=client_id,
        db=db,
        current_user=current_user,
    )


@router.get(
    "/support",
    response_model=SupportAnalyticsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get operational support performance metrics",
    description=(
        "Aggregate operational support analytics including average first response time, "
        "mean time to resolution (MTTR), SLA breach rates, CSAT scores, and priority breakdown [FR-8]."
    ),
)
def get_analytics_support_v1(
    start_time: datetime | None = Query(None, description="Window start UTC timestamp"),
    end_time: datetime | None = Query(None, description="Window end UTC timestamp"),
    priority: str | None = Query(None, description="Priority filter ('P1', 'P2', 'P3')"),
    client_id: str | None = Query(None, description="Client identifier filter"),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> SupportAnalyticsResponse:
    """Get operational support metrics endpoint for GET /api/v1/analytics/support [FR-8]."""
    return get_support_analytics_endpoint(
        start_time=start_time,
        end_time=end_time,
        priority=priority,
        client_id=client_id,
        db=db,
        current_user=current_user,
    )
