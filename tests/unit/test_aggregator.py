"""Unit tests for metric status classification and formatting logic in aggregator service."""

import logging
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.dashboard import MetricSnapshot
from src.models.database import Base
from src.services.dashboard.aggregator import (
    DashboardAggregationService,
    _sanitize_error_message,
    calculate_percentage_change,
    classify_metric_status,
    collate_domain_detail,
    collate_multi_domain_responses,
    collate_summary,
    evaluate_overall_status,
    format_metric_value,
    lookup_baseline_value,
    process_metric_result,
)
from src.services.dashboard.domain_ports import (
    AggregationStatus,
    BreakdownItemDTO,
    DomainConnectorPort,
    DomainMetricsResult,
    MetricResult,
    MetricStatus,
    MetricUnit,
    SummaryAggregationResult,
)


def test_format_metric_value_currency() -> None:
    """Test metric formatting for currency unit type across valid and zero values."""
    assert format_metric_value(12500.0, MetricUnit.CURRENCY) == "$12,500"
    assert format_metric_value(12500.50, MetricUnit.CURRENCY) == "$12,500.50"
    assert format_metric_value(0.0, MetricUnit.CURRENCY) == "$0"


def test_format_metric_value_count() -> None:
    """Test metric formatting for count unit type across valid and zero values."""
    assert format_metric_value(1250.0, MetricUnit.COUNT) == "1,250"
    assert format_metric_value(42.0, MetricUnit.COUNT) == "42"
    assert format_metric_value(0.0, MetricUnit.COUNT) == "0"


def test_format_metric_value_percentage() -> None:
    """Test metric formatting for percentage unit type across valid and zero values."""
    assert format_metric_value(4.2, MetricUnit.PERCENTAGE) == "4.2%"
    assert format_metric_value(32.54, MetricUnit.PERCENTAGE) == "32.5%"
    assert format_metric_value(0.0, MetricUnit.PERCENTAGE) == "0.0%"


def test_format_metric_value_ratio() -> None:
    """Test metric formatting for ratio unit type with time and generic suffixes."""
    assert (
        format_metric_value(2.4, MetricUnit.RATIO, metric_key="resolution_time")
        == "2.4 hrs"
    )
    assert (
        format_metric_value(0.0, MetricUnit.RATIO, metric_key="resolution_time")
        == "0.0 hrs"
    )
    assert format_metric_value(1.5, MetricUnit.RATIO, metric_key="ratio_kpi") == "1.50"
    assert format_metric_value(0.0, MetricUnit.RATIO, metric_key="ratio_kpi") == "0.0"


def test_classify_metric_status_valid() -> None:
    """Test classification of valid non-zero numeric metric states."""
    status, formatted, val = classify_metric_status(
        raw_value=12500.0,
        unit=MetricUnit.CURRENCY,
        metric_key="recurring_revenue",
        domain="financials",
    )
    assert status == MetricStatus.VALID
    assert formatted == "$12,500"
    assert val == 12500.0

    status, formatted, val = classify_metric_status(
        raw_value="4.2",
        unit=MetricUnit.PERCENTAGE,
        metric_key="conversion_rate",
        domain="lead_gen",
    )
    assert status == MetricStatus.VALID
    assert formatted == "4.2%"
    assert val == 4.2


def test_classify_metric_status_zero() -> None:
    """Test explicit zero metric classification distinguishing numerical zero from unavailable."""
    # Count Zero
    status, formatted, val = classify_metric_status(
        raw_value=0.0,
        unit=MetricUnit.COUNT,
        metric_key="open_tickets",
        domain="support",
    )
    assert status == MetricStatus.ZERO
    assert formatted == "0"
    assert val == 0.0

    # Currency Zero
    status, formatted, val = classify_metric_status(
        raw_value=0,
        unit=MetricUnit.CURRENCY,
        metric_key="pipeline_value",
        domain="sales_pipeline",
    )
    assert status == MetricStatus.ZERO
    assert formatted == "$0"
    assert val == 0.0

    # Percentage Zero
    status, formatted, val = classify_metric_status(
        raw_value="0.0",
        unit=MetricUnit.PERCENTAGE,
        metric_key="win_rate",
        domain="sales_pipeline",
    )
    assert status == MetricStatus.ZERO
    assert formatted == "0.0%"
    assert val == 0.0


def test_classify_metric_status_unavailable() -> None:
    """Test classification of missing, None, or unparseable metric states as UNAVAILABLE."""
    # None input
    status, formatted, val = classify_metric_status(
        raw_value=None,
        unit=MetricUnit.CURRENCY,
        metric_key="recurring_revenue",
        domain="financials",
    )
    assert status == MetricStatus.UNAVAILABLE
    assert formatted == "Unavailable"
    assert val is None

    # Invalid string input
    status, formatted, val = classify_metric_status(
        raw_value="invalid_number",
        unit=MetricUnit.COUNT,
        metric_key="lead_volume",
        domain="lead_gen",
    )
    assert status == MetricStatus.UNAVAILABLE
    assert formatted == "Unavailable"
    assert val is None

    # Error detail present with None raw_value
    status, formatted, val = classify_metric_status(
        raw_value=None,
        unit=MetricUnit.CURRENCY,
        metric_key="recurring_revenue",
        domain="financials",
        error_detail="Timeout connecting to upstream endpoint?token=secret123",
    )
    assert status == MetricStatus.UNAVAILABLE
    assert formatted == "Unavailable"
    assert val is None


def test_process_metric_result_dto() -> None:
    """Test process_metric_result helper updates MetricResult DTO accurately."""
    dto_zero = MetricResult(
        domain="support",
        metric_key="open_tickets",
        display_name="Open Tickets",
        raw_value=0.0,
        unit=MetricUnit.COUNT.value,
    )
    updated_zero = process_metric_result(dto_zero)
    assert updated_zero.status == MetricStatus.ZERO
    assert updated_zero.formatted_value == "0"
    assert updated_zero.raw_value == 0.0

    dto_unavailable = MetricResult(
        domain="financials",
        metric_key="recurring_revenue",
        display_name="Recurring Revenue",
        raw_value=None,
        unit=MetricUnit.CURRENCY.value,
        error_detail="Connection timeout",
    )
    updated_unavail = process_metric_result(dto_unavailable)
    assert updated_unavail.status == MetricStatus.UNAVAILABLE
    assert updated_unavail.formatted_value == "Unavailable"
    assert updated_unavail.raw_value is None


def test_dashboard_aggregation_service_instance() -> None:
    """Test DashboardAggregationService class classification methods."""
    service = DashboardAggregationService()
    status, formatted, val = service.classify_metric(
        raw_value=0.0,
        unit=MetricUnit.CURRENCY,
        metric_key="outstanding_invoices",
        domain="financials",
    )
    assert status == MetricStatus.ZERO
    assert formatted == "$0"
    assert val == 0.0

    dto = MetricResult(
        domain="lead_gen",
        metric_key="lead_volume",
        display_name="Lead Volume",
        raw_value=1250.0,
        unit=MetricUnit.COUNT.value,
    )
    evaluated = service.evaluate_metric_result(dto)
    assert evaluated.status == MetricStatus.VALID
    assert evaluated.formatted_value == "1,250"


@patch("src.services.dashboard.aggregator.logger.info")
def test_structured_logging_during_classification(mock_logger_info: patch) -> None:
    """Test that structured logging emits metadata and correlation IDs during classification."""
    with patch(
        "src.services.dashboard.aggregator.get_correlation_id",
        return_value="test-corr-id-123",
    ):
        classify_metric_status(
            raw_value=0.0,
            unit=MetricUnit.CURRENCY,
            metric_key="pipeline_value",
            domain="sales_pipeline",
        )

    mock_logger_info.assert_called_once()
    args, kwargs = mock_logger_info.call_args
    assert "ZERO" in args[0]
    extra = kwargs.get("extra", {})
    assert extra.get("correlation_id") == "test-corr-id-123"
    assert extra.get("domain") == "sales_pipeline"
    assert extra.get("metric_key") == "pipeline_value"
    assert extra.get("status") == MetricStatus.ZERO.value


def test_sanitize_error_message() -> None:
    """Test error message sanitization utility removes sensitive params and truncates."""
    assert _sanitize_error_message(None) == "Unknown error occurred"
    sensitive_msg = "http://api.internal/data?token=secret123&pass=abc"
    cleaned = _sanitize_error_message(sensitive_msg)
    assert "token=secret123" not in cleaned
    assert "pass=abc" not in cleaned
    assert cleaned == "http://api.internal/data"


def test_calculate_percentage_change() -> None:
    """Test percentage change calculations across positive, negative, zero, and None values."""
    # Positive percentage increase
    assert calculate_percentage_change(125.0, 100.0) == 25.0

    # Negative percentage decrease
    assert calculate_percentage_change(80.0, 100.0) == -20.0

    # Decrease to zero
    assert calculate_percentage_change(0.0, 100.0) == -100.0

    # Both zero
    assert calculate_percentage_change(0.0, 0.0) == 0.0

    # Division by zero (baseline is zero, current is non-zero)
    assert calculate_percentage_change(50.0, 0.0) is None

    # None inputs
    assert calculate_percentage_change(None, 100.0) is None
    assert calculate_percentage_change(100.0, None) is None
    assert calculate_percentage_change(None, None) is None

    # Fractional rounding to 2 decimal places
    assert calculate_percentage_change(10.0, 12.0) == -16.67


def test_lookup_baseline_value() -> None:
    """Test database baseline lookup retrieves historical baseline or current values from snapshots."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSession()

    try:
        # None session check
        assert lookup_baseline_value(None, "lead_gen", "conversion_rate") is None

        # Missing record check
        assert lookup_baseline_value(session, "lead_gen", "conversion_rate") is None

        # Insert snapshot with explicit baseline_value
        snapshot = MetricSnapshot(
            domain_name="lead_gen",
            metric_key="conversion_rate",
            current_value=12.5,
            baseline_value=10.0,
            status_code="VALID",
        )
        session.add(snapshot)
        session.commit()

        # Lookup should return explicit baseline_value
        assert lookup_baseline_value(session, "lead_gen", "conversion_rate") == 10.0

        # Insert newer snapshot with baseline_value=None but valid current_value
        snapshot_newer = MetricSnapshot(
            domain_name="lead_gen",
            metric_key="conversion_rate",
            current_value=15.0,
            baseline_value=None,
            status_code="VALID",
        )
        session.add(snapshot_newer)
        session.commit()

        # Lookup should return fallback current_value from the latest snapshot
        assert lookup_baseline_value(session, "lead_gen", "conversion_rate") == 15.0
    finally:
        session.close()


def test_process_metric_result_with_baseline_and_percentage() -> None:
    """Test end-to-end processing of MetricResult DTO including baseline and percentage change."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSession()

    try:
        # Seed snapshot in DB
        snapshot = MetricSnapshot(
            domain_name="financials",
            metric_key="recurring_revenue",
            current_value=10000.0,
            baseline_value=10000.0,
            status_code="VALID",
        )
        session.add(snapshot)
        session.commit()

        # 1. MetricResult with pre-set baseline_value
        dto_preset = MetricResult(
            domain="financials",
            metric_key="recurring_revenue",
            display_name="Recurring Revenue",
            raw_value=12500.0,
            baseline_value=10000.0,
            unit=MetricUnit.CURRENCY.value,
        )
        processed_preset = process_metric_result(dto_preset)
        assert processed_preset.status == MetricStatus.VALID
        assert processed_preset.formatted_value == "$12,500"
        assert processed_preset.baseline_value == 10000.0
        assert processed_preset.percentage_change == 25.0

        # 2. MetricResult with DB baseline lookup via service
        service = DashboardAggregationService(db_session=session)
        dto_db = MetricResult(
            domain="financials",
            metric_key="recurring_revenue",
            display_name="Recurring Revenue",
            raw_value=15000.0,
            baseline_value=None,
            unit=MetricUnit.CURRENCY.value,
        )
        evaluated = service.evaluate_metric_result(dto_db)
        assert evaluated.status == MetricStatus.VALID
        assert evaluated.formatted_value == "$15,000"
        assert evaluated.baseline_value == 10000.0
        assert evaluated.percentage_change == 50.0
    finally:
        session.close()


class DummyConnector(DomainConnectorPort):
    """Mock domain connector for aggregation testing."""

    def __init__(
        self,
        domain_id: str,
        result: DomainMetricsResult | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._domain_id = domain_id
        self._result = result
        self._raise_exc = raise_exc

    @property
    def domain_identifier(self) -> str:
        return self._domain_id

    def fetch_metrics(self, timeframe: str = "30d") -> DomainMetricsResult:
        if self._raise_exc:
            raise self._raise_exc
        if self._result:
            return self._result
        return DomainMetricsResult(
            domain=self._domain_id,
            status="COMPLETE",
            metrics=[
                MetricResult(
                    domain=self._domain_id,
                    metric_key=f"{self._domain_id}_kpi",
                    display_name=f"{self._domain_id.title()} KPI",
                    raw_value=100.0,
                    status=MetricStatus.VALID,
                    formatted_value="100",
                )
            ],
            breakdown_items=[BreakdownItemDTO(label="Segment A", value=50.0)],
        )


def test_evaluate_overall_status_complete_partial_failed() -> None:
    """Test overall status evaluation logic for COMPLETE, PARTIAL, and FAILED states."""
    # 1. Empty domain results -> FAILED
    assert evaluate_overall_status([]) == AggregationStatus.FAILED

    # 2. All domains COMPLETE with valid metrics -> COMPLETE
    valid_res1 = DomainMetricsResult(
        domain="lead_gen",
        status="COMPLETE",
        metrics=[
            MetricResult(
                domain="lead_gen",
                metric_key="volume",
                display_name="Volume",
                raw_value=500.0,
                status=MetricStatus.VALID,
                formatted_value="500",
            )
        ],
    )
    valid_res2 = DomainMetricsResult(
        domain="sales_pipeline",
        status="COMPLETE",
        metrics=[
            MetricResult(
                domain="sales_pipeline",
                metric_key="deals",
                display_name="Deals",
                raw_value=0.0,
                status=MetricStatus.ZERO,
                formatted_value="0",
            )
        ],
    )
    assert (
        evaluate_overall_status([valid_res1, valid_res2]) == AggregationStatus.COMPLETE
    )

    # 3. One domain COMPLETE, one UNAVAILABLE -> PARTIAL
    unavail_res = DomainMetricsResult(
        domain="support",
        status="UNAVAILABLE",
        error_detail="Timeout error",
        metrics=[
            MetricResult(
                domain="support",
                metric_key="open_tickets",
                display_name="Open Tickets",
                raw_value=None,
                status=MetricStatus.UNAVAILABLE,
                formatted_value="Unavailable",
            )
        ],
    )
    assert (
        evaluate_overall_status([valid_res1, unavail_res]) == AggregationStatus.PARTIAL
    )

    # 4. All domains UNAVAILABLE -> FAILED
    unavail_res2 = DomainMetricsResult(
        domain="financials",
        status="UNAVAILABLE",
        error_detail="Connection refused",
        metrics=[
            MetricResult(
                domain="financials",
                metric_key="revenue",
                display_name="Revenue",
                raw_value=None,
                status=MetricStatus.UNAVAILABLE,
                formatted_value="Unavailable",
            )
        ],
    )
    assert (
        evaluate_overall_status([unavail_res, unavail_res2]) == AggregationStatus.FAILED
    )


def test_collate_multi_domain_responses_and_fault_isolation() -> None:
    """Test collation across multiple domain connectors with fault isolation on exceptions."""
    conn_good = DummyConnector(domain_id="lead_gen")
    conn_failing = DummyConnector(
        domain_id="support", raise_exc=RuntimeError("Upstream timeout error")
    )

    collated = collate_multi_domain_responses(
        connectors=[conn_good, conn_failing], timeframe="30d"
    )

    assert len(collated) == 2
    assert collated[0].domain == "lead_gen"
    assert collated[0].status == "COMPLETE"
    assert collated[0].metrics[0].status == MetricStatus.VALID

    assert collated[1].domain == "support"
    assert collated[1].status == "UNAVAILABLE"
    assert "Upstream timeout error" in (collated[1].error_detail or "")


def test_collate_summary_and_service_aggregation() -> None:
    """Test summary collation generating consolidated payload with flat metrics and overall status."""
    conn_lead = DummyConnector(domain_id="lead_gen")
    conn_sales = DummyConnector(domain_id="sales_pipeline")

    service = DashboardAggregationService(connectors=[conn_lead, conn_sales])
    summary = service.aggregate_summary(timeframe="30d")

    assert isinstance(summary, SummaryAggregationResult)
    assert summary.status == AggregationStatus.COMPLETE
    assert len(summary.metrics) == 2
    assert summary.metrics[0].domain == "lead_gen"
    assert summary.metrics[1].domain == "sales_pipeline"
    assert summary.generated_at is not None


def test_collate_domain_detail() -> None:
    """Test collating domain drilldown details for target domain and unconfigured domain."""
    conn_lead = DummyConnector(domain_id="lead_gen")
    conn_sales = DummyConnector(domain_id="sales_pipeline")

    service = DashboardAggregationService(connectors=[conn_lead, conn_sales])

    # Valid configured domain
    lead_detail = service.aggregate_domain_detail("lead_gen", timeframe="30d")
    assert lead_detail.domain == "lead_gen"
    assert lead_detail.status == "COMPLETE"
    assert len(lead_detail.breakdown_items) == 1
    assert lead_detail.breakdown_items[0].label == "Segment A"

    # Unconfigured domain
    unknown_detail = service.aggregate_domain_detail("unknown_domain")
    assert unknown_detail.domain == "unknown_domain"
    assert unknown_detail.status == "UNAVAILABLE"
    assert "No connector configured" in (unknown_detail.error_detail or "")
