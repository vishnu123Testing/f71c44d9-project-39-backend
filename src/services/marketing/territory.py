"""Territory service module for geographic analytics, filtering, and access control.

Provides territory region filtering, postal code pattern matching, unassigned region
grouping, and multi-region user access scoping (FR-007, FR-008, NFR-002).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import fnmatch
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.analytics import LeadEvent
from src.models.marketing import Territory

logger = logging.getLogger(__name__)


class TerritoryNotFoundError(ValueError):
    """Raised when a requested territory record is not found."""

    pass


class TerritoryAccessDeniedError(PermissionError):
    """Raised when a user attempts to access a territory outside their authorized scope."""

    pass


class InvalidTerritoryFilterError(ValueError):
    """Raised when territory filtering input parameters are invalid."""

    pass


@dataclass
class UnassignedLeadDTO:
    """Data transfer object representing an unassigned lead or opportunity event."""

    lead_id: UUID
    postal_code: str | None
    stage_name: str
    deal_value: float | None
    event_timestamp: datetime
    reason: str


@dataclass
class TerritoryDensityDTO:
    """Data transfer object representing density and volume metrics for a territory."""

    territory_id: UUID
    territory_code: str
    territory_name: str
    lead_count: int
    opportunity_count: int
    total_deal_value: float
    average_deal_value: float
    postal_code_breakdown: dict[str, int] = field(default_factory=dict)


@dataclass
class UnassignedDrawerDTO:
    """Container for unassigned leads and opportunities breakdown drawer data."""

    total_unassigned_leads: int
    total_unassigned_value: float
    unassigned_items: list[UnassignedLeadDTO] = field(default_factory=list)


@dataclass
class TerritoryHeatMapDTO:
    """Aggregated response DTO for geographic territory heat map visualization."""

    territories: list[TerritoryDensityDTO]
    unassigned_drawer: UnassignedDrawerDTO
    total_leads: int
    total_value: float


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime instance is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def match_postal_code(postal_code: str | None, patterns_str: str | None) -> bool:
    """Check if a postal code matches any pattern defined in a pattern string.

    Supports comma/newline-separated exact strings, wildcards (*, ?), ranges (e.g. 90001-90899),
    and prefix matching.
    """
    if not postal_code or not patterns_str:
        return False

    code = postal_code.strip().upper()
    if not code:
        return False

    # Base postal code for hyphenated zip extensions (e.g., 90210-1234 -> 90210)
    code_base = code.split("-")[0].strip() if "-" in code else code

    patterns = [p.strip().upper() for p in patterns_str.replace("\n", ",").split(",") if p.strip()]

    for pattern in patterns:
        # Range pattern, e.g., 90001-90899
        if "-" in pattern and not pattern.startswith("-") and not pattern.endswith("-"):
            parts = pattern.split("-", 1)
            start_pat, end_pat = parts[0].strip(), parts[1].strip()
            if start_pat.isdigit() and end_pat.isdigit():
                if code.isdigit() and int(start_pat) <= int(code) <= int(end_pat):
                    return True
                if code_base.isdigit() and int(start_pat) <= int(code_base) <= int(end_pat):
                    return True
            else:
                if start_pat <= code <= end_pat or start_pat <= code_base <= end_pat:
                    return True
            continue

        # Wildcard pattern, e.g., 90*, SW1?
        if "*" in pattern or "?" in pattern:
            if fnmatch.fnmatch(code, pattern) or fnmatch.fnmatch(code_base, pattern):
                return True
            continue

        # Exact match or prefix match
        if code == pattern or code_base == pattern:
            return True
        if code.startswith(pattern) or code_base.startswith(pattern):
            return True

    return False


def get_territory_by_id_or_code(db: Session, identifier: UUID | str) -> Territory | None:
    """Retrieve a Territory record by UUID or territory_code string."""
    target_uuid: UUID | None = None
    if isinstance(identifier, UUID):
        target_uuid = identifier
    else:
        try:
            target_uuid = UUID(str(identifier))
        except ValueError:
            pass

    if target_uuid:
        return db.scalar(select(Territory).where(Territory.id == target_uuid))
    return db.scalar(select(Territory).where(Territory.territory_code == str(identifier)))


def assign_territory_by_postal_code(db: Session, postal_code: str | None) -> Territory | None:
    """Find matching Territory for a given postal code based on defined patterns."""
    if not postal_code:
        return None
    territories = list(db.scalars(select(Territory)).all())
    for t in territories:
        if match_postal_code(postal_code, t.postal_code_patterns):
            return t
    return None


def get_territory_heatmap(
    db: Session,
    lead_status: str | None = None,
    min_deal_value: float | None = None,
    max_deal_value: float | None = None,
    territory_id: UUID | str | None = None,
    start_date_val: datetime | None = None,
    end_date_val: datetime | None = None,
    user_authorized_territories: list[UUID | str] | None = None,
    include_unassigned: bool = True,
) -> TerritoryHeatMapDTO:
    """Generate geographic territory heat map with region density and unassigned drawer.

    Enforces territory filtering (FR-007, FR-008) and multi-region user access scoping (NFR-002).
    """
    start_utc = _ensure_utc(start_date_val)
    end_utc = _ensure_utc(end_date_val)

    if start_utc and end_utc and start_utc > end_utc:
        raise InvalidTerritoryFilterError("start_date_val cannot be later than end_date_val")

    if (
        min_deal_value is not None
        and max_deal_value is not None
        and float(min_deal_value) > float(max_deal_value)
    ):
        raise InvalidTerritoryFilterError("min_deal_value cannot be greater than max_deal_value")

    all_territories = list(db.scalars(select(Territory)).all())
    territory_map = {t.id: t for t in all_territories}

    auth_uuids: set[UUID] = set()
    auth_codes: set[str] = set()

    if user_authorized_territories is not None:
        for auth_item in user_authorized_territories:
            if isinstance(auth_item, UUID):
                auth_uuids.add(auth_item)
            else:
                raw_str = str(auth_item).strip()
                try:
                    auth_uuids.add(UUID(raw_str))
                except ValueError:
                    auth_codes.add(raw_str)

    if territory_id is not None:
        target_territory = get_territory_by_id_or_code(db, territory_id)
        if not target_territory:
            raise TerritoryNotFoundError(f"Territory '{territory_id}' not found.")

        if user_authorized_territories is not None:
            is_authorized = (
                target_territory.id in auth_uuids
                or target_territory.territory_code in auth_codes
            )
            if not is_authorized:
                raise TerritoryAccessDeniedError(
                    f"User is not authorized to access territory '{territory_id}'."
                )

        scoped_territories = [target_territory]
        include_unassigned = False
    else:
        if user_authorized_territories is not None:
            scoped_territories = [
                t
                for t in all_territories
                if t.id in auth_uuids or t.territory_code in auth_codes
            ]
        else:
            scoped_territories = all_territories

    stmt = select(LeadEvent)

    if start_utc:
        stmt = stmt.where(LeadEvent.event_timestamp >= start_utc)
    if end_utc:
        stmt = stmt.where(LeadEvent.event_timestamp <= end_utc)
    if min_deal_value is not None:
        stmt = stmt.where(LeadEvent.deal_value >= float(min_deal_value))
    if max_deal_value is not None:
        stmt = stmt.where(LeadEvent.deal_value <= float(max_deal_value))

    events = list(db.scalars(stmt).all())

    if lead_status:
        statuses = {s.strip().upper() for s in lead_status.split(",") if s.strip()}
        if statuses:
            events = [
                e
                for e in events
                if e.stage_name and e.stage_name.strip().upper() in statuses
            ]

    assigned_events: dict[UUID, list[LeadEvent]] = {t.id: [] for t in all_territories}
    unassigned_events: list[tuple[LeadEvent, str]] = []

    for event in events:
        matched_territory: Territory | None = None

        if event.territory_id and event.territory_id in territory_map:
            matched_territory = territory_map[event.territory_id]
        elif event.postal_code:
            for t in all_territories:
                if match_postal_code(event.postal_code, t.postal_code_patterns):
                    matched_territory = t
                    break

        if matched_territory:
            assigned_events[matched_territory.id].append(event)
        else:
            reason = "Missing postal code" if not event.postal_code else "Unmapped postal code"
            unassigned_events.append((event, reason))

    territory_dtos: list[TerritoryDensityDTO] = []
    for t in scoped_territories:
        t_events = assigned_events.get(t.id, [])
        lead_count = len(t_events)

        opp_events = [
            e for e in t_events if e.deal_value is not None and float(e.deal_value) > 0.0
        ]
        opp_count = len(opp_events)

        total_val = sum(float(e.deal_value) for e in t_events if e.deal_value is not None)
        avg_val = round(total_val / opp_count, 2) if opp_count > 0 else 0.0

        postal_breakdown: dict[str, int] = {}
        for e in t_events:
            p_code = e.postal_code.strip().upper() if e.postal_code else "UNKNOWN"
            postal_breakdown[p_code] = postal_breakdown.get(p_code, 0) + 1

        territory_dtos.append(
            TerritoryDensityDTO(
                territory_id=t.id,
                territory_code=t.territory_code,
                territory_name=t.territory_name,
                lead_count=lead_count,
                opportunity_count=opp_count,
                total_deal_value=total_val,
                average_deal_value=avg_val,
                postal_code_breakdown=postal_breakdown,
            )
        )

    if include_unassigned:
        total_unassigned_leads = len(unassigned_events)
        total_unassigned_val = sum(
            float(e.deal_value) for e, _ in unassigned_events if e.deal_value is not None
        )
        unassigned_items = [
            UnassignedLeadDTO(
                lead_id=e.lead_id,
                postal_code=e.postal_code,
                stage_name=e.stage_name,
                deal_value=float(e.deal_value) if e.deal_value is not None else None,
                event_timestamp=e.event_timestamp,
                reason=reason,
            )
            for e, reason in unassigned_events
        ]
        unassigned_drawer = UnassignedDrawerDTO(
            total_unassigned_leads=total_unassigned_leads,
            total_unassigned_value=total_unassigned_val,
            unassigned_items=unassigned_items,
        )
    else:
        unassigned_drawer = UnassignedDrawerDTO(
            total_unassigned_leads=0,
            total_unassigned_value=0.0,
            unassigned_items=[],
        )

    total_leads = (
        sum(t.lead_count for t in territory_dtos) + unassigned_drawer.total_unassigned_leads
    )
    total_value = (
        sum(t.total_deal_value for t in territory_dtos) + unassigned_drawer.total_unassigned_value
    )

    return TerritoryHeatMapDTO(
        territories=territory_dtos,
        unassigned_drawer=unassigned_drawer,
        total_leads=total_leads,
        total_value=total_value,
    )
