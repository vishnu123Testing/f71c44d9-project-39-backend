"""FastAPI router and Pydantic input validation schemas for visual deal pipeline, stage transitions, action items, stakeholders, and AI forecasting endpoints.

Enforces strict boundary input sanitization, Pydantic validation, synchronous database persistence,
and structured context-correlated logging [FR-001, FR-002, FR-003, NFR-003].
"""

from datetime import datetime, timezone
from enum import Enum
import html
import logging
import re
from typing import Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
    status,
)
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from src.api.deps import CurrentUser, get_current_user
from src.lib.logging import get_correlation_id
from src.models.database import get_db
from src.models.deal import Deal
from src.services.action_items.service import (
    ActionItemDomain,
    ActionItemNotFoundError,
    DealNotFoundError as ActionItemDealNotFoundError,
    InvalidActionItemDataError,
    complete_action_item,
    create_action_item,
    delete_action_item,
    get_action_item_by_id,
    get_action_items_for_deal,
)
from src.services.deals.service import (
    DealDomain,
    DealNotFoundError,
    InvalidDealDataError,
    create_deal,
    get_deal_by_id,
    get_pipeline_board,
    to_deal_domain,
    update_deal_stage,
)
from src.services.forecasting.service import (
    DealNotFoundError as ForecastDealNotFoundError,
    ForecastDomain,
    ForecastNotFoundError,
    get_forecast_by_deal_id,
    recalculate_forecast,
)
from src.services.stakeholders.service import (
    DealNotFoundError as StakeholderDealNotFoundError,
    InvalidStakeholderDataError,
    StakeholderActivityDomain,
    StakeholderDomain,
    StakeholderMapResultDTO,
    StakeholderNotFoundError,
    add_stakeholder_activity,
    create_stakeholder,
    get_stakeholder_map_for_deal,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["deals"])


def sanitize_input_string(v: str | None) -> str | None:
    """Sanitize and clean raw user string inputs.

    Strips leading/trailing whitespace, strips script/HTML markup tags,
    escapes special characters, and removes unexpected control characters.
    """
    if v is None:
        return None

    cleaned = str(v).strip()
    if not cleaned:
        return cleaned

    # Remove HTML/script tags
    cleaned = re.sub(r"<[^>]*>", "", cleaned)
    # Escape remaining special HTML characters
    cleaned = html.escape(cleaned)
    # Remove control characters except tab, space, newline
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", cleaned)
    return cleaned


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class InfluenceLevelEnum(str, Enum):
    """Allowed stakeholder influence levels."""

    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class ActivityTypeEnum(str, Enum):
    """Allowed stakeholder interaction activity types."""

    EMAIL = "Email"
    MEETING = "Meeting"
    CALL = "Call"
    NOTE = "Note"


# ---------------------------------------------------------------------------
# Pydantic Schemas - Deal
# ---------------------------------------------------------------------------


class DealCreateRequest(BaseModel):
    """Request payload schema for creating a new deal."""

    title: str = Field(..., min_length=1, max_length=255, description="Deal title")
    deal_value: float = Field(..., ge=0.0, description="Numerical deal value")
    milestone_stage: str = Field(
        ..., min_length=1, max_length=100, description="Milestone stage name"
    )
    win_probability: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Optional win probability override (0.0 to 1.0)",
    )

    @field_validator("title", "milestone_stage", mode="before")
    @classmethod
    def sanitize_strings(cls, v: Any) -> str:
        """Sanitize string fields."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""


class DealStageUpdateRequest(BaseModel):
    """Request payload schema for updating deal milestone stage transition."""

    milestone_stage: str = Field(
        ..., min_length=1, max_length=100, description="Target milestone stage name"
    )
    win_probability: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Optional win probability override (0.0 to 1.0)",
    )

    @field_validator("milestone_stage", mode="before")
    @classmethod
    def sanitize_stage(cls, v: Any) -> str:
        """Sanitize stage string."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""


class DealResponse(BaseModel):
    """Schema representing deal details in response payloads."""

    id: UUID = Field(..., description="Unique deal UUID")
    title: str = Field(..., description="Deal title")
    deal_value: float = Field(..., description="Numerical deal value")
    milestone_stage: str = Field(..., description="Milestone stage name")
    win_probability: float = Field(
        ..., description="Calculated or assigned win probability (0.0 - 1.0)"
    )
    age_in_days: float = Field(..., description="Calculated deal age in days")
    created_on: datetime = Field(..., description="Deal creation UTC timestamp")
    updated_on: datetime = Field(..., description="Last updated UTC timestamp")

    @classmethod
    def from_domain(cls, domain: DealDomain) -> "DealResponse":
        """Construct DealResponse model from DealDomain entity."""
        return cls(
            id=domain.id,
            title=domain.title,
            deal_value=domain.deal_value,
            milestone_stage=domain.milestone_stage,
            win_probability=domain.win_probability,
            age_in_days=domain.age_in_days,
            created_on=domain.created_on,
            updated_on=domain.updated_on,
        )

    @classmethod
    def from_orm_entity(cls, deal: Deal) -> "DealResponse":
        """Construct DealResponse model from SQLAlchemy Deal ORM entity."""
        domain = to_deal_domain(deal)
        return cls.from_domain(domain)


class PipelineStageGroupResponse(BaseModel):
    """Schema representing a single milestone stage column on the pipeline board."""

    milestone_stage: str = Field(..., description="Milestone stage name")
    total_value: float = Field(
        ..., description="Aggregated value of all deals in this stage"
    )
    deal_count: int = Field(..., description="Total deal count in this stage")
    deals: list[DealResponse] = Field(
        default_factory=list, description="Deal cards in stage column"
    )


class PipelineBoardResponse(BaseModel):
    """Schema representing complete visual pipeline board dataset."""

    stages: list[PipelineStageGroupResponse] = Field(
        default_factory=list, description="Milestone stage column groups"
    )
    total_pipeline_value: float = Field(
        ..., description="Aggregated value across all stages"
    )
    total_deal_count: int = Field(
        ..., description="Total deal count across all stages"
    )
    deals: list[DealResponse] = Field(
        default_factory=list, description="Flattened deal list across pipeline"
    )


# ---------------------------------------------------------------------------
# Pydantic Schemas - Action Items
# ---------------------------------------------------------------------------


class ActionItemCreateRequest(BaseModel):
    """Request payload schema for creating an action item."""

    title: str = Field(
        ..., min_length=1, max_length=255, description="Action item title"
    )
    assigned_owner_id: str = Field(
        ..., min_length=1, max_length=255, description="Assigned owner identifier"
    )
    target_due_date: datetime = Field(
        ..., description="Target due date timestamp in UTC"
    )
    sla_threshold_hours: int | None = Field(
        default=24,
        ge=1,
        le=8760,
        description="SLA breach threshold duration in hours (default 24)",
    )

    @field_validator("title", "assigned_owner_id", mode="before")
    @classmethod
    def sanitize_strings(cls, v: Any) -> str:
        """Sanitize title and owner ID input strings."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""


class ActionItemResponse(BaseModel):
    """Schema representing action item details in response payloads."""

    id: UUID = Field(..., description="Unique action item UUID")
    deal_id: UUID = Field(..., description="Associated deal UUID")
    title: str = Field(..., description="Action item title")
    assigned_owner_id: str = Field(..., description="Assigned owner identifier")
    target_due_date: datetime = Field(..., description="Target due date UTC timestamp")
    sla_threshold_hours: int = Field(..., description="SLA breach threshold hours")
    is_completed: bool = Field(..., description="Boolean completion status flag")
    completed_on: datetime | None = Field(
        default=None, description="Completion UTC timestamp if finished"
    )
    is_escalated: bool = Field(..., description="Boolean SLA escalation status flag")
    escalated_on: datetime | None = Field(
        default=None, description="Escalation UTC timestamp if triggered"
    )
    created_on: datetime = Field(..., description="Creation UTC timestamp")
    is_overdue: bool = Field(
        ..., description="Calculated boolean flag indicating whether item is overdue"
    )

    @classmethod
    def from_domain(cls, domain: ActionItemDomain) -> "ActionItemResponse":
        """Construct ActionItemResponse model from ActionItemDomain entity."""
        return cls(
            id=domain.id,
            deal_id=domain.deal_id,
            title=domain.title,
            assigned_owner_id=domain.assigned_owner_id,
            target_due_date=domain.target_due_date,
            sla_threshold_hours=domain.sla_threshold_hours,
            is_completed=domain.is_completed,
            completed_on=domain.completed_on,
            is_escalated=domain.is_escalated,
            escalated_on=domain.escalated_on,
            created_on=domain.created_on,
            is_overdue=domain.is_overdue,
        )


# ---------------------------------------------------------------------------
# Pydantic Schemas - Stakeholders
# ---------------------------------------------------------------------------


class StakeholderCreateRequest(BaseModel):
    """Request payload schema for mapping a stakeholder contact to a deal."""

    full_name: str = Field(
        ..., min_length=1, max_length=255, description="Full name"
    )
    email: str = Field(
        ..., min_length=3, max_length=255, description="Contact email address"
    )
    org_role: str = Field(
        ..., min_length=1, max_length=255, description="Organizational role/title"
    )
    influence_level: InfluenceLevelEnum = Field(
        default=InfluenceLevelEnum.MEDIUM,
        description="Influence level ('High', 'Medium', 'Low')",
    )
    is_active: bool = Field(
        default=True, description="Active status flag (default True)"
    )

    @field_validator("full_name", "email", "org_role", mode="before")
    @classmethod
    def sanitize_strings(cls, v: Any) -> str:
        """Sanitize stakeholder input strings."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""

    @field_validator("influence_level", mode="before")
    @classmethod
    def normalize_influence(cls, v: Any) -> Any:
        """Normalize influence level string before Enum validation."""
        if isinstance(v, str) and v.strip():
            return v.strip().capitalize()
        return v


class StakeholderResponse(BaseModel):
    """Schema representing stakeholder contact details in response payloads."""

    id: UUID = Field(..., description="Unique stakeholder UUID")
    deal_id: UUID = Field(..., description="Associated deal UUID")
    full_name: str = Field(..., description="Full name")
    email: str = Field(..., description="Contact email address")
    org_role: str = Field(..., description="Organizational role/title")
    influence_level: str = Field(..., description="Influence level rating")
    is_active: bool = Field(..., description="Active status flag")
    created_on: datetime = Field(..., description="Creation UTC timestamp")

    @classmethod
    def from_domain(cls, domain: StakeholderDomain) -> "StakeholderResponse":
        """Construct StakeholderResponse model from StakeholderDomain entity."""
        return cls(
            id=domain.id,
            deal_id=domain.deal_id,
            full_name=domain.full_name,
            email=domain.email,
            org_role=domain.org_role,
            influence_level=domain.influence_level,
            is_active=domain.is_active,
            created_on=domain.created_on,
        )


class ActivityCreateRequest(BaseModel):
    """Request payload schema for logging a stakeholder CRM interaction activity."""

    stakeholder_id: UUID = Field(..., description="Target stakeholder UUID")
    activity_type: ActivityTypeEnum = Field(
        default=ActivityTypeEnum.NOTE,
        description="Activity type ('Email', 'Meeting', 'Call', 'Note')",
    )
    summary: str = Field(..., min_length=1, description="Summary content of activity")
    occurred_on: datetime | None = Field(
        default=None, description="Occurrence timestamp (defaults to current UTC)"
    )

    @field_validator("summary", mode="before")
    @classmethod
    def sanitize_summary(cls, v: Any) -> str:
        """Sanitize activity summary text."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""

    @field_validator("activity_type", mode="before")
    @classmethod
    def normalize_activity_type(cls, v: Any) -> Any:
        """Normalize activity type string before Enum validation."""
        if isinstance(v, str) and v.strip():
            return v.strip().capitalize()
        return v


class StakeholderActivityResponse(BaseModel):
    """Schema representing a stakeholder CRM interaction activity log entry."""

    id: UUID = Field(..., description="Unique activity UUID")
    stakeholder_id: UUID = Field(..., description="Target stakeholder UUID")
    deal_id: UUID = Field(..., description="Associated deal UUID")
    activity_type: str = Field(..., description="Activity type")
    summary: str = Field(..., description="Activity summary text")
    occurred_on: datetime = Field(..., description="Occurrence UTC timestamp")
    created_on: datetime = Field(..., description="Creation UTC timestamp")
    stakeholder_name: str | None = Field(
        default=None, description="Name of associated stakeholder contact if available"
    )

    @classmethod
    def from_domain(cls, domain: StakeholderActivityDomain) -> "StakeholderActivityResponse":
        """Construct StakeholderActivityResponse model from StakeholderActivityDomain entity."""
        return cls(
            id=domain.id,
            stakeholder_id=domain.stakeholder_id,
            deal_id=domain.deal_id,
            activity_type=domain.activity_type,
            summary=domain.summary,
            occurred_on=domain.occurred_on,
            created_on=domain.created_on,
            stakeholder_name=domain.stakeholder_name,
        )


class StakeholderMapResponse(BaseModel):
    """Schema representing stakeholder map and chronological activity feed for a deal."""

    deal_id: UUID = Field(..., description="Target deal UUID")
    stakeholders: list[StakeholderResponse] = Field(
        default_factory=list, description="Mapped stakeholder contacts"
    )
    recent_activities: list[StakeholderActivityResponse] = Field(
        default_factory=list,
        description="Chronological feed of recent CRM interaction activities",
    )
    total_stakeholders: int = Field(..., description="Total count of mapped stakeholders")
    total_activities: int = Field(..., description="Total count of logged activities")

    @classmethod
    def from_dto(cls, dto: StakeholderMapResultDTO) -> "StakeholderMapResponse":
        """Construct StakeholderMapResponse model from StakeholderMapResultDTO."""
        return cls(
            deal_id=dto.deal_id,
            stakeholders=[StakeholderResponse.from_domain(s) for s in dto.stakeholders],
            recent_activities=[
                StakeholderActivityResponse.from_domain(a) for a in dto.recent_activities
            ],
            total_stakeholders=dto.total_stakeholders,
            total_activities=dto.total_activities,
        )


# ---------------------------------------------------------------------------
# Pydantic Schemas - AI Forecast
# ---------------------------------------------------------------------------


class ForecastResponse(BaseModel):
    """Schema representing AI completion timeline forecast details in response payloads."""

    id: UUID | None = Field(default=None, description="Forecast record UUID")
    deal_id: UUID = Field(..., description="Target deal UUID")
    estimated_completion_date: datetime | None = Field(
        default=None, description="Estimated completion date timestamp in UTC"
    )
    confidence_score_pct: float | None = Field(
        default=None, description="Confidence score percentage (0.0 to 100.0)"
    )
    status_code: str = Field(
        ...,
        description="Forecast calculation status ('READY', 'INSUFFICIENT_DATA', 'COMPUTING', 'FAILED')",
    )
    calculated_on: datetime = Field(..., description="Calculation timestamp in UTC")
    error_detail: str | None = Field(
        default=None, description="Error detail message if calculation failed"
    )
    is_sparse: bool = Field(
        default=False, description="Flag indicating if CRM data was sparse or incomplete"
    )

    @classmethod
    def from_domain(cls, domain: ForecastDomain) -> "ForecastResponse":
        """Construct ForecastResponse model from ForecastDomain entity."""
        return cls(
            id=domain.id,
            deal_id=domain.deal_id,
            estimated_completion_date=domain.estimated_completion_date,
            confidence_score_pct=domain.confidence_score_pct,
            status_code=domain.status_code,
            calculated_on=domain.calculated_on,
            error_detail=domain.error_detail,
            is_sparse=domain.is_sparse,
        )


# ---------------------------------------------------------------------------
# Route Handlers
# ---------------------------------------------------------------------------


@router.get(
    "/deals/pipeline",
    response_model=PipelineBoardResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve visual deal pipeline board dataset",
    description=(
        "Retrieve interactive visual deal pipeline board displaying deals grouped by "
        "color-coded milestone stages, deal age in days, value, and probability score (FR-001, FR-002)."
    ),
)
def get_deal_pipeline_board_endpoint(
    stage: str | None = Query(
        default=None, description="Optional milestone stage filter name"
    ),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> PipelineBoardResponse:
    """Retrieve visual deal pipeline board grouped by stage columns."""
    try:
        board_dto = get_pipeline_board(db, stage_filter=stage)
        stages_resp: list[PipelineStageGroupResponse] = []
        all_deals_resp: list[DealResponse] = []

        for s in board_dto.stages:
            stage_deals = [DealResponse.from_domain(d) for d in s.deals]
            all_deals_resp.extend(stage_deals)
            stages_resp.append(
                PipelineStageGroupResponse(
                    milestone_stage=s.milestone_stage,
                    total_value=s.total_value,
                    deal_count=s.deal_count,
                    deals=stage_deals,
                )
            )

        return PipelineBoardResponse(
            stages=stages_resp,
            total_pipeline_value=board_dto.total_pipeline_value,
            total_deal_count=board_dto.total_deal_count,
            deals=all_deals_resp,
        )
    except Exception as exc:
        logger.error(
            f"Failed to retrieve deal pipeline board: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while retrieving the deal pipeline board.",
        ) from exc


@router.post(
    "/deals",
    response_model=DealResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new deal record",
    description="Create a new deal record and dispatch initial AI forecast calculation event.",
)
def create_deal_endpoint(
    payload: DealCreateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> DealResponse:
    """Create a new deal record."""
    try:
        deal = create_deal(
            session=db,
            title=payload.title,
            deal_value=payload.deal_value,
            milestone_stage=payload.milestone_stage,
            win_probability=payload.win_probability,
        )
        return DealResponse.from_orm_entity(deal)
    except (InvalidDealDataError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            f"Failed to create deal record: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating the deal record.",
        ) from exc


@router.get(
    "/deals/{deal_id}",
    response_model=DealResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve deal details by unique deal ID",
    description="Retrieve details, age in days, and win probability for a specific deal record.",
)
def get_deal_endpoint(
    deal_id: UUID,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> DealResponse:
    """Retrieve deal details by deal ID."""
    deal = get_deal_by_id(db, deal_id)
    if not deal:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deal with ID '{deal_id}' not found.",
        )
    return DealResponse.from_orm_entity(deal)


@router.patch(
    "/deals/{deal_id}/stage",
    response_model=DealResponse,
    status_code=status.HTTP_200_OK,
    summary="Update deal milestone stage progression",
    description=(
        "Update deal milestone stage progression directly from visual pipeline interface (FR-003). "
        "Recalculates win probability and dispatches asynchronous AI forecast recalculation event."
    ),
)
def update_deal_stage_endpoint(
    deal_id: UUID,
    payload: DealStageUpdateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> DealResponse:
    """Transition deal milestone stage progression."""
    try:
        deal = update_deal_stage(
            session=db,
            deal_id=deal_id,
            new_stage=payload.milestone_stage,
            win_probability=payload.win_probability,
        )
        return DealResponse.from_orm_entity(deal)
    except DealNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (InvalidDealDataError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            f"Failed to update deal stage transition for {deal_id}: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating the deal stage transition.",
        ) from exc


@router.post(
    "/deals/{deal_id}/action-items",
    response_model=ActionItemResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an action item assigned to a deal",
    description="Create an action item assigned to a deal with owner assignment and SLA configuration.",
)
def create_action_item_endpoint(
    deal_id: UUID,
    payload: ActionItemCreateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> ActionItemResponse:
    """Create an action item assigned to a deal."""
    try:
        domain = create_action_item(
            session=db,
            deal_id=deal_id,
            title=payload.title,
            assigned_owner_id=payload.assigned_owner_id,
            target_due_date=payload.target_due_date,
            sla_threshold_hours=payload.sla_threshold_hours,
        )
        return ActionItemResponse.from_domain(domain)
    except ActionItemDealNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (InvalidActionItemDataError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            f"Failed to create action item for deal {deal_id}: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating the action item.",
        ) from exc


@router.get(
    "/deals/{deal_id}/action-items",
    response_model=list[ActionItemResponse],
    status_code=status.HTTP_200_OK,
    summary="Retrieve action items for a deal",
    description="Retrieve list of action items assigned to a deal with optional inclusion of completed items.",
)
def get_action_items_endpoint(
    deal_id: UUID,
    include_completed: bool = Query(
        default=True, description="Whether to include completed action items"
    ),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> list[ActionItemResponse]:
    """Retrieve action items assigned to a deal."""
    deal = get_deal_by_id(db, deal_id)
    if not deal:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deal with ID '{deal_id}' not found.",
        )
    domains = get_action_items_for_deal(
        db, deal_id=deal_id, include_completed=include_completed
    )
    return [ActionItemResponse.from_domain(d) for d in domains]


@router.patch(
    "/deals/{deal_id}/action-items/{item_id}/complete",
    response_model=ActionItemResponse,
    status_code=status.HTTP_200_OK,
    summary="Mark an action item as completed",
    description="Mark an action item as completed and clear its SLA escalation state.",
)
def complete_action_item_endpoint(
    deal_id: UUID,
    item_id: UUID,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> ActionItemResponse:
    """Mark an action item as completed."""
    try:
        deal = get_deal_by_id(db, deal_id)
        if not deal:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Deal with ID '{deal_id}' not found.",
            )
        domain = complete_action_item(session=db, item_id=item_id)
        if domain.deal_id != deal_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Action item '{item_id}' does not belong to deal '{deal_id}'.",
            )
        return ActionItemResponse.from_domain(domain)
    except ActionItemNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (InvalidActionItemDataError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            f"Failed to complete action item {item_id}: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while completing the action item.",
        ) from exc


@router.delete(
    "/deals/{deal_id}/action-items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Delete an action item",
    description="Permanently delete an action item record. Returns HTTP 204 No Content with empty body.",
)
def delete_action_item_endpoint(
    deal_id: UUID,
    item_id: UUID,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> Response:
    """Delete an action item record."""
    try:
        deal = get_deal_by_id(db, deal_id)
        if not deal:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Deal with ID '{deal_id}' not found.",
            )
        item_domain = get_action_item_by_id(db, item_id)
        if item_domain.deal_id != deal_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Action item '{item_id}' does not belong to deal '{deal_id}'.",
            )
        delete_action_item(db, item_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except ActionItemNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (InvalidActionItemDataError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            f"Failed to delete action item {item_id}: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while deleting the action item.",
        ) from exc


@router.get(
    "/deals/{deal_id}/stakeholders",
    response_model=StakeholderMapResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve stakeholder contacts and activity feed for a deal",
    description="Retrieve mapped stakeholder contacts and chronological CRM interaction activity feed for a deal.",
)
def get_stakeholders_endpoint(
    deal_id: UUID,
    active_only: bool = Query(
        default=False, description="Whether to include active contacts only"
    ),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> StakeholderMapResponse:
    """Retrieve stakeholder map and activity feed for a deal."""
    try:
        map_dto = get_stakeholder_map_for_deal(
            db, deal_id=deal_id, active_only=active_only
        )
        return StakeholderMapResponse.from_dto(map_dto)
    except StakeholderDealNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            f"Failed to retrieve stakeholder map for deal {deal_id}: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while retrieving the stakeholder map.",
        ) from exc


@router.post(
    "/deals/{deal_id}/stakeholders",
    response_model=StakeholderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Map a new stakeholder contact to a deal",
    description="Map a new stakeholder contact with role and influence level to a deal.",
)
def create_stakeholder_endpoint(
    deal_id: UUID,
    payload: StakeholderCreateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> StakeholderResponse:
    """Map a new stakeholder contact to a deal."""
    try:
        domain = create_stakeholder(
            session=db,
            deal_id=deal_id,
            full_name=payload.full_name,
            email=payload.email,
            org_role=payload.org_role,
            influence_level=payload.influence_level.value,
            is_active=payload.is_active,
        )
        return StakeholderResponse.from_domain(domain)
    except StakeholderDealNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (InvalidStakeholderDataError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            f"Failed to create stakeholder contact for deal {deal_id}: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating the stakeholder contact.",
        ) from exc


@router.post(
    "/deals/{deal_id}/stakeholders/activities",
    response_model=StakeholderActivityResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Log a stakeholder CRM interaction activity",
    description="Log a new CRM interaction activity (Email, Meeting, Call, Note) for a mapped stakeholder.",
)
def add_stakeholder_activity_endpoint(
    deal_id: UUID,
    payload: ActivityCreateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> StakeholderActivityResponse:
    """Log a stakeholder CRM interaction activity."""
    try:
        domain = add_stakeholder_activity(
            session=db,
            deal_id=deal_id,
            stakeholder_id=payload.stakeholder_id,
            activity_type=payload.activity_type.value,
            summary=payload.summary,
            occurred_on=payload.occurred_on,
        )
        return StakeholderActivityResponse.from_domain(domain)
    except (StakeholderDealNotFoundError, StakeholderNotFoundError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (InvalidStakeholderDataError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            f"Failed to log stakeholder activity for deal {deal_id}: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while logging the stakeholder activity.",
        ) from exc


@router.get(
    "/deals/{deal_id}/forecast",
    response_model=ForecastResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve AI completion timeline forecast for a deal",
    description=(
        "Retrieve AI estimated completion date and confidence score for a deal (FR-008, FR-009). "
        "Returns status_code INSUFFICIENT_DATA with null date and confidence when CRM history is sparse."
    ),
)
def get_forecast_endpoint(
    deal_id: UUID,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> ForecastResponse:
    """Retrieve AI completion timeline forecast for a deal."""
    try:
        domain = get_forecast_by_deal_id(
            deal_id=deal_id, db_session=db, auto_calculate=True
        )
        return ForecastResponse.from_domain(domain)
    except (ForecastDealNotFoundError, ForecastNotFoundError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            f"Failed to retrieve AI forecast for deal {deal_id}: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while retrieving the deal forecast.",
        ) from exc


@router.post(
    "/deals/{deal_id}/forecast/recalculate",
    response_model=ForecastResponse,
    status_code=status.HTTP_200_OK,
    summary="Recalculate AI completion timeline forecast for a deal",
    description=(
        "Trigger an explicit recalculation of AI estimated completion date and confidence score for a deal (FR-008, FR-009, FR-010). "
        "Returns status_code INSUFFICIENT_DATA with null date and confidence when CRM history is sparse."
    ),
)
def recalculate_forecast_endpoint(
    deal_id: UUID,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> ForecastResponse:
    """Recalculate AI completion timeline forecast for a deal."""
    try:
        domain = recalculate_forecast(
            deal_id=deal_id,
            db_session=db,
            trigger_source="api_recalculate",
        )
        return ForecastResponse.from_domain(domain)
    except (ForecastDealNotFoundError, ForecastNotFoundError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            f"Failed to recalculate AI forecast for deal {deal_id}: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while recalculating the deal forecast.",
        ) from exc


@router.post(
    "/deals/{deal_id}/forecast",
    response_model=ForecastResponse,
    status_code=status.HTTP_200_OK,
    summary="Request or recalculate AI completion timeline forecast for a deal",
    description=(
        "Trigger AI forecast calculation or recalculation for a deal (FR-008, FR-009). "
        "Returns status_code INSUFFICIENT_DATA with null date and confidence when CRM history is sparse."
    ),
)
def post_forecast_endpoint(
    deal_id: UUID,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> ForecastResponse:
    """Request or recalculate AI completion timeline forecast for a deal."""
    return recalculate_forecast_endpoint(deal_id=deal_id, db=db, current_user=current_user)
