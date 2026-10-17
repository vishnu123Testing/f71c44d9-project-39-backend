"""Unit tests for Support Ticketing domain connector adapter."""

from unittest.mock import MagicMock

from src.connectors.support_tickets import SupportTicketsConnector, _sanitize_error_message
from src.services.dashboard.domain_ports import (
    DomainIdentifier,
    MetricStatus,
    MetricUnit,
)


def test_support_tickets_connector_domain_identifier() -> None:
    """Test that SupportTicketsConnector reports correct domain identifier."""
    connector = SupportTicketsConnector()
    assert connector.domain_identifier == DomainIdentifier.SUPPORT.value
    assert connector.domain_identifier == "support"


def test_fetch_metrics_default_success_30d() -> None:
    """Test fetching Support Ticketing metrics for default 30d timeframe."""
    connector = SupportTicketsConnector()
    result = connector.fetch_metrics("30d")

    assert result.domain == "support"
    assert result.status == "COMPLETE"
    assert result.error_detail is None
    assert len(result.metrics) == 3

    # Verify Open Tickets metric
    open_metric = next(m for m in result.metrics if m.metric_key == "open_tickets")
    assert open_metric.display_name == "Open Tickets"
    assert open_metric.raw_value == 42.0
    assert open_metric.baseline_value == 50.0
    assert open_metric.formatted_value == "42"
    assert open_metric.percentage_change == -16.0
    assert open_metric.status == MetricStatus.VALID
    assert open_metric.unit == MetricUnit.COUNT.value

    # Verify Resolution Time metric
    res_metric = next(m for m in result.metrics if m.metric_key == "resolution_time")
    assert res_metric.display_name == "Resolution Time"
    assert res_metric.raw_value == 2.4
    assert res_metric.baseline_value == 2.8
    assert res_metric.formatted_value == "2.4 hrs"
    assert res_metric.percentage_change == -14.29
    assert res_metric.status == MetricStatus.VALID
    assert res_metric.unit == MetricUnit.RATIO.value

    # Verify SLA Compliance metric
    sla_metric = next(m for m in result.metrics if m.metric_key == "sla_compliance")
    assert sla_metric.display_name == "SLA Compliance"
    assert sla_metric.raw_value == 97.5
    assert sla_metric.baseline_value == 95.0
    assert sla_metric.formatted_value == "97.5%"
    assert sla_metric.percentage_change == 2.63
    assert sla_metric.status == MetricStatus.VALID
    assert sla_metric.unit == MetricUnit.PERCENTAGE.value

    # Verify Breakdown Items
    assert len(result.breakdown_items) == 4
    p1 = next(b for b in result.breakdown_items if b.label == "P1 - Critical")
    assert p1.value == 4.0


def test_fetch_metrics_timeframes() -> None:
    """Test metric retrieval for different supported timeframes (7d, 90d)."""
    connector = SupportTicketsConnector()

    res_7d = connector.fetch_metrics("7d")
    assert res_7d.status == "COMPLETE"
    open_7d = next(m for m in res_7d.metrics if m.metric_key == "open_tickets")
    assert open_7d.raw_value == 18.0

    res_90d = connector.fetch_metrics("90d")
    assert res_90d.status == "COMPLETE"
    open_90d = next(m for m in res_90d.metrics if m.metric_key == "open_tickets")
    assert open_90d.raw_value == 135.0


def test_fetch_metrics_invalid_timeframe_boundary_validation() -> None:
    """Test boundary validation rejects invalid timeframe inputs cleanly."""
    connector = SupportTicketsConnector()
    result = connector.fetch_metrics("invalid_timeframe")

    assert result.domain == "support"
    assert result.status == "UNAVAILABLE"
    assert result.error_detail is not None
    assert "Unsupported timeframe parameter" in result.error_detail

    for m in result.metrics:
        assert m.status == MetricStatus.UNAVAILABLE
        assert m.formatted_value == "Unavailable"


def test_fetch_metrics_zero_values_preservation() -> None:
    """Test zero metric values are classified as ZERO and formatted as 0, 0.0 hrs, or 0.0%."""
    zero_data = {
        "metrics": {
            "open_tickets": {"raw_value": 0.0, "baseline_value": 10.0},
            "resolution_time": {"raw_value": 0.0, "baseline_value": 2.0},
            "sla_compliance": {"raw_value": 0.0, "baseline_value": 90.0},
        },
        "breakdowns": [{"label": "P1 - Critical", "value": 0.0}],
    }
    connector = SupportTicketsConnector(http_client=zero_data)
    result = connector.fetch_metrics("30d")

    assert result.status == "COMPLETE"
    open_metric = next(m for m in result.metrics if m.metric_key == "open_tickets")
    assert open_metric.status == MetricStatus.ZERO
    assert open_metric.formatted_value == "0"
    assert open_metric.percentage_change == -100.0

    res_metric = next(m for m in result.metrics if m.metric_key == "resolution_time")
    assert res_metric.status == MetricStatus.ZERO
    assert res_metric.formatted_value == "0.0 hrs"
    assert res_metric.percentage_change == -100.0

    sla_metric = next(m for m in result.metrics if m.metric_key == "sla_compliance")
    assert sla_metric.status == MetricStatus.ZERO
    assert sla_metric.formatted_value == "0.0%"
    assert sla_metric.percentage_change == -100.0


def test_fetch_metrics_simulated_failure_isolation() -> None:
    """Test fault isolation when connector encounters simulated upstream failure."""
    connector = SupportTicketsConnector(simulate_failure=True, retries=2)
    result = connector.fetch_metrics("30d")

    assert result.domain == "support"
    assert result.status == "UNAVAILABLE"
    assert result.error_detail is not None
    assert "Simulated upstream Support Ticketing service failure" in result.error_detail

    for m in result.metrics:
        assert m.status == MetricStatus.UNAVAILABLE
        assert m.formatted_value == "Unavailable"
        assert m.raw_value is None
        assert m.percentage_change is None
    assert result.breakdown_items == []


def test_fetch_metrics_simulated_timeout_handling() -> None:
    """Test timeout handling when connector encounters simulated timeout."""
    connector = SupportTicketsConnector(simulate_timeout=True, timeout=5.0, retries=2)
    result = connector.fetch_metrics("30d")

    assert result.domain == "support"
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
            "open_tickets": {"raw_value": 15.0, "baseline_value": 20.0},
            "resolution_time": {"raw_value": 1.5, "baseline_value": 2.0},
            "sla_compliance": {"raw_value": 99.0, "baseline_value": 95.0},
        },
        "breakdowns": [
            {"label": "P1 - Critical", "value": 1.0},
            {"label": "P2 - High", "value": 4.0},
        ],
    }
    mock_client = MagicMock(return_value=custom_data)
    connector = SupportTicketsConnector(http_client=mock_client)

    result = connector.fetch_metrics("30d")
    assert result.status == "COMPLETE"
    mock_client.assert_called_once()

    open_metric = next(m for m in result.metrics if m.metric_key == "open_tickets")
    assert open_metric.raw_value == 15.0
    assert open_metric.formatted_value == "15"
    assert open_metric.percentage_change == -25.0


def test_sanitize_error_message() -> None:
    """Test error message sanitization utility."""
    assert _sanitize_error_message("") == "Unknown error occurred"
    long_msg = "B" * 300
    cleaned = _sanitize_error_message(long_msg)
    assert len(cleaned) <= 200
    assert cleaned.endswith("...")

    msg_with_query = "http://support.example.com/tickets?api_key=secret_abc123"
    assert "api_key=secret_abc123" not in _sanitize_error_message(msg_with_query)
