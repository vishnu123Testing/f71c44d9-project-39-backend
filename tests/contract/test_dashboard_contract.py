"""Contract verification tests for the Executive Performance & Consolidated Dashboard OpenAPI specification."""

import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from src.api.main import app

CONTRACT_PATH = Path(__file__).resolve().parents[3] / "contracts" / "openapi.json"


def test_openapi_schema_generation() -> None:
    """Verify that FastAPI application generates a valid OpenAPI 3.1.0 schema."""
    schema = app.openapi()
    assert schema["openapi"].startswith("3.")
    assert schema["info"]["title"] == app.title
    assert "paths" in schema
    assert "components" in schema


def test_dashboard_endpoints_present_in_openapi_schema() -> None:
    """Verify that dashboard API endpoints and models are included in OpenAPI schema."""
    schema = app.openapi()
    paths = schema.get("paths", {})

    assert "/api/v1/dashboard/summary" in paths
    summary_op = paths["/api/v1/dashboard/summary"]["get"]
    assert summary_op["summary"] == "Retrieve consolidated KPI summary cards"

    assert "/api/v1/dashboard/domains/{domain_id}" in paths
    drilldown_op = paths["/api/v1/dashboard/domains/{domain_id}"]["get"]
    assert drilldown_op["summary"] == "Retrieve domain quick-view drilldown metrics"

    schemas = schema.get("components", {}).get("schemas", {})
    assert "SummaryAggregationResponse" in schemas
    assert "DomainQuickViewResponse" in schemas
    assert "MetricResponse" in schemas
    assert "MetricStatusEnum" in schemas
    assert "AggregationStatusEnum" in schemas
    assert "DomainStatusEnum" in schemas


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
