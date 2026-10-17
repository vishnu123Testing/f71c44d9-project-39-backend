"""Unit tests for Sales Pipeline domain connector adapter."""

from unittest.mock import MagicMock

from src.connectors.sales_pipeline import SalesPipelineConnector, _sanitize_error_message
from src.services.dashboard.domain_ports import (
    DomainIdentifier,
    MetricStatus,
    MetricUnit,
)


def test_sales_pipeline_connector_domain_identifier() -> None:
    """Test that SalesPipelineConnector reports correct domain identifier."""
    connector = SalesPipelineConnector()
    assert connector.domain_identifier == DomainIdentifier.SALES_PIPELINE.value
    assert connector.domain_identifier == "sales_pipeline"


def test_fetch_metrics_default_success_30d() -> None:
    """Test fetching Sales Pipeline metrics for default 30d timeframe."""
    connector = SalesPipelineConnector()
    result = connector.fetch_metrics("30d")

    assert result.domain == "sales_pipeline"
    assert result.status == "COMPLETE"
    assert result.error_detail is None
    assert len(result.metrics) == 2

    # Verify Pipeline Value metric
    pipe_metric = next(m for m in result.metrics if m.metric_key == "pipeline_value")
    assert pipe_metric.display_name == "Pipeline Value"
    assert pipe_metric.raw_value == 450000.0
    assert pipe_metric.baseline_value == 400000.0
    assert pipe_metric.formatted_value == "$450,000"
    assert pipe_metric.percentage_change == 12.5
    assert pipe_metric.status == MetricStatus.VALID
    assert pipe_metric.unit == MetricUnit.CURRENCY.value

    # Verify Win Rate metric
    win_metric = next(m for m in result.metrics if m.metric_key == "win_rate")
    assert win_metric.display_name == "Win Rate"
    assert win_metric.raw_value == 32.5
    assert win_metric.baseline_value == 30.0
    assert win_metric.formatted_value == "32.5%"
    assert win_metric.percentage_change == 8.33
    assert win_metric.status == MetricStatus.VALID
    assert win_metric.unit == MetricUnit.PERCENTAGE.value

    # Verify Breakdown Items
    assert len(result.breakdown_items) == 4
    proposal = next(b for b in result.breakdown_items if b.label == "Proposal")
    assert proposal.value == 135000.0


def test_fetch_metrics_timeframes() -> None:
    """Test metric retrieval for different supported timeframes (7d, 90d)."""
    connector = SalesPipelineConnector()

    res_7d = connector.fetch_metrics("7d")
    assert res_7d.status == "COMPLETE"
    pipe_7d = next(m for m in res_7d.metrics if m.metric_key == "pipeline_value")
    assert pipe_7d.raw_value == 120000.0

    res_90d = connector.fetch_metrics("90d")
    assert res_90d.status == "COMPLETE"
    pipe_90d = next(m for m in res_90d.metrics if m.metric_key == "pipeline_value")
    assert pipe_90d.raw_value == 1350000.0


def test_fetch_metrics_invalid_timeframe_boundary_validation() -> None:
    """Test boundary validation rejects invalid timeframe inputs cleanly."""
    connector = SalesPipelineConnector()
    result = connector.fetch_metrics("invalid_timeframe")

    assert result.domain == "sales_pipeline"
    assert result.status == "UNAVAILABLE"
    assert result.error_detail is not None
    assert "Unsupported timeframe parameter" in result.error_detail

    for m in result.metrics:
        assert m.status == MetricStatus.UNAVAILABLE
        assert m.formatted_value == "Unavailable"


def test_fetch_metrics_zero_values_preservation() -> None:
    """Test zero metric values are classified as ZERO and formatted as $0 or 0.0%."""
    zero_data = {
        "metrics": {
            "pipeline_value": {"raw_value": 0.0, "baseline_value": 100000.0},
            "win_rate": {"raw_value": 0.0, "baseline_value": 25.0},
        },
        "breakdowns": [{"label": "Qualification", "value": 0.0}],
    }
    connector = SalesPipelineConnector(http_client=zero_data)
    result = connector.fetch_metrics("30d")

    assert result.status == "COMPLETE"
    pipe_metric = next(m for m in result.metrics if m.metric_key == "pipeline_value")
    assert pipe_metric.status == MetricStatus.ZERO
    assert pipe_metric.formatted_value == "$0"
    assert pipe_metric.percentage_change == -100.0

    win_metric = next(m for m in result.metrics if m.metric_key == "win_rate")
    assert win_metric.status == MetricStatus.ZERO
    assert win_metric.formatted_value == "0.0%"
    assert win_metric.percentage_change == -100.0


def test_fetch_metrics_simulated_failure_isolation() -> None:
    """Test fault isolation when connector encounters simulated upstream failure."""
    connector = SalesPipelineConnector(simulate_failure=True, retries=2)
    result = connector.fetch_metrics("30d")

    assert result.domain == "sales_pipeline"
    assert result.status == "UNAVAILABLE"
    assert result.error_detail is not None
    assert "Simulated upstream Sales Pipeline service failure" in result.error_detail

    for m in result.metrics:
        assert m.status == MetricStatus.UNAVAILABLE
        assert m.formatted_value == "Unavailable"
        assert m.raw_value is None
        assert m.percentage_change is None
    assert result.breakdown_items == []


def test_fetch_metrics_simulated_timeout_handling() -> None:
    """Test timeout handling when connector encounters simulated timeout."""
    connector = SalesPipelineConnector(simulate_timeout=True, timeout=5.0, retries=2)
    result = connector.fetch_metrics("30d")

    assert result.domain == "sales_pipeline"
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
            "pipeline_value": {"raw_value": 500000.0, "baseline_value": 400000.0},
            "win_rate": {"raw_value": 40.0, "baseline_value": 30.0},
        },
        "breakdowns": [
            {"label": "In Talks", "value": 300000.0},
            {"label": "Won", "value": 200000.0},
        ],
    }
    mock_client = MagicMock(return_value=custom_data)
    connector = SalesPipelineConnector(http_client=mock_client)

    result = connector.fetch_metrics("30d")
    assert result.status == "COMPLETE"
    mock_client.assert_called_once()

    pipe_metric = next(m for m in result.metrics if m.metric_key == "pipeline_value")
    assert pipe_metric.raw_value == 500000.0
    assert pipe_metric.formatted_value == "$500,000"
    assert pipe_metric.percentage_change == 25.0


def test_sanitize_error_message() -> None:
    """Test error message sanitization utility."""
    assert _sanitize_error_message("") == "Unknown error occurred"
    long_msg = "A" * 300
    cleaned = _sanitize_error_message(long_msg)
    assert len(cleaned) <= 200
    assert cleaned.endswith("...")

    msg_with_query = "http://api.example.com/data?token=secret123&user=admin"
    assert "token=secret123" not in _sanitize_error_message(msg_with_query)
