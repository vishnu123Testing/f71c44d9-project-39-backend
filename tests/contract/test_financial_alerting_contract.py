"""Contract verification tests for Financial Transparency & Multi-Channel Alerting OpenAPI specification [FR-1..FR-12]."""

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


def test_financial_endpoints_present_in_openapi_schema() -> None:
    """Verify that financial metrics API endpoints and models are included in OpenAPI schema [FR-1..FR-4, FR-9, FR-10]."""
    schema = app.openapi()
    paths = schema.get("paths", {})

    # Quotes endpoints [FR-1]
    assert "/api/v1/financial/quotes" in paths
    assert "get" in paths["/api/v1/financial/quotes"]
    assert "post" in paths["/api/v1/financial/quotes"]

    assert "/api/v1/financial/quotes/{quote_id}" in paths
    assert "get" in paths["/api/v1/financial/quotes/{quote_id}"]

    # Invoices endpoints [FR-2]
    assert "/api/v1/financial/invoices" in paths
    assert "get" in paths["/api/v1/financial/invoices"]
    assert "post" in paths["/api/v1/financial/invoices"]

    assert "/api/v1/financial/invoices/{invoice_id}" in paths
    assert "get" in paths["/api/v1/financial/invoices/{invoice_id}"]

    # Payments endpoints [FR-3]
    assert "/api/v1/financial/payments" in paths
    assert "get" in paths["/api/v1/financial/payments"]
    assert "post" in paths["/api/v1/financial/payments"]

    # AR Aging endpoint [FR-4]
    assert "/api/v1/financial/ar-aging" in paths
    assert "get" in paths["/api/v1/financial/ar-aging"]

    schemas = schema.get("components", {}).get("schemas", {})
    assert "QuoteResponse" in schemas
    assert "QuoteCreateRequest" in schemas
    assert "InvoiceResponse" in schemas
    assert "InvoiceCreateRequest" in schemas
    assert "PaymentResponse" in schemas
    assert "PaymentRecordRequest" in schemas
    assert "ARAgingBucketResponse" in schemas
    assert "ARAgingSummaryResponse" in schemas


def test_alerting_endpoints_present_in_openapi_schema() -> None:
    """Verify that notification hub and alerting API endpoints and models are included in OpenAPI schema [FR-5..FR-8, FR-9, FR-10, FR-11, FR-12]."""
    schema = app.openapi()
    paths = schema.get("paths", {})

    # In-App alerts endpoints [FR-5]
    assert "/api/v1/notifications/in-app" in paths
    assert "get" in paths["/api/v1/notifications/in-app"]

    assert "/api/v1/notifications/in-app/{notification_id}/read" in paths
    assert "patch" in paths["/api/v1/notifications/in-app/{notification_id}/read"]

    # Alert trigger rules endpoints [FR-6]
    assert "/api/v1/notifications/triggers" in paths
    assert "get" in paths["/api/v1/notifications/triggers"]
    assert "post" in paths["/api/v1/notifications/triggers"]

    assert "/api/v1/notifications/triggers/{trigger_id}" in paths
    assert "get" in paths["/api/v1/notifications/triggers/{trigger_id}"]
    assert "put" in paths["/api/v1/notifications/triggers/{trigger_id}"]
    assert "delete" in paths["/api/v1/notifications/triggers/{trigger_id}"]

    # Notification preferences endpoints [FR-7]
    assert "/api/v1/notifications/preferences" in paths
    assert "get" in paths["/api/v1/notifications/preferences"]
    assert "put" in paths["/api/v1/notifications/preferences"]

    schemas = schema.get("components", {}).get("schemas", {})
    assert "InAppAlertResponse" in schemas
    assert "TriggerResponse" in schemas
    assert "TriggerCreateRequest" in schemas
    assert "TriggerUpdateRequest" in schemas
    assert "PreferenceResponse" in schemas
    assert "PreferenceUpdateRequest" in schemas


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
