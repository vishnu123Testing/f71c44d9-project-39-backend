"""FastAPI router and Pydantic input/output schemas for alerting and notification management.

Provides endpoints for trigger rules, optimistic-locked user notification preferences,
and in-app alerts with RBAC enforcement and input sanitization [FR-5, FR-6, FR-7, FR-9, FR-10].
"""

from datetime import datetime, timezone
import html
import logging
import re
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.deps import CurrentUser, RequirePermission
from src.lib.logging import get_correlation_id
from src.models.alerting import (
    AlertTrigger,
    AlertUrgency,
    NotificationChannel,
    NotificationLog,
    NotificationPreference,
)
from src.models.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])


def sanitize_input_string(v: str | None) -> str | None:
    """Sanitize and clean raw user string inputs.

    Strips leading/trailing whitespace, strips script/HTML markup tags,
    escapes special HTML characters, and removes control characters.
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


# ---------------------------------------------------------------------------
# Pydantic Schemas - Request and Response Models
# ---------------------------------------------------------------------------


class InAppAlertResponse(BaseModel):
    """Response schema representing an in-app alert notification [FR-5]."""

    notification_id: UUID = Field(..., description="Unique notification log UUID")
    recipient_id: UUID | None = Field(default=None, description="Recipient user UUID if assigned")
    trigger_id: UUID | None = Field(default=None, description="Associated alert trigger UUID if linked")
    urgency: str = Field(..., description="Alert urgency level ('Low', 'Medium', 'High')")
    channel: str = Field(..., description="Notification channel (e.g. 'InApp')")
    delivery_status: str = Field(..., description="Delivery status ('Sent', 'Failed', 'Fallback')")
    message: str | None = Field(default=None, description="Notification message body")
    read_status: bool = Field(..., description="Read status flag")
    dispatched_at: datetime = Field(..., description="Dispatch UTC timestamp")
    correlation_id: str = Field(..., description="Context correlation ID")

    @classmethod
    def from_orm_model(cls, model: NotificationLog) -> "InAppAlertResponse":
        """Construct InAppAlertResponse from NotificationLog ORM entity."""
        return cls(
            notification_id=model.notification_id,
            recipient_id=model.recipient_id,
            trigger_id=model.trigger_id,
            urgency=model.urgency,
            channel=model.channel,
            delivery_status=model.delivery_status,
            message=model.message,
            read_status=model.read_status,
            dispatched_at=model.dispatched_at,
            correlation_id=model.correlation_id,
        )


class TriggerCreateRequest(BaseModel):
    """Request payload for creating a new financial alert trigger rule [FR-6, FR-10]."""

    rule_name: str = Field(..., min_length=1, max_length=100, description="Unique rule name")
    financial_criteria: str = Field(..., min_length=1, max_length=50, description="Financial criteria identifier")
    threshold_amount: float = Field(..., ge=0.0, description="Trigger threshold amount")
    urgency: str = Field(default=AlertUrgency.MEDIUM.value, description="Alert urgency ('Low', 'Medium', 'High')")
    is_active: bool = Field(default=True, description="Whether rule is active")

    @field_validator("rule_name", "financial_criteria", mode="before")
    @classmethod
    def sanitize_text_fields(cls, v: Any) -> Any:
        """Sanitize text input fields."""
        if isinstance(v, str):
            sanitized = sanitize_input_string(v)
            if not sanitized:
                raise ValueError("Field cannot be empty after sanitization")
            return sanitized
        return v

    @field_validator("urgency")
    @classmethod
    def validate_urgency(cls, v: str) -> str:
        """Validate and normalize urgency enum values."""
        clean_val = sanitize_input_string(v) or v
        valid_values = [e.value for e in AlertUrgency]
        for val in valid_values:
            if val.lower() == clean_val.lower():
                return val
        raise ValueError(f"Invalid urgency level '{v}'. Valid levels: {valid_values}")


class TriggerUpdateRequest(BaseModel):
    """Request payload for updating an existing financial alert trigger rule [FR-6, FR-10]."""

    rule_name: str | None = Field(default=None, min_length=1, max_length=100, description="Updated rule name")
    financial_criteria: str | None = Field(default=None, min_length=1, max_length=50, description="Updated criteria")
    threshold_amount: float | None = Field(default=None, ge=0.0, description="Updated threshold amount")
    urgency: str | None = Field(default=None, description="Updated urgency level")
    is_active: bool | None = Field(default=None, description="Updated active state")

    @field_validator("rule_name", "financial_criteria", mode="before")
    @classmethod
    def sanitize_text_fields(cls, v: Any) -> Any:
        """Sanitize text input fields if provided."""
        if isinstance(v, str):
            sanitized = sanitize_input_string(v)
            if not sanitized:
                raise ValueError("Field cannot be empty after sanitization")
            return sanitized
        return v

    @field_validator("urgency")
    @classmethod
    def validate_urgency(cls, v: str | None) -> str | None:
        """Validate urgency enum level if provided."""
        if v is None:
            return None
        clean_val = sanitize_input_string(v) or v
        valid_values = [e.value for e in AlertUrgency]
        for val in valid_values:
            if val.lower() == clean_val.lower():
                return val
        raise ValueError(f"Invalid urgency level '{v}'. Valid levels: {valid_values}")


class TriggerResponse(BaseModel):
    """Response schema representing a financial alert trigger rule [FR-6]."""

    trigger_id: UUID = Field(..., description="Unique trigger UUID")
    rule_name: str = Field(..., description="Rule name")
    financial_criteria: str = Field(..., description="Financial criteria identifier")
    threshold_amount: float = Field(..., description="Threshold amount")
    urgency: str = Field(..., description="Urgency level ('Low', 'Medium', 'High')")
    is_active: bool = Field(..., description="Active flag")
    created_at: datetime = Field(..., description="Creation UTC timestamp")
    updated_at: datetime = Field(..., description="Last update UTC timestamp")

    @classmethod
    def from_orm_model(cls, model: AlertTrigger) -> "TriggerResponse":
        """Construct TriggerResponse from AlertTrigger ORM entity."""
        return cls(
            trigger_id=model.trigger_id,
            rule_name=model.rule_name,
            financial_criteria=model.financial_criteria,
            threshold_amount=float(model.threshold_amount),
            urgency=model.urgency,
            is_active=model.is_active,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class PreferenceUpdateRequest(BaseModel):
    """Request payload for updating user notification preferences with optimistic locking [FR-7, FR-10]."""

    role_name: str = Field(..., min_length=1, max_length=50, description="Target role name (e.g. 'Admin')")
    event_type: str = Field(..., min_length=1, max_length=50, description="Event type string")
    urgency: str = Field(default=AlertUrgency.MEDIUM.value, description="Urgency level ('Low', 'Medium', 'High')")
    channels_json: list[str] = Field(..., description="List of enabled channels, e.g. ['InApp', 'Email']")
    version: int = Field(..., ge=1, description="Current expected version for optimistic locking")

    @field_validator("role_name", "event_type", "urgency", mode="before")
    @classmethod
    def sanitize_text_fields(cls, v: Any) -> Any:
        """Sanitize text input fields."""
        if isinstance(v, str):
            sanitized = sanitize_input_string(v)
            if not sanitized:
                raise ValueError("Field cannot be empty after sanitization")
            return sanitized
        return v

    @field_validator("urgency")
    @classmethod
    def validate_urgency(cls, v: str) -> str:
        """Validate and normalize urgency enum values."""
        clean_val = sanitize_input_string(v) or v
        valid_values = [e.value for e in AlertUrgency]
        for val in valid_values:
            if val.lower() == clean_val.lower():
                return val
        raise ValueError(f"Invalid urgency level '{v}'. Valid levels: {valid_values}")

    @field_validator("channels_json")
    @classmethod
    def validate_channels(cls, v: list[str]) -> list[str]:
        """Validate and normalize channel string list."""
        valid_channels = [e.value for e in NotificationChannel]
        cleaned: list[str] = []
        for ch in v:
            ch_clean = sanitize_input_string(ch) or ch
            matched = False
            for val in valid_channels:
                if val.lower() == ch_clean.lower():
                    cleaned.append(val)
                    matched = True
                    break
            if not matched:
                raise ValueError(f"Invalid channel '{ch}'. Valid channels: {valid_channels}")
        return cleaned


class PreferenceResponse(BaseModel):
    """Response schema representing role-based notification preferences [FR-7]."""

    preference_id: UUID = Field(..., description="Unique preference UUID")
    role_name: str = Field(..., description="Role name")
    event_type: str = Field(..., description="Event type")
    urgency: str = Field(..., description="Urgency level")
    channels_json: list[str] = Field(..., description="Configured channel list")
    version: int = Field(..., description="Optimistic locking version integer")
    created_at: datetime = Field(..., description="Creation UTC timestamp")
    updated_at: datetime = Field(..., description="Last update UTC timestamp")

    @classmethod
    def from_orm_model(cls, model: NotificationPreference) -> "PreferenceResponse":
        """Construct PreferenceResponse from NotificationPreference ORM entity."""
        channels = model.channels_json if isinstance(model.channels_json, list) else []
        return cls(
            preference_id=model.preference_id,
            role_name=model.role_name,
            event_type=model.event_type,
            urgency=model.urgency,
            channels_json=channels,
            version=model.version,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


# ---------------------------------------------------------------------------
# API Routes: Centralized Notification Hub & In-App Alerts [FR-5]
# ---------------------------------------------------------------------------


@router.get(
    "/in-app",
    response_model=list[InAppAlertResponse],
    status_code=status.HTTP_200_OK,
    summary="List in-app alerts",
    description="Retrieve in-app notifications from the centralized notification hub [FR-5, FR-9].",
)
def list_in_app_alerts(
    unread_only: bool = Query(default=False, description="Filter for unread in-app alerts only"),
    recipient_id: UUID | None = Query(default=None, description="Optional recipient user UUID filter"),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="read", resource="financials")),
) -> list[InAppAlertResponse]:
    """Retrieve in-app notifications for the caller's tenant context."""
    try:
        stmt = (
            select(NotificationLog)
            .where(NotificationLog.channel == NotificationChannel.IN_APP.value)
            .order_by(NotificationLog.dispatched_at.desc())
        )

        if unread_only:
            stmt = stmt.where(NotificationLog.read_status == False)

        if recipient_id is not None:
            stmt = stmt.where(NotificationLog.recipient_id == recipient_id)

        logs = db.scalars(stmt).all()
        return [InAppAlertResponse.from_orm_model(log) for log in logs]
    except Exception as exc:
        logger.error(
            "Failed to list in-app alerts: %s",
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing in-app alerts.",
        ) from exc


@router.patch(
    "/in-app/{notification_id}/read",
    response_model=InAppAlertResponse,
    status_code=status.HTTP_200_OK,
    summary="Mark in-app alert as read",
    description="Update read status flag on an in-app notification [FR-5, FR-9].",
)
def mark_in_app_alert_read(
    notification_id: UUID,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="read", resource="financials")),
) -> InAppAlertResponse:
    """Mark a specific in-app alert notification as read."""
    try:
        stmt = select(NotificationLog).where(
            NotificationLog.notification_id == notification_id,
            NotificationLog.channel == NotificationChannel.IN_APP.value,
        )
        log_entry = db.scalars(stmt).first()

        if not log_entry:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"In-app notification with ID '{notification_id}' not found.",
            )

        log_entry.read_status = True
        db.commit()
        db.refresh(log_entry)
        return InAppAlertResponse.from_orm_model(log_entry)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Failed to mark notification %s as read: %s",
            notification_id,
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating the notification.",
        ) from exc


# ---------------------------------------------------------------------------
# API Routes: Alert Trigger Rules [FR-6]
# ---------------------------------------------------------------------------


@router.post(
    "/triggers",
    response_model=TriggerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create custom alert trigger rule",
    description="Configure a new custom alert trigger based on financial criteria and thresholds [FR-6, FR-9, FR-10].",
)
def create_trigger_rule(
    payload: TriggerCreateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="write", resource="financials")),
) -> TriggerResponse:
    """Create a new custom financial alert trigger rule."""
    try:
        stmt = select(AlertTrigger).where(AlertTrigger.rule_name == payload.rule_name)
        existing = db.scalars(stmt).first()
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"An alert trigger rule with name '{payload.rule_name}' already exists.",
            )

        now = datetime.now(timezone.utc)
        trigger = AlertTrigger(
            trigger_id=uuid4(),
            rule_name=payload.rule_name,
            financial_criteria=payload.financial_criteria,
            threshold_amount=payload.threshold_amount,
            urgency=payload.urgency,
            is_active=payload.is_active,
            created_at=now,
            updated_at=now,
        )
        db.add(trigger)
        db.commit()
        db.refresh(trigger)
        return TriggerResponse.from_orm_model(trigger)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Failed to create alert trigger rule: %s",
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating the trigger rule.",
        ) from exc


@router.get(
    "/triggers",
    response_model=list[TriggerResponse],
    status_code=status.HTTP_200_OK,
    summary="List alert trigger rules",
    description="Retrieve configured financial alert trigger rules [FR-6, FR-9].",
)
def list_trigger_rules(
    is_active: bool | None = Query(default=None, description="Optional filter for active/inactive trigger rules"),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="read", resource="financials")),
) -> list[TriggerResponse]:
    """List all configured alert trigger rules."""
    try:
        stmt = select(AlertTrigger).order_by(AlertTrigger.created_at.desc())
        if is_active is not None:
            stmt = stmt.where(AlertTrigger.is_active == is_active)

        triggers = db.scalars(stmt).all()
        return [TriggerResponse.from_orm_model(t) for t in triggers]
    except Exception as exc:
        logger.error(
            "Failed to list alert trigger rules: %s",
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing trigger rules.",
        ) from exc


@router.get(
    "/triggers/{trigger_id}",
    response_model=TriggerResponse,
    status_code=status.HTTP_200_OK,
    summary="Get alert trigger rule by ID",
    description="Fetch a single alert trigger rule by its unique UUID [FR-6, FR-9].",
)
def get_trigger_rule(
    trigger_id: UUID,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="read", resource="financials")),
) -> TriggerResponse:
    """Fetch a single alert trigger rule by ID."""
    try:
        stmt = select(AlertTrigger).where(AlertTrigger.trigger_id == trigger_id)
        trigger = db.scalars(stmt).first()
        if not trigger:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Alert trigger rule with ID '{trigger_id}' not found.",
            )
        return TriggerResponse.from_orm_model(trigger)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Failed to fetch trigger rule %s: %s",
            trigger_id,
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching the trigger rule.",
        ) from exc


@router.put(
    "/triggers/{trigger_id}",
    response_model=TriggerResponse,
    status_code=status.HTTP_200_OK,
    summary="Update alert trigger rule",
    description="Update parameters of an existing alert trigger rule [FR-6, FR-9, FR-10].",
)
def update_trigger_rule(
    trigger_id: UUID,
    payload: TriggerUpdateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="write", resource="financials")),
) -> TriggerResponse:
    """Update an existing financial alert trigger rule."""
    try:
        stmt = select(AlertTrigger).where(AlertTrigger.trigger_id == trigger_id)
        trigger = db.scalars(stmt).first()
        if not trigger:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Alert trigger rule with ID '{trigger_id}' not found.",
            )

        if payload.rule_name is not None and payload.rule_name != trigger.rule_name:
            dup_stmt = select(AlertTrigger).where(AlertTrigger.rule_name == payload.rule_name)
            existing_dup = db.scalars(dup_stmt).first()
            if existing_dup is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"An alert trigger rule with name '{payload.rule_name}' already exists.",
                )
            trigger.rule_name = payload.rule_name

        if payload.financial_criteria is not None:
            trigger.financial_criteria = payload.financial_criteria
        if payload.threshold_amount is not None:
            trigger.threshold_amount = payload.threshold_amount
        if payload.urgency is not None:
            trigger.urgency = payload.urgency
        if payload.is_active is not None:
            trigger.is_active = payload.is_active

        trigger.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(trigger)
        return TriggerResponse.from_orm_model(trigger)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Failed to update trigger rule %s: %s",
            trigger_id,
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating the trigger rule.",
        ) from exc


@router.delete(
    "/triggers/{trigger_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Delete alert trigger rule",
    description="Delete a configured financial alert trigger rule by ID [FR-6, FR-9].",
)
def delete_trigger_rule(
    trigger_id: UUID,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="write", resource="financials")),
) -> Response:
    """Delete an alert trigger rule returning 204 No Content with empty response."""
    try:
        stmt = select(AlertTrigger).where(AlertTrigger.trigger_id == trigger_id)
        trigger = db.scalars(stmt).first()
        if not trigger:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Alert trigger rule with ID '{trigger_id}' not found.",
            )

        db.delete(trigger)
        db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Failed to delete trigger rule %s: %s",
            trigger_id,
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while deleting the trigger rule.",
        ) from exc


# ---------------------------------------------------------------------------
# API Routes: User Notification Preferences (Optimistic Locking) [FR-7]
# ---------------------------------------------------------------------------


@router.put(
    "/preferences",
    response_model=PreferenceResponse,
    status_code=status.HTTP_200_OK,
    summary="Update user notification preferences",
    description="Update role-based notification preferences using optimistic locking via version matching [FR-7, FR-9, FR-10].",
)
def update_notification_preferences(
    payload: PreferenceUpdateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="write", resource="financials")),
) -> PreferenceResponse:
    """Update role-based notification preferences with optimistic locking.

    Raises HTTP 409 Conflict if payload.version does not match the database entity version.
    """
    try:
        stmt = select(NotificationPreference).where(
            NotificationPreference.role_name == payload.role_name,
            NotificationPreference.event_type == payload.event_type,
        )
        pref = db.scalars(stmt).first()

        now = datetime.now(timezone.utc)

        if pref is not None:
            if pref.version != payload.version:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Optimistic lock conflict for preference (role='{payload.role_name}', "
                        f"event_type='{payload.event_type}'): expected version {pref.version}, "
                        f"received version {payload.version}. Please re-fetch and retry."
                    ),
                )

            pref.urgency = payload.urgency
            pref.channels_json = payload.channels_json
            pref.version = pref.version + 1
            pref.updated_at = now
            db.commit()
            db.refresh(pref)
            return PreferenceResponse.from_orm_model(pref)
        else:
            new_pref = NotificationPreference(
                preference_id=uuid4(),
                role_name=payload.role_name,
                event_type=payload.event_type,
                urgency=payload.urgency,
                channels_json=payload.channels_json,
                version=1,
                created_at=now,
                updated_at=now,
            )
            db.add(new_pref)
            db.commit()
            db.refresh(new_pref)
            return PreferenceResponse.from_orm_model(new_pref)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Failed to update notification preferences: %s",
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating notification preferences.",
        ) from exc


@router.get(
    "/preferences",
    response_model=list[PreferenceResponse],
    status_code=status.HTTP_200_OK,
    summary="List notification preferences",
    description="Retrieve configured role-based notification preferences [FR-7, FR-9].",
)
def list_notification_preferences(
    role_name: str | None = Query(default=None, description="Optional role name filter"),
    event_type: str | None = Query(default=None, description="Optional event type filter"),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="read", resource="financials")),
) -> list[PreferenceResponse]:
    """List role-based notification preferences."""
    try:
        stmt = select(NotificationPreference).order_by(
            NotificationPreference.role_name.asc(),
            NotificationPreference.event_type.asc(),
        )

        if role_name is not None:
            clean_role = sanitize_input_string(role_name)
            if clean_role:
                stmt = stmt.where(NotificationPreference.role_name == clean_role)

        if event_type is not None:
            clean_event = sanitize_input_string(event_type)
            if clean_event:
                stmt = stmt.where(NotificationPreference.event_type == clean_event)

        prefs = db.scalars(stmt).all()
        return [PreferenceResponse.from_orm_model(p) for p in prefs]
    except Exception as exc:
        logger.error(
            "Failed to list notification preferences: %s",
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing notification preferences.",
        ) from exc
