"""Authentication endpoints for login, registration, and active identity inspection."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.deps import CurrentUser, create_access_token, get_current_user
from src.models.database import get_db
from src.models.role import Role, UserRole
from src.models.tenant import Tenant
from src.models.user import User
from src.services.access_control.audit import (
    AuditAction,
    AuditOutcome,
    log_audit_event,
)
from src.services.access_control.rbac import RoleName, get_user_roles
from src.services.security import get_password_hash, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Schema for user login credentials payload."""

    email: EmailStr
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    """Schema for user login success response containing bearer token."""

    access_token: str
    token_type: str = "bearer"
    user_id: uuid.UUID
    tenant_id: uuid.UUID


class RegisterRequest(BaseModel):
    """Schema for user registration request payload."""

    email: EmailStr
    password: str = Field(..., min_length=1)
    tenant_id: uuid.UUID | None = None


class RegisterResponse(BaseModel):
    """Schema for user registration success response."""

    user_id: uuid.UUID
    email: str
    tenant_id: uuid.UUID


class UserMeResponse(BaseModel):
    """Schema for active identity lookup response."""

    user_id: uuid.UUID
    email: str
    tenant_id: uuid.UUID
    roles: list[str]


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
)
def login(
    request: Request,
    payload: LoginRequest,
    db: Session = Depends(get_db),
) -> LoginResponse:
    """Authenticate caller credentials, issue JWT access token, and emit audit event."""
    user = db.query(User).filter(User.email == payload.email).first()

    if not user or not verify_password(payload.password, user.password_hash):
        log_audit_event(
            action=AuditAction.AUTH_LOGIN,
            outcome_status=AuditOutcome.DENIED,
            user_id=user.user_id if user else None,
            tenant_id=user.tenant_id if user else None,
            client_ip=request.client.host if request.client else None,
            details={"reason": "Invalid credentials", "email": payload.email},
            db=db,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication credentials were not provided or are invalid.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    roles = get_user_roles(db, user_id=user.user_id, tenant_id=user.tenant_id)
    assigned_role_str = ",".join(roles) if roles else None

    token = create_access_token(
        subject=user.user_id,
        tenant_id=user.tenant_id,
        email=user.email,
    )

    log_audit_event(
        action=AuditAction.AUTH_LOGIN,
        outcome_status=AuditOutcome.SUCCESS,
        user_id=user.user_id,
        assigned_role=assigned_role_str,
        tenant_id=user.tenant_id,
        client_ip=request.client.host if request.client else None,
        db=db,
    )

    return LoginResponse(
        access_token=token,
        token_type="bearer",
        user_id=user.user_id,
        tenant_id=user.tenant_id,
    )


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    request: Request,
    payload: RegisterRequest,
    db: Session = Depends(get_db),
) -> RegisterResponse:
    """Register a new user with default ClientViewer role and emit audit event."""
    existing_user = db.query(User).filter(User.email == payload.email).first()
    if existing_user:
        log_audit_event(
            action=AuditAction.AUTH_REGISTER,
            outcome_status=AuditOutcome.DENIED,
            user_id=existing_user.user_id,
            tenant_id=existing_user.tenant_id,
            client_ip=request.client.host if request.client else None,
            details={
                "reason": "Duplicate email registration attempt",
                "email": payload.email,
            },
            db=db,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User with this email already exists.",
        )

    # Resolve or create tenant
    if payload.tenant_id is not None:
        tenant = (
            db.query(Tenant).filter(Tenant.tenant_id == payload.tenant_id).first()
        )
        if not tenant:
            tenant = Tenant(
                tenant_id=payload.tenant_id,
                name=f"Tenant-{payload.tenant_id}",
            )
            db.add(tenant)
            db.flush()
    else:
        tenant = Tenant(name=f"Tenant for {payload.email}")
        db.add(tenant)
        db.flush()

    # Resolve default role (ClientViewer)
    role = (
        db.query(Role)
        .filter(Role.name == RoleName.CLIENT_VIEWER.value)
        .first()
    )
    if not role:
        role = Role(
            name=RoleName.CLIENT_VIEWER.value,
            description="Client Viewer Role",
        )
        db.add(role)
        db.flush()

    # Create new user and assign default role
    hashed_pwd = get_password_hash(payload.password)
    new_user = User(
        email=payload.email,
        password_hash=hashed_pwd,
        tenant_id=tenant.tenant_id,
    )
    db.add(new_user)

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        log_audit_event(
            action=AuditAction.AUTH_REGISTER,
            outcome_status=AuditOutcome.DENIED,
            client_ip=request.client.host if request.client else None,
            details={
                "reason": "Integrity error on user registration",
                "email": payload.email,
            },
            db=db,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User with this email already exists.",
        )

    user_role = UserRole(
        user_id=new_user.user_id,
        role_id=role.role_id,
        tenant_id=tenant.tenant_id,
    )
    db.add(user_role)

    try:
        db.commit()
        db.refresh(new_user)
    except Exception as exc:
        db.rollback()
        log_audit_event(
            action=AuditAction.AUTH_REGISTER,
            outcome_status=AuditOutcome.ERROR,
            client_ip=request.client.host if request.client else None,
            details={"reason": f"Database commit failed: {exc}"},
            db=db,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating user.",
        ) from exc

    log_audit_event(
        action=AuditAction.AUTH_REGISTER,
        outcome_status=AuditOutcome.SUCCESS,
        user_id=new_user.user_id,
        assigned_role=RoleName.CLIENT_VIEWER.value,
        tenant_id=new_user.tenant_id,
        client_ip=request.client.host if request.client else None,
        db=db,
    )

    return RegisterResponse(
        user_id=new_user.user_id,
        email=new_user.email,
        tenant_id=new_user.tenant_id,
    )


@router.get(
    "/me",
    response_model=UserMeResponse,
    status_code=status.HTTP_200_OK,
)
def get_me(
    current_user: CurrentUser = Depends(get_current_user),
) -> UserMeResponse:
    """Retrieve active caller identity details and assigned roles."""
    return UserMeResponse(
        user_id=current_user.user_id,
        email=current_user.email,
        tenant_id=current_user.tenant_id,
        roles=current_user.roles,
    )
