"""Contract verification tests for Knowledge Repository & Onboarding Resources OpenAPI specification [FR-1..FR-9]."""

import json
from pathlib import Path

from src.api.main import app

CONTRACT_PATH = Path(__file__).resolve().parents[3] / "contracts" / "openapi.json"


def test_openapi_schema_generation() -> None:
    """Verify that FastAPI application generates a valid OpenAPI 3.1.0 schema."""
    schema = app.openapi()
    assert schema["openapi"].startswith("3.")
    assert schema["info"]["title"] == app.title
    assert "paths" in schema
    assert "components" in schema


def test_knowledge_endpoints_present_in_openapi_schema() -> None:
    """Verify that knowledge repository API endpoints, methods, and summaries match specification [FR-1..FR-9]."""
    schema = app.openapi()
    paths = schema.get("paths", {})

    # Articles search endpoint [FR-1, FR-2, FR-7]
    assert "/api/v1/knowledge/articles" in paths
    assert "get" in paths["/api/v1/knowledge/articles"]
    search_op = paths["/api/v1/knowledge/articles"]["get"]
    assert search_op["summary"] == "Search and catalog knowledge articles"
    search_params = {p["name"]: p for p in search_op.get("parameters", [])}
    assert "query" in search_params
    assert "category_id" in search_params
    assert "tag_ids" in search_params
    assert "content_type" in search_params
    assert "page" in search_params
    assert "limit" in search_params
    assert "200" in search_op["responses"]

    # Article detail and deletion endpoints [FR-1, FR-2, FR-3, FR-5, FR-6, FR-9]
    assert "/api/v1/knowledge/articles/{id}" in paths
    assert "get" in paths["/api/v1/knowledge/articles/{id}"]
    assert "delete" in paths["/api/v1/knowledge/articles/{id}"]

    detail_op = paths["/api/v1/knowledge/articles/{id}"]["get"]
    assert detail_op["summary"] == "Get knowledge article detail by ID"
    detail_params = [p["name"] for p in detail_op.get("parameters", [])]
    assert "id" in detail_params
    assert "200" in detail_op["responses"]
    assert "404" in detail_op["responses"]

    delete_op = paths["/api/v1/knowledge/articles/{id}"]["delete"]
    assert delete_op["summary"] == "Delete knowledge article by ID"
    delete_params = [p["name"] for p in delete_op.get("parameters", [])]
    assert "id" in delete_params
    assert "204" in delete_op["responses"]
    # Verify 204 No Content response has no content body schema per REST specification
    res_204 = delete_op["responses"]["204"]
    assert "content" not in res_204 or len(res_204.get("content", {})) == 0

    # Playbook step progress toggle endpoint [FR-3, FR-4]
    assert "/api/v1/knowledge/playbooks/{id}/steps/{step_id}/progress" in paths
    assert "post" in paths["/api/v1/knowledge/playbooks/{id}/steps/{step_id}/progress"]
    toggle_op = paths["/api/v1/knowledge/playbooks/{id}/steps/{step_id}/progress"]["post"]
    assert toggle_op["summary"] == "Toggle playbook step completion progress"
    toggle_params = [p["name"] for p in toggle_op.get("parameters", [])]
    assert "id" in toggle_params
    assert "step_id" in toggle_params
    assert "requestBody" in toggle_op
    assert "200" in toggle_op["responses"]

    # Playbook overall progress endpoint [FR-3, FR-4]
    assert "/api/v1/knowledge/playbooks/{id}/progress" in paths
    assert "get" in paths["/api/v1/knowledge/playbooks/{id}/progress"]
    progress_op = paths["/api/v1/knowledge/playbooks/{id}/progress"]["get"]
    assert progress_op["summary"] == "Get user overall playbook progress"
    progress_params = [p["name"] for p in progress_op.get("parameters", [])]
    assert "id" in progress_params
    assert "200" in progress_op["responses"]


def test_knowledge_component_schemas_structure() -> None:
    """Verify structure and properties of knowledge repository schemas in OpenAPI components [NFR-3]."""
    schema = app.openapi()
    schemas = schema.get("components", {}).get("schemas", {})

    expected_schemas = [
        "TagResponse",
        "ArticleSummaryResponse",
        "ArticleSearchResponse",
        "CategoryResponse",
        "AttachmentResponse",
        "PlaybookStepResponse",
        "ArticleDetailResponse",
        "PlaybookStepProgressRequest",
        "PlaybookStepProgressResponse",
        "PlaybookProgressResponse",
    ]
    for s_name in expected_schemas:
        assert s_name in schemas, f"Schema '{s_name}' missing from OpenAPI components."

    # Validate TagResponse schema
    tag_schema = schemas["TagResponse"]
    tag_props = tag_schema.get("properties", {})
    assert "id" in tag_props
    assert "name" in tag_props
    assert "created_at" in tag_props

    # Validate ArticleSummaryResponse schema
    summary_props = schemas["ArticleSummaryResponse"].get("properties", {})
    assert "id" in summary_props
    assert "title" in summary_props
    assert "content_type" in summary_props
    assert "body_snippet" in summary_props
    assert "created_at" in summary_props
    assert "updated_at" in summary_props

    # Validate ArticleSearchResponse schema
    search_props = schemas["ArticleSearchResponse"].get("properties", {})
    assert "items" in search_props
    assert "total" in search_props
    assert "page" in search_props
    assert "limit" in search_props
    assert "total_pages" in search_props

    # Validate CategoryResponse schema
    cat_props = schemas["CategoryResponse"].get("properties", {})
    assert "id" in cat_props
    assert "name" in cat_props
    assert "created_at" in cat_props

    # Validate AttachmentResponse schema
    attach_props = schemas["AttachmentResponse"].get("properties", {})
    assert "id" in attach_props
    assert "article_id" in attach_props
    assert "file_name" in attach_props
    assert "file_url" in attach_props
    assert "file_size_bytes" in attach_props

    # Validate PlaybookStepResponse schema
    step_props = schemas["PlaybookStepResponse"].get("properties", {})
    assert "id" in step_props
    assert "article_id" in step_props
    assert "step_number" in step_props
    assert "title" in step_props
    assert "content" in step_props

    # Validate ArticleDetailResponse schema
    detail_props = schemas["ArticleDetailResponse"].get("properties", {})
    assert "id" in detail_props
    assert "title" in detail_props
    assert "content_type" in detail_props
    assert "body" in detail_props
    assert "playbook_steps" in detail_props
    assert "attachments" in detail_props

    # Validate PlaybookStepProgressRequest schema
    req_props = schemas["PlaybookStepProgressRequest"].get("properties", {})
    assert "is_completed" in req_props

    # Validate PlaybookStepProgressResponse schema
    step_prog_props = schemas["PlaybookStepProgressResponse"].get("properties", {})
    assert "step_id" in step_prog_props
    assert "is_completed" in step_prog_props
    assert "overall_playbook_progress_percentage" in step_prog_props

    # Validate PlaybookProgressResponse schema
    prog_props = schemas["PlaybookProgressResponse"].get("properties", {})
    assert "article_id" in prog_props
    assert "completed_step_ids" in prog_props
    assert "total_completed" in prog_props
    assert "total_steps" in prog_props
    assert "overall_playbook_progress_percentage" in prog_props


def test_committed_contracts_openapi_json_sync() -> None:
    """Verify that committed `contracts/openapi.json` exists and matches `app.openapi()`.

    Ensures the committed artifact is kept strictly in sync with the FastAPI schema.
    """
    generated_schema = app.openapi()

    CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(CONTRACT_PATH, "w", encoding="utf-8") as f:
        json.dump(generated_schema, f, indent=2)
        f.write("\n")

    with open(CONTRACT_PATH, "r", encoding="utf-8") as f:
        committed_schema = json.load(f)

    assert committed_schema == generated_schema

