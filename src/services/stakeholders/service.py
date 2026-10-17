"""Stakeholder Service implementation for contact mapping, influence tracking, and activity feeds.

This module encapsulates business logic for managing stakeholder contact details,
tracking influence levels (High, Medium, Low), maintaining soft deactivation status,
logging chronological CRM interaction activities, and aggregating deal stakeholder maps (FR-006, FR-007).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Any
import uuid
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.lib.logging import get_correlation_id
from src.models.deal import Deal, Stakeholder, StakeholderActivity

logger = logging.getLogger(__name__)


class StakeholderNotFoundError(ValueError):
    """Raised when a requested stakeholder record is not found in the database."""

    pass


class StakeholderActivityNotFoundError(ValueError):
    """Raised when a requested stakeholder activity log record is not found."""

    pass


class DealNotFoundError(ValueError):
    """Raised when the specified deal for a stakeholder operation does not exist."""

    pass


class InvalidStakeholderDataError(ValueError):
    """Raised when stakeholder input parameters fail domain validation rules."""

    pass


VALID_INFLUENCE_LEVELS: set[str] = {"high", "medium", "low"}
VALID_ACTIVITY_TYPES: set[str] = {"email", "meeting", "call", "note"}


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime instance is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_uuid(val: UUID | str, param_name: str = "ID") -> UUID:
    """Parse a UUID instance or valid UUID string."""
    if isinstance(val, UUID):
        return val
    if isinstance(val, str) and val.strip():
        try:
            return UUID(val.strip())
        except ValueError as err:
            raise InvalidStakeholderDataError(
                f"Invalid {param_name} format: '{val}'"
            ) from err
    raise InvalidStakeholderDataError(f"{param_name} must be a valid UUID or string.")


def _normalize_influence_level(level: str) -> str:
    """Validate and normalize influence level to 'High', 'Medium', or 'Low'."""
    if not isinstance(level, str) or not level.strip():
        raise InvalidStakeholderDataError("Influence level cannot be empty.")
    cleaned = level.strip().lower()
    if cleaned not in VALID_INFLUENCE_LEVELS:
        raise InvalidStakeholderDataError(
            f"Invalid influence level '{level}'. Must be one of: High, Medium, Low."
        )
    return cleaned.capitalize()


def _normalize_activity_type(activity_type: str) -> str:
    """Validate and normalize CRM activity type."""
    if not isinstance(activity_type, str) or not activity_type.strip():
        raise InvalidStakeholderDataError("Activity type cannot be empty.")
    cleaned = activity_type.strip()
    lower_type = cleaned.lower()
    if lower_type in VALID_ACTIVITY_TYPES:
        return lower_type.capitalize()
    return cleaned


def _validate_email(email: str) -> str:
    """Validate email address format."""
    if not isinstance(email, str) or not email.strip():
        raise InvalidStakeholderDataError("Email address cannot be empty.")
    cleaned = email.strip()
    if "@" not in cleaned:
        raise InvalidStakeholderDataError(f"Invalid email address format: '{email}'")
    parts = cleaned.split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise InvalidStakeholderDataError(f"Invalid email address format: '{email}'")
    return cleaned


@dataclass
class StakeholderDomain:
    """Pure domain entity representation of a Deal Stakeholder."""

    id: UUID
    deal_id: UUID
    full_name: str
    email: str
    org_role: str
    influence_level: str
    is_active: bool
    created_on: datetime


@dataclass
class StakeholderActivityDomain:
    """Pure domain entity representation of a Stakeholder Activity log entry."""

    id: UUID
    stakeholder_id: UUID
    deal_id: UUID
    activity_type: str
    summary: str
    occurred_on: datetime
    created_on: datetime
    stakeholder_name: str | None = None


@dataclass
class StakeholderMapResultDTO:
    """Aggregated stakeholder map result for a deal including active contacts and recent activities."""

    deal_id: UUID
    stakeholders: list[StakeholderDomain] = field(default_factory=list)
    recent_activities: list[StakeholderActivityDomain] = field(default_factory=list)
    total_stakeholders: int = 0
    total_activities: int = 0


def to_stakeholder_domain(stakeholder: Stakeholder) -> StakeholderDomain:
    """Map a SQLAlchemy Stakeholder ORM instance to a pure StakeholderDomain entity."""
    return StakeholderDomain(
        id=stakeholder.id,
        deal_id=stakeholder.deal_id,
        full_name=stakeholder.full_name,
        email=stakeholder.email,
        org_role=stakeholder.org_role,
        influence_level=stakeholder.influence_level,
        is_active=stakeholder.is_active,
        created_on=_ensure_utc(stakeholder.created_on) or stakeholder.created_on,
    )


def to_activity_domain(
    activity: StakeholderActivity, stakeholder_name: str | None = None
) -> StakeholderActivityDomain:
    """Map a SQLAlchemy StakeholderActivity ORM instance to a pure StakeholderActivityDomain entity."""
    s_name = stakeholder_name
    if s_name is None and getattr(activity, "stakeholder", None) is not None:
        s_name = activity.stakeholder.full_name

    return StakeholderActivityDomain(
        id=activity.id,
        stakeholder_id=activity.stakeholder_id,
        deal_id=activity.deal_id,
        activity_type=activity.activity_type,
        summary=activity.summary,
        occurred_on=_ensure_utc(activity.occurred_on) or activity.occurred_on,
        created_on=_ensure_utc(activity.created_on) or activity.created_on,
        stakeholder_name=s_name,
    )


def create_stakeholder(
    session: Session,
    deal_id: UUID | str,
    full_name: str,
    email: str,
    org_role: str,
    influence_level: str,
    is_active: bool = True,
) -> StakeholderDomain:
    """Create and persist a new stakeholder contact mapped to a deal.

    Args:
        session: Active SQLAlchemy database session.
        deal_id: UUID or string identifier of the target deal.
        full_name: Stakeholder full name.
        email: Stakeholder contact email address.
        org_role: Organizational role/title (e.g. Executive Sponsor, Decision Maker).
        influence_level: Influence rating ('High', 'Medium', 'Low').
        is_active: Boolean status flag indicating active status (default True).

    Returns:
        StakeholderDomain entity representing created stakeholder.

    Raises:
        DealNotFoundError: If specified deal_id does not exist.
        InvalidStakeholderDataError: If any parameter fails validation.
    """
    deal_uuid = _parse_uuid(deal_id, "deal_id")
    deal = session.get(Deal, deal_uuid)
    if deal is None:
        raise DealNotFoundError(f"Deal with ID '{deal_id}' not found.")

    name_clean = full_name.strip() if isinstance(full_name, str) else ""
    if not name_clean:
        raise InvalidStakeholderDataError("Stakeholder full name cannot be empty.")

    email_clean = _validate_email(email)

    role_clean = org_role.strip() if isinstance(org_role, str) else ""
    if not role_clean:
        raise InvalidStakeholderDataError("Organizational role cannot be empty.")

    influence_clean = _normalize_influence_level(influence_level)

    now_utc = datetime.now(timezone.utc)
    stakeholder = Stakeholder(
        deal_id=deal_uuid,
        full_name=name_clean,
        email=email_clean,
        org_role=role_clean,
        influence_level=influence_clean,
        is_active=bool(is_active),
        created_on=now_utc,
    )
    session.add(stakeholder)
    session.commit()
    session.refresh(stakeholder)

    domain = to_stakeholder_domain(stakeholder)
    logger.info(
        "Stakeholder mapped successfully",
        extra={
            "event_type": "stakeholder_created",
            "stakeholder_id": str(stakeholder.id),
            "deal_id": str(deal_uuid),
            "full_name": name_clean,
            "org_role": role_clean,
            "influence_level": influence_clean,
            "correlation_id": get_correlation_id(),
        },
    )
    return domain


def get_stakeholder_by_id(
    session: Session,
    stakeholder_id: UUID | str,
) -> StakeholderDomain:
    """Retrieve a stakeholder domain entity by unique stakeholder ID.

    Args:
        session: Active SQLAlchemy database session.
        stakeholder_id: UUID or string identifier of the stakeholder.

    Returns:
        StakeholderDomain entity.

    Raises:
        StakeholderNotFoundError: If stakeholder does not exist.
    """
    s_uuid = _parse_uuid(stakeholder_id, "stakeholder_id")
    stakeholder = session.get(Stakeholder, s_uuid)
    if stakeholder is None:
        raise StakeholderNotFoundError(f"Stakeholder with ID '{stakeholder_id}' not found.")
    return to_stakeholder_domain(stakeholder)


def get_stakeholders_for_deal(
    session: Session,
    deal_id: UUID | str,
    active_only: bool = False,
    limit: int | None = None,
    offset: int | None = None,
) -> list[StakeholderDomain]:
    """Retrieve mapped stakeholders for a deal.

    Args:
        session: Active SQLAlchemy database session.
        deal_id: UUID or string identifier of the deal.
        active_only: If True, only returns stakeholders where is_active=True.
        limit: Optional pagination limit.
        offset: Optional pagination offset.

    Returns:
        List of StakeholderDomain objects.

    Raises:
        DealNotFoundError: If specified deal_id does not exist.
    """
    deal_uuid = _parse_uuid(deal_id, "deal_id")
    deal = session.get(Deal, deal_uuid)
    if deal is None:
        raise DealNotFoundError(f"Deal with ID '{deal_id}' not found.")

    query = select(Stakeholder).where(Stakeholder.deal_id == deal_uuid)
    if active_only:
        query = query.where(Stakeholder.is_active.is_(True))

    query = query.order_by(Stakeholder.created_on.desc())

    if offset is not None and offset > 0:
        query = query.offset(offset)
    if limit is not None and limit > 0:
        query = query.limit(limit)

    records = session.scalars(query).all()
    return [to_stakeholder_domain(s) for s in records]


def update_stakeholder(
    session: Session,
    stakeholder_id: UUID | str,
    full_name: str | None = None,
    email: str | None = None,
    org_role: str | None = None,
    influence_level: str | None = None,
    is_active: bool | None = None,
) -> StakeholderDomain:
    """Update contact details, role, influence level, or active status of a stakeholder.

    Args:
        session: Active SQLAlchemy database session.
        stakeholder_id: UUID or string identifier of the stakeholder.
        full_name: Optional updated full name.
        email: Optional updated email.
        org_role: Optional updated organizational role.
        influence_level: Optional updated influence level ('High', 'Medium', 'Low').
        is_active: Optional updated active status flag.

    Returns:
        Updated StakeholderDomain entity.

    Raises:
        StakeholderNotFoundError: If stakeholder is not found.
        InvalidStakeholderDataError: If validation fails for updated parameters.
    """
    s_uuid = _parse_uuid(stakeholder_id, "stakeholder_id")
    stakeholder = session.get(Stakeholder, s_uuid)
    if stakeholder is None:
        raise StakeholderNotFoundError(f"Stakeholder with ID '{stakeholder_id}' not found.")

    if full_name is not None:
        name_clean = full_name.strip() if isinstance(full_name, str) else ""
        if not name_clean:
            raise InvalidStakeholderDataError("Stakeholder full name cannot be empty.")
        stakeholder.full_name = name_clean

    if email is not None:
        stakeholder.email = _validate_email(email)

    if org_role is not None:
        role_clean = org_role.strip() if isinstance(org_role, str) else ""
        if not role_clean:
            raise InvalidStakeholderDataError("Organizational role cannot be empty.")
        stakeholder.org_role = role_clean

    if influence_level is not None:
        stakeholder.influence_level = _normalize_influence_level(influence_level)

    if is_active is not None:
        stakeholder.is_active = bool(is_active)

    session.commit()
    session.refresh(stakeholder)

    domain = to_stakeholder_domain(stakeholder)
    logger.info(
        "Stakeholder details updated successfully",
        extra={
            "event_type": "stakeholder_updated",
            "stakeholder_id": str(stakeholder.id),
            "deal_id": str(stakeholder.deal_id),
            "is_active": stakeholder.is_active,
            "correlation_id": get_correlation_id(),
        },
    )
    return domain


def deactivate_stakeholder(
    session: Session,
    stakeholder_id: UUID | str,
) -> StakeholderDomain:
    """Flag a stakeholder as inactive (soft contact deletion) while retaining historical logs.

    Args:
        session: Active SQLAlchemy database session.
        stakeholder_id: UUID or string identifier of the stakeholder.

    Returns:
        Updated StakeholderDomain entity with is_active=False.

    Raises:
        StakeholderNotFoundError: If stakeholder does not exist.
    """
    return update_stakeholder(session, stakeholder_id, is_active=False)


def delete_stakeholder(
    session: Session,
    stakeholder_id: UUID | str,
) -> None:
    """Permanently delete a stakeholder record.

    Args:
        session: Active SQLAlchemy database session.
        stakeholder_id: UUID or string identifier of the stakeholder.

    Raises:
        StakeholderNotFoundError: If stakeholder does not exist.
    """
    s_uuid = _parse_uuid(stakeholder_id, "stakeholder_id")
    stakeholder = session.get(Stakeholder, s_uuid)
    if stakeholder is None:
        raise StakeholderNotFoundError(f"Stakeholder with ID '{stakeholder_id}' not found.")

    session.delete(stakeholder)
    session.commit()
    logger.info(
        "Stakeholder deleted permanently",
        extra={
            "event_type": "stakeholder_deleted",
            "stakeholder_id": str(s_uuid),
            "correlation_id": get_correlation_id(),
        },
    )


def add_stakeholder_activity(
    session: Session,
    deal_id: UUID | str,
    stakeholder_id: UUID | str,
    activity_type: str,
    summary: str,
    occurred_on: datetime | str | None = None,
) -> StakeholderActivityDomain:
    """Log a new CRM interaction activity for a mapped stakeholder on a deal.

    Args:
        session: Active SQLAlchemy database session.
        deal_id: UUID or string identifier of the deal.
        stakeholder_id: UUID or string identifier of the stakeholder.
        activity_type: Type of CRM interaction ('Email', 'Meeting', 'Call', 'Note').
        summary: Content summary of interaction.
        occurred_on: Datetime or ISO timestamp string of interaction occurrence. Defaults to now UTC.

    Returns:
        StakeholderActivityDomain entity.

    Raises:
        DealNotFoundError: If deal does not exist.
        StakeholderNotFoundError: If stakeholder does not exist or does not belong to deal.
        InvalidStakeholderDataError: If input data is invalid.
    """
    deal_uuid = _parse_uuid(deal_id, "deal_id")
    s_uuid = _parse_uuid(stakeholder_id, "stakeholder_id")

    deal = session.get(Deal, deal_uuid)
    if deal is None:
        raise DealNotFoundError(f"Deal with ID '{deal_id}' not found.")

    stakeholder = session.get(Stakeholder, s_uuid)
    if stakeholder is None:
        raise StakeholderNotFoundError(f"Stakeholder with ID '{stakeholder_id}' not found.")

    if stakeholder.deal_id != deal_uuid:
        raise InvalidStakeholderDataError(
            f"Stakeholder '{stakeholder_id}' belongs to deal '{stakeholder.deal_id}', not '{deal_id}'."
        )

    act_type_clean = _normalize_activity_type(activity_type)

    summary_clean = summary.strip() if isinstance(summary, str) else ""
    if not summary_clean:
        raise InvalidStakeholderDataError("Activity summary cannot be empty.")

    if occurred_on is None:
        occ_dt = datetime.now(timezone.utc)
    elif isinstance(occurred_on, str):
        try:
            occ_dt = datetime.fromisoformat(occurred_on.strip())
        except ValueError as err:
            raise InvalidStakeholderDataError(
                f"Invalid occurred_on timestamp format: '{occurred_on}'"
            ) from err
    elif isinstance(occurred_on, datetime):
        occ_dt = occurred_on
    else:
        raise InvalidStakeholderDataError("occurred_on must be a datetime or ISO string.")

    occ_dt = _ensure_utc(occ_dt)
    if occ_dt is None:
        raise InvalidStakeholderDataError("occurred_on timestamp cannot be None.")

    now_utc = datetime.now(timezone.utc)
    activity = StakeholderActivity(
        deal_id=deal_uuid,
        stakeholder_id=s_uuid,
        activity_type=act_type_clean,
        summary=summary_clean,
        occurred_on=occ_dt,
        created_on=now_utc,
    )
    session.add(activity)
    session.commit()
    session.refresh(activity)

    domain = to_activity_domain(activity, stakeholder_name=stakeholder.full_name)
    logger.info(
        "Stakeholder activity logged successfully",
        extra={
            "event_type": "stakeholder_activity_logged",
            "activity_id": str(activity.id),
            "deal_id": str(deal_uuid),
            "stakeholder_id": str(s_uuid),
            "activity_type": act_type_clean,
            "occurred_on": occ_dt.isoformat(),
            "correlation_id": get_correlation_id(),
        },
    )
    return domain


def get_activities_for_deal(
    session: Session,
    deal_id: UUID | str,
    stakeholder_id: UUID | str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[StakeholderActivityDomain]:
    """Retrieve chronological feed of recent CRM interaction activities for a deal.

    Activities are ordered chronologically descending (newest first).

    Args:
        session: Active SQLAlchemy database session.
        deal_id: UUID or string identifier of the deal.
        stakeholder_id: Optional filter for a specific stakeholder ID.
        limit: Optional pagination limit.
        offset: Optional pagination offset.

    Returns:
        List of StakeholderActivityDomain objects sorted chronologically descending.

    Raises:
        DealNotFoundError: If deal does not exist.
    """
    deal_uuid = _parse_uuid(deal_id, "deal_id")
    deal = session.get(Deal, deal_uuid)
    if deal is None:
        raise DealNotFoundError(f"Deal with ID '{deal_id}' not found.")

    query = select(StakeholderActivity).where(StakeholderActivity.deal_id == deal_uuid)

    if stakeholder_id is not None:
        s_uuid = _parse_uuid(stakeholder_id, "stakeholder_id")
        query = query.where(StakeholderActivity.stakeholder_id == s_uuid)

    query = query.order_by(StakeholderActivity.occurred_on.desc())

    if offset is not None and offset > 0:
        query = query.offset(offset)
    if limit is not None and limit > 0:
        query = query.limit(limit)

    records = session.scalars(query).all()
    results: list[StakeholderActivityDomain] = []
    for act in records:
        s_name = act.stakeholder.full_name if act.stakeholder else None
        results.append(to_activity_domain(act, stakeholder_name=s_name))
    return results


def get_activities_for_stakeholder(
    session: Session,
    stakeholder_id: UUID | str,
    limit: int | None = None,
) -> list[StakeholderActivityDomain]:
    """Retrieve chronological activity logs for a specific stakeholder.

    Args:
        session: Active SQLAlchemy database session.
        stakeholder_id: UUID or string identifier of the stakeholder.
        limit: Optional maximum number of activity records.

    Returns:
        List of StakeholderActivityDomain items sorted chronologically descending.

    Raises:
        StakeholderNotFoundError: If stakeholder does not exist.
    """
    s_uuid = _parse_uuid(stakeholder_id, "stakeholder_id")
    stakeholder = session.get(Stakeholder, s_uuid)
    if stakeholder is None:
        raise StakeholderNotFoundError(f"Stakeholder with ID '{stakeholder_id}' not found.")

    query = (
        select(StakeholderActivity)
        .where(StakeholderActivity.stakeholder_id == s_uuid)
        .order_by(StakeholderActivity.occurred_on.desc())
    )
    if limit is not None and limit > 0:
        query = query.limit(limit)

    records = session.scalars(query).all()
    return [to_activity_domain(a, stakeholder_name=stakeholder.full_name) for a in records]


def get_stakeholder_map_for_deal(
    session: Session,
    deal_id: UUID | str,
    active_only: bool = False,
    activity_limit: int | None = 50,
) -> StakeholderMapResultDTO:
    """Retrieve complete stakeholder map and chronological interaction activity feed for a deal.

    Args:
        session: Active SQLAlchemy database session.
        deal_id: UUID or string identifier of the deal.
        active_only: If True, only active contacts are included.
        activity_limit: Maximum recent activity feed items returned.

    Returns:
        StakeholderMapResultDTO containing mapped contacts and chronological activities.

    Raises:
        DealNotFoundError: If deal does not exist.
    """
    deal_uuid = _parse_uuid(deal_id, "deal_id")
    deal = session.get(Deal, deal_uuid)
    if deal is None:
        raise DealNotFoundError(f"Deal with ID '{deal_id}' not found.")

    stakeholders = get_stakeholders_for_deal(session, deal_uuid, active_only=active_only)
    activities = get_activities_for_deal(session, deal_uuid, limit=activity_limit)

    return StakeholderMapResultDTO(
        deal_id=deal_uuid,
        stakeholders=stakeholders,
        recent_activities=activities,
        total_stakeholders=len(stakeholders),
        total_activities=len(activities),
    )


def sync_crm_stakeholders_and_activities(
    session: Session,
    deal_id: UUID | str,
    crm_stakeholders: list[Any],
    crm_activities: list[Any],
) -> StakeholderMapResultDTO:
    """Synchronize external CRM stakeholder contacts and interaction activities with deal records.

    Handles creation or update of contacts, soft contact deactivation for missing contacts,
    and ingestion of interaction activity logs.

    Args:
        session: Active SQLAlchemy database session.
        deal_id: UUID or string identifier of the deal.
        crm_stakeholders: List of CRMStakeholderDTO objects or dicts.
        crm_activities: List of CRMActivityDTO objects or dicts.

    Returns:
        StakeholderMapResultDTO representing updated deal stakeholder map.

    Raises:
        DealNotFoundError: If deal does not exist.
    """
    deal_uuid = _parse_uuid(deal_id, "deal_id")
    deal = session.get(Deal, deal_uuid)
    if deal is None:
        raise DealNotFoundError(f"Deal with ID '{deal_id}' not found.")

    # Fetch existing stakeholders for the deal
    existing_records = session.scalars(
        select(Stakeholder).where(Stakeholder.deal_id == deal_uuid)
    ).all()
    existing_by_email: dict[str, Stakeholder] = {
        s.email.lower(): s for s in existing_records
    }

    seen_emails: set[str] = set()

    # Process incoming CRM stakeholders
    for item in crm_stakeholders:
        if isinstance(item, dict):
            s_name = item.get("full_name") or item.get("name") or "Unknown"
            s_email = item.get("email") or ""
            s_role = item.get("org_role") or item.get("role") or "Contact"
            s_influence = item.get("influence_level") or "Medium"
            s_active = item.get("is_active", True)
        else:
            s_name = getattr(item, "full_name", "Unknown")
            s_email = getattr(item, "email", "")
            s_role = getattr(item, "org_role", "Contact")
            s_influence = getattr(item, "influence_level", "Medium")
            s_active = getattr(item, "is_active", True)

        if not s_email or not isinstance(s_email, str) or "@" not in s_email:
            continue

        email_key = s_email.strip().lower()
        seen_emails.add(email_key)

        if email_key in existing_by_email:
            stk = existing_by_email[email_key]
            stk.full_name = s_name.strip() if isinstance(s_name, str) else stk.full_name
            stk.org_role = s_role.strip() if isinstance(s_role, str) else stk.org_role
            stk.influence_level = _normalize_influence_level(s_influence)
            stk.is_active = bool(s_active)
        else:
            try:
                stk = Stakeholder(
                    deal_id=deal_uuid,
                    full_name=s_name.strip(),
                    email=_validate_email(s_email),
                    org_role=s_role.strip() or "Contact",
                    influence_level=_normalize_influence_level(s_influence),
                    is_active=bool(s_active),
                    created_on=datetime.now(timezone.utc),
                )
                session.add(stk)
                session.flush()
                existing_by_email[email_key] = stk
            except InvalidStakeholderDataError:
                continue

    # Soft-deactivate stakeholders not present in the new CRM payload
    for email_key, stk in existing_by_email.items():
        if email_key not in seen_emails and stk.is_active:
            stk.is_active = False

    session.commit()

    # Process incoming CRM activities
    for act_item in crm_activities:
        if isinstance(act_item, dict):
            act_type = act_item.get("activity_type") or "Note"
            summary = act_item.get("summary") or ""
            occ = act_item.get("occurred_on")
            stk_id_str = act_item.get("stakeholder_id")
        else:
            act_type = getattr(act_item, "activity_type", "Note")
            summary = getattr(act_item, "summary", "")
            occ = getattr(act_item, "occurred_on", None)
            stk_id_str = getattr(act_item, "stakeholder_id", None)

        if not summary:
            continue

        target_stk: Stakeholder | None = None
        if stk_id_str:
            try:
                stk_uuid = _parse_uuid(stk_id_str, "stakeholder_id")
                target_stk = session.get(Stakeholder, stk_uuid)
            except InvalidStakeholderDataError:
                target_stk = None

        if target_stk is None and existing_records:
            # Fall back to first stakeholder if unlinked
            target_stk = existing_records[0]

        if target_stk is None:
            continue

        try:
            add_stakeholder_activity(
                session=session,
                deal_id=deal_uuid,
                stakeholder_id=target_stk.id,
                activity_type=act_type,
                summary=summary,
                occurred_on=occ,
            )
        except InvalidStakeholderDataError:
            continue

    return get_stakeholder_map_for_deal(session, deal_uuid)


class StakeholderService:
    """Service class encapsulating stakeholder contact mapping and activity feed operations."""

    def __init__(self, session: Session) -> None:
        """Initialize StakeholderService with an active database session."""
        self.session = session

    def create_stakeholder(
        self,
        deal_id: UUID | str,
        full_name: str,
        email: str,
        org_role: str,
        influence_level: str,
        is_active: bool = True,
    ) -> StakeholderDomain:
        """Create a new stakeholder contact for a deal."""
        return create_stakeholder(
            self.session, deal_id, full_name, email, org_role, influence_level, is_active
        )

    def get_stakeholder_by_id(self, stakeholder_id: UUID | str) -> StakeholderDomain:
        """Get stakeholder domain entity by ID."""
        return get_stakeholder_by_id(self.session, stakeholder_id)

    def get_stakeholders_for_deal(
        self,
        deal_id: UUID | str,
        active_only: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[StakeholderDomain]:
        """Get mapped stakeholders for a deal."""
        return get_stakeholders_for_deal(
            self.session, deal_id, active_only=active_only, limit=limit, offset=offset
        )

    def update_stakeholder(
        self,
        stakeholder_id: UUID | str,
        full_name: str | None = None,
        email: str | None = None,
        org_role: str | None = None,
        influence_level: str | None = None,
        is_active: bool | None = None,
    ) -> StakeholderDomain:
        """Update stakeholder fields."""
        return update_stakeholder(
            self.session,
            stakeholder_id,
            full_name=full_name,
            email=email,
            org_role=org_role,
            influence_level=influence_level,
            is_active=is_active,
        )

    def deactivate_stakeholder(self, stakeholder_id: UUID | str) -> StakeholderDomain:
        """Soft-deactivate a stakeholder."""
        return deactivate_stakeholder(self.session, stakeholder_id)

    def delete_stakeholder(self, stakeholder_id: UUID | str) -> None:
        """Permanently delete a stakeholder."""
        return delete_stakeholder(self.session, stakeholder_id)

    def add_activity(
        self,
        deal_id: UUID | str,
        stakeholder_id: UUID | str,
        activity_type: str,
        summary: str,
        occurred_on: datetime | str | None = None,
    ) -> StakeholderActivityDomain:
        """Log a new CRM interaction activity."""
        return add_stakeholder_activity(
            self.session, deal_id, stakeholder_id, activity_type, summary, occurred_on
        )

    def get_activities_for_deal(
        self,
        deal_id: UUID | str,
        stakeholder_id: UUID | str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[StakeholderActivityDomain]:
        """Get chronological activity feed for a deal."""
        return get_activities_for_deal(
            self.session, deal_id, stakeholder_id=stakeholder_id, limit=limit, offset=offset
        )

    def get_activities_for_stakeholder(
        self,
        stakeholder_id: UUID | str,
        limit: int | None = None,
    ) -> list[StakeholderActivityDomain]:
        """Get activities for a specific stakeholder."""
        return get_activities_for_stakeholder(self.session, stakeholder_id, limit=limit)

    def get_stakeholder_map(
        self,
        deal_id: UUID | str,
        active_only: bool = False,
        activity_limit: int | None = 50,
    ) -> StakeholderMapResultDTO:
        """Get full stakeholder map and activity feed for a deal."""
        return get_stakeholder_map_for_deal(
            self.session, deal_id, active_only=active_only, activity_limit=activity_limit
        )

    def sync_from_crm(
        self,
        deal_id: UUID | str,
        crm_stakeholders: list[Any],
        crm_activities: list[Any],
    ) -> StakeholderMapResultDTO:
        """Synchronize stakeholders and activities from external CRM payload."""
        return sync_crm_stakeholders_and_activities(
            self.session, deal_id, crm_stakeholders, crm_activities
        )
