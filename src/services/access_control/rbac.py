"""Role-Based Access Control (RBAC) evaluation engine.

This service enforces tenant boundaries and evaluates role permissions for client entity
records and domain sections. Evaluation logic is optimized for sub-20ms overhead.
"""

from dataclasses import dataclass
from enum import Enum
import time
from typing import Any
import uuid

from sqlalchemy.orm import Session

from src.models.role import Role, UserRole


class RBACError(Exception):
    """Base exception for RBAC access control errors."""

    pass


class TenantAccessDeniedError(RBACError):
    """Raised when a cross-tenant access attempt is detected and denied."""

    pass


class PermissionDeniedError(RBACError):
    """Raised when a user lacks required role permissions for an action."""

    pass


class RoleName(str, Enum):
    """Standard system role identifiers."""

    ADMIN = "Admin"
    CLIENT_STANDARD = "ClientStandard"
    CLIENT_VIEWER = "ClientViewer"


# Explicit role permission mappings
# Resource -> Allowed Actions
ROLE_PERMISSIONS: dict[str, dict[str, set[str]]] = {
    RoleName.ADMIN.value: {
        "*": {
            "read",
            "view",
            "write",
            "create",
            "update",
            "delete",
            "admin",
            "manage",
            "interact",
        },
        "audit": {
            "read",
            "view",
            "export",
        },
    },
    RoleName.CLIENT_STANDARD.value: {
        "dashboard": {"read", "view", "write", "create", "update", "interact"},
        "performance": {"read", "view", "write", "create", "update", "interact"},
        "contracts": {"read", "view", "write", "create", "update", "interact"},
        "support": {"read", "view", "write", "create", "update", "interact"},
        "financials": {"read", "view", "write", "create", "update", "interact"},
        "knowledge": {"read", "view", "write", "create", "update", "interact"},
        "client_record": {"read", "view", "write", "create", "update"},
    },
    RoleName.CLIENT_VIEWER.value: {
        "dashboard": {"read", "view"},
        "performance": {"read", "view"},
        "contracts": {"read", "view"},
        "support": {"read", "view"},
        "financials": {"read", "view"},
        "knowledge": {"read", "view"},
        "client_record": {"read", "view"},
    },
}

NAVIGATION_ITEMS: list[dict[str, str]] = [
    {
        "title": "Dashboard",
        "path": "/dashboard",
        "resource": "dashboard",
        "icon_id": "dashboard",
    },
    {
        "title": "Performance",
        "path": "/performance",
        "resource": "performance",
        "icon_id": "performance",
    },
    {
        "title": "Contracts",
        "path": "/contracts",
        "resource": "contracts",
        "icon_id": "contracts",
    },
    {
        "title": "Support",
        "path": "/support",
        "resource": "support",
        "icon_id": "support",
    },
    {
        "title": "Financials",
        "path": "/financials",
        "resource": "financials",
        "icon_id": "financials",
    },
    {
        "title": "Knowledge Base",
        "path": "/knowledge",
        "resource": "knowledge",
        "icon_id": "knowledge",
    },
]


@dataclass
class EvaluationResult:
    """Result payload of an RBAC evaluation check."""

    allowed: bool
    reason: str
    caller_tenant_id: str
    target_tenant_id: str | None
    roles: list[str]
    action: str
    resource: str
    evaluated_at_ms: float


def verify_tenant_boundary(
    caller_tenant_id: uuid.UUID | str | None,
    target_tenant_id: uuid.UUID | str | None,
) -> bool:
    """Verify that caller tenant matches target resource tenant boundary.

    Returns True if caller_tenant_id == target_tenant_id, False otherwise.
    """
    if caller_tenant_id is None or target_tenant_id is None:
        return False
    return str(caller_tenant_id).lower() == str(target_tenant_id).lower()


def enforce_tenant_boundary(
    caller_tenant_id: uuid.UUID | str,
    target_tenant_id: uuid.UUID | str,
) -> None:
    """Enforce tenant boundary, raising TenantAccessDeniedError if cross-tenant attempt."""
    if not verify_tenant_boundary(caller_tenant_id, target_tenant_id):
        raise TenantAccessDeniedError(
            f"Cross-tenant access denied: caller tenant '{caller_tenant_id}' "
            f"cannot access resource tenant '{target_tenant_id}'."
        )


def get_user_roles(
    session: Session,
    user_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str,
) -> list[str]:
    """Retrieve assigned role names for a user within a specific tenant context."""
    user_uuid = (
        user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    )
    tenant_uuid = (
        tenant_id
        if isinstance(tenant_id, uuid.UUID)
        else uuid.UUID(str(tenant_id))
    )

    query = (
        session.query(Role.name)
        .join(UserRole, UserRole.role_id == Role.role_id)
        .filter(UserRole.user_id == user_uuid, UserRole.tenant_id == tenant_uuid)
    )
    results = query.all()
    return [r[0] for r in results]


def has_role_permission(
    roles: list[str],
    action: str,
    resource: str = "*",
) -> bool:
    """Evaluate whether any of the user's roles grant permission for action on resource.

    Evaluated in-memory to meet sub-20ms latency criteria.
    """
    if not roles:
        return False

    normalized_action = action.lower().strip()
    normalized_resource = resource.lower().strip()

    for role_name in roles:
        role_rules = ROLE_PERMISSIONS.get(role_name)
        if not role_rules:
            continue

        # Check exact resource rules first
        if normalized_resource in role_rules:
            allowed_actions = role_rules[normalized_resource]
            if "*" in allowed_actions or normalized_action in allowed_actions:
                return True

        # Check wildcard resource rules
        if "*" in role_rules:
            allowed_actions = role_rules["*"]
            if "*" in allowed_actions or normalized_action in allowed_actions:
                return True

    return False


def evaluate_access(
    caller_tenant_id: uuid.UUID | str,
    target_tenant_id: uuid.UUID | str | None,
    user_roles: list[str],
    action: str,
    resource: str = "*",
) -> EvaluationResult:
    """Evaluate role permissions and tenant boundary access.

    Guaranteed sub-20ms performance under peak load.
    """
    start_time = time.perf_counter()

    caller_tenant_str = str(caller_tenant_id)
    target_tenant_str = str(target_tenant_id) if target_tenant_id is not None else None

    # 1. Enforce tenant boundary if target_tenant_id is specified
    if target_tenant_id is not None:
        if not verify_tenant_boundary(caller_tenant_id, target_tenant_id):
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return EvaluationResult(
                allowed=False,
                reason=(
                    f"Cross-tenant access rejected: caller tenant '{caller_tenant_str}' "
                    f"does not match target tenant '{target_tenant_str}'."
                ),
                caller_tenant_id=caller_tenant_str,
                target_tenant_id=target_tenant_str,
                roles=user_roles,
                action=action,
                resource=resource,
                evaluated_at_ms=elapsed_ms,
            )

    # 2. Evaluate role permissions
    if not has_role_permission(roles=user_roles, action=action, resource=resource):
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        return EvaluationResult(
            allowed=False,
            reason=(
                f"Permission denied: roles {user_roles} lack '{action}' "
                f"permission on resource '{resource}'."
            ),
            caller_tenant_id=caller_tenant_str,
            target_tenant_id=target_tenant_str,
            roles=user_roles,
            action=action,
            resource=resource,
            evaluated_at_ms=elapsed_ms,
        )

    elapsed_ms = (time.perf_counter() - start_time) * 1000.0
    return EvaluationResult(
        allowed=True,
        reason="Access granted.",
        caller_tenant_id=caller_tenant_str,
        target_tenant_id=target_tenant_str,
        roles=user_roles,
        action=action,
        resource=resource,
        evaluated_at_ms=elapsed_ms,
    )


def enforce_access(
    caller_tenant_id: uuid.UUID | str,
    target_tenant_id: uuid.UUID | str | None,
    user_roles: list[str],
    action: str,
    resource: str = "*",
) -> EvaluationResult:
    """Evaluate access and raise appropriate RBAC exceptions if access is denied."""
    result = evaluate_access(
        caller_tenant_id=caller_tenant_id,
        target_tenant_id=target_tenant_id,
        user_roles=user_roles,
        action=action,
        resource=resource,
    )
    if not result.allowed:
        if target_tenant_id is not None and not verify_tenant_boundary(
            caller_tenant_id, target_tenant_id
        ):
            raise TenantAccessDeniedError(result.reason)
        raise PermissionDeniedError(result.reason)
    return result


def get_authorized_navigation_items(roles: list[str]) -> list[dict[str, Any]]:
    """Build navigation menu items marked with accessibility status for given user roles."""
    items: list[dict[str, Any]] = []
    for nav_def in NAVIGATION_ITEMS:
        accessible = has_role_permission(
            roles=roles,
            action="read",
            resource=nav_def["resource"],
        )
        items.append(
            {
                "title": nav_def["title"],
                "path": nav_def["path"],
                "is_accessible": accessible,
                "icon_id": nav_def["icon_id"],
            }
        )
    return items
