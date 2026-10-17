from datetime import date as date_type, datetime, timezone
import uuid

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.database import Base
from src.models.user import User


class Contract(Base):
    """ORM model representing a contract record and document container."""

    __tablename__ = "contracts"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    category: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="DRAFT",
    )
    counterparty: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    start_on: Mapped[date_type | None] = mapped_column(
        Date,
        nullable=True,
    )
    expires_on: Mapped[date_type | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
    )
    parent_contract_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("contracts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    parent_contract: Mapped["Contract | None"] = relationship(
        "Contract",
        remote_side=[id],
        back_populates="child_contracts",
    )
    child_contracts: Mapped[list["Contract"]] = relationship(
        "Contract",
        back_populates="parent_contract",
    )
    clauses: Mapped[list["ContractClause"]] = relationship(
        "ContractClause",
        back_populates="contract",
        cascade="all, delete-orphan",
    )
    slas: Mapped[list["ContractSLA"]] = relationship(
        "ContractSLA",
        back_populates="contract",
        cascade="all, delete-orphan",
    )
    document_versions: Mapped[list["DocumentVersion"]] = relationship(
        "DocumentVersion",
        back_populates="contract",
        cascade="all, delete-orphan",
    )
    amendments: Mapped[list["ContractAmendment"]] = relationship(
        "ContractAmendment",
        back_populates="contract",
        cascade="all, delete-orphan",
    )
    signature_requests: Mapped[list["SignatureRequest"]] = relationship(
        "SignatureRequest",
        back_populates="contract",
        cascade="all, delete-orphan",
    )
    renewal_alerts: Mapped[list["RenewalAlert"]] = relationship(
        "RenewalAlert",
        back_populates="contract",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Contract(id={self.id!r}, title={self.title!r}, category={self.category!r}, status={self.status!r})>"


class ContractClause(Base):
    """ORM model representing specific clauses within a contract."""

    __tablename__ = "contract_clauses"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    clause_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    body_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    is_highlighted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    contract: Mapped[Contract] = relationship(
        "Contract",
        back_populates="clauses",
    )

    def __repr__(self) -> str:
        return f"<ContractClause(id={self.id!r}, contract_id={self.contract_id!r}, clause_type={self.clause_type!r})>"


class ContractSLA(Base):
    """ORM model representing Service Level Agreement metrics and targets."""

    __tablename__ = "contract_slas"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    metric_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    target_value: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    penalty_terms: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    contract: Mapped[Contract] = relationship(
        "Contract",
        back_populates="slas",
    )

    def __repr__(self) -> str:
        return f"<ContractSLA(id={self.id!r}, contract_id={self.contract_id!r}, metric_name={self.metric_name!r})>"


class DocumentVersion(Base):
    """ORM model representing uploaded contract document versions with checksums."""

    __tablename__ = "document_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("user.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    category_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )
    file_path: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
    )
    checksum: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    contract: Mapped[Contract] = relationship(
        "Contract",
        back_populates="document_versions",
    )
    uploaded_by: Mapped[User | None] = relationship("User")

    def __repr__(self) -> str:
        return f"<DocumentVersion(id={self.id!r}, contract_id={self.contract_id!r}, version_number={self.version_number!r})>"


class ContractAmendment(Base):
    """ORM model representing immutable append-only contract amendment history."""

    __tablename__ = "contract_amendments"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("user.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sequence_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    summary: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    contract: Mapped[Contract] = relationship(
        "Contract",
        back_populates="amendments",
    )
    author: Mapped[User | None] = relationship("User")

    def __repr__(self) -> str:
        return f"<ContractAmendment(id={self.id!r}, contract_id={self.contract_id!r}, sequence_number={self.sequence_number!r})>"


class SignatureRequest(Base):
    """ORM model representing digital signature requests and execution status."""

    __tablename__ = "signature_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    signatory_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("user.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="PENDING",
    )
    signature_digest: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    contract: Mapped[Contract] = relationship(
        "Contract",
        back_populates="signature_requests",
    )
    signatory: Mapped[User | None] = relationship("User")

    def __repr__(self) -> str:
        return f"<SignatureRequest(id={self.id!r}, contract_id={self.contract_id!r}, status={self.status!r})>"


class RenewalAlert(Base):
    """ORM model representing dispatched contract renewal alert notifications."""

    __tablename__ = "renewal_alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    threshold_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    dispatched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    contract: Mapped[Contract] = relationship(
        "Contract",
        back_populates="renewal_alerts",
    )

    def __repr__(self) -> str:
        return f"<RenewalAlert(id={self.id!r}, contract_id={self.contract_id!r}, threshold_days={self.threshold_days!r})>"
