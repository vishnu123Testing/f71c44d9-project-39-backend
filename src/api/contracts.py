"""FastAPI routes for contract lifecycle, document uploads, signature hub, and amendment management.

Enforces role-based access control, input validation, upload streams, DELETE cleanup,
and context-correlated structured logging [FR-1, FR-2, FR-3, FR-5, FR-6, FR-7, FR-8, FR-10, FR-11, NFR-1, NFR-2].
"""

from datetime import date as date_type, datetime, timezone
from enum import Enum
import html
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from src.api.deps import CurrentUser, get_current_user
from src.connectors.notification_connector import NotificationConnector
from src.connectors.signature_connector import NativeSignatureConnector
from src.connectors.storage_connector import LocalStorageConnector
from src.lib.config import settings
from src.lib.logging import get_correlation_id
from src.models.contract import Contract
from src.models.database import get_db
from src.services.contract.amendment_service import (
    AmendmentService,
    ImmutableAuditTrailError,
)
from src.services.contract.lifecycle_service import ContractLifecycleService
from src.services.contract.models import (
    ContractCategory,
    ContractStatus,
    SignatureStatus,
)
from src.services.contract.signature_service import (
    InvalidSignatureStateError,
    SignatureRequestNotFoundError,
    SignatureService,
    UnauthorizedSignatoryError,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["contracts"])


# ---------------------------------------------------------------------------
# Enums and Pydantic Schemas for Request / Response Validation
# ---------------------------------------------------------------------------


class ContractCategoryEnum(str, Enum):
    """Supported contract categories."""

    CONTRACT = "Contract"
    SOW = "SOW"
    NDA = "NDA"
    EXHIBIT = "Exhibit"


class ContractStatusEnum(str, Enum):
    """Contract lifecycle status states."""

    DRAFT = "DRAFT"
    PENDING_SIGNATURE = "PENDING_SIGNATURE"
    ACTIVE = "ACTIVE"
    AMENDED = "AMENDED"
    TERMINATED = "TERMINATED"
    EXPIRED = "EXPIRED"


class ClauseCreateRequest(BaseModel):
    """Request payload for creating a contract clause."""

    clause_type: str = Field(..., min_length=1, max_length=100)
    title: str = Field(..., min_length=1, max_length=255)
    body_text: str = Field(..., min_length=1)
    is_highlighted: bool = False

    @field_validator("clause_type", "title", mode="before")
    @classmethod
    def sanitize_clause_fields(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip()
        return v


class SLACreateRequest(BaseModel):
    """Request payload for defining a contract SLA metric target."""

    metric_name: str = Field(..., min_length=1, max_length=255)
    target_value: str = Field(..., min_length=1, max_length=255)
    penalty_terms: str | None = None

    @field_validator("metric_name", "target_value", mode="before")
    @classmethod
    def sanitize_sla_fields(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip()
        return v


class ContractCreateRequest(BaseModel):
    """Request payload for creating a new contract record [FR-3, FR-10]."""

    title: str = Field(..., min_length=1, max_length=255)
    category: ContractCategoryEnum
    counterparty: str = Field(..., min_length=1, max_length=255)
    start_on: date_type | None = None
    expires_on: date_type | None = None
    parent_contract_id: UUID | None = None
    clauses: list[ClauseCreateRequest] | None = None
    slas: list[SLACreateRequest] | None = None

    @field_validator("title", "counterparty", mode="before")
    @classmethod
    def sanitize_text_fields(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip()
        return v


class ContractUpdateRequest(BaseModel):
    """Request payload for updating an existing contract record."""

    title: str | None = Field(None, min_length=1, max_length=255)
    category: ContractCategoryEnum | None = None
    counterparty: str | None = Field(None, min_length=1, max_length=255)
    status: ContractStatusEnum | None = None
    start_on: date_type | None = None
    expires_on: date_type | None = None

    @field_validator("title", "counterparty", mode="before")
    @classmethod
    def sanitize_text_fields(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip()
        return v


class ClauseResponse(BaseModel):
    """Response schema for a contract clause."""

    id: UUID
    contract_id: UUID
    clause_type: str
    title: str
    body_text: str
    is_highlighted: bool
    created_at: datetime


class SLAResponse(BaseModel):
    """Response schema for a contract SLA."""

    id: UUID
    contract_id: UUID
    metric_name: str
    target_value: str
    penalty_terms: str | None
    created_at: datetime


class DocumentVersionResponse(BaseModel):
    """Response schema for a document version record [FR-4]."""

    id: UUID
    contract_id: UUID
    category_type: str
    version_number: int
    file_path: str
    checksum: str
    uploaded_by_id: UUID | None
    created_at: datetime


class AmendmentCreateRequest(BaseModel):
    """Request payload for creating a contract amendment [FR-7]."""

    summary: str = Field(..., min_length=1)

    @field_validator("summary", mode="before")
    @classmethod
    def sanitize_summary(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip()
        return v


class AmendmentDetailResponse(BaseModel):
    """Response schema for an amendment audit trail entry [FR-7, FR-8]."""

    id: UUID
    contract_id: UUID
    sequence_number: int
    summary: str
    author_id: UUID | None
    created_at: datetime


class AmendmentListResponse(BaseModel):
    """Response list wrapper for chronological contract amendments [FR-7, FR-8]."""

    items: list[AmendmentDetailResponse]
    total_count: int


class SignatureCreateRequest(BaseModel):
    """Request payload for initiating an e-signature request [FR-5]."""

    signatory_id: UUID | None = None


class SignatureRequestResponse(BaseModel):
    """Response schema for an e-signature request [FR-5, FR-6]."""

    id: UUID
    contract_id: UUID
    signatory_id: UUID | None
    status: str
    signature_digest: str | None
    executed_at: datetime | None
    created_at: datetime


class SignatureListResponse(BaseModel):
    """Response list wrapper for pending e-signature requests in hub [FR-5]."""

    items: list[SignatureRequestResponse]
    total_count: int


class SignatureExecuteRequest(BaseModel):
    """Request payload for executing a pending digital signature [FR-6]."""

    signature_digest: str = Field(..., min_length=1, max_length=255)

    @field_validator("signature_digest", mode="before")
    @classmethod
    def sanitize_digest(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip()
        return v


class SignatureExecuteResponse(BaseModel):
    """Response schema following successful signature execution [FR-6]."""

    request_id: UUID
    contract_id: UUID
    status: str
    executed_at: datetime
    signature_digest: str


class ContractSummaryResponse(BaseModel):
    """Summary response item for repository list view [FR-11]."""

    id: UUID
    title: str
    category: str
    status: str
    counterparty: str
    start_on: date_type | None
    expires_on: date_type | None
    parent_contract_id: UUID | None
    created_at: datetime
    updated_at: datetime
    sla_count: int = 0
    active_slas_breached: bool = False


class ContractListResponse(BaseModel):
    """Paginated response for contract list query [FR-11]."""

    items: list[ContractSummaryResponse]
    total_count: int
    page: int
    limit: int


class ContractDetailResponse(BaseModel):
    """Detailed response model for contract inspection [FR-1]."""

    id: UUID
    title: str
    category: str
    status: str
    counterparty: str
    start_on: date_type | None
    expires_on: date_type | None
    parent_contract_id: UUID | None
    created_at: datetime
    updated_at: datetime
    clauses: list[ClauseResponse] = []
    highlighted_clauses: list[ClauseResponse] = []
    slas: list[SLAResponse] = []
    document_versions: list[DocumentVersionResponse] = []
    active_version: DocumentVersionResponse | None = None
    amendments: list[AmendmentDetailResponse] = []
    signature_requests: list[SignatureRequestResponse] = []
    child_contract_ids: list[UUID] = []


# ---------------------------------------------------------------------------
# Dependency Injection Helpers for Services & Connectors
# ---------------------------------------------------------------------------


def get_storage_connector() -> LocalStorageConnector:
    """Dependency provider for LocalStorageConnector."""
    return LocalStorageConnector()


def get_notification_connector(
    db: Session = Depends(get_db),
) -> NotificationConnector:
    """Dependency provider for NotificationConnector."""
    return NotificationConnector(db=db)


def get_signature_connector(
    db: Session = Depends(get_db),
    notification_connector: NotificationConnector = Depends(get_notification_connector),
) -> NativeSignatureConnector:
    """Dependency provider for NativeSignatureConnector."""
    return NativeSignatureConnector(db=db, notification_port=notification_connector)


def get_lifecycle_service(
    db: Session = Depends(get_db),
    storage_connector: LocalStorageConnector = Depends(get_storage_connector),
    notification_connector: NotificationConnector = Depends(get_notification_connector),
) -> ContractLifecycleService:
    """Dependency provider for ContractLifecycleService."""
    return ContractLifecycleService(
        db=db,
        storage_port=storage_connector,
        notification_port=notification_connector,
    )


def get_amendment_service(
    db: Session = Depends(get_db),
) -> AmendmentService:
    """Dependency provider for AmendmentService."""
    return AmendmentService(db=db)


def get_signature_service(
    db: Session = Depends(get_db),
    signature_connector: NativeSignatureConnector = Depends(get_signature_connector),
    notification_connector: NotificationConnector = Depends(get_notification_connector),
) -> SignatureService:
    """Dependency provider for SignatureService."""
    return SignatureService(
        db=db,
        signature_port=signature_connector,
        notification_port=notification_connector,
    )


def _build_contract_detail_response(
    contract: Contract,
    amendment_service: AmendmentService,
    signature_service: SignatureService,
) -> ContractDetailResponse:
    """Map a SQLAlchemy Contract ORM instance to a ContractDetailResponse Pydantic model."""
    clauses = [
        ClauseResponse(
            id=c.id,
            contract_id=c.contract_id,
            clause_type=c.clause_type,
            title=c.title,
            body_text=c.body_text,
            is_highlighted=c.is_highlighted,
            created_at=c.created_at,
        )
        for c in (contract.clauses or [])
    ]
    highlighted_clauses = [c for c in clauses if c.is_highlighted]

    slas = [
        SLAResponse(
            id=s.id,
            contract_id=s.contract_id,
            metric_name=s.metric_name,
            target_value=s.target_value,
            penalty_terms=s.penalty_terms,
            created_at=s.created_at,
        )
        for s in (contract.slas or [])
    ]

    sorted_doc_versions = sorted(
        contract.document_versions or [], key=lambda v: v.version_number
    )
    doc_versions = [
        DocumentVersionResponse(
            id=d.id,
            contract_id=d.contract_id,
            category_type=d.category_type,
            version_number=d.version_number,
            file_path=d.file_path,
            checksum=d.checksum,
            uploaded_by_id=d.uploaded_by_id,
            created_at=d.created_at,
        )
        for d in sorted_doc_versions
    ]
    active_version = doc_versions[-1] if doc_versions else None

    amendments_domain = amendment_service.get_amendments_for_contract(contract.id)
    amendments = [
        AmendmentDetailResponse(
            id=a.id,
            contract_id=a.contract_id,
            sequence_number=a.sequence_number,
            summary=a.summary,
            author_id=a.author_id,
            created_at=a.created_at,
        )
        for a in amendments_domain
    ]

    sig_requests_domain = signature_service.list_signature_requests(contract.id)
    signature_requests = [
        SignatureRequestResponse(
            id=sr.id,
            contract_id=sr.contract_id,
            signatory_id=sr.signatory_id,
            status=sr.status.value if hasattr(sr.status, "value") else str(sr.status),
            signature_digest=sr.signature_digest,
            executed_at=sr.executed_at,
            created_at=sr.created_at,
        )
        for sr in sig_requests_domain
    ]

    child_contract_ids = [c.id for c in (contract.child_contracts or [])]

    return ContractDetailResponse(
        id=contract.id,
        title=contract.title,
        category=contract.category,
        status=contract.status,
        counterparty=contract.counterparty,
        start_on=contract.start_on,
        expires_on=contract.expires_on,
        parent_contract_id=contract.parent_contract_id,
        created_at=contract.created_at,
        updated_at=contract.updated_at,
        clauses=clauses,
        highlighted_clauses=highlighted_clauses,
        slas=slas,
        document_versions=doc_versions,
        active_version=active_version,
        amendments=amendments,
        signature_requests=signature_requests,
        child_contract_ids=child_contract_ids,
    )


# ---------------------------------------------------------------------------
# Contract Management Route Handlers
# ---------------------------------------------------------------------------


@router.get(
    "/contracts",
    response_model=ContractListResponse,
)
def list_contracts(
    category: str | None = None,
    status: str | None = None,
    counterparty: str | None = None,
    expires_before: date_type | None = None,
    expires_after: date_type | None = None,
    search_query: str | None = None,
    parent_contract_id: UUID | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
    lifecycle_service: ContractLifecycleService = Depends(get_lifecycle_service),
) -> ContractListResponse:
    """Multi-criteria repository filtering across category, status, expiration date, counterparty [FR-1, FR-11]."""
    results, total_count = lifecycle_service.search_contracts(
        category=category,
        status=status,
        counterparty=counterparty,
        expires_before=expires_before,
        expires_after=expires_after,
        search_query=search_query,
        parent_contract_id=parent_contract_id,
        page=page,
        limit=limit,
    )

    items = [
        ContractSummaryResponse(
            id=c.id,
            title=c.title,
            category=c.category,
            status=c.status,
            counterparty=c.counterparty,
            start_on=c.start_on,
            expires_on=c.expires_on,
            parent_contract_id=c.parent_contract_id,
            created_at=c.created_at,
            updated_at=c.updated_at,
            sla_count=len(c.slas or []),
            active_slas_breached=False,
        )
        for c in results
    ]

    return ContractListResponse(
        items=items,
        total_count=total_count,
        page=page,
        limit=limit,
    )


@router.post(
    "/contracts",
    response_model=ContractDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_contract(
    payload: ContractCreateRequest,
    current_user: CurrentUser = Depends(get_current_user),
    lifecycle_service: ContractLifecycleService = Depends(get_lifecycle_service),
    amendment_service: AmendmentService = Depends(get_amendment_service),
    signature_service: SignatureService = Depends(get_signature_service),
) -> ContractDetailResponse:
    """Create a new contract agreement record [FR-3, FR-10]."""
    clauses_data = (
        [c.model_dump() for c in payload.clauses] if payload.clauses else None
    )
    slas_data = [s.model_dump() for s in payload.slas] if payload.slas else None

    try:
        contract = lifecycle_service.create_contract(
            title=payload.title,
            category=payload.category,
            counterparty=payload.counterparty,
            start_on=payload.start_on,
            expires_on=payload.expires_on,
            parent_contract_id=payload.parent_contract_id,
            performed_by_id=current_user.user_id,
            clauses=clauses_data,
            slas=slas_data,
        )
    except ValueError as val_err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err),
        )

    return _build_contract_detail_response(
        contract,
        amendment_service=amendment_service,
        signature_service=signature_service,
    )


@router.get(
    "/contracts/{id}",
    response_model=ContractDetailResponse,
)
def get_contract_detail(
    id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    lifecycle_service: ContractLifecycleService = Depends(get_lifecycle_service),
    amendment_service: AmendmentService = Depends(get_amendment_service),
    signature_service: SignatureService = Depends(get_signature_service),
) -> ContractDetailResponse:
    """Fetch contract details including highlighted clauses, key terms, and active SLAs [FR-1]."""
    contract = lifecycle_service.get_contract_by_id(id)
    if not contract:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contract '{id}' not found.",
        )

    return _build_contract_detail_response(
        contract,
        amendment_service=amendment_service,
        signature_service=signature_service,
    )


@router.put(
    "/contracts/{id}",
    response_model=ContractDetailResponse,
)
def update_contract(
    id: UUID,
    payload: ContractUpdateRequest,
    current_user: CurrentUser = Depends(get_current_user),
    lifecycle_service: ContractLifecycleService = Depends(get_lifecycle_service),
    amendment_service: AmendmentService = Depends(get_amendment_service),
    signature_service: SignatureService = Depends(get_signature_service),
) -> ContractDetailResponse:
    """Update contract attributes and status transitions."""
    try:
        updated = lifecycle_service.update_contract(
            contract_id=id,
            title=payload.title,
            category=payload.category,
            counterparty=payload.counterparty,
            status=payload.status,
            start_on=payload.start_on,
            expires_on=payload.expires_on,
            performed_by_id=current_user.user_id,
        )
    except ValueError as val_err:
        if "not found" in str(val_err).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(val_err),
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err),
        )

    return _build_contract_detail_response(
        updated,
        amendment_service=amendment_service,
        signature_service=signature_service,
    )


@router.delete(
    "/contracts/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
def delete_contract(
    id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    lifecycle_service: ContractLifecycleService = Depends(get_lifecycle_service),
) -> Response:
    """Delete contract record and associated physical documents [Architecture §5]."""
    success = lifecycle_service.delete_contract(
        contract_id=id,
        performed_by_id=current_user.user_id,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contract '{id}' not found.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Document Upload and Versioning Route Handlers
# ---------------------------------------------------------------------------


@router.post(
    "/contracts/{id}/documents",
    response_model=DocumentVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_contract_document(
    id: UUID,
    file: UploadFile = File(...),
    category_type: str = Form(...),
    current_user: CurrentUser = Depends(get_current_user),
    lifecycle_service: ContractLifecycleService = Depends(get_lifecycle_service),
) -> DocumentVersionResponse:
    """Upload a document file payload, increment version number sequentially [FR-3, FR-4, FR-10]."""
    contract = lifecycle_service.get_contract_by_id(id)
    if not contract:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contract '{id}' not found.",
        )

    allowed_categories = [c.value for c in ContractCategory]
    category_type_clean = category_type.strip()
    if category_type_clean not in allowed_categories:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid document category '{category_type}'. Allowed categories: {allowed_categories}",
        )

    try:
        content = file.file.read()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to read file payload stream: {exc}",
        )

    if len(content) > settings.MAX_UPLOAD_SIZE_BYTES:
        max_mb = settings.MAX_UPLOAD_SIZE_BYTES / (1024 * 1024)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File payload size ({len(content)} bytes) exceeds maximum allowable upload limit ({max_mb:.1f} MB).",
        )

    file_name = Path(file.filename or "document.pdf").name
    file_ext = Path(file_name).suffix.lower()
    if file_ext not in settings.ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File extension '{file_ext}' is not permitted. Allowed extensions: {settings.ALLOWED_UPLOAD_EXTENSIONS}",
        )

    try:
        doc_version = lifecycle_service.upload_document_version(
            contract_id=id,
            category_type=category_type_clean,
            file_name=file_name,
            content=content,
            uploaded_by_id=current_user.user_id,
        )
    except ValueError as val_err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err),
        )

    return DocumentVersionResponse(
        id=doc_version.id,
        contract_id=doc_version.contract_id,
        category_type=doc_version.category_type,
        version_number=doc_version.version_number,
        file_path=doc_version.file_path,
        checksum=doc_version.checksum,
        uploaded_by_id=doc_version.uploaded_by_id,
        created_at=doc_version.created_at,
    )


@router.get(
    "/contracts/{id}/documents/{version_id}",
    response_class=Response,
)
def download_contract_document(
    id: UUID,
    version_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    lifecycle_service: ContractLifecycleService = Depends(get_lifecycle_service),
) -> Response:
    """Download document version payload stream [FR-4]."""
    contract = lifecycle_service.get_contract_by_id(id)
    if not contract:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contract '{id}' not found.",
        )

    try:
        doc_version, content = lifecycle_service.retrieve_document_payload(version_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )

    if doc_version.contract_id != id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Document version '{version_id}' does not belong to contract '{id}'.",
        )

    file_name = Path(doc_version.file_path).name
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{file_name}"',
        },
    )


# ---------------------------------------------------------------------------
# Clause and SLA Management Route Handlers
# ---------------------------------------------------------------------------


@router.post(
    "/contracts/{id}/clauses",
    response_model=ClauseResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_contract_clause(
    id: UUID,
    payload: ClauseCreateRequest,
    current_user: CurrentUser = Depends(get_current_user),
    lifecycle_service: ContractLifecycleService = Depends(get_lifecycle_service),
) -> ClauseResponse:
    """Add a clause entry to a contract [FR-1]."""
    try:
        clause = lifecycle_service.add_clause(
            contract_id=id,
            clause_type=payload.clause_type,
            title=payload.title,
            body_text=payload.body_text,
            is_highlighted=payload.is_highlighted,
        )
    except ValueError as val_err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND
            if "not found" in str(val_err).lower()
            else status.HTTP_400_BAD_REQUEST,
            detail=str(val_err),
        )

    return ClauseResponse(
        id=clause.id,
        contract_id=clause.contract_id,
        clause_type=clause.clause_type,
        title=clause.title,
        body_text=clause.body_text,
        is_highlighted=clause.is_highlighted,
        created_at=clause.created_at,
    )


@router.post(
    "/contracts/{id}/slas",
    response_model=SLAResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_contract_sla(
    id: UUID,
    payload: SLACreateRequest,
    current_user: CurrentUser = Depends(get_current_user),
    lifecycle_service: ContractLifecycleService = Depends(get_lifecycle_service),
) -> SLAResponse:
    """Add a Service Level Agreement metric target to a contract [FR-1]."""
    try:
        sla = lifecycle_service.add_sla(
            contract_id=id,
            metric_name=payload.metric_name,
            target_value=payload.target_value,
            penalty_terms=payload.penalty_terms,
        )
    except ValueError as val_err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND
            if "not found" in str(val_err).lower()
            else status.HTTP_400_BAD_REQUEST,
            detail=str(val_err),
        )

    return SLAResponse(
        id=sla.id,
        contract_id=sla.contract_id,
        metric_name=sla.metric_name,
        target_value=sla.target_value,
        penalty_terms=sla.penalty_terms,
        created_at=sla.created_at,
    )


@router.get(
    "/contracts/{id}/slas/evaluate",
)
def evaluate_contract_slas(
    id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    lifecycle_service: ContractLifecycleService = Depends(get_lifecycle_service),
) -> dict[str, Any]:
    """Evaluate active SLAs, highlighted clauses, and key terms for an agreement [FR-1]."""
    try:
        return lifecycle_service.evaluate_contract_slas(contract_id=id)
    except ValueError as val_err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(val_err),
        )


# ---------------------------------------------------------------------------
# Amendment Audit Trail Route Handlers
# ---------------------------------------------------------------------------


@router.post(
    "/contracts/{id}/amendments",
    response_model=AmendmentDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_contract_amendment(
    id: UUID,
    payload: AmendmentCreateRequest,
    current_user: CurrentUser = Depends(get_current_user),
    amendment_service: AmendmentService = Depends(get_amendment_service),
) -> AmendmentDetailResponse:
    """Append a new amendment to a contract's immutable audit trail [FR-7]."""
    try:
        amendment = amendment_service.add_amendment(
            contract_id=id,
            summary=payload.summary,
            author_id=current_user.user_id,
            performed_by_id=current_user.user_id,
        )
    except ValueError as val_err:
        if "not found" in str(val_err).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(val_err),
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err),
        )

    return AmendmentDetailResponse(
        id=amendment.id,
        contract_id=amendment.contract_id,
        sequence_number=amendment.sequence_number,
        summary=amendment.summary,
        author_id=amendment.author_id,
        created_at=amendment.created_at,
    )


@router.get(
    "/contracts/{id}/amendments",
    response_model=AmendmentListResponse,
)
def list_contract_amendments(
    id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    lifecycle_service: ContractLifecycleService = Depends(get_lifecycle_service),
    amendment_service: AmendmentService = Depends(get_amendment_service),
) -> AmendmentListResponse:
    """Retrieve chronological read-only amendment audit entries for a contract [FR-7, FR-8]."""
    contract = lifecycle_service.get_contract_by_id(id)
    if not contract:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contract '{id}' not found.",
        )

    amendments = amendment_service.get_amendments_for_contract(id)
    items = [
        AmendmentDetailResponse(
            id=a.id,
            contract_id=a.contract_id,
            sequence_number=a.sequence_number,
            summary=a.summary,
            author_id=a.author_id,
            created_at=a.created_at,
        )
        for a in amendments
    ]
    return AmendmentListResponse(items=items, total_count=len(items))


@router.put(
    "/contracts/{id}/amendments/{amendment_id}",
    status_code=status.HTTP_403_FORBIDDEN,
)
def update_contract_amendment_blocked(
    id: UUID,
    amendment_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    """Block and reject any attempt to modify an existing amendment audit entry [FR-8, NFR-4]."""
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Contract amendment records are immutable. Editing or updating past audit trail entries is strictly forbidden [FR-8, NFR-4].",
    )


@router.delete(
    "/contracts/{id}/amendments/{amendment_id}",
    status_code=status.HTTP_403_FORBIDDEN,
)
def delete_contract_amendment_blocked(
    id: UUID,
    amendment_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    """Block and reject any attempt to delete an existing amendment audit entry [FR-8, NFR-4]."""
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Contract amendment records are immutable. Deleting past audit trail entries is strictly forbidden [FR-8, NFR-4].",
    )


# ---------------------------------------------------------------------------
# E-Signature Hub Route Handlers
# ---------------------------------------------------------------------------


@router.get(
    "/signatures/pending",
    response_model=SignatureListResponse,
)
def list_pending_signatures(
    signatory_id: UUID | None = None,
    contract_id: UUID | None = None,
    current_user: CurrentUser = Depends(get_current_user),
    signature_service: SignatureService = Depends(get_signature_service),
) -> SignatureListResponse:
    """Retrieve all e-signature requests currently in PENDING status for central hub [FR-5]."""
    reqs = signature_service.get_pending_signatures(
        signatory_id=signatory_id,
        contract_id=contract_id,
    )
    items = [
        SignatureRequestResponse(
            id=r.id,
            contract_id=r.contract_id,
            signatory_id=r.signatory_id,
            status=r.status.value if hasattr(r.status, "value") else str(r.status),
            signature_digest=r.signature_digest,
            executed_at=r.executed_at,
            created_at=r.created_at,
        )
        for r in reqs
    ]
    return SignatureListResponse(items=items, total_count=len(items))


@router.post(
    "/contracts/{id}/signatures/request",
    response_model=SignatureRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
def request_contract_signature(
    id: UUID,
    payload: SignatureCreateRequest | None = None,
    current_user: CurrentUser = Depends(get_current_user),
    signature_service: SignatureService = Depends(get_signature_service),
) -> SignatureRequestResponse:
    """Initiate an e-signature request for a contract [FR-5]."""
    signatory_id = payload.signatory_id if payload else None
    try:
        req = signature_service.request_signature(
            contract_id=id,
            signatory_id=signatory_id,
        )
    except SignatureRequestNotFoundError as not_found_err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(not_found_err),
        )
    except InvalidSignatureStateError as state_err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(state_err),
        )

    return SignatureRequestResponse(
        id=req.id,
        contract_id=req.contract_id,
        signatory_id=req.signatory_id,
        status=req.status.value if hasattr(req.status, "value") else str(req.status),
        signature_digest=req.signature_digest,
        executed_at=req.executed_at,
        created_at=req.created_at,
    )


@router.post(
    "/signatures/{id}/execute",
    response_model=SignatureExecuteResponse,
)
def execute_signature(
    id: UUID,
    payload: SignatureExecuteRequest,
    current_user: CurrentUser = Depends(get_current_user),
    signature_service: SignatureService = Depends(get_signature_service),
) -> SignatureExecuteResponse:
    """Capture digital signature execution, transition contract status to ACTIVE, notify parties [FR-6]."""
    try:
        result = signature_service.execute_signature(
            request_id=id,
            signature_digest=payload.signature_digest,
            signatory_id=current_user.user_id,
        )
    except SignatureRequestNotFoundError as not_found_err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(not_found_err),
        )
    except InvalidSignatureStateError as state_err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(state_err),
        )
    except UnauthorizedSignatoryError as unauth_err:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(unauth_err),
        )
    except ValueError as val_err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err),
        )

    return SignatureExecuteResponse(
        request_id=result.request_id,
        contract_id=result.contract_id,
        status=result.status.value if hasattr(result.status, "value") else str(result.status),
        executed_at=result.executed_at,
        signature_digest=result.signature_digest,
    )


@router.post(
    "/signatures/{id}/reject",
    response_model=SignatureRequestResponse,
)
def reject_signature(
    id: UUID,
    reason: str | None = None,
    current_user: CurrentUser = Depends(get_current_user),
    signature_service: SignatureService = Depends(get_signature_service),
) -> SignatureRequestResponse:
    """Reject an e-signature request and transition status to REJECTED."""
    try:
        req = signature_service.reject_signature(
            request_id=id,
            reason=reason,
            signatory_id=current_user.user_id,
        )
    except SignatureRequestNotFoundError as not_found_err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(not_found_err),
        )
    except (InvalidSignatureStateError, ValueError) as state_err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(state_err),
        )
    except UnauthorizedSignatoryError as unauth_err:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(unauth_err),
        )

    return SignatureRequestResponse(
        id=req.id,
        contract_id=req.contract_id,
        signatory_id=req.signatory_id,
        status=req.status.value if hasattr(req.status, "value") else str(req.status),
        signature_digest=req.signature_digest,
        executed_at=req.executed_at,
        created_at=req.created_at,
    )
