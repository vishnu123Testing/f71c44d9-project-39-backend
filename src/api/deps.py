"""FastAPI security and authentication dependencies.

Provides bearer token authentication, current user context resolution, database session
injection, and server-side RBAC / tenant boundary enforcement dependencies.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from src.lib.config import settings
from src.models.database import get_db
from src.models.user import User
from src.services.access_control.audit import (
    AuditAction,
    AuditOutcome,
    log_audit_event,
)
from src.services.access_control.rbac import (
    evaluate_access,
    get_user_roles,
    verify_tenant_boundary,
)

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass
class CurrentUser:
    """Dataclass holding context for an authenticated caller."""

    user_id: uuid.UUID
    email: str
    tenant_id: uuid.UUID
    roles: list[str]
    user: User


def create_access_token(
    subject: str | uuid.UUID,
    tenant_id: str | uuid.UUID,
    email: str | None = None,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Generate a signed JWT bearer access token."""
    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode: dict[str, Any] = {
        "sub": str(subject),
        "tenant_id": str(tenant_id),
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    if email:
        to_encode["email"] = email
    if extra_claims:
        to_encode.update(extra_claims)

    return jwt.encode(
        to_encode,
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT bearer access token.

    Raises HTTP 401 Unauthorized if token is expired, invalid, or malformed.
    """
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
        return payload
    except (jwt.ExpiredSignatureError, jwt.PyJWTError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication credentials were not provided or are invalid.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> CurrentUser:
    """FastAPI dependency resolving the authenticated caller from the Bearer token."""
    token: str | None = None
    if credentials and credentials.credentials:
        token = credentials.credentials
    else:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1].strip()

    if not token:
        log_audit_event(
            action=AuditAction.ACCESS_EVALUATE,
            outcome_status=AuditOutcome.DENIED,
            client_ip=request.client.host if request.client else None,
            details={"reason": "Missing Bearer token in Authorization header"},
            db=db,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication credentials were not provided or are invalid.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(token)

    sub = payload.get("sub")
    tenant_id_str = payload.get("tenant_id")
    if not sub or not tenant_id_str:
        log_audit_event(
            action=AuditAction.ACCESS_EVALUATE,
            outcome_status=AuditOutcome.DENIED,
            client_ip=request.client.host if request.client else None,
            details={"reason": "Missing sub or tenant_id claims in token payload"},
            db=db,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication credentials were not provided or are invalid.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_uuid = uuid.UUID(str(sub))
        tenant_uuid = uuid.UUID(str(tenant_id_str))
    except (ValueError, TypeError):
        log_audit_event(
            action=AuditAction.ACCESS_EVALUATE,
            outcome_status=AuditOutcome.DENIED,
            client_ip=request.client.host if request.client else None,
            details={"reason": "Invalid UUID format in token claims"},
            db=db,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication credentials were not provided or are invalid.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = (
        db.query(User)
        .filter(User.user_id == user_uuid, User.tenant_id == tenant_uuid)
        .first()
    )

    if not user:
        log_audit_event(
            action=AuditAction.ACCESS_EVALUATE,
            outcome_status=AuditOutcome.DENIED,
            user_id=user_uuid,
            tenant_id=tenant_uuid,
            client_ip=request.client.host if request.client else None,
            details={"reason": "User or tenant association not found in database"},
            db=db,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication credentials were not provided or are invalid.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    roles = get_user_roles(db, user_id=user.user_id, tenant_id=user.tenant_id)

    return CurrentUser(
        user_id=user.user_id,
        email=user.email,
        tenant_id=user.tenant_id,
        roles=roles,
        user=user,
    )


def get_current_active_user(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Dependency verifying current user is active."""
    return current_user


class RequirePermission:
    """FastAPI security dependency for enforcing server-side RBAC and tenant boundaries."""

    def __init__(
        self,
        action: str,
        resource: str = "*",
        target_tenant_param: str | None = None,
    ) -> None:
        self.action = action
        self.resource = resource
        self.target_tenant_param = target_tenant_param

    def __call__(
        self,
        request: Request,
        current_user: CurrentUser = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> CurrentUser:
        target_tenant_id: uuid.UUID | str | None = None

        if self.target_tenant_param:
            if self.target_tenant_param in request.path_params:
                target_tenant_id = request.path_params[self.target_tenant_param]
            elif self.target_tenant_param in request.query_params:
                target_tenant_id = request.query_params[self.target_tenant_param]
            elif (
                hasattr(request, "headers")
                and self.target_tenant_param in request.headers
            ):
                target_tenant_id = request.headers[self.target_tenant_param]

        eval_result = evaluate_access(
            caller_tenant_id=current_user.tenant_id,
            target_tenant_id=target_tenant_id,
            user_roles=current_user.roles,
            action=self.action,
            resource=self.resource,
        )

        if not eval_result.allowed:
            is_cross_tenant = (
                target_tenant_id is not None
                and not verify_tenant_boundary(
                    current_user.tenant_id, target_tenant_id
                )
            )

            audit_action = (
                AuditAction.ROLE_EVALUATION_FAILURE
                if not is_cross_tenant
                else AuditAction.ACCESS_DENIED
            )

            log_audit_event(
                action=audit_action,
                outcome_status=AuditOutcome.FORBIDDEN,
                user_id=current_user.user_id,
                assigned_role=",".join(current_user.roles)
                if current_user.roles
                else "None",
                tenant_id=current_user.tenant_id,
                resource_id=str(target_tenant_id)
                if target_tenant_id
                else self.resource,
                client_ip=request.client.host if request.client else None,
                details={
                    "reason": eval_result.reason,
                    "resource": self.resource,
                    "action": self.action,
                },
                db=db,
            )

            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access to the requested resource is denied for current role and entity.",
            )

        return current_user


class RequireRole:
    """FastAPI security dependency requiring specific role name(s)."""

    def __init__(self, allowed_roles: list[str] | str) -> None:
        if isinstance(allowed_roles, str):
            self.allowed_roles = [allowed_roles]
        else:
            self.allowed_roles = allowed_roles

    def __call__(
        self,
        request: Request,
        current_user: CurrentUser = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> CurrentUser:
        has_allowed_role = any(
            role in self.allowed_roles for role in current_user.roles
        )
        if not has_allowed_role:
            log_audit_event(
                action=AuditAction.ROLE_EVALUATION_FAILURE,
                outcome_status=AuditOutcome.FORBIDDEN,
                user_id=current_user.user_id,
                assigned_role=",".join(current_user.roles)
                if current_user.roles
                else "None",
                tenant_id=current_user.tenant_id,
                resource_id="role_check",
                client_ip=request.client.host if request.client else None,
                details={
                    "reason": f"Required roles {self.allowed_roles}, user has {current_user.roles}"
                },
                db=db,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access to the requested resource is denied for current role and entity.",
            )
        return current_user


def enforce_tenant_scope(
    caller_tenant_id: uuid.UUID | str,
    target_tenant_id: uuid.UUID | str,
    request: Request | None = None,
    db: Session | None = None,
    user_id: uuid.UUID | str | None = None,
) -> None:
    """Verify tenant boundary, raising HTTP 403 Forbidden on violation."""
    if not verify_tenant_boundary(caller_tenant_id, target_tenant_id):
        log_audit_event(
            action=AuditAction.ACCESS_DENIED,
            outcome_status=AuditOutcome.FORBIDDEN,
            user_id=user_id,
            tenant_id=caller_tenant_id,
            resource_id=str(target_tenant_id),
            client_ip=request.client.host if request and request.client else None,
            details={
                "reason": (
                    f"Cross-tenant access attempt: caller tenant '{caller_tenant_id}' "
                    f"vs target tenant '{target_tenant_id}'"
                )
            },
            db=db,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access to the requested resource is denied for current role and entity.",
        )
