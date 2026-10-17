"""Unit tests for Financials domain connector adapter."""

from unittest.mock import MagicMock

from src.connectors.financials import FinancialsConnector, _sanitize_error_message
from src.services.dashboard.domain_ports import (
    DomainIdentifier,
    MetricStatus,
    MetricUnit,
)


def test_financials_connector_domain_identifier() -> None:
    """Test that FinancialsConnector reports correct domain identifier."""
    connector = FinancialsConnector()
    assert connector.domain_identifier == DomainIdentifier.FINANCIALS.value
    assert connector.domain_identifier == "financials"


def test_fetch_metrics_default_success_30d() -> None:
    """Test fetching Financial Summary metrics for default 30d timeframe."""
    connector = FinancialsConnector()
    result = connector.fetch_metrics("30d")

    assert result.domain == "financials"
    assert result.status == "COMPLETE"
    assert result.error_detail is None
    assert len(result.metrics) == 2

    # Verify Recurring Revenue metric
    mrr_metric = next(
        m for m in result.metrics if m.metric_key == "recurring_revenue"
    )
    assert mrr_metric.display_name == "Recurring Revenue"
    assert mrr_metric.raw_value == 320000.0
    assert mrr_metric.baseline_value == 300000.0
    assert mrr_metric.formatted_value == "$320,000"
    assert mrr_metric.percentage_change == 6.67
    assert mrr_metric.status == MetricStatus.VALID
    assert mrr_metric.unit == MetricUnit.CURRENCY.value

    # Verify Outstanding Invoices metric
    inv_metric = next(
        m for m in result.metrics if m.metric_key == "outstanding_invoices"
    )
    assert inv_metric.display_name == "Outstanding Invoices"
    assert inv_metric.raw_value == 450000.0 or inv_metric.raw_value == 45000.0
    assert inv_metric.raw_value == 45000.0
    assert inv_metric.baseline_value == 50000.0
    assert inv_metric.formatted_value == "$45,000"
    assert inv_metric.percentage_change == -10.0
    assert inv_metric.status == MetricStatus.VALID
    assert inv_metric.unit == MetricUnit.CURRENCY.value

    # Verify Breakdown Items
    assert len(result.breakdown_items) == 4
    ent = next(
        b for b in result.breakdown_items if b.label == "Enterprise Subscriptions"
    )
    assert ent.value == 176000.0


def test_fetch_metrics_timeframes() -> None:
    """Test metric retrieval for different supported timeframes (7d, 90d)."""
    connector = FinancialsConnector()

    res_7d = connector.fetch_metrics("7d")
    assert res_7d.status == "COMPLETE"
    mrr_7d = next(m for m in res_7d.metrics if m.metric_key == "recurring_revenue")
    assert mrr_7d.raw_value == 80000.0

    res_90d = connector.fetch_metrics("90d")
    assert res_90d.status == "COMPLETE"
    mrr_90d = next(
        m for m in res_90d.metrics if m.metric_key == "recurring_revenue"
    )
    assert mrr_90d.raw_value == 960000.0


def test_fetch_metrics_invalid_timeframe_boundary_validation() -> None:
    """Test boundary validation rejects invalid timeframe inputs cleanly."""
    connector = FinancialsConnector()
    result = connector.fetch_metrics("invalid_timeframe")

    assert result.domain == "financials"
    assert result.status == "UNAVAILABLE"
    assert result.error_detail is not None
    assert "Unsupported timeframe parameter" in result.error_detail

    for m in result.metrics:
        assert m.status == MetricStatus.UNAVAILABLE
        assert m.formatted_value == "Unavailable"


def test_fetch_metrics_zero_values_preservation() -> None:
    """Test zero metric values are classified as ZERO and formatted as $0."""
    zero_data = {
        "metrics": {
            "recurring_revenue": {"raw_value": 0.0, "baseline_value": 10000.0},
            "outstanding_invoices": {"raw_value": 0.0, "baseline_value": 5000.0},
        },
        "breakdowns": [{"label": "Enterprise Subscriptions", "value": 0.0}],
    }
    connector = FinancialsConnector(http_client=zero_data)
    result = connector.fetch_metrics("30d")

    assert result.status == "COMPLETE"
    mrr_metric = next(
        m for m in result.metrics if m.metric_key == "recurring_revenue"
    )
    assert mrr_metric.status == MetricStatus.ZERO
    assert mrr_metric.formatted_value == "$0"
    assert mrr_metric.percentage_change == -100.0

    inv_metric = next(
        m for m in result.metrics if m.metric_key == "outstanding_invoices"
    )
    assert inv_metric.status == MetricStatus.ZERO
    assert inv_metric.formatted_value == "$0"
    assert inv_metric.percentage_change == -100.0


def test_fetch_metrics_simulated_failure_isolation() -> None:
    """Test fault isolation when connector encounters simulated upstream failure."""
    connector = FinancialsConnector(simulate_failure=True, retries=2)
    result = connector.fetch_metrics("30d")

    assert result.domain == "financials"
    assert result.status == "UNAVAILABLE"
    assert result.error_detail is not None
    assert (
        "Simulated upstream Financial service failure" in result.error_detail
    )

    for m in result.metrics:
        assert m.status == MetricStatus.UNAVAILABLE
        assert m.formatted_value == "Unavailable"
        assert m.raw_value is None
        assert m.percentage_change is None
    assert result.breakdown_items == []


def test_fetch_metrics_simulated_timeout_handling() -> None:
    """Test timeout handling when connector encounters simulated timeout."""
    connector = FinancialsConnector(simulate_timeout=True, timeout=5.0, retries=2)
    result = connector.fetch_metrics("30d")

    assert result.domain == "financials"
    assert result.status == "UNAVAILABLE"
    assert result.error_detail is not None
    assert "timed out after 5.0s" in result.error_detail

    for m in result.metrics:
        assert m.status == MetricStatus.UNAVAILABLE
        assert m.formatted_value == "Unavailable"


def test_fetch_metrics_custom_http_client() -> None:
    """Test dependency injection with custom HTTP client returning custom data."""
    custom_data = {
        "metrics": {
            "recurring_revenue": {"raw_value": 500000.0, "baseline_value": 400000.0},
            "outstanding_invoices": {
                "raw_value": 20000.0,
                "baseline_value": 25000.0,
            },
        },
        "breakdowns": [
            {"label": "Enterprise Subscriptions", "value": 300000.0},
            {"label": "SMB Subscriptions", "value": 200000.0},
        ],
    }
    mock_client = MagicMock(return_value=custom_data)
    connector = FinancialsConnector(http_client=mock_client)

    result = connector.fetch_metrics("30d")
    assert result.status == "COMPLETE"
    mock_client.assert_called_once()

    mrr_metric = next(
        m for m in result.metrics if m.metric_key == "recurring_revenue"
    )
    assert mrr_metric.raw_value == 500000.0
    assert mrr_metric.formatted_value == "$500,000"
    assert mrr_metric.percentage_change == 25.0


def test_sanitize_error_message() -> None:
    """Test error message sanitization utility."""
    assert _sanitize_error_message("") == "Unknown error occurred"
    long_msg = "C" * 300
    cleaned = _sanitize_error_message(long_msg)
    assert len(cleaned) <= 200
    assert cleaned.endswith("...")

    msg_with_query = "http://financials.example.com/api?token=secret_xyz"
    assert "token=secret_xyz" not in _sanitize_error_message(msg_with_query)
