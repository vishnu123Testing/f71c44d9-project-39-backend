"""FastAPI router and Pydantic request/response validation schemas for knowledge repository and onboarding resource endpoints.

Provides full-text search, category and tag filtering, content type filtering, multi-tenant isolation,
input parameter sanitization, structured JSON logging with correlation IDs, and domain exception handling
[FR-1, FR-2, FR-7, FR-8, FR-9, NFR-1, NFR-3, NFR-4].
"""

import html
import logging
import re
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.api.deps import CurrentUser, get_current_user
from src.lib.logging import get_correlation_id
from src.models.database import get_db
from src.services.knowledge.article_service import (
    ArticleError,
    ArticleNotFoundError,
    CategoryNotFoundError,
    InvalidArticleDataError,
    TagNotFoundError,
    delete_article,
    get_article_by_id,
    search_articles,
)
from src.services.knowledge.playbook_service import (
    InvalidPlaybookDataError,
    InvalidStepReferenceError,
    PlaybookError,
    PlaybookNotFoundError,
    StepNotFoundError,
    get_playbook_progress,
    toggle_step_completion,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def sanitize_input_string(v: str | None) -> str | None:
    """Sanitize and clean raw user string inputs.

    Strips leading/trailing whitespace, strips script/HTML markup tags,
    escapes special characters, and removes unexpected control characters.
    """
    if v is None:
        return None

    cleaned = str(v).strip()
    if not cleaned:
        return cleaned

    # Remove HTML/script tags
    cleaned = re.sub(r"<[^>]*>", "", cleaned)
    # Escape remaining special HTML characters
    cleaned = html.escape(cleaned)
    # Remove control characters except tab, space, newline
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", cleaned)
    return cleaned


# ---------------------------------------------------------------------------
# Pydantic Schemas - Response Models
# ---------------------------------------------------------------------------


class TagResponse(BaseModel):
    """Schema representing a tag in response payloads [FR-1, FR-7]."""

    id: UUID = Field(..., description="Unique tag UUID")
    name: str = Field(..., description="Tag display name")
    created_at: datetime = Field(..., description="Tag creation UTC timestamp")

    class Config:
        from_attributes = True


class ArticleSummaryResponse(BaseModel):
    """Schema representing a lightweight article summary card [FR-1, FR-2, FR-7]."""

    id: UUID = Field(..., description="Unique article UUID")
    title: str = Field(..., description="Article title")
    content_type: str = Field(..., description="Content type (FAQ, Playbook, BestPractice, TrainingVideo)")
    body_snippet: str = Field(..., description="Brief truncated snippet of article content")
    tenant_id: str | None = Field(default=None, description="Client tenant ID or null for global resources")
    category_id: UUID | None = Field(default=None, description="Associated category UUID")
    category_name: str | None = Field(default=None, description="Associated category name")
    tags: list[TagResponse] = Field(default_factory=list, description="List of associated tags")
    video_url: str | None = Field(default=None, description="Optional external video URL")
    is_published: bool = Field(default=True, description="Whether the article is published")
    created_at: datetime = Field(..., description="Article creation UTC timestamp")
    updated_at: datetime = Field(..., description="Article last update UTC timestamp")

    class Config:
        from_attributes = True


class ArticleSearchResponse(BaseModel):
    """Schema representing paginated search results for knowledge articles [FR-1, FR-2, NFR-1, NFR-3]."""

    items: list[ArticleSummaryResponse] = Field(..., description="List of matching article summary cards")
    total: int = Field(..., description="Total count of matching distinct articles")
    page: int = Field(..., description="Current page index (starts at 1)")
    limit: int = Field(..., description="Items per page limit")
    total_pages: int = Field(..., description="Total available pages")

    class Config:
        from_attributes = True


class CategoryResponse(BaseModel):
    """Schema representing a category in response payloads [FR-1, FR-7]."""

    id: UUID = Field(..., description="Unique category UUID")
    name: str = Field(..., description="Category display name")
    description: str | None = Field(default=None, description="Optional category description")
    created_at: datetime = Field(..., description="Category creation UTC timestamp")

    class Config:
        from_attributes = True


class AttachmentResponse(BaseModel):
    """Schema representing a document attachment in response payloads [FR-6]."""

    id: UUID = Field(..., description="Unique attachment UUID")
    article_id: UUID = Field(..., description="Parent article UUID")
    file_name: str = Field(..., description="Attachment file display name")
    file_url: str = Field(..., description="Attachment download or preview URL")
    file_size_bytes: int = Field(..., description="File size in bytes")
    created_at: datetime = Field(..., description="Attachment creation UTC timestamp")

    class Config:
        from_attributes = True


class PlaybookStepResponse(BaseModel):
    """Schema representing an operational playbook step in response payloads [FR-3]."""

    id: UUID = Field(..., description="Unique playbook step UUID")
    article_id: UUID = Field(..., description="Parent article UUID")
    step_number: int = Field(..., description="Sequential step index starting at 1")
    title: str = Field(..., description="Step display title")
    content: str = Field(..., description="Step detailed instructional content")
    checkpoint_instructions: str | None = Field(
        default=None, description="Optional verification or checkpoint guidance"
    )

    class Config:
        from_attributes = True


class ArticleDetailResponse(BaseModel):
    """Schema representing full detailed article contents [FR-1, FR-2, FR-3, FR-5, FR-6, FR-9]."""

    id: UUID = Field(..., description="Unique article UUID")
    title: str = Field(..., description="Article title")
    content_type: str = Field(..., description="Content type (FAQ, Playbook, BestPractice, TrainingVideo)")
    body: str = Field(..., description="Full body content or markdown text")
    tenant_id: str | None = Field(default=None, description="Client tenant ID or null for global resources")
    category: CategoryResponse | None = Field(default=None, description="Associated category details")
    tags: list[TagResponse] = Field(default_factory=list, description="List of associated tags")
    playbook_steps: list[PlaybookStepResponse] = Field(
        default_factory=list, description="Ordered list of operational playbook steps"
    )
    attachments: list[AttachmentResponse] = Field(
        default_factory=list, description="List of linked technical attachments and documents"
    )
    video_url: str | None = Field(default=None, description="Optional external video media URL")
    is_published: bool = Field(default=True, description="Whether the article is published")
    created_at: datetime = Field(..., description="Article creation UTC timestamp")
    updated_at: datetime = Field(..., description="Article last update UTC timestamp")

    class Config:
        from_attributes = True


class PlaybookStepProgressRequest(BaseModel):
    """Schema representing step completion status toggle payload [FR-4, NFR-3]."""

    is_completed: bool = Field(..., description="Whether the step is marked as completed")


class PlaybookStepProgressResponse(BaseModel):
    """Schema representing step completion progress response payload [FR-3, FR-4]."""

    step_id: UUID = Field(..., description="Unique playbook step UUID")
    is_completed: bool = Field(..., description="Step completion status")
    completed_at: datetime | None = Field(
        default=None, description="UTC timestamp when step was completed or null if uncompleted"
    )
    overall_playbook_progress_percentage: float = Field(
        ..., description="Calculated overall playbook completion percentage (0.0 to 100.0)"
    )

    class Config:
        from_attributes = True


class PlaybookProgressResponse(BaseModel):
    """Schema representing user overall playbook completion progress summary payload [FR-3, FR-4]."""

    article_id: UUID = Field(..., description="Unique playbook article UUID")
    completed_step_ids: list[UUID] = Field(
        default_factory=list, description="List of completed step UUIDs for current user"
    )
    total_completed: int = Field(..., description="Count of completed steps")
    total_steps: int = Field(..., description="Total count of steps in playbook")
    overall_playbook_progress_percentage: float = Field(
        ..., description="Overall completion progress percentage (0.0 to 100.0)"
    )

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Route Handlers
# ---------------------------------------------------------------------------


@router.get(
    "/articles",
    response_model=ArticleSearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Search and catalog knowledge articles",
    description=(
        "Search and filter knowledge base articles by query text, category_id, tag_ids, "
        "and content_type with input parameter validation, structured JSON logging, and client tenant boundary isolation "
        "[FR-1, FR-2, FR-7, FR-8, FR-9, NFR-1, NFR-3, NFR-4]."
    ),
)
def search_articles_endpoint(
    query: str | None = Query(default=None, description="Search keyword for article title, body, category, or tags"),
    category_id: str | None = Query(default=None, description="Optional category UUID filter"),
    tag_ids: list[str] | None = Query(default=None, description="Optional list of tag UUID filters"),
    content_type: str | None = Query(
        default=None, description="Optional content type filter ('FAQ', 'Playbook', 'BestPractice', 'TrainingVideo')"
    ),
    page: int = Query(default=1, ge=1, description="Page index starting at 1"),
    limit: int = Query(default=20, ge=1, le=100, description="Items per page limit (1-100)"),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> ArticleSearchResponse:
    """Search and filter knowledge articles catalog with input validation and structured logging."""
    sanitized_query = sanitize_input_string(query) if query else None
    user_id_str = str(current_user.user_id) if current_user and current_user.user_id else None
    tenant_id_str = str(current_user.tenant_id) if current_user and current_user.tenant_id else None

    if sanitized_query and len(sanitized_query) > 500:
        logger.warning(
            "Search query exceeds maximum allowed length of 500 characters",
            extra={
                "correlation_id": get_correlation_id(),
                "query_length": len(sanitized_query),
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Search query exceeds maximum allowed length of 500 characters.",
        )

    sanitized_category_id = sanitize_input_string(category_id) if category_id else None
    sanitized_content_type = sanitize_input_string(content_type) if content_type else None

    parsed_tag_ids: list[str] = []
    if tag_ids:
        for tag_item in tag_ids:
            if tag_item:
                # Support comma-separated tag ID query parameters if supplied
                parts = [p.strip() for p in tag_item.split(",") if p.strip()]
                for part in parts:
                    cleaned_part = sanitize_input_string(part)
                    if cleaned_part:
                        parsed_tag_ids.append(cleaned_part)

    logger.info(
        "Executing knowledge articles search",
        extra={
            "correlation_id": get_correlation_id(),
            "user_id": user_id_str,
            "tenant_id": tenant_id_str,
            "query": sanitized_query,
            "category_id": sanitized_category_id,
            "content_type": sanitized_content_type,
            "tag_ids_count": len(parsed_tag_ids),
            "page": page,
            "limit": limit,
        },
    )

    try:
        search_result = search_articles(
            session=db,
            query=sanitized_query,
            category_id=sanitized_category_id,
            tag_ids=parsed_tag_ids if parsed_tag_ids else None,
            content_type=sanitized_content_type,
            tenant_id=tenant_id_str,
            include_all_tenants=False,
            is_published=True,
            page=page,
            limit=limit,
        )

        items_response = [
            ArticleSummaryResponse(
                id=item.id,
                title=item.title,
                content_type=item.content_type,
                body_snippet=item.body_snippet,
                tenant_id=item.tenant_id,
                category_id=item.category_id,
                category_name=item.category_name,
                tags=[
                    TagResponse(
                        id=t.id,
                        name=t.name,
                        created_at=t.created_at,
                    )
                    for t in item.tags
                ],
                video_url=item.video_url,
                is_published=item.is_published,
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
            for item in search_result.items
        ]

        logger.info(
            "Knowledge articles search completed successfully",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "total_results": search_result.total,
                "returned_count": len(items_response),
                "page": search_result.page,
                "total_pages": search_result.total_pages,
            },
        )

        return ArticleSearchResponse(
            items=items_response,
            total=search_result.total,
            page=search_result.page,
            limit=search_result.limit,
            total_pages=search_result.total_pages,
        )
    except (InvalidArticleDataError, CategoryNotFoundError, TagNotFoundError) as exc:
        logger.warning(
            f"Invalid parameters for knowledge article search: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ArticleError as exc:
        logger.warning(
            f"Knowledge article search domain error: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            f"Failed to execute knowledge article search: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while searching knowledge articles.",
        ) from exc


@router.get(
    "/articles/{id}",
    response_model=ArticleDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Get knowledge article detail by ID",
    description=(
        "Retrieve full detailed knowledge article contents with category, tags, playbook steps, "
        "and attachments, filtered by tenant boundary permissions [FR-1, FR-2, FR-3, FR-5, FR-6, FR-9, NFR-1, NFR-3, NFR-4]."
    ),
)
def get_article_detail_endpoint(
    id: str,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> ArticleDetailResponse:
    """Retrieve detailed knowledge article by ID with multi-tenant access control and structured logging."""
    sanitized_id = sanitize_input_string(id)
    tenant_id_str = str(current_user.tenant_id) if current_user and current_user.tenant_id else None
    user_id_str = str(current_user.user_id) if current_user and current_user.user_id else None

    if not sanitized_id:
        logger.warning(
            "Article ID parameter is empty",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Article ID parameter must not be empty.",
        )

    logger.info(
        f"Retrieving knowledge article detail for ID '{sanitized_id}'",
        extra={
            "correlation_id": get_correlation_id(),
            "article_id": sanitized_id,
            "user_id": user_id_str,
            "tenant_id": tenant_id_str,
        },
    )

    try:
        article = get_article_by_id(
            session=db,
            article_id=sanitized_id,
            tenant_id=tenant_id_str,
            include_all_tenants=False,
            is_published_only=True,
        )

        category_resp = (
            CategoryResponse(
                id=article.category.id,
                name=article.category.name,
                description=article.category.description,
                created_at=article.category.created_at,
            )
            if article.category
            else None
        )

        tags_resp = [
            TagResponse(
                id=t.id,
                name=t.name,
                created_at=t.created_at,
            )
            for t in article.tags
        ]

        steps_resp = [
            PlaybookStepResponse(
                id=s.id,
                article_id=s.article_id,
                step_number=s.step_number,
                title=s.title,
                content=s.content,
                checkpoint_instructions=s.checkpoint_instructions,
            )
            for s in article.playbook_steps
        ]

        attachments_resp = [
            AttachmentResponse(
                id=a.id,
                article_id=a.article_id,
                file_name=a.file_name,
                file_url=a.file_url,
                file_size_bytes=a.file_size_bytes,
                created_at=a.created_at,
            )
            for a in article.attachments
        ]

        logger.info(
            f"Retrieved knowledge article detail for ID '{sanitized_id}' successfully",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "content_type": article.content_type,
                "steps_count": len(steps_resp),
                "attachments_count": len(attachments_resp),
            },
        )

        return ArticleDetailResponse(
            id=article.id,
            title=article.title,
            content_type=article.content_type,
            body=article.body,
            tenant_id=article.tenant_id,
            category=category_resp,
            tags=tags_resp,
            playbook_steps=steps_resp,
            attachments=attachments_resp,
            video_url=article.video_url,
            is_published=article.is_published,
            created_at=article.created_at,
            updated_at=article.updated_at,
        )
    except ArticleNotFoundError as exc:
        logger.warning(
            f"Knowledge article not found or inaccessible for ID '{sanitized_id}': {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except InvalidArticleDataError as exc:
        logger.warning(
            f"Invalid parameter for knowledge article detail ID '{sanitized_id}': {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ArticleError as exc:
        logger.warning(
            f"Knowledge article domain error for ID '{sanitized_id}': {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            f"Failed to retrieve knowledge article detail for ID '{sanitized_id}': {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while retrieving knowledge article details.",
        ) from exc


@router.post(
    "/playbooks/{id}/steps/{step_id}/progress",
    response_model=PlaybookStepProgressResponse,
    status_code=status.HTTP_200_OK,
    summary="Toggle playbook step completion progress",
    description=(
        "Mark an individual step in an operational playbook as completed or uncompleted "
        "and return updated completion status and overall progress percentage [FR-3, FR-4, FR-8, FR-9, NFR-1, NFR-3, NFR-4]."
    ),
)
def toggle_playbook_step_progress_endpoint(
    id: str,
    step_id: str,
    payload: PlaybookStepProgressRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> PlaybookStepProgressResponse:
    """Toggle playbook step completion status and return updated progress summary with structured logging."""
    sanitized_id = sanitize_input_string(id)
    sanitized_step_id = sanitize_input_string(step_id)

    user_id_str = str(current_user.user_id) if current_user and current_user.user_id else None
    tenant_id_str = str(current_user.tenant_id) if current_user and current_user.tenant_id else None

    if not sanitized_id:
        logger.warning(
            "Playbook ID parameter is empty",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Playbook ID parameter must not be empty.",
        )
    if not sanitized_step_id:
        logger.warning(
            "Step ID parameter is empty",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Step ID parameter must not be empty.",
        )

    if not user_id_str or not tenant_id_str:
        logger.warning(
            "Authentication context missing user_id or tenant_id",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User authentication context missing user ID or tenant ID.",
        )

    logger.info(
        f"Toggling playbook step progress for playbook '{sanitized_id}' step '{sanitized_step_id}' to is_completed={payload.is_completed}",
        extra={
            "correlation_id": get_correlation_id(),
            "article_id": sanitized_id,
            "step_id": sanitized_step_id,
            "is_completed": payload.is_completed,
            "user_id": user_id_str,
            "tenant_id": tenant_id_str,
        },
    )

    try:
        progress_dto = toggle_step_completion(
            db=db,
            article_id=sanitized_id,
            step_id=sanitized_step_id,
            user_id=user_id_str,
            tenant_id=tenant_id_str,
            is_completed=payload.is_completed,
        )

        logger.info(
            f"Toggled playbook step progress successfully for playbook '{sanitized_id}' step '{sanitized_step_id}'",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "step_id": sanitized_step_id,
                "is_completed": progress_dto.is_completed,
                "overall_progress_pct": progress_dto.overall_playbook_progress_percentage,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
            },
        )

        return PlaybookStepProgressResponse(
            step_id=progress_dto.step_id,
            is_completed=progress_dto.is_completed,
            completed_at=progress_dto.completed_at,
            overall_playbook_progress_percentage=progress_dto.overall_playbook_progress_percentage,
        )
    except (PlaybookNotFoundError, StepNotFoundError) as exc:
        logger.warning(
            f"Playbook or step resource not found: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "step_id": sanitized_step_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (InvalidStepReferenceError, InvalidPlaybookDataError) as exc:
        logger.warning(
            f"Invalid playbook step reference or parameter: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "step_id": sanitized_step_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except PlaybookError as exc:
        logger.warning(
            f"Playbook domain error: {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "step_id": sanitized_step_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            f"Failed to toggle playbook step progress for playbook '{sanitized_id}' step '{sanitized_step_id}': {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "step_id": sanitized_step_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating playbook step progress.",
        ) from exc


@router.get(
    "/playbooks/{id}/progress",
    response_model=PlaybookProgressResponse,
    status_code=status.HTTP_200_OK,
    summary="Get user overall playbook progress",
    description=(
        "Retrieve current user's step completion progress summary and overall percentage "
        "for an operational playbook [FR-3, FR-4, FR-8, FR-9, NFR-1, NFR-3, NFR-4]."
    ),
)
def get_playbook_progress_endpoint(
    id: str,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> PlaybookProgressResponse:
    """Retrieve overall playbook progress summary for the authenticated user with structured logging."""
    sanitized_id = sanitize_input_string(id)

    user_id_str = str(current_user.user_id) if current_user and current_user.user_id else None
    tenant_id_str = str(current_user.tenant_id) if current_user and current_user.tenant_id else None

    if not sanitized_id:
        logger.warning(
            "Playbook ID parameter is empty",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Playbook ID parameter must not be empty.",
        )

    if not user_id_str or not tenant_id_str:
        logger.warning(
            "Authentication context missing user_id or tenant_id",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User authentication context missing user ID or tenant ID.",
        )

    logger.info(
        f"Retrieving user overall playbook progress for playbook '{sanitized_id}'",
        extra={
            "correlation_id": get_correlation_id(),
            "article_id": sanitized_id,
            "user_id": user_id_str,
            "tenant_id": tenant_id_str,
        },
    )

    try:
        summary = get_playbook_progress(
            db=db,
            article_id=sanitized_id,
            user_id=user_id_str,
            tenant_id=tenant_id_str,
        )

        logger.info(
            f"Retrieved user overall playbook progress for playbook '{sanitized_id}' successfully",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "total_completed": summary.total_completed,
                "total_steps": summary.total_steps,
                "overall_progress_pct": summary.overall_playbook_progress_percentage,
            },
        )

        return PlaybookProgressResponse(
            article_id=summary.article_id,
            completed_step_ids=summary.completed_step_ids,
            total_completed=summary.total_completed,
            total_steps=summary.total_steps,
            overall_playbook_progress_percentage=summary.overall_playbook_progress_percentage,
        )
    except PlaybookNotFoundError as exc:
        logger.warning(
            f"Playbook not found or inaccessible for ID '{sanitized_id}': {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except InvalidPlaybookDataError as exc:
        logger.warning(
            f"Invalid parameter for playbook progress ID '{sanitized_id}': {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except PlaybookError as exc:
        logger.warning(
            f"Playbook domain error for ID '{sanitized_id}': {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            f"Failed to retrieve playbook progress for playbook '{sanitized_id}': {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while retrieving playbook progress.",
        ) from exc


@router.delete(
    "/articles/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Delete knowledge article by ID",
    description=(
        "Permanently delete a knowledge article and its child resources by ID "
        "with multi-tenant boundary validation [FR-1, FR-9, NFR-3, NFR-4]."
    ),
)
def delete_article_endpoint(
    id: str,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> Response:
    """Delete a knowledge article and its child resources by ID with structured logging."""
    sanitized_id = sanitize_input_string(id)
    tenant_id_str = str(current_user.tenant_id) if current_user and current_user.tenant_id else None
    user_id_str = str(current_user.user_id) if current_user and current_user.user_id else None

    if not sanitized_id:
        logger.warning(
            "Article ID parameter is empty for deletion",
            extra={
                "correlation_id": get_correlation_id(),
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Article ID parameter must not be empty.",
        )

    logger.info(
        f"Executing deletion of knowledge article ID '{sanitized_id}'",
        extra={
            "correlation_id": get_correlation_id(),
            "article_id": sanitized_id,
            "user_id": user_id_str,
            "tenant_id": tenant_id_str,
        },
    )

    try:
        delete_article(
            session=db,
            article_id=sanitized_id,
            tenant_id=tenant_id_str,
            include_all_tenants=False,
        )

        logger.info(
            f"Knowledge article ID '{sanitized_id}' deleted successfully",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
            },
        )

        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except ArticleNotFoundError as exc:
        logger.warning(
            f"Knowledge article not found or inaccessible for deletion ID '{sanitized_id}': {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except InvalidArticleDataError as exc:
        logger.warning(
            f"Invalid parameter for knowledge article deletion ID '{sanitized_id}': {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ArticleError as exc:
        logger.warning(
            f"Knowledge article domain error on deletion for ID '{sanitized_id}': {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            f"Failed to delete knowledge article for ID '{sanitized_id}': {exc}",
            extra={
                "correlation_id": get_correlation_id(),
                "article_id": sanitized_id,
                "user_id": user_id_str,
                "tenant_id": tenant_id_str,
                "error": str(exc),
            },
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while deleting the knowledge article.",
        ) from exc
