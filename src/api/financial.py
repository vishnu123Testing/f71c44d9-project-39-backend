"""FastAPI router and Pydantic input/output schemas for financial transparency endpoints.

Provides endpoints for quotes, invoices, payment history, and AR aging duration buckets
with RBAC enforcement and input sanitization [FR-1, FR-2, FR-3, FR-4, FR-9, FR-10].
"""

from datetime import datetime
import html
import logging
import re
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from src.api.deps import CurrentUser, RequirePermission
from src.lib.logging import get_correlation_id
from src.models.database import get_db
from src.services.financial.metrics import (
    ARAgingBucketDTO,
    ARAgingSummaryDTO,
    InvalidFinancialDataError,
    InvoiceDTO,
    InvoiceNotFoundError,
    PaymentDTO,
    PaymentNotFoundError,
    QuoteDTO,
    QuoteNotFoundError,
    calculate_ar_aging,
    create_invoice,
    create_quote,
    get_invoice_by_id,
    get_quote_by_id,
    list_invoices,
    list_payments,
    list_quotes,
    record_payment,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/financial", tags=["financial"])


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
# Pydantic Schemas - Response Models
# ---------------------------------------------------------------------------


class QuoteResponse(BaseModel):
    """Schema representing financial quote details [FR-1]."""

    quote_id: UUID = Field(..., description="Unique quote UUID")
    customer_id: UUID = Field(..., description="Target customer UUID")
    approval_state: str = Field(..., description="Quote approval state")
    expiration_date_time: datetime = Field(..., description="Expiration UTC timestamp")
    total_amount: float = Field(..., description="Total quote amount")
    created_at: datetime = Field(..., description="Creation UTC timestamp")
    updated_at: datetime = Field(..., description="Last updated UTC timestamp")
    is_expired: bool = Field(..., description="Boolean flag indicating whether quote is expired")

    @classmethod
    def from_dto(cls, dto: QuoteDTO) -> "QuoteResponse":
        """Construct QuoteResponse from domain QuoteDTO."""
        return cls(
            quote_id=dto.quote_id,
            customer_id=dto.customer_id,
            approval_state=dto.approval_state,
            expiration_date_time=dto.expiration_date_time,
            total_amount=dto.total_amount,
            created_at=dto.created_at,
            updated_at=dto.updated_at,
            is_expired=dto.is_expired,
        )


class InvoiceResponse(BaseModel):
    """Schema representing invoice details with status and overdue metadata [FR-2]."""

    invoice_id: UUID = Field(..., description="Unique invoice UUID")
    quote_id: UUID | None = Field(default=None, description="Associated quote UUID if linked")
    customer_id: UUID = Field(..., description="Target customer UUID")
    status: str = Field(..., description="Invoice status")
    issue_date_time: datetime = Field(..., description="Issue UTC timestamp")
    due_date_time: datetime = Field(..., description="Due UTC timestamp")
    total_amount: float = Field(..., description="Total invoice amount")
    outstanding_amount: float = Field(..., description="Outstanding unpaid balance")
    created_at: datetime = Field(..., description="Creation UTC timestamp")
    updated_at: datetime = Field(..., description="Last updated UTC timestamp")
    days_overdue: int = Field(..., description="Calculated days overdue past due date")

    @classmethod
    def from_dto(cls, dto: InvoiceDTO) -> "InvoiceResponse":
        """Construct InvoiceResponse from domain InvoiceDTO."""
        return cls(
            invoice_id=dto.invoice_id,
            quote_id=dto.quote_id,
            customer_id=dto.customer_id,
            status=dto.status,
            issue_date_time=dto.issue_date_time,
            due_date_time=dto.due_date_time,
            total_amount=dto.total_amount,
            outstanding_amount=dto.outstanding_amount,
            created_at=dto.created_at,
            updated_at=dto.updated_at,
            days_overdue=dto.days_overdue,
        )


class PaymentResponse(BaseModel):
    """Schema representing payment history details linked to an invoice [FR-3]."""

    payment_id: UUID = Field(..., description="Unique payment UUID")
    invoice_id: UUID = Field(..., description="Associated invoice UUID")
    payment_date_time: datetime = Field(..., description="Payment UTC timestamp")
    amount_paid: float = Field(..., description="Amount paid")
    payment_method: str = Field(..., description="Payment method string")
    created_at: datetime = Field(..., description="Creation UTC timestamp")

    @classmethod
    def from_dto(cls, dto: PaymentDTO) -> "PaymentResponse":
        """Construct PaymentResponse from domain PaymentDTO."""
        return cls(
            payment_id=dto.payment_id,
            invoice_id=dto.invoice_id,
            payment_date_time=dto.payment_date_time,
            amount_paid=dto.amount_paid,
            payment_method=dto.payment_method,
            created_at=dto.created_at,
        )


class ARAgingBucketResponse(BaseModel):
    """Schema representing an individual AR aging duration bucket [FR-4]."""

    bucket_name: str = Field(..., description="Bucket label (0-30, 31-60, 61-90, 90+)")
    total_amount: float = Field(..., description="Total outstanding balance in duration bucket")
    invoice_count: int = Field(..., description="Count of open invoices in duration bucket")

    @classmethod
    def from_dto(cls, dto: ARAgingBucketDTO) -> "ARAgingBucketResponse":
        """Construct ARAgingBucketResponse from domain ARAgingBucketDTO."""
        return cls(
            bucket_name=dto.bucket_name,
            total_amount=dto.total_amount,
            invoice_count=dto.invoice_count,
        )


class ARAgingSummaryResponse(BaseModel):
    """Schema representing Accounts Receivable (AR) aging summary duration buckets [FR-4]."""

    customer_id: UUID | None = Field(default=None, description="Customer UUID filter if supplied")
    as_of_date_time: datetime = Field(..., description="Calculation timestamp in UTC")
    total_outstanding: float = Field(..., description="Total outstanding AR balance across all buckets")
    total_invoices: int = Field(..., description="Total count of open unpaid invoices")
    bucket_0_30: ARAgingBucketResponse = Field(..., description="0 to 30 days duration bucket")
    bucket_31_60: ARAgingBucketResponse = Field(..., description="31 to 60 days duration bucket")
    bucket_61_90: ARAgingBucketResponse = Field(..., description="61 to 90 days duration bucket")
    bucket_90_plus: ARAgingBucketResponse = Field(..., description="90+ days duration bucket")

    @classmethod
    def from_dto(cls, dto: ARAgingSummaryDTO) -> "ARAgingSummaryResponse":
        """Construct ARAgingSummaryResponse from domain ARAgingSummaryDTO."""
        return cls(
            customer_id=dto.customer_id,
            as_of_date_time=dto.as_of_date_time,
            total_outstanding=dto.total_outstanding,
            total_invoices=dto.total_invoices,
            bucket_0_30=ARAgingBucketResponse.from_dto(dto.bucket_0_30),
            bucket_31_60=ARAgingBucketResponse.from_dto(dto.bucket_31_60),
            bucket_61_90=ARAgingBucketResponse.from_dto(dto.bucket_61_90),
            bucket_90_plus=ARAgingBucketResponse.from_dto(dto.bucket_90_plus),
        )


# ---------------------------------------------------------------------------
# Pydantic Schemas - Request Models
# ---------------------------------------------------------------------------


class QuoteCreateRequest(BaseModel):
    """Request payload for creating a new quote record [FR-1, FR-10]."""

    customer_id: UUID = Field(..., description="Target customer UUID")
    expiration_date_time: datetime = Field(..., description="Quote expiration date and time in UTC")
    total_amount: float = Field(..., ge=0.0, description="Total quote amount (must be non-negative)")
    approval_state: str = Field(default="Draft", description="Approval state (Draft, Pending_Approval, Approved, Expired)")

    @field_validator("approval_state", mode="before")
    @classmethod
    def sanitize_approval_state(cls, v: Any) -> str:
        """Sanitize approval state string input."""
        if v is None:
            return "Draft"
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized else "Draft"


class InvoiceCreateRequest(BaseModel):
    """Request payload for creating a new invoice record [FR-2, FR-10]."""

    customer_id: UUID = Field(..., description="Target customer UUID")
    issue_date_time: datetime = Field(..., description="Invoice issue timestamp in UTC")
    due_date_time: datetime = Field(..., description="Invoice due timestamp in UTC")
    total_amount: float = Field(..., ge=0.0, description="Total invoice amount (must be non-negative)")
    quote_id: UUID | None = Field(default=None, description="Associated quote UUID if linked")
    status: str = Field(default="Current", description="Invoice status (Current, Pending, Past_Due, Paid)")
    outstanding_amount: float | None = Field(default=None, ge=0.0, description="Outstanding balance")

    @field_validator("status", mode="before")
    @classmethod
    def sanitize_status(cls, v: Any) -> str:
        """Sanitize invoice status string input."""
        if v is None:
            return "Current"
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized else "Current"


class PaymentRecordRequest(BaseModel):
    """Request payload for recording a payment linked to an invoice [FR-3, FR-10]."""

    invoice_id: UUID = Field(..., description="Associated invoice UUID")
    amount_paid: float = Field(..., gt=0.0, description="Payment amount (must be greater than zero)")
    payment_method: str = Field(..., min_length=1, max_length=50, description="Payment method string")
    payment_date_time: datetime | None = Field(default=None, description="Optional payment UTC timestamp")

    @field_validator("payment_method", mode="before")
    @classmethod
    def sanitize_payment_method(cls, v: Any) -> str:
        """Sanitize payment method string input."""
        if v is None:
            return ""
        sanitized = sanitize_input_string(str(v))
        return sanitized if sanitized is not None else ""


# ---------------------------------------------------------------------------
# API Route Handlers
# ---------------------------------------------------------------------------


@router.get(
    "/quotes",
    response_model=list[QuoteResponse],
    status_code=status.HTTP_200_OK,
    summary="List quotes",
    description="Retrieve authorized quotes with approval states and explicit expiration dates [FR-1, FR-9, FR-10].",
)
def list_quotes_endpoint(
    approval_state: str | None = Query(default=None, description="Optional approval state filter"),
    customer_id: UUID | None = Query(default=None, description="Optional customer UUID filter"),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="read", resource="financials")),
) -> list[QuoteResponse]:
    """Retrieve and filter quotes from database."""
    sanitized_state = sanitize_input_string(approval_state) if approval_state else None
    try:
        dtos = list_quotes(db, customer_id=customer_id, approval_state=sanitized_state)
        return [QuoteResponse.from_dto(dto) for dto in dtos]
    except Exception as exc:
        logger.error(
            "Failed to list quotes: %s",
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while retrieving quotes.",
        ) from exc


@router.get(
    "/quotes/{quote_id}",
    response_model=QuoteResponse,
    status_code=status.HTTP_200_OK,
    summary="Get quote by ID",
    description="Fetch a single quote record by its unique UUID [FR-1, FR-9].",
)
def get_quote_endpoint(
    quote_id: UUID,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="read", resource="financials")),
) -> QuoteResponse:
    """Fetch a single quote by ID or raise HTTP 404."""
    try:
        dto = get_quote_by_id(db, quote_id=quote_id)
        return QuoteResponse.from_dto(dto)
    except QuoteNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            "Failed to fetch quote %s: %s",
            quote_id,
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching the quote.",
        ) from exc


@router.post(
    "/quotes",
    response_model=QuoteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new quote",
    description="Create a new quote record with explicit expiration date and approval state [FR-1, FR-9, FR-10].",
)
def create_quote_endpoint(
    payload: QuoteCreateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="write", resource="financials")),
) -> QuoteResponse:
    """Create a new quote record."""
    try:
        dto = create_quote(
            db=db,
            customer_id=payload.customer_id,
            expiration_date_time=payload.expiration_date_time,
            total_amount=payload.total_amount,
            approval_state=payload.approval_state,
        )
        return QuoteResponse.from_dto(dto)
    except InvalidFinancialDataError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            "Failed to create quote: %s",
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating the quote.",
        ) from exc


@router.get(
    "/invoices",
    response_model=list[InvoiceResponse],
    status_code=status.HTTP_200_OK,
    summary="List invoices",
    description="Retrieve current, pending, and historical invoices with status filters [FR-2, FR-9, FR-10].",
)
def list_invoices_endpoint(
    status_filter: str | None = Query(default=None, alias="status", description="Optional status filter"),
    customer_id: UUID | None = Query(default=None, description="Optional customer UUID filter"),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="read", resource="financials")),
) -> list[InvoiceResponse]:
    """Retrieve and filter invoices from database."""
    sanitized_status = sanitize_input_string(status_filter) if status_filter else None
    try:
        dtos = list_invoices(db, customer_id=customer_id, status=sanitized_status)
        return [InvoiceResponse.from_dto(dto) for dto in dtos]
    except Exception as exc:
        logger.error(
            "Failed to list invoices: %s",
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while retrieving invoices.",
        ) from exc


@router.get(
    "/invoices/{invoice_id}",
    response_model=InvoiceResponse,
    status_code=status.HTTP_200_OK,
    summary="Get invoice by ID",
    description="Fetch a single invoice record by its unique UUID [FR-2, FR-9].",
)
def get_invoice_endpoint(
    invoice_id: UUID,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="read", resource="financials")),
) -> InvoiceResponse:
    """Fetch a single invoice by ID or raise HTTP 404."""
    try:
        dto = get_invoice_by_id(db, invoice_id=invoice_id)
        return InvoiceResponse.from_dto(dto)
    except InvoiceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            "Failed to fetch invoice %s: %s",
            invoice_id,
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching the invoice.",
        ) from exc


@router.post(
    "/invoices",
    response_model=InvoiceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new invoice",
    description="Create a new invoice record with issue/due dates and amounts [FR-2, FR-9, FR-10].",
)
def create_invoice_endpoint(
    payload: InvoiceCreateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="write", resource="financials")),
) -> InvoiceResponse:
    """Create a new invoice record."""
    try:
        dto = create_invoice(
            db=db,
            customer_id=payload.customer_id,
            issue_date_time=payload.issue_date_time,
            due_date_time=payload.due_date_time,
            total_amount=payload.total_amount,
            quote_id=payload.quote_id,
            status=payload.status,
            outstanding_amount=payload.outstanding_amount,
        )
        return InvoiceResponse.from_dto(dto)
    except InvalidFinancialDataError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            "Failed to create invoice: %s",
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating the invoice.",
        ) from exc


@router.get(
    "/payments",
    response_model=list[PaymentResponse],
    status_code=status.HTTP_200_OK,
    summary="List payment history",
    description="Retrieve complete payment history records linked to invoices [FR-3, FR-9, FR-10].",
)
def list_payments_endpoint(
    invoice_id: UUID | None = Query(default=None, description="Optional invoice UUID filter"),
    customer_id: UUID | None = Query(default=None, description="Optional customer UUID filter"),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="read", resource="financials")),
) -> list[PaymentResponse]:
    """Retrieve and filter payment history records from database."""
    try:
        dtos = list_payments(db, invoice_id=invoice_id, customer_id=customer_id)
        return [PaymentResponse.from_dto(dto) for dto in dtos]
    except Exception as exc:
        logger.error(
            "Failed to list payments: %s",
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while retrieving payment history.",
        ) from exc


@router.post(
    "/payments",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a payment",
    description="Record a payment linked to an invoice and update outstanding balance [FR-3, FR-9, FR-10].",
)
def record_payment_endpoint(
    payload: PaymentRecordRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="write", resource="financials")),
) -> PaymentResponse:
    """Record a payment linked to an invoice."""
    try:
        dto = record_payment(
            db=db,
            invoice_id=payload.invoice_id,
            amount_paid=payload.amount_paid,
            payment_method=payload.payment_method,
            payment_date_time=payload.payment_date_time,
        )
        return PaymentResponse.from_dto(dto)
    except InvoiceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except InvalidFinancialDataError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            "Failed to record payment: %s",
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while recording the payment.",
        ) from exc


@router.get(
    "/ar-aging",
    response_model=ARAgingSummaryResponse,
    status_code=status.HTTP_200_OK,
    summary="Get AR aging duration buckets",
    description="Present visual AR aging metrics segmented into 0-30, 31-60, 61-90, and 90+ day duration buckets [FR-4, FR-9].",
)
def get_ar_aging_endpoint(
    customer_id: UUID | None = Query(default=None, description="Optional customer UUID filter"),
    as_of_date_time: datetime | None = Query(default=None, description="Optional calculation cutoff timestamp in UTC"),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(RequirePermission(action="read", resource="financials")),
) -> ARAgingSummaryResponse:
    """Calculate AR aging duration buckets."""
    try:
        summary_dto = calculate_ar_aging(
            db=db,
            customer_id=customer_id,
            as_of_date_time=as_of_date_time,
        )
        return ARAgingSummaryResponse.from_dto(summary_dto)
    except Exception as exc:
        logger.error(
            "Failed to calculate AR aging metrics: %s",
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while calculating AR aging metrics.",
        ) from exc
