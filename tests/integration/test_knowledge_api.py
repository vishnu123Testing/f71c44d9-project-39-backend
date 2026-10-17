"""Integration tests for GET /api/v1/knowledge/articles search and catalog endpoint [FR-1, FR-2, FR-7, FR-8, FR-9, NFR-1, NFR-3]."""

from datetime import datetime, timezone
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.api.deps import create_access_token
from src.api.main import app
from src.models.database import Base, get_db
from src.models.knowledge import Attachment, Category, KnowledgeArticle, PlaybookStep, Tag, UserStepProgress
from src.models.role import Role, UserRole
from src.models.tenant import Tenant
from src.models.user import User
from src.services.access_control.rbac import RoleName
from src.services.knowledge.article_service import create_article, create_category, get_or_create_tags
from src.services.security import get_password_hash


@pytest.fixture
def db_session() -> Session:
    """In-memory SQLite database session for knowledge API integration testing."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session: Session) -> TestClient:
    """TestClient instance with overridden database session dependency."""

    def _override_get_db() -> Session:
        return db_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def test_user(db_session: Session) -> tuple[Tenant, User]:
    """Seed database with primary tenant, user, and ClientViewer role."""
    tenant = Tenant(name="Alpha Corp Tenant")
    db_session.add(tenant)
    db_session.commit()

    hashed_pwd = get_password_hash("KnowledgeTest123!")
    user = User(
        email="viewer@alphacorp.com",
        password_hash=hashed_pwd,
        tenant_id=tenant.tenant_id,
    )
    db_session.add(user)
    db_session.commit()

    role = Role(
        name=RoleName.CLIENT_VIEWER.value,
        description="Client Viewer Role",
    )
    db_session.add(role)
    db_session.commit()

    user_role = UserRole(
        user_id=user.user_id,
        role_id=role.role_id,
        tenant_id=tenant.tenant_id,
    )
    db_session.add(user_role)
    db_session.commit()

    return tenant, user


@pytest.fixture
def auth_headers(test_user: tuple[Tenant, User]) -> dict[str, str]:
    """Generate authorization header with Bearer token for test user."""
    tenant, user = test_user
    token = create_access_token(
        subject=user.user_id,
        tenant_id=tenant.tenant_id,
        email=user.email,
    )
    return {"Authorization": f"Bearer {token}"}


def test_search_articles_unauthenticated(client: TestClient) -> None:
    """Verify endpoint rejects requests without Bearer token [FR-9]."""
    response = client.get("/api/v1/knowledge/articles")
    assert response.status_code == 401


def test_search_articles_empty_catalog(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Verify search returns empty list with 200 OK when no articles match."""
    response = client.get("/api/v1/knowledge/articles", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["page"] == 1
    assert data["limit"] == 20
    assert data["total_pages"] == 0


def test_search_articles_with_query_and_filters(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Test full-text query, category, tag, and content_type filters [FR-1, FR-2, FR-7]."""
    tenant, user = test_user
    cat_faq = create_category(db_session, name="General FAQs", description="Frequently asked questions")
    cat_playbook = create_category(db_session, name="Operational Playbooks", description="Step guides")

    tags = get_or_create_tags(db_session, ["Onboarding", "Billing", "Security"])
    tag_map = {t.name: t for t in tags}

    # Article 1: Global FAQ
    art1 = create_article(
        db_session,
        title="How to reset password",
        content_type="FAQ",
        body="Detailed instructions on resetting your user credentials.",
        tenant_id=None,  # Global
        category_id=cat_faq.id,
        tag_ids=[tag_map["Onboarding"].id],
    )

    # Article 2: Tenant-specific Playbook
    art2 = create_article(
        db_session,
        title="Quarterly Compliance Review",
        content_type="Playbook",
        body="Step-by-step checklist for quarterly security compliance audits.",
        tenant_id=str(tenant.tenant_id),
        category_id=cat_playbook.id,
        tag_ids=[tag_map["Security"].id, tag_map["Onboarding"].id],
    )

    # Search with keyword "password"
    res1 = client.get("/api/v1/knowledge/articles?query=password", headers=auth_headers)
    assert res1.status_code == 200
    d1 = res1.json()
    assert d1["total"] == 1
    assert d1["items"][0]["title"] == "How to reset password"

    # Search with category_id
    res2 = client.get(f"/api/v1/knowledge/articles?category_id={cat_playbook.id}", headers=auth_headers)
    assert res2.status_code == 200
    d2 = res2.json()
    assert d2["total"] == 1
    assert d2["items"][0]["title"] == "Quarterly Compliance Review"

    # Search with content_type
    res3 = client.get("/api/v1/knowledge/articles?content_type=Playbook", headers=auth_headers)
    assert res3.status_code == 200
    d3 = res3.json()
    assert d3["total"] == 1
    assert d3["items"][0]["content_type"] == "Playbook"

    # Search with tag_ids
    res4 = client.get(f"/api/v1/knowledge/articles?tag_ids={tag_map['Security'].id}", headers=auth_headers)
    assert res4.status_code == 200
    d4 = res4.json()
    assert d4["total"] == 1
    assert d4["items"][0]["id"] == str(art2.id)


def test_search_articles_tenant_isolation(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify tenant isolation prevents seeing another tenant's private articles [FR-9]."""
    tenant_a, user_a = test_user
    tenant_b_id = str(uuid.uuid4())

    # Create article for Tenant A
    art_a = create_article(
        db_session,
        title="Tenant A Private Doc",
        content_type="FAQ",
        body="Sensitive internal process for Tenant A.",
        tenant_id=str(tenant_a.tenant_id),
    )

    # Create article for Tenant B
    art_b = create_article(
        db_session,
        title="Tenant B Secret Manual",
        content_type="FAQ",
        body="Confidential operations manual for Tenant B.",
        tenant_id=tenant_b_id,
    )

    # Create global article
    art_global = create_article(
        db_session,
        title="Global Onboarding Welcome",
        content_type="FAQ",
        body="Public onboarding resources for all clients.",
        tenant_id=None,
    )

    response = client.get("/api/v1/knowledge/articles", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()

    titles = [item["title"] for item in data["items"]]
    assert "Tenant A Private Doc" in titles
    assert "Global Onboarding Welcome" in titles
    assert "Tenant B Secret Manual" not in titles


def test_search_articles_input_validation(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Verify validation on invalid inputs (query length, invalid content type) [FR-8, NFR-3]."""
    # Exceeding query max length
    long_query = "x" * 501
    res1 = client.get(f"/api/v1/knowledge/articles?query={long_query}", headers=auth_headers)
    assert res1.status_code == 400
    assert "maximum allowed length" in res1.json()["detail"]

    # Invalid content_type
    res2 = client.get("/api/v1/knowledge/articles?content_type=InvalidType", headers=auth_headers)
    assert res2.status_code == 400
    assert "Invalid content type" in res2.json()["detail"]


def test_get_article_detail_unauthenticated(client: TestClient) -> None:
    """Verify endpoint rejects unauthenticated requests [FR-9]."""
    random_id = str(uuid.uuid4())
    response = client.get(f"/api/v1/knowledge/articles/{random_id}")
    assert response.status_code == 401


def test_get_article_detail_success(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify successfully retrieving article detail with category, tags, steps, and attachments [FR-1, FR-3, FR-6]."""
    tenant, user = test_user
    cat = create_category(db_session, name="Best Practices", description="Industry guidelines")
    tags = get_or_create_tags(db_session, ["Guide", "Architecture"])

    steps_data = [
        {"step_number": 1, "title": "Setup workspace", "content": "Install tools", "checkpoint_instructions": "Verify version"},
        {"step_number": 2, "title": "Build app", "content": "Run build command"},
    ]
    attachments_data = [
        {"file_name": "spec.pdf", "file_url": "https://storage.local/spec.pdf", "file_size_bytes": 1024}
    ]

    art = create_article(
        db_session,
        title="Architecture Setup Guide",
        content_type="Playbook",
        body="Comprehensive walkthrough for environment setup.",
        tenant_id=str(tenant.tenant_id),
        category_id=cat.id,
        tag_ids=[t.id for t in tags],
        playbook_steps_data=steps_data,
        attachments_data=attachments_data,
    )

    response = client.get(f"/api/v1/knowledge/articles/{art.id}", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()

    assert data["id"] == str(art.id)
    assert data["title"] == "Architecture Setup Guide"
    assert data["content_type"] == "Playbook"
    assert data["body"] == "Comprehensive walkthrough for environment setup."
    assert data["tenant_id"] == str(tenant.tenant_id)
    assert data["category"]["name"] == "Best Practices"
    assert len(data["tags"]) == 2
    assert len(data["playbook_steps"]) == 2
    assert data["playbook_steps"][0]["title"] == "Setup workspace"
    assert len(data["attachments"]) == 1
    assert data["attachments"][0]["file_name"] == "spec.pdf"


def test_get_article_detail_not_found(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Verify 404 NOT FOUND returned for non-existent article ID [FR-1]."""
    random_id = str(uuid.uuid4())
    response = client.get(f"/api/v1/knowledge/articles/{random_id}", headers=auth_headers)
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_get_article_detail_invalid_uuid(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Verify 400 BAD REQUEST returned for malformed UUID string [FR-8, NFR-3]."""
    response = client.get("/api/v1/knowledge/articles/not-a-valid-uuid", headers=auth_headers)
    assert response.status_code == 400
    assert "Invalid UUID" in response.json()["detail"]


def test_get_article_detail_tenant_isolation(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify multi-tenant boundary prevents viewing another tenant's private article [FR-9]."""
    tenant_a, user_a = test_user
    tenant_b_id = str(uuid.uuid4())

    art_b = create_article(
        db_session,
        title="Tenant B Private Strategy",
        content_type="BestPractice",
        body="Internal strategy documentation for Tenant B.",
        tenant_id=tenant_b_id,
    )

    # Attempting to fetch Tenant B's article as Tenant A user should return 404 (or 403 / unaccessible)
    response = client.get(f"/api/v1/knowledge/articles/{art_b.id}", headers=auth_headers)
    assert response.status_code == 404


def test_toggle_playbook_step_progress_unauthenticated(client: TestClient) -> None:
    """Verify endpoint rejects unauthenticated requests [FR-4, FR-9]."""
    random_id = str(uuid.uuid4())
    random_step_id = str(uuid.uuid4())
    response = client.post(
        f"/api/v1/knowledge/playbooks/{random_id}/steps/{random_step_id}/progress",
        json={"is_completed": True},
    )
    assert response.status_code == 401


def test_toggle_playbook_step_progress_success(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify successfully toggling playbook step completion status and progress percentage [FR-3, FR-4]."""
    tenant, user = test_user
    steps_data = [
        {"step_number": 1, "title": "Step One", "content": "Instruction 1"},
        {"step_number": 2, "title": "Step Two", "content": "Instruction 2"},
    ]
    art = create_article(
        db_session,
        title="Onboarding Playbook",
        content_type="Playbook",
        body="Step-by-step onboarding walkthrough.",
        tenant_id=str(tenant.tenant_id),
        playbook_steps_data=steps_data,
    )

    step_1_id = str(art.playbook_steps[0].id)
    step_2_id = str(art.playbook_steps[1].id)

    # Toggle step 1 to completed
    res1 = client.post(
        f"/api/v1/knowledge/playbooks/{art.id}/steps/{step_1_id}/progress",
        headers=auth_headers,
        json={"is_completed": True},
    )
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["step_id"] == step_1_id
    assert data1["is_completed"] is True
    assert data1["completed_at"] is not None
    assert data1["overall_playbook_progress_percentage"] == 50.0

    # Toggle step 2 to completed
    res2 = client.post(
        f"/api/v1/knowledge/playbooks/{art.id}/steps/{step_2_id}/progress",
        headers=auth_headers,
        json={"is_completed": True},
    )
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["step_id"] == step_2_id
    assert data2["is_completed"] is True
    assert data2["overall_playbook_progress_percentage"] == 100.0

    # Toggle step 1 back to incomplete
    res3 = client.post(
        f"/api/v1/knowledge/playbooks/{art.id}/steps/{step_1_id}/progress",
        headers=auth_headers,
        json={"is_completed": False},
    )
    assert res3.status_code == 200
    data3 = res3.json()
    assert data3["step_id"] == step_1_id
    assert data3["is_completed"] is False
    assert data3["completed_at"] is None
    assert data3["overall_playbook_progress_percentage"] == 50.0


def test_toggle_playbook_step_progress_step_not_found(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify 404 NOT FOUND returned when step_id does not exist [FR-4]."""
    tenant, user = test_user
    art = create_article(
        db_session,
        title="Sample Playbook",
        content_type="Playbook",
        body="Sample body",
        tenant_id=str(tenant.tenant_id),
        playbook_steps_data=[{"step_number": 1, "title": "Step 1", "content": "Content 1"}],
    )
    fake_step_id = str(uuid.uuid4())
    res = client.post(
        f"/api/v1/knowledge/playbooks/{art.id}/steps/{fake_step_id}/progress",
        headers=auth_headers,
        json={"is_completed": True},
    )
    assert res.status_code == 404


def test_toggle_playbook_step_progress_invalid_step_reference(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify 400 BAD REQUEST returned when step does not belong to specified playbook article [FR-4, FR-8]."""
    tenant, user = test_user
    art1 = create_article(
        db_session,
        title="Playbook 1",
        content_type="Playbook",
        body="Body 1",
        tenant_id=str(tenant.tenant_id),
        playbook_steps_data=[{"step_number": 1, "title": "P1 S1", "content": "Content"}],
    )
    art2 = create_article(
        db_session,
        title="Playbook 2",
        content_type="Playbook",
        body="Body 2",
        tenant_id=str(tenant.tenant_id),
        playbook_steps_data=[{"step_number": 1, "title": "P2 S1", "content": "Content"}],
    )

    art2_step_id = str(art2.playbook_steps[0].id)

    # Try referencing art2's step on art1
    res = client.post(
        f"/api/v1/knowledge/playbooks/{art1.id}/steps/{art2_step_id}/progress",
        headers=auth_headers,
        json={"is_completed": True},
    )
    assert res.status_code == 400
    assert "does not belong to playbook" in res.json()["detail"]


def test_get_playbook_progress_success(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify GET /api/v1/knowledge/playbooks/{id}/progress returns overall progress summary [FR-3, FR-4]."""
    tenant, user = test_user
    steps_data = [
        {"step_number": 1, "title": "Step 1", "content": "Instruction 1"},
        {"step_number": 2, "title": "Step 2", "content": "Instruction 2"},
    ]
    art = create_article(
        db_session,
        title="Onboarding Playbook",
        content_type="Playbook",
        body="Walkthrough body.",
        tenant_id=str(tenant.tenant_id),
        playbook_steps_data=steps_data,
    )

    # Initially 0 steps completed
    res0 = client.get(
        f"/api/v1/knowledge/playbooks/{art.id}/progress",
        headers=auth_headers,
    )
    assert res0.status_code == 200
    data0 = res0.json()
    assert data0["article_id"] == str(art.id)
    assert data0["completed_step_ids"] == []
    assert data0["total_completed"] == 0
    assert data0["total_steps"] == 2
    assert data0["overall_playbook_progress_percentage"] == 0.0

    step_1_id = str(art.playbook_steps[0].id)

    # Complete step 1
    client.post(
        f"/api/v1/knowledge/playbooks/{art.id}/steps/{step_1_id}/progress",
        headers=auth_headers,
        json={"is_completed": True},
    )

    # Query overall progress
    res1 = client.get(
        f"/api/v1/knowledge/playbooks/{art.id}/progress",
        headers=auth_headers,
    )
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["article_id"] == str(art.id)
    assert data1["completed_step_ids"] == [step_1_id]
    assert data1["total_completed"] == 1
    assert data1["total_steps"] == 2
    assert data1["overall_playbook_progress_percentage"] == 50.0


def test_get_playbook_progress_not_found(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify 404 NOT FOUND returned when querying progress for non-existent playbook [FR-4]."""
    fake_id = str(uuid.uuid4())
    res = client.get(
        f"/api/v1/knowledge/playbooks/{fake_id}/progress",
        headers=auth_headers,
    )
    assert res.status_code == 404


def test_get_playbook_progress_invalid_id(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify 400 BAD REQUEST returned when querying progress with malformed playbook ID [FR-8]."""
    res = client.get(
        "/api/v1/knowledge/playbooks/invalid-uuid-format/progress",
        headers=auth_headers,
    )
    assert res.status_code == 400


def test_delete_article_unauthenticated(client: TestClient) -> None:
    """Verify 401 UNAUTHORIZED returned when deleting article without authentication token [FR-9]."""
    fake_id = str(uuid.uuid4())
    res = client.delete(f"/api/v1/knowledge/articles/{fake_id}")
    assert res.status_code == 401


def test_delete_article_success(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify DELETE /api/v1/knowledge/articles/{id} deletes article and returns 204 No Content with empty body [FR-1, FR-9]."""
    tenant, user = test_user
    art = create_article(
        db_session,
        title="Article to Delete",
        content_type="FAQ",
        body="This article will be deleted.",
        tenant_id=str(tenant.tenant_id),
    )

    # Verify article exists
    res_get = client.get(
        f"/api/v1/knowledge/articles/{art.id}",
        headers=auth_headers,
    )
    assert res_get.status_code == 200

    # Delete article
    res_del = client.delete(
        f"/api/v1/knowledge/articles/{art.id}",
        headers=auth_headers,
    )
    assert res_del.status_code == 204
    assert res_del.content == b""

    # Verify article is deleted
    res_get_after = client.get(
        f"/api/v1/knowledge/articles/{art.id}",
        headers=auth_headers,
    )
    assert res_get_after.status_code == 404


def test_delete_article_not_found(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify 404 NOT FOUND returned when deleting non-existent article [FR-1]."""
    fake_id = str(uuid.uuid4())
    res = client.delete(
        f"/api/v1/knowledge/articles/{fake_id}",
        headers=auth_headers,
    )
    assert res.status_code == 404


def test_delete_article_invalid_id(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify 400 BAD REQUEST returned when deleting article with malformed UUID [FR-8]."""
    res = client.delete(
        "/api/v1/knowledge/articles/invalid-uuid-format",
        headers=auth_headers,
    )
    assert res.status_code == 400


def test_delete_article_tenant_isolation(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify 404 NOT FOUND returned when attempting to delete another tenant's article [FR-9]."""
    other_tenant_id = str(uuid.uuid4())
    art = create_article(
        db_session,
        title="Other Tenant Article",
        content_type="FAQ",
        body="Secret article.",
        tenant_id=other_tenant_id,
    )

    res = client.delete(
        f"/api/v1/knowledge/articles/{art.id}",
        headers=auth_headers,
    )
    assert res.status_code == 404


def test_search_articles_pagination(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify page and limit query parameters properly paginate catalog search results [FR-1, NFR-1]."""
    tenant, user = test_user
    for i in range(1, 4):
        create_article(
            db_session,
            title=f"Paginated Doc {i}",
            content_type="FAQ",
            body=f"Content body for document number {i}.",
            tenant_id=str(tenant.tenant_id),
        )

    # Page 1 with limit=2
    res1 = client.get("/api/v1/knowledge/articles?page=1&limit=2", headers=auth_headers)
    assert res1.status_code == 200
    d1 = res1.json()
    assert len(d1["items"]) == 2
    assert d1["total"] == 3
    assert d1["page"] == 1
    assert d1["limit"] == 2
    assert d1["total_pages"] == 2

    # Page 2 with limit=2
    res2 = client.get("/api/v1/knowledge/articles?page=2&limit=2", headers=auth_headers)
    assert res2.status_code == 200
    d2 = res2.json()
    assert len(d2["items"]) == 1
    assert d2["page"] == 2


def test_search_articles_sanitizes_html_and_script_tags(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify input search queries containing script and HTML tags are sanitized safely [FR-8, NFR-3]."""
    tenant, user = test_user
    create_article(
        db_session,
        title="Safe Onboarding Guide",
        content_type="FAQ",
        body="Instructions for onboarding.",
        tenant_id=str(tenant.tenant_id),
    )

    res = client.get("/api/v1/knowledge/articles?query=<script>alert('xss')</script>Onboarding", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert data["items"][0]["title"] == "Safe Onboarding Guide"


def test_get_training_video_article_detail(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify retrieving TrainingVideo article with video_url and attached documentation [FR-5, FR-6]."""
    tenant, user = test_user
    attachments_data = [
        {"file_name": "slides.pdf", "file_url": "https://storage.local/slides.pdf", "file_size_bytes": 2048}
    ]
    art = create_article(
        db_session,
        title="Module 1: Product Tour Video",
        content_type="TrainingVideo",
        body="Walkthrough video introducing the platform features.",
        tenant_id=str(tenant.tenant_id),
        video_url="https://cdn.example.com/videos/product_tour.mp4",
        attachments_data=attachments_data,
    )

    res = client.get(f"/api/v1/knowledge/articles/{art.id}", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == str(art.id)
    assert data["content_type"] == "TrainingVideo"
    assert data["video_url"] == "https://cdn.example.com/videos/product_tour.mp4"
    assert len(data["attachments"]) == 1
    assert data["attachments"][0]["file_name"] == "slides.pdf"


def test_sql_persistence_step_progress_in_database(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify step completion progress toggle persists UserStepProgress entity directly in SQL database [FR-4]."""
    tenant, user = test_user
    steps_data = [
        {"step_number": 1, "title": "Checklist Step 1", "content": "Execute command"}
    ]
    art = create_article(
        db_session,
        title="SQL Persistence Playbook",
        content_type="Playbook",
        body="Playbook testing database persistence.",
        tenant_id=str(tenant.tenant_id),
        playbook_steps_data=steps_data,
    )
    step_id = str(art.playbook_steps[0].id)

    # Toggle progress via API
    res = client.post(
        f"/api/v1/knowledge/playbooks/{art.id}/steps/{step_id}/progress",
        headers=auth_headers,
        json={"is_completed": True},
    )
    assert res.status_code == 200

    # Query SQL database session directly
    db_progress = (
        db_session.query(UserStepProgress)
        .filter_by(
            user_id=str(user.user_id),
            tenant_id=str(tenant.tenant_id),
            step_id=art.playbook_steps[0].id,
        )
        .first()
    )
    assert db_progress is not None
    assert db_progress.is_completed is True


def test_sql_persistence_cascade_article_deletion(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify deleting article CASCADE removes child steps and attachments from database persistence [FR-1]."""
    tenant, user = test_user
    steps_data = [{"step_number": 1, "title": "Step 1", "content": "Instruction"}]
    attachments_data = [{"file_name": "doc.pdf", "file_url": "https://storage.local/doc.pdf", "file_size_bytes": 500}]

    art = create_article(
        db_session,
        title="Cascade Delete Article",
        content_type="Playbook",
        body="Article body.",
        tenant_id=str(tenant.tenant_id),
        playbook_steps_data=steps_data,
        attachments_data=attachments_data,
    )
    art_id = art.id
    step_id = art.playbook_steps[0].id
    attachment_id = art.attachments[0].id

    # Delete article via API
    res = client.delete(f"/api/v1/knowledge/articles/{art_id}", headers=auth_headers)
    assert res.status_code == 204

    # Direct database verification
    assert db_session.query(KnowledgeArticle).filter_by(id=art_id).first() is None
    assert db_session.query(PlaybookStep).filter_by(id=step_id).first() is None
    assert db_session.query(Attachment).filter_by(id=attachment_id).first() is None


def test_playbook_progress_tenant_isolation(
    client: TestClient, db_session: Session, test_user: tuple[Tenant, User], auth_headers: dict[str, str]
) -> None:
    """Verify Tenant A user cannot toggle or query progress on Tenant B's private playbook [FR-4, FR-9]."""
    other_tenant_id = str(uuid.uuid4())
    steps_data = [{"step_number": 1, "title": "Secret Step", "content": "Confidential"}]
    art = create_article(
        db_session,
        title="Tenant B Private Playbook",
        content_type="Playbook",
        body="Private playbook.",
        tenant_id=other_tenant_id,
        playbook_steps_data=steps_data,
    )
    step_id = str(art.playbook_steps[0].id)

    # Attempt to toggle progress as Tenant A user -> 404 NOT FOUND
    res_toggle = client.post(
        f"/api/v1/knowledge/playbooks/{art.id}/steps/{step_id}/progress",
        headers=auth_headers,
        json={"is_completed": True},
    )
    assert res_toggle.status_code == 404

    # Attempt to query progress as Tenant A user -> 404 NOT FOUND
    res_get = client.get(
        f"/api/v1/knowledge/playbooks/{art.id}/progress",
        headers=auth_headers,
    )
    assert res_get.status_code == 404

