import pytest

from src.services.dashboard.domain_ports import (
    AggregationStatus,
    BreakdownItemDTO,
    DomainConnectorPort,
    DomainIdentifier,
    DomainMetricsResult,
    MetricResult,
    MetricStatus,
    MetricUnit,
    SummaryAggregationResult,
)


def test_metric_status_enum_values() -> None:
    """Test MetricStatus enum values match expected operational states."""
    assert MetricStatus.VALID == "VALID"
    assert MetricStatus.ZERO == "ZERO"
    assert MetricStatus.UNAVAILABLE == "UNAVAILABLE"


def test_aggregation_status_enum_values() -> None:
    """Test AggregationStatus enum values match expected multi-domain operational states."""
    assert AggregationStatus.COMPLETE == "COMPLETE"
    assert AggregationStatus.PARTIAL == "PARTIAL"
    assert AggregationStatus.FAILED == "FAILED"


def test_domain_identifier_enum_values() -> None:
    """Test DomainIdentifier enum values match core operational domains."""
    assert DomainIdentifier.LEAD_GEN == "lead_gen"
    assert DomainIdentifier.SALES_PIPELINE == "sales_pipeline"
    assert DomainIdentifier.SUPPORT == "support"
    assert DomainIdentifier.FINANCIALS == "financials"


def test_metric_unit_enum_values() -> None:
    """Test MetricUnit enum values match expected measurement unit types."""
    assert MetricUnit.CURRENCY == "currency"
    assert MetricUnit.COUNT == "count"
    assert MetricUnit.PERCENTAGE == "percentage"
    assert MetricUnit.RATIO == "ratio"


def test_metric_result_defaults_and_custom() -> None:
    """Test MetricResult DTO default initialization and custom parameter creation."""
    default_dto = MetricResult(
        domain=DomainIdentifier.LEAD_GEN.value,
        metric_key="lead_volume",
        display_name="Lead Volume",
    )
    assert default_dto.domain == "lead_gen"
    assert default_dto.metric_key == "lead_volume"
    assert default_dto.display_name == "Lead Volume"
    assert default_dto.raw_value is None
    assert default_dto.baseline_value is None
    assert default_dto.formatted_value == "Unavailable"
    assert default_dto.percentage_change is None
    assert default_dto.status == MetricStatus.UNAVAILABLE
    assert default_dto.unit == MetricUnit.COUNT.value
    assert default_dto.error_detail is None

    custom_dto = MetricResult(
        domain=DomainIdentifier.FINANCIALS.value,
        metric_key="recurring_revenue",
        display_name="Recurring Revenue",
        raw_value=15000.0,
        baseline_value=12000.0,
        formatted_value="$15,000",
        percentage_change=25.0,
        status=MetricStatus.VALID,
        unit=MetricUnit.CURRENCY.value,
        error_detail=None,
    )
    assert custom_dto.domain == "financials"
    assert custom_dto.metric_key == "recurring_revenue"
    assert custom_dto.raw_value == 15000.0
    assert custom_dto.percentage_change == 25.0
    assert custom_dto.status == MetricStatus.VALID
    assert custom_dto.unit == "currency"


def test_breakdown_item_dto() -> None:
    """Test BreakdownItemDTO creation and attributes."""
    item = BreakdownItemDTO(label="Organic", value=120.0)
    assert item.label == "Organic"
    assert item.value == 120.0


def test_domain_metrics_result_dto() -> None:
    """Test DomainMetricsResult DTO initialization."""
    metric = MetricResult(
        domain="support",
        metric_key="open_tickets",
        display_name="Open Tickets",
        raw_value=0.0,
        status=MetricStatus.ZERO,
        formatted_value="0",
    )
    breakdown = BreakdownItemDTO(label="P1 - Critical", value=0.0)

    result = DomainMetricsResult(
        domain="support",
        status="COMPLETE",
        metrics=[metric],
        breakdown_items=[breakdown],
    )

    assert result.domain == "support"
    assert result.status == "COMPLETE"
    assert len(result.metrics) == 1
    assert result.metrics[0].status == MetricStatus.ZERO
    assert len(result.breakdown_items) == 1
    assert result.breakdown_items[0].label == "P1 - Critical"
    assert result.error_detail is None


def test_domain_connector_port_abstract_interface() -> None:
    """Test that DomainConnectorPort cannot be instantiated directly and requires subclass implementation."""
    with pytest.raises(TypeError):
        DomainConnectorPort()  # type: ignore[abstract]

    class MockConnector(DomainConnectorPort):
        @property
        def domain_identifier(self) -> str:
            return "mock_domain"

        def fetch_metrics(self, timeframe: str = "30d") -> DomainMetricsResult:
            return DomainMetricsResult(
                domain=self.domain_identifier,
                status="COMPLETE",
                metrics=[],
                breakdown_items=[],
            )

    connector = MockConnector()
    assert connector.domain_identifier == "mock_domain"
    res = connector.fetch_metrics("7d")
    assert res.domain == "mock_domain"


def test_summary_aggregation_result_dto() -> None:
    """Test SummaryAggregationResult DTO initialization."""
    summary = SummaryAggregationResult(
        status=AggregationStatus.COMPLETE,
        generated_at="2026-10-06T12:00:00Z",
        metrics=[],
        domain_results=[],
    )
    assert summary.status == AggregationStatus.COMPLETE
    assert summary.generated_at == "2026-10-06T12:00:00Z"
    assert res.status == "COMPLETE"
