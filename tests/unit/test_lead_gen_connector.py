"""Unit tests for Lead Generation domain connector adapter."""

from unittest.mock import MagicMock

from src.connectors.lead_gen import LeadGenConnector, _sanitize_error_message
from src.services.dashboard.domain_ports import (
    DomainIdentifier,
    MetricStatus,
    MetricUnit,
)


def test_lead_gen_connector_domain_identifier() -> None:
    """Test that LeadGenConnector reports correct domain identifier."""
    connector = LeadGenConnector()
    assert connector.domain_identifier == DomainIdentifier.LEAD_GEN.value
    assert connector.domain_identifier == "lead_gen"


def test_fetch_metrics_default_success_30d() -> None:
    """Test fetching Lead Gen metrics for default 30d timeframe."""
    connector = LeadGenConnector()
    result = connector.fetch_metrics("30d")

    assert result.domain == "lead_gen"
    assert result.status == "COMPLETE"
    assert result.error_detail is None
    assert len(result.metrics) == 2

    # Verify Lead Volume metric
    vol_metric = next(m for m in result.metrics if m.metric_key == "lead_volume")
    assert vol_metric.display_name == "Lead Volume"
    assert vol_metric.raw_value == 1250.0
    assert vol_metric.baseline_value == 1000.0
    assert vol_metric.formatted_value == "1,250"
    assert vol_metric.percentage_change == 25.0
    assert vol_metric.status == MetricStatus.VALID
    assert vol_metric.unit == MetricUnit.COUNT.value

    # Verify Conversion Rate metric
    conv_metric = next(m for m in result.metrics if m.metric_key == "conversion_rate")
    assert conv_metric.display_name == "Conversion Rate"
    assert conv_metric.raw_value == 4.2
    assert conv_metric.baseline_value == 3.8
    assert conv_metric.formatted_value == "4.2%"
    assert conv_metric.percentage_change == 10.53
    assert conv_metric.status == MetricStatus.VALID
    assert conv_metric.unit == MetricUnit.PERCENTAGE.value

    # Verify Breakdown Items
    assert len(result.breakdown_items) == 4
    organic = next(b for b in result.breakdown_items if b.label == "Organic Search")
    assert organic.value == 450.0


def test_fetch_metrics_timeframes() -> None:
    """Test metric retrieval for different supported timeframes (7d, 90d)."""
    connector = LeadGenConnector()

    res_7d = connector.fetch_metrics("7d")
    assert res_7d.status == "COMPLETE"
    vol_7d = next(m for m in res_7d.metrics if m.metric_key == "lead_volume")
    assert vol_7d.raw_value == 310.0

    res_90d = connector.fetch_metrics("90d")
    assert res_90d.status == "COMPLETE"
    vol_90d = next(m for m in res_90d.metrics if m.metric_key == "lead_volume")
    assert vol_90d.raw_value == 3800.0


def test_fetch_metrics_invalid_timeframe_boundary_validation() -> None:
    """Test boundary validation rejects invalid timeframe inputs cleanly."""
    connector = LeadGenConnector()
    result = connector.fetch_metrics("invalid_timeframe")

    assert result.domain == "lead_gen"
    assert result.status == "UNAVAILABLE"
    assert result.error_detail is not None
    assert "Unsupported timeframe parameter" in result.error_detail

    for m in result.metrics:
        assert m.status == MetricStatus.UNAVAILABLE
        assert m.formatted_value == "Unavailable"


def test_fetch_metrics_zero_values_preservation() -> None:
    """Test zero metric values are classified as ZERO and formatted as 0 or 0.0%."""
    zero_data = {
        "metrics": {
            "lead_volume": {"raw_value": 0.0, "baseline_value": 100.0},
            "conversion_rate": {"raw_value": 0.0, "baseline_value": 5.0},
        },
        "breakdowns": [{"label": "Organic", "value": 0.0}],
    }
    connector = LeadGenConnector(http_client=zero_data)
    result = connector.fetch_metrics("30d")

    assert result.status == "COMPLETE"
    vol_metric = next(m for m in result.metrics if m.metric_key == "lead_volume")
    assert vol_metric.status == MetricStatus.ZERO
    assert vol_metric.formatted_value == "0"
    assert vol_metric.percentage_change == -100.0

    conv_metric = next(m for m in result.metrics if m.metric_key == "conversion_rate")
    assert conv_metric.status == MetricStatus.ZERO
    assert conv_metric.formatted_value == "0.0%"
    assert conv_metric.percentage_change == -100.0


def test_fetch_metrics_simulated_failure_isolation() -> None:
    """Test fault isolation when connector encounters simulated upstream failure."""
    connector = LeadGenConnector(simulate_failure=True, retries=2)
    result = connector.fetch_metrics("30d")

    assert result.domain == "lead_gen"
    assert result.status == "UNAVAILABLE"
    assert result.error_detail is not None
    assert "Simulated upstream Lead Gen service failure" in result.error_detail

    for m in result.metrics:
        assert m.status == MetricStatus.UNAVAILABLE
        assert m.formatted_value == "Unavailable"
        assert m.raw_value is None
        assert m.percentage_change is None
    assert result.breakdown_items == []


def test_fetch_metrics_simulated_timeout_handling() -> None:
    """Test timeout handling when connector encounters simulated timeout."""
    connector = LeadGenConnector(simulate_timeout=True, timeout=5.0, retries=2)
    result = connector.fetch_metrics("30d")

    assert result.domain == "lead_gen"
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
            "lead_volume": {"raw_value": 500.0, "baseline_value": 400.0},
            "conversion_rate": {"raw_value": 5.0, "baseline_value": 4.0},
        },
        "breakdowns": [
            {"label": "Social", "value": 200.0},
            {"label": "Email", "value": 300.0},
        ],
    }
    mock_client = MagicMock(return_value=custom_data)
    connector = LeadGenConnector(http_client=mock_client)

    result = connector.fetch_metrics("30d")
    assert result.status == "COMPLETE"
    mock_client.assert_called_once()

    vol_metric = next(m for m in result.metrics if m.metric_key == "lead_volume")
    assert vol_metric.raw_value == 500.0
    assert vol_metric.formatted_value == "500"
    assert vol_metric.percentage_change == 25.0


def test_sanitize_error_message() -> None:
    """Test error message sanitization utility."""
    assert _sanitize_error_message("") == "Unknown error occurred"
    long_msg = "A" * 300
    cleaned = _sanitize_error_message(long_msg)
    assert len(cleaned) <= 200
    assert cleaned.endswith("...")

    msg_with_query = "http://api.example.com/data?token=secret123&user=admin"
    assert "token=secret123" not in _sanitize_error_message(msg_with_query)
