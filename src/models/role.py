import uuid

from sqlalchemy import ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.database import Base
from src.models.tenant import Tenant
from src.models.user import User


class Role(Base):
    """ORM model representing a security role for RBAC lookup."""

    __tablename__ = "role"

    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<Role(role_id={self.role_id!r}, name={self.name!r})>"


class UserRole(Base):
    """ORM model mapping a user to a role within a specific tenant context."""

    __tablename__ = "user_role"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("user.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("role.role_id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenant.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )

    user: Mapped[User] = relationship("User")
    role: Mapped[Role] = relationship("Role")
    tenant: Mapped[Tenant] = relationship("Tenant")

    def __repr__(self) -> str:
        return (
            f"<UserRole(user_id={self.user_id!r}, "
            f"role_id={self.role_id!r}, "
            f"tenant_id={self.tenant_id!r})>"
        )
