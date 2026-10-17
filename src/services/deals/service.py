"""Deal Service implementation for visual deal pipeline management.

This module encapsulates business logic for querying deals grouped by stage columns,
updating milestone stage progression, calculating deal age in days and win probability scores,
and dispatching asynchronous AI forecast recalculation events (FR-001, FR-002, FR-003, NFR-001).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Any, Callable
import uuid
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.lib.logging import get_correlation_id
from src.models.deal import Deal

logger = logging.getLogger(__name__)


class DealNotFoundError(ValueError):
    """Raised when a requested deal record is not found in the database."""

    pass


class InvalidDealDataError(ValueError):
    """Raised when deal input parameters fail domain validation rules."""

    pass


STAGE_WIN_PROBABILITIES: dict[str, float] = {
    "qualification": 0.20,
    "needs analysis": 0.35,
    "discovery": 0.35,
    "proposal": 0.60,
    "negotiation": 0.80,
    "closed won": 1.00,
    "closed lost": 0.00,
}

DEFAULT_MILESTONE_STAGES: list[str] = [
    "Qualification",
    "Needs Analysis",
    "Proposal",
    "Negotiation",
    "Closed Won",
    "Closed Lost",
]


@dataclass
class DealDomain:
    """Pure domain entity representation of a Deal with calculated age and probability."""

    id: UUID
    title: str
    deal_value: float
    milestone_stage: str
    win_probability: float
    age_in_days: float
    created_on: datetime
    updated_on: datetime


@dataclass
class PipelineStageGroupDTO:
    """Grouped summary of deals for a single milestone stage column."""

    milestone_stage: str
    total_value: float
    deal_count: int
    deals: list[DealDomain] = field(default_factory=list)


@dataclass
class PipelineBoardResultDTO:
    """Consolidated pipeline board dataset grouped across milestone stage columns."""

    stages: list[PipelineStageGroupDTO] = field(default_factory=list)
    total_pipeline_value: float = 0.0
    total_deal_count: int = 0


def get_default_win_probability(stage_name: str) -> float:
    """Determine default win probability score for a milestone stage name.

    Args:
        stage_name: String name of the milestone stage.

    Returns:
        Float win probability score between 0.0 and 1.0.
    """
    if not stage_name or not isinstance(stage_name, str):
        return 0.10

    normalized = stage_name.strip().lower()

    if normalized in STAGE_WIN_PROBABILITIES:
        return STAGE_WIN_PROBABILITIES[normalized]

    # Keyword fallback matching for non-standard stage naming variations
    if "won" in normalized:
        return 1.00
    if "lost" in normalized:
        return 0.00
    if "negotiat" in normalized:
        return 0.80
    if "propos" in normalized:
        return 0.60
    if "analy" in normalized or "discover" in normalized:
        return 0.35
    if "qualif" in normalized or "prospect" in normalized or "lead" in normalized:
        return 0.20

    return 0.10


def calculate_deal_age_in_days(
    created_on: datetime,
    ref_time: datetime | None = None,
) -> float:
    """Calculate the age of a deal in days relative to a reference time or current UTC.

    Args:
        created_on: Timestamp when the deal was created.
        ref_time: Optional reference timestamp (defaults to current UTC time).

    Returns:
        Float number of days (non-negative, rounded to 1 decimal place).
    """
    if ref_time is None:
        ref_time = datetime.now(timezone.utc)

    if created_on.tzinfo is None:
        created_on = created_on.replace(tzinfo=timezone.utc)
    if ref_time.tzinfo is None:
        ref_time = ref_time.replace(tzinfo=timezone.utc)

    delta = ref_time - created_on
    age_days = delta.total_seconds() / 86400.0
    return max(0.0, round(age_days, 1))


def to_deal_domain(
    deal: Deal,
    ref_time: datetime | None = None,
) -> DealDomain:
    """Convert a SQLAlchemy Deal ORM entity instance into a pure domain entity DealDomain.

    Args:
        deal: SQLAlchemy Deal model instance.
        ref_time: Optional reference timestamp for age calculation.

    Returns:
        DealDomain instance containing calculated age_in_days.
    """
    age = calculate_deal_age_in_days(deal.created_on, ref_time=ref_time)
    return DealDomain(
        id=deal.id,
        title=deal.title,
        deal_value=float(deal.deal_value),
        milestone_stage=deal.milestone_stage,
        win_probability=float(deal.win_probability),
        age_in_days=age,
        created_on=deal.created_on,
        updated_on=deal.updated_on,
    )


def dispatch_forecast_recalculation(
    deal_id: UUID | str,
    trigger_source: str = "stage_update",
    dispatcher: Callable[[str, str], Any] | None = None,
) -> bool:
    """Dispatch asynchronous AI forecast recalculation event for a deal.

    Args:
        deal_id: Unique identifier of the deal.
        trigger_source: Contextual trigger reason (e.g., 'stage_update', 'value_update').
        dispatcher: Optional custom dispatcher callable taking (deal_id_str, trigger_source).

    Returns:
        Boolean True if dispatched successfully, False otherwise.
    """
    deal_id_str = str(deal_id)
    correlation_id = get_correlation_id()

    logger.info(
        "Dispatching AI forecast recalculation event",
        extra={
            "deal_id": deal_id_str,
            "trigger_source": trigger_source,
            "correlation_id": correlation_id,
        },
    )

    if dispatcher is not None:
        try:
            dispatcher(deal_id_str, trigger_source)
            return True
        except Exception as exc:
            logger.warning(
                "Custom forecast recalculation dispatcher failed",
                extra={
                    "deal_id": deal_id_str,
                    "error": str(exc),
                    "correlation_id": correlation_id,
                },
            )
            return False

    try:
        from src.workers.forecast_worker import recalculate_deal_forecast_task

        recalculate_deal_forecast_task.delay(
            deal_id_str,
            trigger_source=trigger_source,
            correlation_id=correlation_id,
        )
        return True
    except Exception as exc:
        logger.debug(
            "Celery worker forecast recalculation task unavailable or dispatch skipped: %s",
            exc,
            extra={
                "deal_id": deal_id_str,
                "correlation_id": correlation_id,
            },
        )
        return False


def _validate_deal_inputs(
    title: str | None = None,
    deal_value: float | None = None,
    milestone_stage: str | None = None,
    win_probability: float | None = None,
) -> None:
    """Validate deal input fields against domain business rules."""
    if title is not None:
        if not title or not title.strip():
            raise InvalidDealDataError("Deal title cannot be empty")
        if len(title.strip()) > 255:
            raise InvalidDealDataError("Deal title exceeds maximum length of 255 characters")

    if deal_value is not None:
        if deal_value < 0.0:
            raise InvalidDealDataError("Deal value must be a non-negative number")

    if milestone_stage is not None:
        if not milestone_stage or not milestone_stage.strip():
            raise InvalidDealDataError("Milestone stage cannot be empty")
        if len(milestone_stage.strip()) > 100:
            raise InvalidDealDataError("Milestone stage exceeds maximum length of 100 characters")

    if win_probability is not None:
        if not (0.0 <= win_probability <= 1.0):
            raise InvalidDealDataError("Win probability score must be between 0.0 and 1.0")


def get_deal_by_id(session: Session, deal_id: UUID | str) -> Deal | None:
    """Retrieve a Deal ORM entity by its primary key ID.

    Args:
        session: Active synchronous SQLAlchemy session.
        deal_id: UUID or string representation of deal ID.

    Returns:
        Deal instance if found, or None.
    """
    if isinstance(deal_id, str):
        try:
            parsed_id = UUID(deal_id)
        except ValueError:
            return None
    else:
        parsed_id = deal_id

    return session.get(Deal, parsed_id)


def get_deals(
    session: Session,
    stage: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Deal]:
    """Query deals from database with optional milestone stage filtering and pagination.

    Args:
        session: Active synchronous SQLAlchemy session.
        stage: Optional milestone stage filter.
        limit: Maximum records to return.
        offset: Offset pagination count.

    Returns:
        List of matching Deal ORM instances ordered by creation date descending.
    """
    stmt = select(Deal)
    if stage and stage.strip():
        stmt = stmt.where(Deal.milestone_stage == stage.strip())

    stmt = stmt.order_by(Deal.created_on.desc()).offset(offset).limit(limit)
    return list(session.scalars(stmt).all())


def get_pipeline_board(
    session: Session,
    stage_filter: str | None = None,
) -> PipelineBoardResultDTO:
    """Retrieve complete visual pipeline board data grouped by milestone stage columns.

    Args:
        session: Active synchronous SQLAlchemy session.
        stage_filter: Optional specific milestone stage filter.

    Returns:
        PipelineBoardResultDTO containing stage columns, deal cards, total values, and counts.
    """
    deals = get_deals(session, stage=stage_filter, limit=1000)
    ref_time = datetime.now(timezone.utc)

    grouped_deals: dict[str, list[DealDomain]] = {}
    total_pipeline_value = 0.0
    total_deal_count = 0

    for deal in deals:
        domain_deal = to_deal_domain(deal, ref_time=ref_time)
        stage_name = deal.milestone_stage
        if stage_name not in grouped_deals:
            grouped_deals[stage_name] = []
        grouped_deals[stage_name].append(domain_deal)
        total_pipeline_value += domain_deal.deal_value
        total_deal_count += 1

    stage_groups: list[PipelineStageGroupDTO] = []

    if stage_filter and stage_filter.strip():
        stages_to_render = [stage_filter.strip()]
    else:
        known_stages_map = {s.lower(): s for s in DEFAULT_MILESTONE_STAGES}
        stages_to_render = list(DEFAULT_MILESTONE_STAGES)
        for stage_name in grouped_deals.keys():
            if stage_name.lower() not in known_stages_map:
                stages_to_render.append(stage_name)

    for stage_name in stages_to_render:
        stage_deals = grouped_deals.get(stage_name, [])
        if not stage_deals:
            for key, val in grouped_deals.items():
                if key.lower() == stage_name.lower():
                    stage_deals = val
                    break

        stage_total_val = sum(d.deal_value for d in stage_deals)
        stage_groups.append(
            PipelineStageGroupDTO(
                milestone_stage=stage_name,
                total_value=round(stage_total_val, 2),
                deal_count=len(stage_deals),
                deals=stage_deals,
            )
        )

    return PipelineBoardResultDTO(
        stages=stage_groups,
        total_pipeline_value=round(total_pipeline_value, 2),
        total_deal_count=total_deal_count,
    )


def create_deal(
    session: Session,
    title: str,
    deal_value: float,
    milestone_stage: str,
    win_probability: float | None = None,
    dispatch_forecast: bool = True,
    forecast_dispatcher: Callable[[str, str], Any] | None = None,
) -> Deal:
    """Create a new deal in the database and dispatch initial forecast calculation.

    Args:
        session: Active synchronous SQLAlchemy session.
        title: Deal title string.
        deal_value: Numerical deal value.
        milestone_stage: Initial milestone stage string.
        win_probability: Optional win probability override (0.0 - 1.0).
        dispatch_forecast: Whether to trigger forecast recalculation event.
        forecast_dispatcher: Optional custom dispatcher function.

    Returns:
        Created and persisted Deal ORM entity.
    """
    _validate_deal_inputs(
        title=title,
        deal_value=deal_value,
        milestone_stage=milestone_stage,
        win_probability=win_probability,
    )

    clean_title = title.strip()
    clean_stage = milestone_stage.strip()

    if win_probability is None:
        calc_prob = get_default_win_probability(clean_stage)
    else:
        calc_prob = win_probability

    now = datetime.now(timezone.utc)
    deal = Deal(
        title=clean_title,
        deal_value=float(deal_value),
        milestone_stage=clean_stage,
        win_probability=float(calc_prob),
        created_on=now,
        updated_on=now,
    )

    session.add(deal)
    session.commit()
    session.refresh(deal)

    logger.info(
        "Created new deal",
        extra={
            "deal_id": str(deal.id),
            "title": clean_title,
            "milestone_stage": clean_stage,
            "deal_value": deal.deal_value,
            "win_probability": deal.win_probability,
            "correlation_id": get_correlation_id(),
        },
    )

    if dispatch_forecast:
        dispatch_forecast_recalculation(
            deal.id,
            trigger_source="deal_created",
            dispatcher=forecast_dispatcher,
        )

    return deal


def update_deal_stage(
    session: Session,
    deal_id: UUID | str,
    new_stage: str,
    win_probability: float | None = None,
    dispatch_forecast: bool = True,
    forecast_dispatcher: Callable[[str, str], Any] | None = None,
) -> Deal:
    """Update a deal's milestone stage, recalculating default win probability and timestamps.

    Args:
        session: Active synchronous SQLAlchemy session.
        deal_id: Unique deal identifier.
        new_stage: New milestone stage string.
        win_probability: Optional win probability override (0.0 - 1.0).
        dispatch_forecast: Whether to trigger forecast recalculation task.
        forecast_dispatcher: Optional custom dispatcher function.

    Returns:
        Updated Deal ORM entity.

    Raises:
        DealNotFoundError: If deal with given ID does not exist.
        InvalidDealDataError: If inputs fail validation rules.
    """
    _validate_deal_inputs(
        milestone_stage=new_stage,
        win_probability=win_probability,
    )

    deal = get_deal_by_id(session, deal_id)
    if not deal:
        raise DealNotFoundError(f"Deal with ID '{deal_id}' not found")

    clean_stage = new_stage.strip()
    old_stage = deal.milestone_stage

    if win_probability is None:
        calc_prob = get_default_win_probability(clean_stage)
    else:
        calc_prob = win_probability

    now = datetime.now(timezone.utc)
    deal.milestone_stage = clean_stage
    deal.win_probability = float(calc_prob)
    deal.updated_on = now

    session.add(deal)
    session.commit()
    session.refresh(deal)

    logger.info(
        "Updated deal stage transition",
        extra={
            "deal_id": str(deal.id),
            "old_stage": old_stage,
            "new_stage": clean_stage,
            "win_probability": deal.win_probability,
            "correlation_id": get_correlation_id(),
        },
    )

    if dispatch_forecast:
        dispatch_forecast_recalculation(
            deal.id,
            trigger_source="stage_transition",
            dispatcher=forecast_dispatcher,
        )

    return deal


# Alias for stage transition operations
transition_deal_stage = update_deal_stage


def update_deal(
    session: Session,
    deal_id: UUID | str,
    title: str | None = None,
    deal_value: float | None = None,
    milestone_stage: str | None = None,
    win_probability: float | None = None,
    dispatch_forecast: bool = True,
    forecast_dispatcher: Callable[[str, str], Any] | None = None,
) -> Deal:
    """Update general fields of an existing deal record.

    Args:
        session: Active synchronous SQLAlchemy session.
        deal_id: Unique deal identifier.
        title: Optional new deal title.
        deal_value: Optional new deal numerical value.
        milestone_stage: Optional new milestone stage.
        win_probability: Optional new win probability score.
        dispatch_forecast: Whether to trigger forecast recalculation task.
        forecast_dispatcher: Optional custom dispatcher function.

    Returns:
        Updated Deal ORM entity.
    """
    _validate_deal_inputs(
        title=title,
        deal_value=deal_value,
        milestone_stage=milestone_stage,
        win_probability=win_probability,
    )

    deal = get_deal_by_id(session, deal_id)
    if not deal:
        raise DealNotFoundError(f"Deal with ID '{deal_id}' not found")

    stage_changed = False
    value_changed = False

    if title is not None:
        deal.title = title.strip()

    if deal_value is not None:
        if float(deal.deal_value) != float(deal_value):
            value_changed = True
        deal.deal_value = float(deal_value)

    if milestone_stage is not None:
        clean_stage = milestone_stage.strip()
        if deal.milestone_stage != clean_stage:
            stage_changed = True
            deal.milestone_stage = clean_stage
            if win_probability is None:
                deal.win_probability = get_default_win_probability(clean_stage)

    if win_probability is not None:
        deal.win_probability = float(win_probability)

    deal.updated_on = datetime.now(timezone.utc)

    session.add(deal)
    session.commit()
    session.refresh(deal)

    if dispatch_forecast and (stage_changed or value_changed):
        trigger_reason = "stage_update" if stage_changed else "value_update"
        dispatch_forecast_recalculation(
            deal.id,
            trigger_source=trigger_reason,
            dispatcher=forecast_dispatcher,
        )

    return deal


def delete_deal(session: Session, deal_id: UUID | str) -> bool:
    """Delete a deal record from the database.

    Args:
        session: Active synchronous SQLAlchemy session.
        deal_id: Unique deal identifier.

    Returns:
        True if deal was deleted, False if deal was not found.
    """
    deal = get_deal_by_id(session, deal_id)
    if not deal:
        return False

    session.delete(deal)
    session.commit()

    logger.info(
        "Deleted deal record",
        extra={
            "deal_id": str(deal.id),
            "correlation_id": get_correlation_id(),
        },
    )
    return True


class DealService:
    """Encapsulates business operations for visual deal pipeline management."""

    def __init__(
        self,
        session: Session | None = None,
        forecast_dispatcher: Callable[[str, str], Any] | None = None,
    ) -> None:
        """Initialize DealService instance with optional session and forecast dispatcher.

        Args:
            session: Optional default SQLAlchemy session.
            forecast_dispatcher: Optional custom forecast recalculation dispatcher function.
        """
        self.session = session
        self.forecast_dispatcher = forecast_dispatcher

    def _get_session(self, session: Session | None = None) -> Session:
        """Resolve active SQLAlchemy session or raise ValueError if none available."""
        s = session or self.session
        if s is None:
            raise ValueError("SQLAlchemy session must be provided to DealService operation")
        return s

    def get_by_id(
        self,
        deal_id: UUID | str,
        session: Session | None = None,
    ) -> Deal | None:
        """Retrieve a Deal by ID."""
        return get_deal_by_id(self._get_session(session), deal_id)

    def get_deals(
        self,
        stage: str | None = None,
        limit: int = 100,
        offset: int = 0,
        session: Session | None = None,
    ) -> list[Deal]:
        """Query deals with optional stage filtering and pagination."""
        return get_deals(self._get_session(session), stage=stage, limit=limit, offset=offset)

    def get_pipeline_board(
        self,
        stage_filter: str | None = None,
        session: Session | None = None,
    ) -> PipelineBoardResultDTO:
        """Retrieve visual board dataset grouped by stage columns."""
        return get_pipeline_board(self._get_session(session), stage_filter=stage_filter)

    def create_deal(
        self,
        title: str,
        deal_value: float,
        milestone_stage: str,
        win_probability: float | None = None,
        dispatch_forecast: bool = True,
        session: Session | None = None,
    ) -> Deal:
        """Create a new deal and dispatch forecast recalculation."""
        return create_deal(
            session=self._get_session(session),
            title=title,
            deal_value=deal_value,
            milestone_stage=milestone_stage,
            win_probability=win_probability,
            dispatch_forecast=dispatch_forecast,
            forecast_dispatcher=self.forecast_dispatcher,
        )

    def update_stage(
        self,
        deal_id: UUID | str,
        new_stage: str,
        win_probability: float | None = None,
        dispatch_forecast: bool = True,
        session: Session | None = None,
    ) -> Deal:
        """Transition a deal's milestone stage."""
        return update_deal_stage(
            session=self._get_session(session),
            deal_id=deal_id,
            new_stage=new_stage,
            win_probability=win_probability,
            dispatch_forecast=dispatch_forecast,
            forecast_dispatcher=self.forecast_dispatcher,
        )

    def update_deal(
        self,
        deal_id: UUID | str,
        title: str | None = None,
        deal_value: float | None = None,
        milestone_stage: str | None = None,
        win_probability: float | None = None,
        dispatch_forecast: bool = True,
        session: Session | None = None,
    ) -> Deal:
        """Update deal attributes."""
        return update_deal(
            session=self._get_session(session),
            deal_id=deal_id,
            title=title,
            deal_value=deal_value,
            milestone_stage=milestone_stage,
            win_probability=win_probability,
            dispatch_forecast=dispatch_forecast,
            forecast_dispatcher=self.forecast_dispatcher,
        )

    def delete_deal(
        self,
        deal_id: UUID | str,
        session: Session | None = None,
    ) -> bool:
        """Delete a deal."""
        return delete_deal(self._get_session(session), deal_id)
