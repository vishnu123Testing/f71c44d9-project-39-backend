"""Playbook service implementation [FR-3, FR-4, FR-8, FR-9, NFR-1, NFR-3, NFR-4].

This module encapsulates business logic for:
- Sequential step progression and navigation within operational playbooks (FR-3)
- User step completion toggling and persistence across tenant boundaries (FR-4, FR-9)
- Overall playbook completion progress percentage calculation (FR-3, FR-4)
- Retrieving step details with user completion state
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.lib.logging import get_correlation_id
from src.models.knowledge import KnowledgeArticle, PlaybookStep, UserStepProgress

logger = logging.getLogger(__name__)


class PlaybookError(ValueError):
    """Base domain exception for playbook service errors."""

    pass


class PlaybookNotFoundError(PlaybookError):
    """Raised when a requested playbook article is not found, unpublished, or inaccessible."""

    pass


class StepNotFoundError(PlaybookError):
    """Raised when a specified step ID does not exist."""

    pass


class InvalidStepReferenceError(PlaybookError):
    """Raised when a step does not belong to the specified playbook article."""

    pass


class InvalidPlaybookDataError(PlaybookError):
    """Raised when input parameters fail validation rules."""

    pass


def _ensure_utc(dt: datetime | None) -> datetime:
    """Ensure a datetime instance is timezone-aware in UTC."""
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_uuid(val: UUID | str, param_name: str = "UUID") -> UUID:
    """Safely convert a string or UUID object into a UUID instance.

    Args:
        val: UUID object or string representation.
        param_name: Contextual name of parameter for error reporting.

    Returns:
        UUID instance.

    Raises:
        InvalidPlaybookDataError: If the provided string is not a valid UUID format.
    """
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val).strip())
    except (ValueError, AttributeError, TypeError) as err:
        raise InvalidPlaybookDataError(
            f"Invalid {param_name} format: '{val}'"
        ) from err


@dataclass
class PlaybookStepProgressDTO:
    """Data transfer object representing step completion outcome and updated overall progress [FR-4]."""

    step_id: UUID
    is_completed: bool
    completed_at: datetime | None
    overall_playbook_progress_percentage: float


@dataclass
class PlaybookProgressSummaryDTO:
    """Data transfer object representing user completion summary for a playbook [FR-3, FR-4]."""

    article_id: UUID
    completed_step_ids: list[UUID]
    total_completed: int
    total_steps: int
    overall_playbook_progress_percentage: float


@dataclass
class PlaybookStepDetailDTO:
    """Data transfer object representing a playbook step enriched with user progress [FR-3, FR-4]."""

    id: UUID
    article_id: UUID
    step_number: int
    title: str
    content: str
    checkpoint_instructions: str | None
    is_completed: bool
    completed_at: datetime | None


def _get_published_playbook_article(
    db: Session,
    article_id: UUID,
    tenant_id: str,
) -> KnowledgeArticle:
    """Fetch published KnowledgeArticle verifying tenant access policy [FR-9].

    Args:
        db: Synchronous SQLAlchemy database session.
        article_id: Primary key UUID of the knowledge article.
        tenant_id: Active client tenant identifier.

    Returns:
        Matching KnowledgeArticle ORM instance.

    Raises:
        PlaybookNotFoundError: If article does not exist, is unpublished, or violates tenant boundary.
    """
    stmt = select(KnowledgeArticle).where(
        KnowledgeArticle.id == article_id,
        KnowledgeArticle.is_published == True,  # noqa: E712
    )
    article = db.scalar(stmt)
    if article is None:
        raise PlaybookNotFoundError(f"Playbook article '{article_id}' not found or unpublished.")

    # Enforce multi-tenant access: article must be global (tenant_id is None) or match tenant_id
    if article.tenant_id is not None and article.tenant_id != tenant_id:
        raise PlaybookNotFoundError(
            f"Playbook article '{article_id}' is not accessible for tenant '{tenant_id}'."
        )

    return article


def toggle_step_completion(
    db: Session,
    article_id: UUID | str,
    step_id: UUID | str,
    user_id: str,
    tenant_id: str,
    is_completed: bool,
) -> PlaybookStepProgressDTO:
    """Toggle completion status for a playbook step and update persistent progress [FR-4, FR-9].

    Args:
        db: Synchronous SQLAlchemy database session.
        article_id: UUID or string ID of parent playbook article.
        step_id: UUID or string ID of target playbook step.
        user_id: Non-empty string identifying the active user.
        tenant_id: Non-empty string identifying user's tenant organization.
        is_completed: Target completion boolean status.

    Returns:
        PlaybookStepProgressDTO containing updated step state and total percentage.

    Raises:
        InvalidPlaybookDataError: If user_id/tenant_id is empty or UUID is malformed.
        PlaybookNotFoundError: If playbook article is missing or inaccessible.
        StepNotFoundError: If step_id does not exist.
        InvalidStepReferenceError: If step_id does not belong to specified article_id.
    """
    correlation_id = get_correlation_id()

    if not user_id or not isinstance(user_id, str) or not user_id.strip():
        raise InvalidPlaybookDataError("User ID must be a non-empty string.")
    if not tenant_id or not isinstance(tenant_id, str) or not tenant_id.strip():
        raise InvalidPlaybookDataError("Tenant ID must be a non-empty string.")

    parsed_article_id = _parse_uuid(article_id, "article_id")
    parsed_step_id = _parse_uuid(step_id, "step_id")

    clean_user_id = user_id.strip()
    clean_tenant_id = tenant_id.strip()

    # 1. Verify playbook article exists and is accessible
    article = _get_published_playbook_article(db, parsed_article_id, clean_tenant_id)

    # 2. Verify step exists and belongs to article
    step_stmt = select(PlaybookStep).where(PlaybookStep.id == parsed_step_id)
    step = db.scalar(step_stmt)
    if step is None:
        raise StepNotFoundError(f"Playbook step '{parsed_step_id}' not found.")

    if step.article_id != article.id:
        raise InvalidStepReferenceError(
            f"Step '{parsed_step_id}' does not belong to playbook '{article.id}'."
        )

    # 3. Query existing UserStepProgress
    progress_stmt = select(UserStepProgress).where(
        UserStepProgress.user_id == clean_user_id,
        UserStepProgress.tenant_id == clean_tenant_id,
        UserStepProgress.step_id == step.id,
    )
    progress_record = db.scalar(progress_stmt)

    now_utc = datetime.now(timezone.utc)
    completed_at_result: datetime | None = None

    if is_completed:
        completed_at_result = now_utc
        if progress_record is not None:
            progress_record.is_completed = True
            progress_record.completed_at = now_utc
        else:
            progress_record = UserStepProgress(
                user_id=clean_user_id,
                tenant_id=clean_tenant_id,
                step_id=step.id,
                is_completed=True,
                completed_at=now_utc,
            )
            db.add(progress_record)
    else:
        completed_at_result = None
        if progress_record is not None:
            progress_record.is_completed = False

    db.commit()

    # 4. Recalculate progress percentage
    progress_summary = get_playbook_progress(
        db=db,
        article_id=article.id,
        user_id=clean_user_id,
        tenant_id=clean_tenant_id,
    )

    logger.info(
        "Toggled playbook step completion status [FR-4]",
        extra={
            "correlation_id": correlation_id,
            "article_id": str(article.id),
            "step_id": str(step.id),
            "user_id": clean_user_id,
            "tenant_id": clean_tenant_id,
            "is_completed": is_completed,
            "overall_progress_percentage": progress_summary.overall_playbook_progress_percentage,
        },
    )

    return PlaybookStepProgressDTO(
        step_id=step.id,
        is_completed=is_completed,
        completed_at=completed_at_result,
        overall_playbook_progress_percentage=progress_summary.overall_playbook_progress_percentage,
    )


def get_playbook_progress(
    db: Session,
    article_id: UUID | str,
    user_id: str,
    tenant_id: str,
) -> PlaybookProgressSummaryDTO:
    """Calculate user step completion summary and overall progress percentage for a playbook [FR-3, FR-4].

    Args:
        db: Synchronous SQLAlchemy database session.
        article_id: UUID or string ID of playbook article.
        user_id: Non-empty string identifying the user.
        tenant_id: Non-empty string identifying tenant organization.

    Returns:
        PlaybookProgressSummaryDTO containing completed step IDs and progress percentage.

    Raises:
        InvalidPlaybookDataError: If user_id/tenant_id is empty or article_id is malformed.
        PlaybookNotFoundError: If playbook article is missing or inaccessible.
    """
    if not user_id or not isinstance(user_id, str) or not user_id.strip():
        raise InvalidPlaybookDataError("User ID must be a non-empty string.")
    if not tenant_id or not isinstance(tenant_id, str) or not tenant_id.strip():
        raise InvalidPlaybookDataError("Tenant ID must be a non-empty string.")

    parsed_article_id = _parse_uuid(article_id, "article_id")
    clean_user_id = user_id.strip()
    clean_tenant_id = tenant_id.strip()

    article = _get_published_playbook_article(db, parsed_article_id, clean_tenant_id)

    # Query all steps for this playbook
    steps_stmt = (
        select(PlaybookStep.id)
        .where(PlaybookStep.article_id == article.id)
        .order_by(PlaybookStep.step_number)
    )
    all_step_ids = list(db.scalars(steps_stmt).all())
    total_steps = len(all_step_ids)

    if total_steps == 0:
        return PlaybookProgressSummaryDTO(
            article_id=article.id,
            completed_step_ids=[],
            total_completed=0,
            total_steps=0,
            overall_playbook_progress_percentage=0.0,
        )

    # Query completed steps for user/tenant
    completed_stmt = select(UserStepProgress.step_id).where(
        UserStepProgress.user_id == clean_user_id,
        UserStepProgress.tenant_id == clean_tenant_id,
        UserStepProgress.step_id.in_(all_step_ids),
        UserStepProgress.is_completed == True,  # noqa: E712
    )
    completed_step_ids = list(db.scalars(completed_stmt).all())
    total_completed = len(completed_step_ids)

    progress_pct = round((total_completed / total_steps) * 100.0, 2)

    return PlaybookProgressSummaryDTO(
        article_id=article.id,
        completed_step_ids=completed_step_ids,
        total_completed=total_completed,
        total_steps=total_steps,
        overall_playbook_progress_percentage=progress_pct,
    )


def calculate_playbook_progress_percentage(
    db: Session,
    article_id: UUID | str,
    user_id: str,
    tenant_id: str,
) -> float:
    """Calculate and return only the float completion percentage for a playbook [FR-3, FR-4].

    Args:
        db: Synchronous SQLAlchemy database session.
        article_id: UUID or string ID of playbook article.
        user_id: User identifier.
        tenant_id: Tenant identifier.

    Returns:
        Float progress percentage between 0.0 and 100.0.
    """
    summary = get_playbook_progress(db, article_id, user_id, tenant_id)
    return summary.overall_playbook_progress_percentage


def get_playbook_steps_with_progress(
    db: Session,
    article_id: UUID | str,
    user_id: str,
    tenant_id: str,
) -> list[PlaybookStepDetailDTO]:
    """Retrieve all playbook steps enriched with current user completion progress [FR-3, FR-4].

    Args:
        db: Synchronous SQLAlchemy database session.
        article_id: UUID or string ID of target playbook article.
        user_id: User identifier.
        tenant_id: Tenant identifier.

    Returns:
        List of PlaybookStepDetailDTO ordered sequentially by step_number.
    """
    if not user_id or not isinstance(user_id, str) or not user_id.strip():
        raise InvalidPlaybookDataError("User ID must be a non-empty string.")
    if not tenant_id or not isinstance(tenant_id, str) or not tenant_id.strip():
        raise InvalidPlaybookDataError("Tenant ID must be a non-empty string.")

    parsed_article_id = _parse_uuid(article_id, "article_id")
    clean_user_id = user_id.strip()
    clean_tenant_id = tenant_id.strip()

    article = _get_published_playbook_article(db, parsed_article_id, clean_tenant_id)

    steps_stmt = (
        select(PlaybookStep)
        .where(PlaybookStep.article_id == article.id)
        .order_by(PlaybookStep.step_number)
    )
    steps = list(db.scalars(steps_stmt).all())

    if not steps:
        return []

    step_ids = [s.id for s in steps]
    progress_stmt = select(UserStepProgress).where(
        UserStepProgress.user_id == clean_user_id,
        UserStepProgress.tenant_id == clean_tenant_id,
        UserStepProgress.step_id.in_(step_ids),
    )
    progress_map = {
        p.step_id: p for p in db.scalars(progress_stmt).all()
    }

    results: list[PlaybookStepDetailDTO] = []
    for step in steps:
        p = progress_map.get(step.id)
        is_completed = bool(p and p.is_completed)
        completed_at = p.completed_at if (p and p.is_completed) else None

        results.append(
            PlaybookStepDetailDTO(
                id=step.id,
                article_id=step.article_id,
                step_number=step.step_number,
                title=step.title,
                content=step.content,
                checkpoint_instructions=step.checkpoint_instructions,
                is_completed=is_completed,
                completed_at=completed_at,
            )
        )

    return results


def get_next_incomplete_step(
    db: Session,
    article_id: UUID | str,
    user_id: str,
    tenant_id: str,
) -> PlaybookStepDetailDTO | None:
    """Find and return the next uncompleted step in sequential step_number order [FR-3].

    Args:
        db: Synchronous SQLAlchemy database session.
        article_id: UUID or string ID of playbook article.
        user_id: User identifier.
        tenant_id: Tenant identifier.

    Returns:
        Next incomplete PlaybookStepDetailDTO or None if all steps are complete or no steps exist.
    """
    steps = get_playbook_steps_with_progress(db, article_id, user_id, tenant_id)
    for step_dto in steps:
        if not step_dto.is_completed:
            return step_dto
    return None
