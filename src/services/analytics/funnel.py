"""Conversion funnel service module for stage progression analysis and bottleneck detection.

Provides real-time conversion funnel visualization data and drop-off rate
bottleneck detection (FR-004, FR-005).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from src.lib.config import get_settings
from src.models.analytics import FunnelThreshold, LeadEvent

logger = logging.getLogger(__name__)

DEFAULT_FUNNEL_STAGES: list[str] = [
    "Lead",
    "MQL",
    "SQL",
    "Opportunity",
    "Customer",
]


class InvalidFunnelDataError(ValueError):
    """Raised when input parameters fail domain validation rules for funnel analysis."""

    pass


class FunnelNotFoundError(ValueError):
    """Raised when a requested funnel configuration or stage definition is not found."""

    pass


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime instance is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_uuid(val: UUID | str | None, param_name: str) -> UUID | None:
    """Parse a UUID or string value into a UUID instance."""
    if val is None:
        return None
    if isinstance(val, UUID):
        return val
    try:
        return UUID(val)
    except ValueError as exc:
        raise InvalidFunnelDataError(
            f"Invalid UUID string format for '{param_name}': {val}"
        ) from exc


@dataclass
class FunnelStageDTO:
    """Data transfer object representing conversion performance for a single funnel stage."""

    stage_name: str
    lead_count: int
    conversion_rate: float
    dropoff_rate: float
    is_bottleneck: bool
    threshold_percent: float
    overall_conversion_rate: float = 0.0

    @property
    def count(self) -> int:
        """Alias property for lead_count to support variable contract variations."""
        return self.lead_count

    def to_dict(self) -> dict[str, Any]:
        """Convert DTO to a dictionary representation for API serialization."""
        return {
            "stage_name": self.stage_name,
            "count": self.lead_count,
            "lead_count": self.lead_count,
            "conversion_rate": self.conversion_rate,
            "dropoff_rate": self.dropoff_rate,
            "is_bottleneck": self.is_bottleneck,
            "threshold_percent": self.threshold_percent,
            "overall_conversion_rate": self.overall_conversion_rate,
        }


def get_funnel_thresholds(db: Session) -> dict[str, float]:
    """Retrieve all configured stage drop-off thresholds from the database."""
    stmt = select(FunnelThreshold)
    threshold_records = db.scalars(stmt).all()
    return {rec.stage_name: float(rec.max_dropoff_percent) for rec in threshold_records}


def set_funnel_threshold(
    db: Session,
    stage_name: str,
    max_dropoff_percent: float,
) -> FunnelThreshold:
    """Set or update the max drop-off threshold percentage for a specific funnel stage."""
    clean_stage = stage_name.strip() if stage_name else ""
    if not clean_stage:
        raise InvalidFunnelDataError("Stage name cannot be empty when configuring a threshold.")

    if max_dropoff_percent < 0.0 or max_dropoff_percent > 100.0:
        raise InvalidFunnelDataError(
            f"Threshold percentage must be between 0.0 and 100.0. Got: {max_dropoff_percent}"
        )

    stmt = select(FunnelThreshold).where(FunnelThreshold.stage_name == clean_stage)
    threshold_rec = db.scalars(stmt).first()

    if threshold_rec is not None:
        threshold_rec.max_dropoff_percent = max_dropoff_percent
    else:
        threshold_rec = FunnelThreshold(
            stage_name=clean_stage,
            max_dropoff_percent=max_dropoff_percent,
        )
        db.add(threshold_rec)

    db.commit()
    db.refresh(threshold_rec)
    logger.info(
        "Configured funnel threshold for stage '%s': %.2f%%",
        clean_stage,
        max_dropoff_percent,
    )
    return threshold_rec


def calculate_stage_dropoff(
    current_count: int,
    previous_count: int,
) -> tuple[float, float]:
    """Calculate conversion rate and drop-off rate percentages from stage lead counts.

    Returns (conversion_rate, dropoff_rate).
    If previous_count <= 0, returns (0.0, 0.0) to prevent division by zero.
    """
    if previous_count <= 0:
        return 0.0, 0.0

    curr_val = max(0, current_count)
    prev_val = previous_count

    raw_cr = (curr_val / prev_val) * 100.0
    conversion_rate = round(min(100.0, raw_cr), 2)

    raw_dropoff = ((prev_val - curr_val) / prev_val) * 100.0
    dropoff_rate = round(max(0.0, raw_dropoff), 2)

    return conversion_rate, dropoff_rate


def detect_bottleneck(
    dropoff_rate: float,
    threshold_percent: float,
) -> bool:
    """Determine whether a stage's drop-off rate exceeds the configured bottleneck threshold."""
    return dropoff_rate >= threshold_percent


def analyze_conversion_funnel(
    db: Session,
    start_date_val: datetime | None = None,
    end_date_val: datetime | None = None,
    channel_id: UUID | str | None = None,
    campaign_id: UUID | str | None = None,
    stages: list[str] | None = None,
    default_threshold: float | None = None,
) -> list[FunnelStageDTO]:
    """Analyze conversion funnel stage progression and detect drop-off rate bottlenecks.

    Computes stage-by-stage lead counts, step conversion rates, drop-off rates,
    and bottleneck flags (FR-004, FR-005).
    """
    start_utc = _ensure_utc(start_date_val)
    end_utc = _ensure_utc(end_date_val)

    if start_utc and end_utc and start_utc > end_utc:
        raise InvalidFunnelDataError(
            f"start_date_val ({start_utc}) cannot be after end_date_val ({end_utc})."
        )

    parsed_channel_id = _parse_uuid(channel_id, "channel_id")
    parsed_campaign_id = _parse_uuid(campaign_id, "campaign_id")

    # Determine threshold fallback
    if default_threshold is not None:
        if default_threshold < 0.0 or default_threshold > 100.0:
            raise InvalidFunnelDataError(
                f"default_threshold must be between 0.0 and 100.0. Got: {default_threshold}"
            )
        fallback_threshold = default_threshold
    else:
        try:
            fallback_threshold = float(get_settings().DEFAULT_FUNNEL_BOTTLENECK_THRESHOLD)
        except Exception:
            fallback_threshold = 50.0

    # Determine stage sequence
    if stages is not None:
        if len(stages) == 0:
            raise InvalidFunnelDataError("Stage list cannot be empty when provided.")
        clean_stages = [s.strip() for s in stages if s and s.strip()]
        if not clean_stages:
            raise InvalidFunnelDataError("Provided stage list contains only blank names.")
        stage_sequence = clean_stages
    else:
        stage_sequence = list(DEFAULT_FUNNEL_STAGES)
        # Query distinct stage_names from LeadEvent to capture custom database stage names
        stmt_distinct = select(distinct(LeadEvent.stage_name))
        if start_utc:
            stmt_distinct = stmt_distinct.where(LeadEvent.event_timestamp >= start_utc)
        if end_utc:
            stmt_distinct = stmt_distinct.where(LeadEvent.event_timestamp <= end_utc)
        if parsed_channel_id:
            stmt_distinct = stmt_distinct.where(LeadEvent.channel_id == parsed_channel_id)
        if parsed_campaign_id:
            stmt_distinct = stmt_distinct.where(LeadEvent.campaign_id == parsed_campaign_id)

        db_stages = db.scalars(stmt_distinct).all()
        for s_name in db_stages:
            if s_name and s_name not in stage_sequence:
                stage_sequence.append(s_name)

    # Fetch configured stage thresholds
    threshold_map = get_funnel_thresholds(db)

    # Aggregate lead counts grouped by stage_name
    count_stmt = select(
        LeadEvent.stage_name,
        func.count(distinct(LeadEvent.lead_id)).label("lead_count"),
    ).where(LeadEvent.stage_name.in_(stage_sequence))

    if start_utc:
        count_stmt = count_stmt.where(LeadEvent.event_timestamp >= start_utc)
    if end_utc:
        count_stmt = count_stmt.where(LeadEvent.event_timestamp <= end_utc)
    if parsed_channel_id:
        count_stmt = count_stmt.where(LeadEvent.channel_id == parsed_channel_id)
    if parsed_campaign_id:
        count_stmt = count_stmt.where(LeadEvent.campaign_id == parsed_campaign_id)

    count_stmt = count_stmt.group_by(LeadEvent.stage_name)
    rows = db.execute(count_stmt).all()
    stage_counts: dict[str, int] = {row.stage_name: int(row.lead_count) for row in rows}

    funnel_results: list[FunnelStageDTO] = []
    top_stage_count = stage_counts.get(stage_sequence[0], 0) if stage_sequence else 0

    for idx, stage in enumerate(stage_sequence):
        curr_count = stage_counts.get(stage, 0)

        # Lookup threshold: exact stage match, transition match ("Prev_to_Curr"), or fallback
        applied_threshold = threshold_map.get(stage)
        if applied_threshold is None and idx > 0:
            transition_key = f"{stage_sequence[idx - 1]}_to_{stage}"
            applied_threshold = threshold_map.get(transition_key)

        if applied_threshold is None:
            applied_threshold = fallback_threshold

        if idx == 0:
            conversion_rate = 100.0 if curr_count > 0 else 0.0
            dropoff_rate = 0.0
            is_bottleneck = False
            overall_cr = 100.0 if curr_count > 0 else 0.0
        else:
            prev_stage = stage_sequence[idx - 1]
            prev_count = stage_counts.get(prev_stage, 0)
            conversion_rate, dropoff_rate = calculate_stage_dropoff(curr_count, prev_count)
            is_bottleneck = (
                detect_bottleneck(dropoff_rate, applied_threshold)
                if prev_count > 0
                else False
            )
            overall_cr = (
                round((curr_count / top_stage_count) * 100.0, 2)
                if top_stage_count > 0
                else 0.0
            )

        funnel_results.append(
            FunnelStageDTO(
                stage_name=stage,
                lead_count=curr_count,
                conversion_rate=conversion_rate,
                dropoff_rate=dropoff_rate,
                is_bottleneck=is_bottleneck,
                threshold_percent=applied_threshold,
                overall_conversion_rate=overall_cr,
            )
        )

    return funnel_results


def get_bottleneck_stages(funnel_stages: list[FunnelStageDTO]) -> list[FunnelStageDTO]:
    """Filter funnel stages to return only those flagged as bottlenecks."""
    return [s for s in funnel_stages if s.is_bottleneck]


def get_funnel_summary(
    db: Session,
    start_date_val: datetime | None = None,
    end_date_val: datetime | None = None,
    channel_id: UUID | str | None = None,
    campaign_id: UUID | str | None = None,
    stages: list[str] | None = None,
    default_threshold: float | None = None,
) -> dict[str, Any]:
    """Generate a comprehensive funnel summary containing stage metrics and bottleneck highlights."""
    stage_dtos = analyze_conversion_funnel(
        db=db,
        start_date_val=start_date_val,
        end_date_val=end_date_val,
        channel_id=channel_id,
        campaign_id=campaign_id,
        stages=stages,
        default_threshold=default_threshold,
    )

    bottlenecks = get_bottleneck_stages(stage_dtos)
    top_count = stage_dtos[0].lead_count if stage_dtos else 0
    converted_count = stage_dtos[-1].lead_count if stage_dtos else 0

    overall_conversion = (
        round((converted_count / top_count) * 100.0, 2) if top_count > 0 else 0.0
    )

    return {
        "stages": [s.to_dict() for s in stage_dtos],
        "total_top_leads": top_count,
        "total_converted_leads": converted_count,
        "overall_conversion_rate": overall_conversion,
        "bottleneck_count": len(bottlenecks),
        "has_bottlenecks": len(bottlenecks) > 0,
        "bottlenecks": [s.to_dict() for s in bottlenecks],
    }
