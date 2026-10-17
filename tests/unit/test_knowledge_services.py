"""Unit tests for knowledge repository article service [FR-1, FR-2, FR-7, FR-8, FR-9, NFR-1, NFR-3, NFR-4].

Tests search query filtering, category and tag management, tenant boundary isolation,
article creation, detail retrieval, updating, and deletion operations.
"""

from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.models.database import Base
from src.services.knowledge.article_service import (
    ArticleDetailDTO,
    ArticleNotFoundError,
    ArticleSearchResultDTO,
    CategoryNotFoundError,
    InvalidArticleDataError,
    create_article,
    create_category,
    delete_article,
    get_article_by_id,
    get_or_create_tags,
    list_categories,
    list_tags,
    search_articles,
    update_article,
)
from src.services.knowledge.playbook_service import (
    InvalidPlaybookDataError,
    InvalidStepReferenceError,
    PlaybookNotFoundError,
    StepNotFoundError,
    calculate_playbook_progress_percentage,
    get_next_incomplete_step,
    get_playbook_progress,
    get_playbook_steps_with_progress,
    toggle_step_completion,
)


@pytest.fixture
def db_session() -> Session:
    """Fixture providing an isolated in-memory SQLite database session."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    session = testing_session_local()
    try:
        yield session
    finally:
        session.close()


def test_category_and_tag_management(db_session: Session) -> None:
    """Test creating and listing categories and tags [FR-1, FR-7]."""
    cat1 = create_category(db_session, name="FAQs", description="Frequently Asked Questions")
    cat2 = create_category(db_session, name="Playbooks", description="Operational Guides")

    # Duplicate name should return existing
    cat1_dup = create_category(db_session, name="faqs")
    assert cat1_dup.id == cat1.id

    categories = list_categories(db_session)
    assert len(categories) == 2
    assert categories[0].name in ("FAQs", "Playbooks")

    tags = get_or_create_tags(db_session, ["Onboarding", "Billing", "Onboarding"])
    assert len(tags) == 2

    all_tags = list_tags(db_session)
    assert len(all_tags) == 2


def test_create_and_get_article_detail(db_session: Session) -> None:
    """Test article creation with steps and attachments, and detail retrieval [FR-1, FR-3, FR-6]."""
    cat = create_category(db_session, name="Best Practices", description="Industry guidelines")
    tags = get_or_create_tags(db_session, ["Security", "Compliance"])
    tag_ids = [t.id for t in tags]

    article = create_article(
        db_session,
        title="Security Compliance Guide",
        content_type="BestPractice",
        body="This article covers security policies and compliance procedures.",
        tenant_id="tenant_alpha",
        category_id=cat.id,
        tag_ids=tag_ids,
        video_url="https://cdn.example.com/embed/v100",
        is_published=True,
        playbook_steps_data=[
            {
                "step_number": 1,
                "title": "Enable MFA",
                "content": "Turn on two-factor authentication.",
                "checkpoint_instructions": "Verify phone or authenticator app.",
            },
            {
                "step_number": 2,
                "title": "Audit Passwords",
                "content": "Check for weak passwords.",
            },
        ],
        attachments_data=[
            {
                "file_name": "Compliance_Policy.pdf",
                "file_url": "https://cdn.example.com/docs/Compliance_Policy.pdf",
                "file_size_bytes": 102400,
            }
        ],
    )

    assert article.title == "Security Compliance Guide"
    assert article.content_type == "BestPractice"
    assert article.tenant_id == "tenant_alpha"
    assert article.category is not None
    assert article.category.name == "Best Practices"
    assert len(article.tags) == 2
    assert len(article.playbook_steps) == 2
    assert article.playbook_steps[0].step_number == 1
    assert article.playbook_steps[0].title == "Enable MFA"
    assert len(article.attachments) == 1
    assert article.attachments[0].file_name == "Compliance_Policy.pdf"

    # Detail retrieval with matching tenant_id
    detail = get_article_by_id(db_session, article.id, tenant_id="tenant_alpha")
    assert detail.id == article.id
    assert detail.title == "Security Compliance Guide"

    # Detail retrieval with mismatched tenant_id should raise ArticleNotFoundError
    with pytest.raises(ArticleNotFoundError):
        get_article_by_id(db_session, article.id, tenant_id="tenant_beta")


def test_search_articles_filters(db_session: Session) -> None:
    """Test full-text query, category, tag, content_type, and tenant isolation filtering [FR-2, FR-7, FR-9]."""
    cat_faq = create_category(db_session, name="FAQs")
    cat_playbook = create_category(db_session, name="Playbooks")

    tags_onboarding = get_or_create_tags(db_session, ["Onboarding"])
    tags_billing = get_or_create_tags(db_session, ["Billing"])

    # Article 1: Global FAQ
    art1 = create_article(
        db_session,
        title="General Account FAQ",
        content_type="FAQ",
        body="How to manage your account details and password reset.",
        tenant_id=None,
        category_id=cat_faq.id,
        tag_ids=[tags_onboarding[0].id],
    )

    # Article 2: Tenant Alpha Playbook
    art2 = create_article(
        db_session,
        title="Billing Onboarding Playbook",
        content_type="Playbook",
        body="Step-by-step workflow for setting up client billing credentials.",
        tenant_id="tenant_alpha",
        category_id=cat_playbook.id,
        tag_ids=[tags_billing[0].id, tags_onboarding[0].id],
    )

    # Article 3: Tenant Beta Training Video
    art3 = create_article(
        db_session,
        title="Video Guide: Advanced Invoicing",
        content_type="TrainingVideo",
        body="Watch this video to learn about automated invoicing features.",
        tenant_id="tenant_beta",
        category_id=cat_faq.id,
        tag_ids=[tags_billing[0].id],
        video_url="https://cdn.example.com/v200",
    )

    # Search 1: Query "password" for tenant_alpha (should return Global Article 1)
    res1 = search_articles(db_session, query="password", tenant_id="tenant_alpha")
    assert res1.total == 1
    assert res1.items[0].id == art1.id

    # Search 2: Query "billing" for tenant_alpha (should return Article 2, but NOT Article 3 from tenant_beta)
    res2 = search_articles(db_session, query="billing", tenant_id="tenant_alpha")
    assert res2.total == 1
    assert res2.items[0].id == art2.id

    # Search 3: Query "billing" for tenant_beta (should return Article 3)
    res3 = search_articles(db_session, query="billing", tenant_id="tenant_beta")
    assert res3.total == 1
    assert res3.items[0].id == art3.id

    # Search 4: Filter by category_id = cat_playbook.id for tenant_alpha
    res4 = search_articles(db_session, category_id=cat_playbook.id, tenant_id="tenant_alpha")
    assert res4.total == 1
    assert res4.items[0].id == art2.id

    # Search 5: Filter by tag_ids = [tags_onboarding[0].id] for tenant_alpha (returns art1 global + art2)
    res5 = search_articles(
        db_session,
        tag_ids=[tags_onboarding[0].id],
        tenant_id="tenant_alpha",
    )
    assert res5.total == 2

    # Search 6: Filter by content_type = "TrainingVideo" for tenant_beta
    res6 = search_articles(
        db_session, content_type="TrainingVideo", tenant_id="tenant_beta"
    )
    assert res6.total == 1
    assert res6.items[0].id == art3.id


def test_update_and_delete_article(db_session: Session) -> None:
    """Test updating article attributes and deleting articles [FR-1]."""
    art = create_article(
        db_session,
        title="Old Title",
        content_type="FAQ",
        body="Initial body content.",
        tenant_id="tenant_x",
    )

    updated = update_article(
        db_session,
        article_id=art.id,
        title="Updated Title",
        body="New updated body text.",
        tenant_id="tenant_x",
    )
    assert updated.title == "Updated Title"
    assert updated.body == "New updated body text."

    # Deleting article with wrong tenant_id raises error
    with pytest.raises(ArticleNotFoundError):
        delete_article(db_session, art.id, tenant_id="tenant_y")

    # Deleting with matching tenant succeeds
    delete_article(db_session, art.id, tenant_id="tenant_x")

    with pytest.raises(ArticleNotFoundError):
        get_article_by_id(db_session, art.id, tenant_id="tenant_x")


def test_invalid_data_handling(db_session: Session) -> None:
    """Test validation errors on empty title/body or invalid content_type [FR-8, NFR-3]."""
    with pytest.raises(InvalidArticleDataError):
        create_article(db_session, title="", content_type="FAQ", body="Some body")

    with pytest.raises(InvalidArticleDataError):
        create_article(db_session, title="Title", content_type="FAQ", body="   ")

    with pytest.raises(InvalidArticleDataError):
        create_article(
            db_session, title="Title", content_type="InvalidType", body="Body"
        )

    with pytest.raises(InvalidArticleDataError):
        get_article_by_id(db_session, "not-a-valid-uuid")


def test_playbook_step_completion_toggling(db_session: Session) -> None:
    """Test step completion toggling, timestamp setting, and progress recalculations [FR-3, FR-4]."""
    playbook = create_article(
        db_session,
        title="Onboarding Playbook",
        content_type="Playbook",
        body="Step-by-step onboarding guide.",
        tenant_id="tenant_a",
        playbook_steps_data=[
            {"step_number": 1, "title": "Setup Workspace", "content": "Create account and workspace."},
            {"step_number": 2, "title": "Configure SSO", "content": "Connect identity provider."},
            {"step_number": 3, "title": "Invite Team", "content": "Send invitations to team members."},
        ],
    )

    steps = playbook.playbook_steps
    assert len(steps) == 3

    # Initial progress should be 0%
    summary0 = get_playbook_progress(db_session, playbook.id, user_id="user_1", tenant_id="tenant_a")
    assert summary0.total_steps == 3
    assert summary0.total_completed == 0
    assert summary0.overall_playbook_progress_percentage == 0.0

    # Toggle Step 1 completion -> 33.33%
    res1 = toggle_step_completion(
        db_session,
        article_id=playbook.id,
        step_id=steps[0].id,
        user_id="user_1",
        tenant_id="tenant_a",
        is_completed=True,
    )
    assert res1.is_completed is True
    assert res1.completed_at is not None
    assert res1.overall_playbook_progress_percentage == 33.33

    # Toggle Step 2 completion -> 66.67%
    res2 = toggle_step_completion(
        db_session,
        article_id=playbook.id,
        step_id=steps[1].id,
        user_id="user_1",
        tenant_id="tenant_a",
        is_completed=True,
    )
    assert res2.is_completed is True
    assert res2.overall_playbook_progress_percentage == 66.67

    # Toggle Step 3 completion -> 100.0%
    res3 = toggle_step_completion(
        db_session,
        article_id=playbook.id,
        step_id=steps[2].id,
        user_id="user_1",
        tenant_id="tenant_a",
        is_completed=True,
    )
    assert res3.is_completed is True
    assert res3.overall_playbook_progress_percentage == 100.0

    # Toggle Step 2 back to incomplete -> 66.67%
    res_untoggle = toggle_step_completion(
        db_session,
        article_id=playbook.id,
        step_id=steps[1].id,
        user_id="user_1",
        tenant_id="tenant_a",
        is_completed=False,
    )
    assert res_untoggle.is_completed is False
    assert res_untoggle.completed_at is None
    assert res_untoggle.overall_playbook_progress_percentage == 66.67

    # Direct percentage calculation check
    pct = calculate_playbook_progress_percentage(
        db_session, playbook.id, user_id="user_1", tenant_id="tenant_a"
    )
    assert pct == 66.67


def test_playbook_progress_summary_and_navigation(db_session: Session) -> None:
    """Test step detail list retrieval and next incomplete step navigation [FR-3, FR-4]."""
    playbook = create_article(
        db_session,
        title="Deployment Playbook",
        content_type="Playbook",
        body="Deployment process steps.",
        tenant_id="tenant_b",
        playbook_steps_data=[
            {"step_number": 1, "title": "Build Docker Image", "content": "Run docker build."},
            {"step_number": 2, "title": "Run Smoke Tests", "content": "Execute automated smoke test suite."},
        ],
    )

    # Next step before completing any step -> Step 1
    next_step1 = get_next_incomplete_step(
        db_session, playbook.id, user_id="user_2", tenant_id="tenant_b"
    )
    assert next_step1 is not None
    assert next_step1.step_number == 1
    assert next_step1.title == "Build Docker Image"

    # Complete Step 1
    toggle_step_completion(
        db_session,
        article_id=playbook.id,
        step_id=playbook.playbook_steps[0].id,
        user_id="user_2",
        tenant_id="tenant_b",
        is_completed=True,
    )

    # Next step after completing Step 1 -> Step 2
    next_step2 = get_next_incomplete_step(
        db_session, playbook.id, user_id="user_2", tenant_id="tenant_b"
    )
    assert next_step2 is not None
    assert next_step2.step_number == 2
    assert next_step2.title == "Run Smoke Tests"

    # Complete Step 2
    toggle_step_completion(
        db_session,
        article_id=playbook.id,
        step_id=playbook.playbook_steps[1].id,
        user_id="user_2",
        tenant_id="tenant_b",
        is_completed=True,
    )

    # Next step when all complete -> None
    next_step_none = get_next_incomplete_step(
        db_session, playbook.id, user_id="user_2", tenant_id="tenant_b"
    )
    assert next_step_none is None

    # Step details list check
    step_details = get_playbook_steps_with_progress(
        db_session, playbook.id, user_id="user_2", tenant_id="tenant_b"
    )
    assert len(step_details) == 2
    assert all(s.is_completed for s in step_details)


def test_playbook_error_handling_and_tenant_isolation(db_session: Session) -> None:
    """Test validation and error scenarios for playbook step progression [FR-8, FR-9, NFR-3]."""
    playbook_a = create_article(
        db_session,
        title="Tenant A Private Playbook",
        content_type="Playbook",
        body="Private instructions.",
        tenant_id="tenant_a",
        playbook_steps_data=[
            {"step_number": 1, "title": "Private Step 1", "content": "Content."}
        ],
    )

    playbook_b = create_article(
        db_session,
        title="Tenant B Private Playbook",
        content_type="Playbook",
        body="Other private instructions.",
        tenant_id="tenant_b",
        playbook_steps_data=[
            {"step_number": 1, "title": "Other Step 1", "content": "Content."}
        ],
    )

    step_a_id = playbook_a.playbook_steps[0].id
    step_b_id = playbook_b.playbook_steps[0].id

    # 1. Invalid User ID / Tenant ID
    with pytest.raises(InvalidPlaybookDataError):
        toggle_step_completion(
            db_session, playbook_a.id, step_a_id, user_id="", tenant_id="tenant_a", is_completed=True
        )

    with pytest.raises(InvalidPlaybookDataError):
        get_playbook_progress(
            db_session, playbook_a.id, user_id="user_1", tenant_id="   "
        )

    # 2. Invalid UUID format
    with pytest.raises(InvalidPlaybookDataError):
        toggle_step_completion(
            db_session, "bad-uuid", step_a_id, user_id="user_1", tenant_id="tenant_a", is_completed=True
        )

    # 3. Tenant mismatch (tenant_b trying to access tenant_a playbook) -> PlaybookNotFoundError
    with pytest.raises(PlaybookNotFoundError):
        toggle_step_completion(
            db_session, playbook_a.id, step_a_id, user_id="user_1", tenant_id="tenant_b", is_completed=True
        )

    # 4. Non-existent step ID -> StepNotFoundError
    fake_step_id = uuid.uuid4()
    with pytest.raises(StepNotFoundError):
        toggle_step_completion(
            db_session, playbook_a.id, fake_step_id, user_id="user_1", tenant_id="tenant_a", is_completed=True
        )

    # 5. Mismatched step_id (step_b_id supplied with playbook_a) -> InvalidStepReferenceError
    with pytest.raises(InvalidStepReferenceError):
        toggle_step_completion(
            db_session, playbook_a.id, step_b_id, user_id="user_1", tenant_id="tenant_a", is_completed=True
        )
