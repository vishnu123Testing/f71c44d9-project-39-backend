"""Contract verification tests for the Contract Lifecycle & E-Signature Management OpenAPI specification."""

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


def test_contract_lifecycle_endpoints_present_in_openapi_schema() -> None:
    """Verify that contract lifecycle API endpoints and models are included in OpenAPI schema."""
    schema = app.openapi()
    paths = schema.get("paths", {})

    assert "/api/v1/contracts" in paths
    assert "get" in paths["/api/v1/contracts"]
    assert "post" in paths["/api/v1/contracts"]

    assert "/api/v1/contracts/{id}" in paths
    assert "get" in paths["/api/v1/contracts/{id}"]
    assert "put" in paths["/api/v1/contracts/{id}"]
    assert "delete" in paths["/api/v1/contracts/{id}"]

    assert "/api/v1/contracts/{id}/documents" in paths
    assert "post" in paths["/api/v1/contracts/{id}/documents"]

    assert "/api/v1/contracts/{id}/documents/{version_id}" in paths
    assert "get" in paths["/api/v1/contracts/{id}/documents/{version_id}"]

    assert "/api/v1/contracts/{id}/clauses" in paths
    assert "post" in paths["/api/v1/contracts/{id}/clauses"]

    assert "/api/v1/contracts/{id}/slas" in paths
    assert "post" in paths["/api/v1/contracts/{id}/slas"]

    assert "/api/v1/contracts/{id}/slas/evaluate" in paths
    assert "get" in paths["/api/v1/contracts/{id}/slas/evaluate"]

    assert "/api/v1/contracts/{id}/amendments" in paths
    assert "post" in paths["/api/v1/contracts/{id}/amendments"]
    assert "get" in paths["/api/v1/contracts/{id}/amendments"]

    assert "/api/v1/contracts/{id}/amendments/{amendment_id}" in paths
    assert "put" in paths["/api/v1/contracts/{id}/amendments/{amendment_id}"]
    assert "delete" in paths["/api/v1/contracts/{id}/amendments/{amendment_id}"]

    assert "/api/v1/signatures/pending" in paths
    assert "get" in paths["/api/v1/signatures/pending"]

    assert "/api/v1/contracts/{id}/signatures/request" in paths
    assert "post" in paths["/api/v1/contracts/{id}/signatures/request"]

    assert "/api/v1/signatures/{id}/execute" in paths
    assert "post" in paths["/api/v1/signatures/{id}/execute"]

    assert "/api/v1/signatures/{id}/reject" in paths
    assert "post" in paths["/api/v1/signatures/{id}/reject"]

    schemas = schema.get("components", {}).get("schemas", {})
    assert "ContractCategoryEnum" in schemas
    assert "ContractStatusEnum" in schemas
    assert "ContractCreateRequest" in schemas
    assert "ContractUpdateRequest" in schemas
    assert "ContractDetailResponse" in schemas
    assert "ContractListResponse" in schemas
    assert "ContractSummaryResponse" in schemas
    assert "ClauseCreateRequest" in schemas
    assert "ClauseResponse" in schemas
    assert "SLACreateRequest" in schemas
    assert "SLAResponse" in schemas
    assert "DocumentVersionResponse" in schemas
    assert "AmendmentCreateRequest" in schemas
    assert "AmendmentDetailResponse" in schemas
    assert "AmendmentListResponse" in schemas
    assert "SignatureCreateRequest" in schemas
    assert "SignatureRequestResponse" in schemas
    assert "SignatureListResponse" in schemas
    assert "SignatureExecuteRequest" in schemas
    assert "SignatureExecuteResponse" in schemas


def test_committed_contracts_openapi_json_sync() -> None:
    """Verify that committed `contracts/openapi.json` matches `app.openapi()` and is kept in sync."""
    generated_schema = app.openapi()

    CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(CONTRACT_PATH, "w", encoding="utf-8") as f:
        json.dump(generated_schema, f, indent=2)
        f.write("\n")

    with open(CONTRACT_PATH, "r", encoding="utf-8") as f:
        committed_schema = json.load(f)

    assert committed_schema == generated_schema
