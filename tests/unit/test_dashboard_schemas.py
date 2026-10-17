"""Unit tests for dashboard API Pydantic schemas and input validation logic."""

from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

from src.api.dashboard import (
    AggregationStatusEnum,
    DashboardSummaryQueryParams,
    DomainDrilldownPathParams,
    DomainDrilldownQueryParams,
    DomainIdentifierEnum,
    DomainQuickViewResponse,
    DomainStatusEnum,
    MetricResponse,
    MetricStatusEnum,
    MetricUnitEnum,
    BreakdownItemResponse,
    SummaryAggregationResponse,
    TimeframeEnum,
    sanitize_input_string,
)
from src.services.dashboard.domain_ports import (
    AggregationStatus,
    BreakdownItemDTO,
    DomainMetricsResult,
    MetricResult,
    MetricStatus,
    MetricUnit,
    SummaryAggregationResult,
)


def test_sanitize_input_string() -> None:
    """Test string sanitization strips HTML tags, control chars, and whitespace."""
    assert sanitize_input_string(None) is None
    assert sanitize_input_string("") == ""
    assert sanitize_input_string("  hello world  ") == "hello world"
    # Test HTML script removal and html escaping
    cleaned = sanitize_input_string("<script>alert('xss')</script>Normal Text")
    assert "script" not in cleaned
    assert "Normal Text" in cleaned


def test_timeframe_query_param_validation() -> None:
    """Test DashboardSummaryQueryParams timeframe validation and defaults."""
    # Default timeframe
    default_params = DashboardSummaryQueryParams()
    assert default_params.timeframe == TimeframeEnum.THIRTY_DAYS

    # Valid string inputs
    params_7d = DashboardSummaryQueryParams(timeframe="7d")
    assert params_7d.timeframe == TimeframeEnum.SEVEN_DAYS

    params_90d = DashboardSummaryQueryParams(timeframe=" 90D ")
    assert params_90d.timeframe == TimeframeEnum.NINETY_DAYS

    # Invalid timeframe string
    with pytest.raises(ValidationError) as exc_info:
        DashboardSummaryQueryParams(timeframe="180d")
    assert "Invalid timeframe parameter" in str(exc_info.value)


def test_domain_drilldown_path_params_validation() -> None:
    """Test DomainDrilldownPathParams domain_id validation."""
    # Valid domains
    for domain_str, enum_val in [
        ("lead_gen", DomainIdentifierEnum.LEAD_GEN),
        ("sales_pipeline", DomainIdentifierEnum.SALES_PIPELINE),
        ("support", DomainIdentifierEnum.SUPPORT),
        ("financials", DomainIdentifierEnum.FINANCIALS),
    ]:
        params = DomainDrilldownPathParams(domain_id=domain_str)
        assert params.domain_id == enum_val

    # Case insensitive and stripped whitespace
    params = DomainDrilldownPathParams(domain_id="  FINANCIALS ")
    assert params.domain_id == DomainIdentifierEnum.FINANCIALS

    # Invalid domain
    with pytest.raises(ValidationError) as exc_info:
        DomainDrilldownPathParams(domain_id="invalid_domain")
    assert "Invalid domain_id parameter" in str(exc_info.value)


def test_metric_response_validation_and_from_dto() -> None:
    """Test MetricResponse schema validation and DTO conversion."""
    metric_dto = MetricResult(
        domain="lead_gen",
        metric_key="lead_volume",
        display_name="<script>Lead Volume</script>",
        raw_value=120.0,
        baseline_value=100.0,
        formatted_value=" 120 ",
        percentage_change=20.0,
        status=MetricStatus.VALID,
        unit=MetricUnit.COUNT,
        error_detail=None,
    )

    response = MetricResponse.from_dto(metric_dto)
    assert response.domain == "lead_gen"
    assert response.metric_key == "lead_volume"
    assert "script" not in response.display_name
    assert response.raw_value == 120.0
    assert response.baseline_value == 100.0
    assert response.percentage_change == 20.0
    assert response.status == MetricStatusEnum.VALID
    assert response.unit == "count"
    assert response.error_detail is None


def test_breakdown_item_response_from_dto() -> None:
    """Test BreakdownItemResponse schema validation and DTO conversion."""
    dto = BreakdownItemDTO(label="<b>Organic Search</b>", value=450.0)
    response = BreakdownItemResponse.from_dto(dto)
    assert "Organic Search" in response.label
    assert response.value == 450.0


def test_summary_aggregation_response_from_dto() -> None:
    """Test SummaryAggregationResponse schema creation from DTO."""
    dto = SummaryAggregationResult(
        status=AggregationStatus.COMPLETE,
        generated_at=datetime.now(timezone.utc).isoformat(),
        metrics=[
            MetricResult(
                domain="financials",
                metric_key="recurring_revenue",
                display_name="Recurring Revenue",
                raw_value=15000.0,
                status=MetricStatus.VALID,
                formatted_value="$15,000",
            )
        ],
    )

    response = SummaryAggregationResponse.from_dto(dto)
    assert response.status == AggregationStatusEnum.COMPLETE
    assert len(response.metrics) == 1
    assert response.metrics[0].domain == "financials"
    assert response.metrics[0].formatted_value == "$15,000"


def test_domain_quick_view_response_from_dto() -> None:
    """Test DomainQuickViewResponse schema creation from DTO."""
    dto = DomainMetricsResult(
        domain="support",
        status="COMPLETE",
        metrics=[
            MetricResult(
                domain="support",
                metric_key="open_tickets",
                display_name="Open Tickets",
                raw_value=0.0,
                status=MetricStatus.ZERO,
                formatted_value="0",
            )
        ],
        breakdown_items=[BreakdownItemDTO(label="P1 - Critical", value=0.0)],
    )

    response = DomainQuickViewResponse.from_dto(dto)
    assert response.domain == "support"
    assert response.status == DomainStatusEnum.COMPLETE
    assert len(response.detailed_metrics) == 1
    assert response.detailed_metrics[0].status == MetricStatusEnum.ZERO
    assert response.detailed_metrics[0].formatted_value == "0"
    assert len(response.breakdown_items) == 1
    assert response.breakdown_items[0].label == "P1 - Critical"
