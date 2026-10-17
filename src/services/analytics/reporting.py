"""Analytics reporting service module for channel acquisition and performance metrics.

Provides channel acquisition lead aggregation, quality score computation,
zero-spend ROI division safety (FR-001, FR-002, FR-003), and comparative
campaign trend calculations for up to 5 simultaneous campaigns (FR-006).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.analytics import (
    AcquisitionChannel,
    Campaign,
    FunnelThreshold,
    LeadEvent,
    ReportAuditLog,
)

logger = logging.getLogger(__name__)


class ChannelNotFoundError(ValueError):
    """Raised when a requested acquisition channel record is not found."""

    pass


class CampaignNotFoundError(ValueError):
    """Raised when a requested campaign record is not found."""

    pass


class ConfigurationNotFoundError(ValueError):
    """Raised when a requested reporting configuration or threshold record is not found."""

    pass


class InvalidReportingDataError(ValueError):
    """Raised when input parameters fail domain validation rules."""

    pass


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime instance is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def calculate_channel_roi(revenue: float | None, spend: float | None) -> str:
    """Calculate ROI percentage string from revenue and spend.

    Returns "N/A" if spend or revenue is zero, None, or negative to avoid
    division-by-zero errors and represent zero-performance channels clearly
    (FR-003).
    """
    if spend is None or revenue is None:
        return "N/A"

    spend_val = float(spend)
    revenue_val = float(revenue)

    if spend_val <= 0.0 or revenue_val <= 0.0:
        return "N/A"

    roi_pct = ((revenue_val - spend_val) / spend_val) * 100.0
    return f"{roi_pct:.2f}%"


def compute_lead_quality_score(scores: list[int | float | None]) -> float:
    """Compute average lead quality score across attributed lead events.

    Returns 0.0 if no valid quality scores are present.
    """
    valid_scores = [float(s) for s in scores if s is not None]
    if not valid_scores:
        return 0.0
    return round(sum(valid_scores) / len(valid_scores), 2)


def calculate_cost_per_lead(budget: float | None, lead_count: int) -> float:
    """Calculate cost per lead (CPL).

    Returns 0.0 if lead_count is zero or budget is None/negative.
    """
    if not budget or budget <= 0.0 or lead_count <= 0:
        return 0.0
    return round(float(budget) / float(lead_count), 2)


def calculate_engagement_rate(events: list[LeadEvent]) -> float:
    """Calculate engagement rate percentage across a collection of lead events.

    Uses average quality_score if available; otherwise calculates percentage of
    leads progressing beyond initial stage. Returns 0.0 for empty events.
    """
    if not events:
        return 0.0

    valid_scores = [float(e.quality_score) for e in events if e.quality_score is not None]
    if valid_scores:
        avg_score = sum(valid_scores) / len(valid_scores)
        return round(avg_score, 2)

    engaged_count = sum(
        1 for e in events if e.stage_name and e.stage_name.upper() not in ("LEAD", "NEW")
    )
    return round((engaged_count / len(events)) * 100.0, 2)


@dataclass
class ChannelAcquisitionReportDTO:
    """Data transfer object representing channel acquisition performance metrics."""

    channel_id: UUID
    channel_name: str
    lead_count: int
    quality_score: float
    spend_amount: float
    revenue_amount: float
    roi: str


@dataclass
class CampaignTrendPointDTO:
    """Time-series data point for campaign trend metrics."""

    period_val: str
    lead_count: int
    cost_per_lead: float
    engagement_rate: float


@dataclass
class CampaignMetricsDTO:
    """Summary and time-series metrics for an individual campaign in comparison."""

    campaign_id: UUID
    campaign_name: str
    budget: float
    total_leads: int
    overall_cpl: float
    overall_engagement_rate: float
    trend_points: list[CampaignTrendPointDTO]


@dataclass
class ComparativeCampaignTrendDTO:
    """Container for comparative campaign trend results across up to 5 campaigns."""

    start_date_val: datetime | None
    end_date_val: datetime | None
    campaigns: list[CampaignMetricsDTO]


def get_channel_acquisition_report(
    db: Session,
    start_date_val: datetime | None = None,
    end_date_val: datetime | None = None,
    channel_id: UUID | str | None = None,
    sort_by: str | None = None,
    sort_order: str = "desc",
) -> list[ChannelAcquisitionReportDTO]:
    """Aggregate channel acquisition metrics, compute quality scores, and calculate ROI.

    Filters by optional start_date_val, end_date_val, and channel_id (FR-001).
    Computes quality scores per channel (FR-002) and safe ROI values (FR-003).
    Supports sorting by quality_score, spend, revenue, lead_count, channel_name, or roi.
    """
    start_utc = _ensure_utc(start_date_val)
    end_utc = _ensure_utc(end_date_val)

    if start_utc and end_utc and start_utc > end_utc:
        raise InvalidReportingDataError("start_date_val cannot be later than end_date_val")

    channel_query = select(AcquisitionChannel)
    target_channel_uuid: UUID | None = None

    if channel_id:
        if isinstance(channel_id, UUID):
            target_channel_uuid = channel_id
        else:
            try:
                target_channel_uuid = UUID(channel_id)
            except ValueError:
                pass

        if target_channel_uuid:
            channel_query = channel_query.where(AcquisitionChannel.id == target_channel_uuid)
        else:
            channel_query = channel_query.where(AcquisitionChannel.channel_name == str(channel_id))

    channels = list(db.scalars(channel_query).all())

    if channel_id and not channels:
        raise ChannelNotFoundError(f"Acquisition channel '{channel_id}' not found.")

    reports: list[ChannelAcquisitionReportDTO] = []

    for ch in channels:
        lead_query = select(LeadEvent).where(LeadEvent.channel_id == ch.id)

        if start_utc:
            lead_query = lead_query.where(LeadEvent.event_timestamp >= start_utc)
        if end_utc:
            lead_query = lead_query.where(LeadEvent.event_timestamp <= end_utc)

        events = list(db.scalars(lead_query).all())

        lead_count = len(events)
        quality_scores = [e.quality_score for e in events if e.quality_score is not None]
        avg_quality_score = compute_lead_quality_score(quality_scores)

        revenue_amount = sum(
            float(e.revenue_amount) for e in events if e.revenue_amount is not None
        )
        spend_amount = float(ch.spend_amount) if ch.spend_amount is not None else 0.0

        roi_str = calculate_channel_roi(revenue_amount, spend_amount)

        reports.append(
            ChannelAcquisitionReportDTO(
                channel_id=ch.id,
                channel_name=ch.channel_name,
                lead_count=lead_count,
                quality_score=avg_quality_score,
                spend_amount=spend_amount,
                revenue_amount=revenue_amount,
                roi=roi_str,
            )
        )

    if sort_by:
        sort_key = sort_by.lower().strip()
        reverse_flag = sort_order.lower().strip() != "asc"

        def _get_sort_val(item: ChannelAcquisitionReportDTO) -> Any:
            if sort_key in ("quality_score", "quality"):
                return item.quality_score
            elif sort_key in ("spend", "spend_amount"):
                return item.spend_amount
            elif sort_key in ("revenue", "revenue_amount"):
                return item.revenue_amount
            elif sort_key in ("lead_count", "leads", "lead_volume"):
                return item.lead_count
            elif sort_key in ("channel_name", "name"):
                return item.channel_name
            elif sort_key == "roi":
                if item.roi == "N/A":
                    return -999999999.0
                try:
                    return float(item.roi.rstrip("%"))
                except ValueError:
                    return -999999999.0
            return item.quality_score

        reports.sort(key=_get_sort_val, reverse=reverse_flag)

    return reports


def get_campaign_trend_comparison(
    db: Session,
    campaign_ids: list[UUID | str] | str,
    start_date_val: datetime | None = None,
    end_date_val: datetime | None = None,
) -> ComparativeCampaignTrendDTO:
    """Calculate comparative campaign trends for up to 5 simultaneous campaigns (FR-006).

    Accepts 2 to 5 campaign IDs or campaign names, optional start_date_val and end_date_val.
    Returns ComparativeCampaignTrendDTO containing per-campaign metrics and time-series
    acquisition volume, CPL, and engagement rates.
    """
    if isinstance(campaign_ids, str):
        raw_ids = [c.strip() for c in campaign_ids.split(",") if c.strip()]
    else:
        raw_ids = [str(c).strip() if isinstance(c, UUID) else c for c in campaign_ids]

    unique_ids: list[str | UUID] = []
    for item in raw_ids:
        if item not in unique_ids:
            unique_ids.append(item)

    if len(unique_ids) > 5:
        raise InvalidReportingDataError("Maximum 5 campaigns allowed for comparison")
    if len(unique_ids) < 2:
        raise InvalidReportingDataError("At least 2 campaigns required for comparison")

    start_utc = _ensure_utc(start_date_val)
    end_utc = _ensure_utc(end_date_val)

    if start_utc and end_utc and start_utc > end_utc:
        raise InvalidReportingDataError("start_date_val cannot be later than end_date_val")

    campaigns_list: list[Campaign] = []
    for identifier in unique_ids:
        target_uuid: UUID | None = None
        if isinstance(identifier, UUID):
            target_uuid = identifier
        else:
            try:
                target_uuid = UUID(str(identifier))
            except ValueError:
                pass

        if target_uuid:
            stmt = select(Campaign).where(Campaign.id == target_uuid)
        else:
            stmt = select(Campaign).where(Campaign.campaign_name == str(identifier))

        campaign_obj = db.scalar(stmt)
        if not campaign_obj:
            raise CampaignNotFoundError(f"Campaign '{identifier}' not found.")
        campaigns_list.append(campaign_obj)

    # Determine date range if bounds were not provided
    if start_utc is None or end_utc is None:
        all_timestamps: list[datetime] = []
        for camp in campaigns_list:
            if camp.start_date_val:
                all_timestamps.append(_ensure_utc(camp.start_date_val))
            if camp.end_date_val:
                all_timestamps.append(_ensure_utc(camp.end_date_val))

            event_ts_list = db.scalars(
                select(LeadEvent.event_timestamp).where(LeadEvent.campaign_id == camp.id)
            ).all()
            all_timestamps.extend([_ensure_utc(ts) for ts in event_ts_list if ts])

        if all_timestamps:
            if start_utc is None:
                start_utc = min(all_timestamps)
            if end_utc is None:
                end_utc = max(all_timestamps)

    if start_utc is None:
        start_utc = datetime.now(timezone.utc) - timedelta(days=30)
    if end_utc is None:
        end_utc = datetime.now(timezone.utc)

    if start_utc > end_utc:
        start_utc, end_utc = end_utc, start_utc

    campaign_metrics_list: list[CampaignMetricsDTO] = []

    # Daily bucket calculations
    num_days = max(1, (end_utc.date() - start_utc.date()).days + 1)
    day_dates = [start_utc.date() + timedelta(days=i) for i in range(num_days)]

    for camp in campaigns_list:
        lead_query = (
            select(LeadEvent)
            .where(LeadEvent.campaign_id == camp.id)
            .where(LeadEvent.event_timestamp >= start_utc)
            .where(LeadEvent.event_timestamp <= end_utc)
        )
        events = list(db.scalars(lead_query).all())

        total_leads = len(events)
        camp_budget = float(camp.budget) if camp.budget is not None else 0.0
        overall_cpl = calculate_cost_per_lead(camp_budget, total_leads)
        overall_engagement = calculate_engagement_rate(events)

        trend_points: list[CampaignTrendPointDTO] = []
        for curr_date in day_dates:
            day_events = [
                e for e in events if _ensure_utc(e.event_timestamp).date() == curr_date
            ]
            day_lead_count = len(day_events)
            day_cpl = calculate_cost_per_lead(camp_budget, day_lead_count)
            day_engagement = calculate_engagement_rate(day_events)

            trend_points.append(
                CampaignTrendPointDTO(
                    period_val=curr_date.isoformat(),
                    lead_count=day_lead_count,
                    cost_per_lead=day_cpl,
                    engagement_rate=day_engagement,
                )
            )

        campaign_metrics_list.append(
            CampaignMetricsDTO(
                campaign_id=camp.id,
                campaign_name=camp.campaign_name,
                budget=camp_budget,
                total_leads=total_leads,
                overall_cpl=overall_cpl,
                overall_engagement_rate=overall_engagement,
                trend_points=trend_points,
            )
        )

    return ComparativeCampaignTrendDTO(
        start_date_val=start_utc,
        end_date_val=end_utc,
        campaigns=campaign_metrics_list,
    )


def create_report_audit_log(
    db: Session,
    action_type: str,
    config_delta: str | None = None,
    user_id: UUID | str | None = None,
) -> ReportAuditLog:
    """Create and persist an immutable audit log entry for reporting configuration changes (FR-011)."""
    clean_action = action_type.strip() if action_type else ""
    if not clean_action:
        raise InvalidReportingDataError("action_type cannot be empty when creating an audit log.")

    parsed_user_id: UUID | None = None
    if user_id:
        if isinstance(user_id, UUID):
            parsed_user_id = user_id
        else:
            try:
                parsed_user_id = UUID(str(user_id))
            except ValueError:
                parsed_user_id = None

    audit_entry = ReportAuditLog(
        user_id=parsed_user_id,
        action_type=clean_action,
        config_delta=config_delta.strip() if config_delta else None,
    )
    db.add(audit_entry)
    db.commit()
    db.refresh(audit_entry)

    logger.info(
        "Persisted report audit log record id=%s action_type='%s'",
        audit_entry.id,
        clean_action,
    )
    return audit_entry


def delete_report_configuration(
    db: Session,
    config_id: UUID | str,
) -> bool:
    """Delete a reporting configuration definition or metric rule threshold (FR-011).

    Searches FunnelThreshold and ReportAuditLog models by ID or stage_name.
    """
    target_uuid: UUID | None = None
    raw_str = str(config_id).strip()

    if isinstance(config_id, UUID):
        target_uuid = config_id
    else:
        try:
            target_uuid = UUID(raw_str)
        except ValueError:
            target_uuid = None

    # 1. Try finding in FunnelThreshold
    if target_uuid:
        threshold_stmt = select(FunnelThreshold).where(FunnelThreshold.id == target_uuid)
    else:
        threshold_stmt = select(FunnelThreshold).where(FunnelThreshold.stage_name == raw_str)

    threshold_rec = db.scalar(threshold_stmt)
    if threshold_rec is not None:
        db.delete(threshold_rec)
        db.commit()
        logger.info("Deleted FunnelThreshold config id=%s", threshold_rec.id)
        return True

    # 2. Try finding in ReportAuditLog if target_uuid is present
    if target_uuid:
        audit_stmt = select(ReportAuditLog).where(ReportAuditLog.id == target_uuid)
        audit_rec = db.scalar(audit_stmt)
        if audit_rec is not None:
            db.delete(audit_rec)
            db.commit()
            logger.info("Deleted ReportAuditLog record id=%s", audit_rec.id)
            return True

    raise ConfigurationNotFoundError(f"Reporting configuration '{config_id}' not found.")

