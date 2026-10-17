"""Non-blocking, fail-safe structured audit logging service.

Emits formatted JSON audit events to stdout with correlation metadata and
optionally persists audit log entries to the database via SQLAlchemy.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import sys
from typing import Any
import uuid

from sqlalchemy.orm import Session

from src.lib.logging import get_correlation_id
from src.models.audit import AuditLog

logger = logging.getLogger("audit")


class AuditOutcome(str, Enum):
    """Standard audit event outcome status codes."""

    SUCCESS = "SUCCESS"
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"
    FORBIDDEN = "FORBIDDEN"
    ERROR = "ERROR"


class AuditAction(str, Enum):
    """Standard audit action identifiers."""

    AUTH_LOGIN = "auth.login"
    AUTH_REGISTER = "auth.register"
    AUTH_LOGOUT = "auth.logout"
    ACCESS_EVALUATE = "access.evaluate"
    ACCESS_GRANTED = "access.granted"
    ACCESS_DENIED = "access.denied"
    ROLE_EVALUATION_FAILURE = "role.eval_failure"
    RECORD_CREATE = "record.create"
    RECORD_READ = "record.read"
    RECORD_UPDATE = "record.update"
    RECORD_DELETE = "record.delete"


SENSITIVE_KEYS = {
    "password",
    "pass",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "authorization",
    "api_key",
    "credentials",
    "private_key",
}


def sanitize_details(details: dict[str, Any] | None) -> dict[str, Any] | None:
    """Mask sensitive fields in detail dictionary."""
    if details is None:
        return None

    sanitized: dict[str, Any] = {}
    for key, value in details.items():
        if isinstance(key, str) and any(
            sensitive in key.lower() for sensitive in SENSITIVE_KEYS
        ):
            sanitized[key] = "***"
        elif isinstance(value, dict):
            sanitized[key] = sanitize_details(value)
        else:
            sanitized[key] = value
    return sanitized


def parse_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    """Safely parse a UUID from str or UUID object."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


@dataclass
class AuditEvent:
    """Structured representation of a system audit log event."""

    action: str
    outcome_status: str
    event_id: uuid.UUID = field(default_factory=uuid.uuid4)
    logged_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    user_id: uuid.UUID | None = None
    assigned_role: str | None = None
    tenant_id: uuid.UUID | None = None
    resource_id: str | None = None
    client_ip: str | None = None
    correlation_id: uuid.UUID | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert AuditEvent to dictionary with ISO timestamps and stringified UUIDs."""
        data: dict[str, Any] = {
            "event_id": str(self.event_id),
            "logged_at": self.logged_at.isoformat(),
            "action": self.action,
            "outcome_status": self.outcome_status,
            "user_id": str(self.user_id) if self.user_id else None,
            "assigned_role": self.assigned_role,
            "tenant_id": str(self.tenant_id) if self.tenant_id else None,
            "resource_id": self.resource_id,
            "client_ip": self.client_ip,
            "correlation_id": str(self.correlation_id)
            if self.correlation_id
            else None,
        }
        if self.details:
            data["details"] = sanitize_details(self.details)
        return data

    def to_json(self) -> str:
        """Serialize AuditEvent to JSON string."""
        return json.dumps(self.to_dict(), default=str)


def log_audit_event(
    action: str,
    outcome_status: str,
    user_id: uuid.UUID | str | None = None,
    assigned_role: str | None = None,
    tenant_id: uuid.UUID | str | None = None,
    resource_id: str | None = None,
    client_ip: str | None = None,
    correlation_id: uuid.UUID | str | None = None,
    db: Session | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent | None:
    """Log a non-blocking structured audit event to stdout and optional database storage.

    Guaranteed fail-safe: any error encountered during formatting, IO, or persistence
    is trapped and swallowed so request processing is never disrupted.
    """
    try:
        parsed_user_id = parse_uuid(user_id)
        parsed_tenant_id = parse_uuid(tenant_id)

        # Resolve correlation ID from context if not explicitly provided
        parsed_corr_id = parse_uuid(correlation_id)
        if parsed_corr_id is None:
            ctx_corr_id = get_correlation_id()
            if ctx_corr_id:
                parsed_corr_id = parse_uuid(ctx_corr_id)

        sanitized_det = sanitize_details(details)

        event = AuditEvent(
            action=action,
            outcome_status=outcome_status,
            user_id=parsed_user_id,
            assigned_role=assigned_role,
            tenant_id=parsed_tenant_id,
            resource_id=resource_id,
            client_ip=client_ip,
            correlation_id=parsed_corr_id,
            details=sanitized_det,
        )

        # 1. Emit JSON audit log to stdout stream
        json_output = event.to_json()
        sys.stdout.write(f"{json_output}\n")
        sys.stdout.flush()

        # Also emit to logger
        logger.info(json_output)

        # 2. Synchronous DB persistence if database session provided
        if db is not None:
            try:
                audit_record = AuditLog(
                    audit_id=event.event_id,
                    logged_at=event.logged_at,
                    user_id=event.user_id,
                    assigned_role=event.assigned_role,
                    tenant_id=event.tenant_id,
                    action=event.action,
                    resource_id=event.resource_id,
                    client_ip=event.client_ip,
                    outcome_status=event.outcome_status,
                    correlation_id=event.correlation_id,
                )
                db.add(audit_record)
                db.commit()
            except Exception as db_exc:
                db.rollback()
                sys.stderr.write(
                    f"[AuditService] DB persistence failed safely: {db_exc}\n"
                )

        return event

    except Exception as exc:
        sys.stderr.write(
            f"[AuditService] Fail-safe trap engaged. Audit log error: {exc}\n"
        )
        return None


def get_tenant_audit_logs(
    db: Session,
    tenant_id: uuid.UUID | str,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[AuditLog], int]:
    """Retrieve audit log entries for a specific tenant boundary."""
    parsed_tenant_id = parse_uuid(tenant_id)
    if not parsed_tenant_id:
        return [], 0

    query = db.query(AuditLog).filter(AuditLog.tenant_id == parsed_tenant_id)
    total_count = query.count()

    logs = (
        query.order_by(AuditLog.logged_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return logs, total_count


class AuditService:
    """Service wrapper for audit logging operations."""

    @staticmethod
    def log_event(
        action: str,
        outcome_status: str,
        user_id: uuid.UUID | str | None = None,
        assigned_role: str | None = None,
        tenant_id: uuid.UUID | str | None = None,
        resource_id: str | None = None,
        client_ip: str | None = None,
        correlation_id: uuid.UUID | str | None = None,
        db: Session | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent | None:
        """Static method wrapper for log_audit_event."""
        return log_audit_event(
            action=action,
            outcome_status=outcome_status,
            user_id=user_id,
            assigned_role=assigned_role,
            tenant_id=tenant_id,
            resource_id=resource_id,
            client_ip=client_ip,
            correlation_id=correlation_id,
            db=db,
            details=details,
        )

    @staticmethod
    def get_logs(
        db: Session,
        tenant_id: uuid.UUID | str,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[AuditLog], int]:
        """Static method wrapper for get_tenant_audit_logs."""
        return get_tenant_audit_logs(
            db=db, tenant_id=tenant_id, limit=limit, offset=offset
        )
