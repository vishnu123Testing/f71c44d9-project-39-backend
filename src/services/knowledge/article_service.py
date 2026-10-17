"""Knowledge Article service implementation [FR-1, FR-2, FR-7, FR-8, FR-9, NFR-1, NFR-3, NFR-4].

This module encapsulates business logic for:
- Full-text and keyword search across article titles, body content, tags, and category metadata (FR-2)
- Categorization and filtering by content type, category ID, and tag IDs (FR-1, FR-7)
- Multi-tenant boundary isolation enforcing client access policies (FR-9)
- Retrieving detailed knowledge articles with joined categories, tags, steps, and attachments (FR-1)
- Creating, updating, and deleting knowledge articles and associated resources
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import math
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from src.lib.logging import get_correlation_id
from src.models.knowledge import (
    ArticleTag,
    Attachment,
    Category,
    KnowledgeArticle,
    PlaybookStep,
    Tag,
)

logger = logging.getLogger(__name__)

VALID_CONTENT_TYPES: set[str] = {"FAQ", "Playbook", "BestPractice", "TrainingVideo"}


class ArticleError(ValueError):
    """Base domain exception for article service errors."""

    pass


class ArticleNotFoundError(ArticleError):
    """Raised when a requested knowledge article is not found or not accessible."""

    pass


class CategoryNotFoundError(ArticleError):
    """Raised when a specified category is not found."""

    pass


class TagNotFoundError(ArticleError):
    """Raised when a specified tag is not found."""

    pass


class InvalidArticleDataError(ArticleError):
    """Raised when knowledge article input parameters fail validation rules."""

    pass


def _ensure_utc(dt: datetime | None) -> datetime:
    """Ensure a datetime instance is timezone-aware in UTC.

    Args:
        dt: Optional datetime object.

    Returns:
        Timezone-aware datetime in UTC.
    """
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_uuid(val: UUID | str) -> UUID:
    """Safely convert a string or UUID object into a UUID instance.

    Args:
        val: UUID object or string representation.

    Returns:
        UUID instance.

    Raises:
        InvalidArticleDataError: If the provided string is not a valid UUID format.
    """
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val).strip())
    except (ValueError, AttributeError, TypeError) as err:
        raise InvalidArticleDataError(f"Invalid UUID string format: '{val}'") from err


def _make_body_snippet(body: str, max_length: int = 200) -> str:
    """Generate a brief truncated text snippet from an article body.

    Args:
        body: Plaintext or markdown article content.
        max_length: Maximum character length of the snippet.

    Returns:
        Truncated snippet string with ellipsis if trimmed.
    """
    if not body:
        return ""
    clean_body = body.strip()
    if len(clean_body) <= max_length:
        return clean_body
    return clean_body[:max_length].rstrip() + "..."


def _normalize_content_type(raw_type: str) -> str:
    """Normalize and validate a content type string.

    Args:
        raw_type: String representation of content type.

    Returns:
        Normalized matching content type string.

    Raises:
        InvalidArticleDataError: If the content type is not supported.
    """
    if not raw_type or not isinstance(raw_type, str):
        raise InvalidArticleDataError("Content type must be a non-empty string.")

    cleaned = raw_type.strip().lower()
    for valid_type in VALID_CONTENT_TYPES:
        if valid_type.lower() == cleaned:
            return valid_type

    raise InvalidArticleDataError(
        f"Invalid content type '{raw_type}'. Allowed types: {sorted(list(VALID_CONTENT_TYPES))}"
    )


@dataclass
class CategoryDTO:
    """Data transfer object representing a content category [FR-1, FR-7]."""

    id: UUID
    name: str
    description: str | None
    created_at: datetime


@dataclass
class TagDTO:
    """Data transfer object representing a functional tag [FR-1, FR-7]."""

    id: UUID
    name: str
    created_at: datetime


@dataclass
class AttachmentDTO:
    """Data transfer object representing a document attachment linked to an article [FR-6]."""

    id: UUID
    article_id: UUID
    file_name: str
    file_url: str
    file_size_bytes: int
    created_at: datetime


@dataclass
class PlaybookStepDTO:
    """Data transfer object representing a step in an operational playbook [FR-3]."""

    id: UUID
    article_id: UUID
    step_number: int
    title: str
    content: str
    checkpoint_instructions: str | None


@dataclass
class ArticleSummaryDTO:
    """Data transfer object representing a lightweight article summary card [FR-1, FR-2]."""

    id: UUID
    title: str
    content_type: str
    body_snippet: str
    tenant_id: str | None
    category_id: UUID | None
    category_name: str | None
    tags: list[TagDTO]
    video_url: str | None
    is_published: bool
    created_at: datetime
    updated_at: datetime


@dataclass
class ArticleDetailDTO:
    """Data transfer object representing full detailed article contents [FR-1, FR-2, FR-3, FR-5, FR-6]."""

    id: UUID
    title: str
    content_type: str
    body: str
    tenant_id: str | None
    category: CategoryDTO | None
    tags: list[TagDTO] = field(default_factory=list)
    playbook_steps: list[PlaybookStepDTO] = field(default_factory=list)
    attachments: list[AttachmentDTO] = field(default_factory=list)
    video_url: str | None = None
    is_published: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ArticleSearchResultDTO:
    """Data transfer object containing paginated search and filter results [FR-2, NFR-1]."""

    items: list[ArticleSummaryDTO]
    total: int
    page: int
    limit: int
    total_pages: int


def _article_to_summary_dto(article: KnowledgeArticle) -> ArticleSummaryDTO:
    """Convert a KnowledgeArticle ORM instance to an ArticleSummaryDTO.

    Args:
        article: KnowledgeArticle ORM object.

    Returns:
        Populated ArticleSummaryDTO instance.
    """
    cat_name = article.category.name if article.category else None
    tags_list = [
        TagDTO(
            id=tag.id,
            name=tag.name,
            created_at=_ensure_utc(tag.created_at),
        )
        for tag in (article.tags or [])
    ]
    return ArticleSummaryDTO(
        id=article.id,
        title=article.title,
        content_type=article.content_type,
        body_snippet=_make_body_snippet(article.body),
        tenant_id=article.tenant_id,
        category_id=article.category_id,
        category_name=cat_name,
        tags=tags_list,
        video_url=article.video_url,
        is_published=article.is_published,
        created_at=_ensure_utc(article.created_at),
        updated_at=_ensure_utc(article.updated_at),
    )


def _article_to_detail_dto(article: KnowledgeArticle) -> ArticleDetailDTO:
    """Convert a KnowledgeArticle ORM instance to an ArticleDetailDTO.

    Args:
        article: KnowledgeArticle ORM object.

    Returns:
        Populated ArticleDetailDTO instance.
    """
    category_dto = (
        CategoryDTO(
            id=article.category.id,
            name=article.category.name,
            description=article.category.description,
            created_at=_ensure_utc(article.category.created_at),
        )
        if article.category
        else None
    )

    tags_list = [
        TagDTO(
            id=tag.id,
            name=tag.name,
            created_at=_ensure_utc(tag.created_at),
        )
        for tag in (article.tags or [])
    ]

    attachments_list = [
        AttachmentDTO(
            id=att.id,
            article_id=att.article_id,
            file_name=att.file_name,
            file_url=att.file_url,
            file_size_bytes=att.file_size_bytes,
            created_at=_ensure_utc(att.created_at),
        )
        for att in (article.attachments or [])
    ]

    sorted_steps = sorted(article.playbook_steps or [], key=lambda s: s.step_number)
    steps_list = [
        PlaybookStepDTO(
            id=step.id,
            article_id=step.article_id,
            step_number=step.step_number,
            title=step.title,
            content=step.content,
            checkpoint_instructions=step.checkpoint_instructions,
        )
        for step in sorted_steps
    ]

    return ArticleDetailDTO(
        id=article.id,
        title=article.title,
        content_type=article.content_type,
        body=article.body,
        tenant_id=article.tenant_id,
        category=category_dto,
        tags=tags_list,
        playbook_steps=steps_list,
        attachments=attachments_list,
        video_url=article.video_url,
        is_published=article.is_published,
        created_at=_ensure_utc(article.created_at),
        updated_at=_ensure_utc(article.updated_at),
    )


def search_articles(
    session: Session,
    query: str | None = None,
    category_id: UUID | str | None = None,
    tag_ids: list[UUID | str] | None = None,
    content_type: str | None = None,
    tenant_id: str | None = None,
    include_all_tenants: bool = False,
    is_published: bool | None = True,
    page: int = 1,
    limit: int = 20,
) -> ArticleSearchResultDTO:
    """Perform multi-criteria full-text search and filtering across knowledge articles [FR-1, FR-2, FR-7, FR-8, FR-9, NFR-1].

    Args:
        session: Synchronous database session.
        query: Optional search keyword to match titles, body content, tags, or category metadata.
        category_id: Optional category ID filter.
        tag_ids: Optional list of tag IDs to filter by.
        content_type: Optional content type filter ("FAQ", "Playbook", "BestPractice", "TrainingVideo").
        tenant_id: Optional tenant identifier for client boundary isolation.
        include_all_tenants: If True, bypasses tenant restriction (e.g. for administrative access).
        is_published: If True, returns published articles only. Defaults to True.
        page: Page index starting at 1. Defaults to 1.
        limit: Number of items per page. Defaults to 20.

    Returns:
        ArticleSearchResultDTO containing matching items, total count, and pagination metadata.
    """
    page_num = max(1, page)
    limit_num = max(1, min(100, limit))

    stmt = select(KnowledgeArticle).options(
        joinedload(KnowledgeArticle.category),
        selectinload(KnowledgeArticle.tags),
    )

    # 1. Search Query Filter (FR-2)
    if query and query.strip():
        clean_q = query.strip()
        pattern = f"%{clean_q}%"
        stmt = stmt.where(
            or_(
                KnowledgeArticle.title.ilike(pattern),
                KnowledgeArticle.body.ilike(pattern),
                KnowledgeArticle.category.has(Category.name.ilike(pattern)),
                KnowledgeArticle.category.has(Category.description.ilike(pattern)),
                KnowledgeArticle.tags.any(Tag.name.ilike(pattern)),
            )
        )

    # 2. Category Filter (FR-1, FR-7)
    if category_id:
        parsed_cat_id = _parse_uuid(category_id)
        stmt = stmt.where(KnowledgeArticle.category_id == parsed_cat_id)

    # 3. Tag IDs Filter (FR-1, FR-7)
    if tag_ids:
        parsed_tag_ids = [_parse_uuid(t) for t in tag_ids if t]
        if parsed_tag_ids:
            stmt = stmt.where(KnowledgeArticle.tags.any(Tag.id.in_(parsed_tag_ids)))

    # 4. Content Type Filter (FR-7)
    if content_type and content_type.strip():
        norm_type = _normalize_content_type(content_type)
        stmt = stmt.where(
            func.lower(KnowledgeArticle.content_type) == norm_type.lower()
        )

    # 5. Multi-Tenant Boundary Isolation (FR-9)
    if not include_all_tenants:
        if tenant_id and tenant_id.strip():
            stmt = stmt.where(
                or_(
                    KnowledgeArticle.tenant_id == tenant_id.strip(),
                    KnowledgeArticle.tenant_id.is_(None),
                )
            )
        else:
            stmt = stmt.where(KnowledgeArticle.tenant_id.is_(None))

    # 6. Publishing State Filter
    if is_published is not None:
        stmt = stmt.where(KnowledgeArticle.is_published == is_published)

    # Calculate total count of matching distinct articles
    count_stmt = select(func.count(func.distinct(KnowledgeArticle.id))).select_from(
        stmt.subquery()
    )
    total_count = session.scalar(count_stmt) or 0

    # Apply ordering and pagination
    offset_val = (page_num - 1) * limit_num
    paginated_stmt = (
        stmt.order_by(
            KnowledgeArticle.updated_at.desc(),
            KnowledgeArticle.created_at.desc(),
        )
        .offset(offset_val)
        .limit(limit_num)
    )

    articles = session.scalars(paginated_stmt).unique().all()
    summary_dtos = [_article_to_summary_dto(a) for a in articles]
    total_pages = math.ceil(total_count / limit_num) if total_count > 0 else 0

    logger.info(
        "Executed knowledge article search query",
        extra={
            "correlation_id": get_correlation_id(),
            "query": query,
            "category_id": str(category_id) if category_id else None,
            "content_type": content_type,
            "tenant_id": tenant_id,
            "total_count": total_count,
            "page": page_num,
            "limit": limit_num,
        },
    )

    return ArticleSearchResultDTO(
        items=summary_dtos,
        total=total_count,
        page=page_num,
        limit=limit_num,
        total_pages=total_pages,
    )


def get_article_by_id(
    session: Session,
    article_id: UUID | str,
    tenant_id: str | None = None,
    include_all_tenants: bool = False,
    is_published_only: bool = True,
) -> ArticleDetailDTO:
    """Retrieve full knowledge article details by ID with tenant boundary validation [FR-1, FR-2, FR-9].

    Args:
        session: Synchronous database session.
        article_id: UUID or string ID of the knowledge article.
        tenant_id: Optional tenant boundary identifier.
        include_all_tenants: If True, bypasses tenant boundary check.
        is_published_only: If True, rejects unpublished articles.

    Returns:
        ArticleDetailDTO containing full article attributes, category, tags, steps, and attachments.

    Raises:
        ArticleNotFoundError: If the article does not exist or is inaccessible to the caller tenant.
    """
    parsed_id = _parse_uuid(article_id)

    stmt = (
        select(KnowledgeArticle)
        .options(
            joinedload(KnowledgeArticle.category),
            selectinload(KnowledgeArticle.tags),
            selectinload(KnowledgeArticle.playbook_steps),
            selectinload(KnowledgeArticle.attachments),
        )
        .where(KnowledgeArticle.id == parsed_id)
    )

    article = session.scalars(stmt).unique().first()

    if not article:
        logger.warning(
            f"Knowledge article not found with ID {parsed_id}",
            extra={"correlation_id": get_correlation_id(), "article_id": str(parsed_id)},
        )
        raise ArticleNotFoundError(f"Knowledge article not found with ID '{parsed_id}'.")

    # Verify publishing status if restricted
    if is_published_only and not article.is_published:
        logger.warning(
            f"Attempted access to unpublished knowledge article {parsed_id}",
            extra={"correlation_id": get_correlation_id(), "article_id": str(parsed_id)},
        )
        raise ArticleNotFoundError(f"Knowledge article not found with ID '{parsed_id}'.")

    # Multi-Tenant Boundary Enforcement (FR-9)
    if not include_all_tenants and article.tenant_id is not None:
        if not tenant_id or article.tenant_id != tenant_id.strip():
            logger.warning(
                f"Multi-tenant access rejected for article {parsed_id}",
                extra={
                    "correlation_id": get_correlation_id(),
                    "article_id": str(parsed_id),
                    "article_tenant_id": article.tenant_id,
                    "caller_tenant_id": tenant_id,
                },
            )
            raise ArticleNotFoundError(f"Knowledge article not found with ID '{parsed_id}'.")

    return _article_to_detail_dto(article)


def get_article_model(
    session: Session,
    article_id: UUID | str,
    tenant_id: str | None = None,
    include_all_tenants: bool = False,
) -> KnowledgeArticle:
    """Retrieve raw KnowledgeArticle ORM model instance for internal service operations.

    Args:
        session: Synchronous database session.
        article_id: UUID or string ID of the article.
        tenant_id: Optional caller tenant identifier.
        include_all_tenants: If True, bypasses tenant restriction.

    Returns:
        KnowledgeArticle ORM instance.

    Raises:
        ArticleNotFoundError: If article is not found or tenant unauthorized.
    """
    parsed_id = _parse_uuid(article_id)
    stmt = select(KnowledgeArticle).where(KnowledgeArticle.id == parsed_id)
    article = session.scalars(stmt).first()

    if not article:
        raise ArticleNotFoundError(f"Knowledge article not found with ID '{parsed_id}'.")

    if not include_all_tenants and article.tenant_id is not None:
        if not tenant_id or article.tenant_id != tenant_id.strip():
            raise ArticleNotFoundError(f"Knowledge article not found with ID '{parsed_id}'.")

    return article


def create_article(
    session: Session,
    title: str,
    content_type: str,
    body: str,
    tenant_id: str | None = None,
    category_id: UUID | str | None = None,
    tag_ids: list[UUID | str] | None = None,
    video_url: str | None = None,
    is_published: bool = True,
    playbook_steps_data: list[dict[str, Any]] | None = None,
    attachments_data: list[dict[str, Any]] | None = None,
) -> ArticleDetailDTO:
    """Create a new knowledge article with optional category, tags, steps, and attachments [FR-1, FR-3, FR-6, FR-7].

    Args:
        session: Synchronous database session.
        title: Title of the knowledge resource.
        content_type: Content type classification ("FAQ", "Playbook", "BestPractice", "TrainingVideo").
        body: Body text or markdown content.
        tenant_id: Optional client tenant isolation ID.
        category_id: Optional associated category ID.
        tag_ids: Optional list of tag IDs to link.
        video_url: Optional media URL for video training modules.
        is_published: Publishing status boolean flag. Defaults to True.
        playbook_steps_data: Optional list of dicts for initializing playbook steps.
        attachments_data: Optional list of dicts for initializing attachments.

    Returns:
        ArticleDetailDTO representing the created article.
    """
    if not title or not title.strip():
        raise InvalidArticleDataError("Article title must not be empty.")
    if not body or not body.strip():
        raise InvalidArticleDataError("Article body content must not be empty.")

    norm_type = _normalize_content_type(content_type)
    now_utc = datetime.now(timezone.utc)
    article_id = uuid4()

    parsed_cat_id = _parse_uuid(category_id) if category_id else None
    if parsed_cat_id:
        category_obj = session.get(Category, parsed_cat_id)
        if not category_obj:
            raise CategoryNotFoundError(f"Category not found with ID '{parsed_cat_id}'.")

    article = KnowledgeArticle(
        id=article_id,
        title=title.strip(),
        content_type=norm_type,
        body=body.strip(),
        tenant_id=tenant_id.strip() if tenant_id and tenant_id.strip() else None,
        category_id=parsed_cat_id,
        video_url=video_url.strip() if video_url and video_url.strip() else None,
        is_published=is_published,
        created_at=now_utc,
        updated_at=now_utc,
    )
    session.add(article)

    # Attach Tags
    if tag_ids:
        parsed_tag_ids = [_parse_uuid(tid) for tid in tag_ids if tid]
        if parsed_tag_ids:
            tags = session.scalars(select(Tag).where(Tag.id.in_(parsed_tag_ids))).all()
            article.tags = list(tags)

    # Initialize Playbook Steps
    if playbook_steps_data:
        for index, step_dict in enumerate(playbook_steps_data, start=1):
            step_num = step_dict.get("step_number", index)
            step_title = step_dict.get("title", f"Step {step_num}")
            step_content = step_dict.get("content", "")
            step_checkpoint = step_dict.get("checkpoint_instructions")

            step_obj = PlaybookStep(
                id=uuid4(),
                article_id=article_id,
                step_number=step_num,
                title=step_title.strip() if step_title else f"Step {step_num}",
                content=step_content.strip() if step_content else "",
                checkpoint_instructions=(
                    step_checkpoint.strip() if step_checkpoint else None
                ),
            )
            session.add(step_obj)

    # Initialize Attachments
    if attachments_data:
        for att_dict in attachments_data:
            f_name = att_dict.get("file_name", "Document")
            f_url = att_dict.get("file_url", "#")
            f_size = att_dict.get("file_size_bytes", 0)

            att_obj = Attachment(
                id=uuid4(),
                article_id=article_id,
                file_name=f_name.strip() if f_name else "Document",
                file_url=f_url.strip() if f_url else "#",
                file_size_bytes=max(0, int(f_size)),
                created_at=now_utc,
            )
            session.add(att_obj)

    session.flush()
    session.commit()

    logger.info(
        f"Created knowledge article {article_id}",
        extra={
            "correlation_id": get_correlation_id(),
            "article_id": str(article_id),
            "content_type": norm_type,
            "tenant_id": article.tenant_id,
        },
    )

    return get_article_by_id(session, article_id, include_all_tenants=True)


def update_article(
    session: Session,
    article_id: UUID | str,
    title: str | None = None,
    content_type: str | None = None,
    body: str | None = None,
    category_id: UUID | str | None = None,
    tag_ids: list[UUID | str] | None = None,
    video_url: str | None = None,
    is_published: bool | None = None,
    tenant_id: str | None = None,
    include_all_tenants: bool = False,
) -> ArticleDetailDTO:
    """Update an existing knowledge article's attributes [FR-1, FR-7].

    Args:
        session: Synchronous database session.
        article_id: UUID or string ID of article to update.
        title: Optional new title.
        content_type: Optional new content type.
        body: Optional new body text.
        category_id: Optional new category ID.
        tag_ids: Optional new list of tag IDs.
        video_url: Optional new video URL.
        is_published: Optional new publishing status flag.
        tenant_id: Caller tenant ID.
        include_all_tenants: Bypass tenant boundary check.

    Returns:
        ArticleDetailDTO reflecting updated article values.
    """
    article = get_article_model(session, article_id, tenant_id=tenant_id, include_all_tenants=include_all_tenants)

    if title is not None:
        if not title.strip():
            raise InvalidArticleDataError("Article title must not be empty.")
        article.title = title.strip()

    if body is not None:
        if not body.strip():
            raise InvalidArticleDataError("Article body content must not be empty.")
        article.body = body.strip()

    if content_type is not None:
        article.content_type = _normalize_content_type(content_type)

    if category_id is not None:
        if category_id == "" or category_id is None:
            article.category_id = None
        else:
            parsed_cat_id = _parse_uuid(category_id)
            category_obj = session.get(Category, parsed_cat_id)
            if not category_obj:
                raise CategoryNotFoundError(f"Category not found with ID '{parsed_cat_id}'.")
            article.category_id = parsed_cat_id

    if tag_ids is not None:
        parsed_tag_ids = [_parse_uuid(tid) for tid in tag_ids if tid]
        if parsed_tag_ids:
            tags = session.scalars(select(Tag).where(Tag.id.in_(parsed_tag_ids))).all()
            article.tags = list(tags)
        else:
            article.tags = []

    if video_url is not None:
        article.video_url = video_url.strip() if video_url.strip() else None

    if is_published is not None:
        article.is_published = is_published

    article.updated_at = datetime.now(timezone.utc)
    session.commit()

    return get_article_by_id(session, article.id, include_all_tenants=True)


def delete_article(
    session: Session,
    article_id: UUID | str,
    tenant_id: str | None = None,
    include_all_tenants: bool = False,
) -> None:
    """Delete a knowledge article and cascade delete child steps and attachments [FR-1].

    Args:
        session: Synchronous database session.
        article_id: UUID or string ID of article to delete.
        tenant_id: Optional caller tenant boundary ID.
        include_all_tenants: Bypass tenant check if True.
    """
    article = get_article_model(session, article_id, tenant_id=tenant_id, include_all_tenants=include_all_tenants)
    session.delete(article)
    session.commit()

    logger.info(
        f"Deleted knowledge article {article_id}",
        extra={"correlation_id": get_correlation_id(), "article_id": str(article_id)},
    )


# -----------------------------------------------------------------------------
# Category and Tag Management Helper Services [FR-1, FR-7]
# -----------------------------------------------------------------------------


def list_categories(session: Session) -> list[CategoryDTO]:
    """Retrieve list of all content categories ordered by name [FR-1, FR-7].

    Args:
        session: Synchronous database session.

    Returns:
        List of CategoryDTO objects.
    """
    stmt = select(Category).order_by(Category.name.asc())
    categories = session.scalars(stmt).all()
    return [
        CategoryDTO(
            id=cat.id,
            name=cat.name,
            description=cat.description,
            created_at=_ensure_utc(cat.created_at),
        )
        for cat in categories
    ]


def create_category(
    session: Session, name: str, description: str | None = None
) -> CategoryDTO:
    """Create a new content category [FR-1, FR-7].

    Args:
        session: Synchronous database session.
        name: Unique category name.
        description: Optional description.

    Returns:
        Created CategoryDTO instance.
    """
    if not name or not name.strip():
        raise InvalidArticleDataError("Category name must not be empty.")

    clean_name = name.strip()
    existing = session.scalars(select(Category).where(func.lower(Category.name) == clean_name.lower())).first()
    if existing:
        return CategoryDTO(
            id=existing.id,
            name=existing.name,
            description=existing.description,
            created_at=_ensure_utc(existing.created_at),
        )

    cat = Category(
        id=uuid4(),
        name=clean_name,
        description=description.strip() if description else None,
        created_at=datetime.now(timezone.utc),
    )
    session.add(cat)
    session.commit()

    return CategoryDTO(
        id=cat.id,
        name=cat.name,
        description=cat.description,
        created_at=_ensure_utc(cat.created_at),
    )


def list_tags(session: Session) -> list[TagDTO]:
    """Retrieve list of all functional tags ordered by name [FR-1, FR-7].

    Args:
        session: Synchronous database session.

    Returns:
        List of TagDTO objects.
    """
    stmt = select(Tag).order_by(Tag.name.asc())
    tags = session.scalars(stmt).all()
    return [
        TagDTO(
            id=tag.id,
            name=tag.name,
            created_at=_ensure_utc(tag.created_at),
        )
        for tag in tags
    ]


def get_or_create_tags(session: Session, tag_names: list[str]) -> list[TagDTO]:
    """Ensure tags exist for a list of tag names and return their DTOs [FR-1, FR-7].

    Args:
        session: Synchronous database session.
        tag_names: List of string tag names.

    Returns:
        List of TagDTO objects.
    """
    result: list[TagDTO] = []
    now_utc = datetime.now(timezone.utc)

    for raw_name in tag_names:
        if not raw_name or not raw_name.strip():
            continue
        clean_name = raw_name.strip()
        existing = session.scalars(select(Tag).where(func.lower(Tag.name) == clean_name.lower())).first()
        if existing:
            result.append(
                TagDTO(
                    id=existing.id,
                    name=existing.name,
                    created_at=_ensure_utc(existing.created_at),
                )
            )
        else:
            new_tag = Tag(
                id=uuid4(),
                name=clean_name,
                created_at=now_utc,
            )
            session.add(new_tag)
            session.flush()
            result.append(
                TagDTO(
                    id=new_tag.id,
                    name=new_tag.name,
                    created_at=now_utc,
                )
            )

    session.commit()
    return result
