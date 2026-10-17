"""Metric aggregation and business logic service.

This module implements metric status classification logic distinguishing VALID,
ZERO, and UNAVAILABLE metric states with structured JSON logging, baseline comparative
lookup capabilities, and multi-domain response collation for the executive dashboard.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from src.lib.logging import get_correlation_id
from src.models.dashboard import MetricSnapshot
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

logger = logging.getLogger(__name__)


def _sanitize_error_message(msg: str | None) -> str:
    """Sanitize error messages to ensure sensitive tokens or credentials are not exposed."""
    if not msg:
        return "Unknown error occurred"
    cleaned = str(msg).split("?")[0]
    cleaned = cleaned.replace("\n", " ").strip()
    if len(cleaned) > 200:
        cleaned = cleaned[:197] + "..."
    return cleaned


def format_metric_value(
    raw_value: float,
    unit: str | MetricUnit = MetricUnit.COUNT,
    metric_key: str = "",
) -> str:
    """Format a numerical metric value into human-readable string according to unit type.

    Args:
        raw_value: Unformatted numerical metric value.
        unit: Measurement unit (CURRENCY, COUNT, PERCENTAGE, RATIO).
        metric_key: Optional metric key identifier to determine specific unit suffixes.

    Returns:
        Formatted display string (e.g., "$12,500", "$0", "0", "0.0%", "2.4 hrs").
    """
    unit_str = unit.value if isinstance(unit, MetricUnit) else str(unit).lower()

    if raw_value == 0.0:
        if unit_str == MetricUnit.CURRENCY.value:
            return "$0"
        if unit_str == MetricUnit.PERCENTAGE.value:
            return "0.0%"
        if unit_str == MetricUnit.RATIO.value:
            if "time" in metric_key.lower() or "resolution" in metric_key.lower():
                return "0.0 hrs"
            return "0.0"
        return "0"

    if unit_str == MetricUnit.CURRENCY.value:
        if raw_value.is_integer():
            return f"${int(raw_value):,}"
        return f"${raw_value:,.2f}"

    if unit_str == MetricUnit.PERCENTAGE.value:
        return f"{raw_value:.1f}%"

    if unit_str == MetricUnit.RATIO.value:
        if "time" in metric_key.lower() or "resolution" in metric_key.lower():
            return f"{raw_value:.1f} hrs"
        return f"{raw_value:.2f}"

    # Default COUNT
    if raw_value.is_integer():
        return f"{int(raw_value):,}"
    return f"{raw_value:,.2f}"


def classify_metric_status(
    raw_value: float | int | str | None,
    unit: str | MetricUnit = MetricUnit.COUNT,
    metric_key: str = "",
    domain: str = "",
    error_detail: str | None = None,
) -> tuple[MetricStatus, str, float | None]:
    """Classify a metric's operational status and format its display value.

    Distinguishes strictly between VALID (non-zero numeric), ZERO (explicit numerical zero),
    and UNAVAILABLE (None, missing, or error) metric states with structured logging.

    Args:
        raw_value: Raw numerical or string metric value.
        unit: MetricUnit enum or string ('currency', 'count', 'percentage', 'ratio').
        metric_key: Unique metric identifier for contextual logging.
        domain: Domain identifier for contextual logging.
        error_detail: Optional error message if metric retrieval failed.

    Returns:
        Tuple of (MetricStatus, formatted_value_string, float_raw_value_or_none).
    """
    if raw_value is None or (error_detail is not None and raw_value is None):
        sanitized_err = (
            _sanitize_error_message(error_detail)
            if error_detail
            else "Metric value is None or unavailable"
        )
        logger.info(
            "Metric status classified as UNAVAILABLE",
            extra={
                "correlation_id": get_correlation_id(),
                "domain": domain or "unknown",
                "metric_key": metric_key or "unknown",
                "status": MetricStatus.UNAVAILABLE.value,
                "error_detail": sanitized_err,
            },
        )
        return MetricStatus.UNAVAILABLE, "Unavailable", None

    try:
        val_float = float(raw_value)
    except (ValueError, TypeError) as exc:
        sanitized_err = f"Failed to parse numeric value '{raw_value}': {_sanitize_error_message(str(exc))}"
        logger.warning(
            "Metric status classified as UNAVAILABLE due to parse error",
            extra={
                "correlation_id": get_correlation_id(),
                "domain": domain or "unknown",
                "metric_key": metric_key or "unknown",
                "status": MetricStatus.UNAVAILABLE.value,
                "error_detail": sanitized_err,
            },
        )
        return MetricStatus.UNAVAILABLE, "Unavailable", None

    if val_float == 0.0:
        formatted_str = format_metric_value(0.0, unit, metric_key)
        logger.info(
            "Metric status classified as ZERO",
            extra={
                "correlation_id": get_correlation_id(),
                "domain": domain or "unknown",
                "metric_key": metric_key or "unknown",
                "status": MetricStatus.ZERO.value,
                "raw_value": 0.0,
                "formatted_value": formatted_str,
            },
        )
        return MetricStatus.ZERO, formatted_str, 0.0

    formatted_str = format_metric_value(val_float, unit, metric_key)
    logger.info(
        "Metric status classified as VALID",
        extra={
            "correlation_id": get_correlation_id(),
            "domain": domain or "unknown",
            "metric_key": metric_key or "unknown",
            "status": MetricStatus.VALID.value,
            "raw_value": val_float,
            "formatted_value": formatted_str,
        },
    )
    return MetricStatus.VALID, formatted_str, val_float


def calculate_percentage_change(
    current_value: float | None,
    baseline_value: float | None,
) -> float | None:
    """Calculate percentage change between current_value and comparative baseline_value.

    Args:
        current_value: Present numerical metric value.
        baseline_value: Comparative historical baseline value.

    Returns:
        Float rounded to 2 decimal places representing percentage change,
        or None if calculation cannot be performed (e.g., missing input or division by zero).
    """
    if current_value is None or baseline_value is None:
        return None

    if baseline_value == 0.0:
        if current_value == 0.0:
            return 0.0
        return None

    diff = current_value - baseline_value
    pct_change = (diff / baseline_value) * 100.0
    return round(pct_change, 2)


def lookup_baseline_value(
    db_session: Session | None,
    domain: str,
    metric_key: str,
) -> float | None:
    """Lookup the latest baseline value for a domain metric snapshot from the database.

    Args:
        db_session: Synchronous SQLAlchemy database session.
        domain: Domain identifier string.
        metric_key: Specific metric key identifier.

    Returns:
        Float baseline value if found in database, else None.
    """
    if db_session is None or not domain or not metric_key:
        return None

    try:
        snapshot = (
            db_session.query(MetricSnapshot)
            .filter(
                MetricSnapshot.domain_name == domain,
                MetricSnapshot.metric_key == metric_key,
            )
            .order_by(MetricSnapshot.recorded_at.desc())
            .first()
        )
        if snapshot is not None:
            if snapshot.baseline_value is not None:
                return float(snapshot.baseline_value)
            if snapshot.current_value is not None:
                return float(snapshot.current_value)
    except Exception as exc:
        logger.warning(
            "Failed to lookup baseline value from database",
            extra={
                "correlation_id": get_correlation_id(),
                "domain": domain,
                "metric_key": metric_key,
                "error_detail": _sanitize_error_message(str(exc)),
            },
        )

    return None


def process_metric_result(
    metric: MetricResult,
    db_session: Session | None = None,
) -> MetricResult:
    """Classify and format an existing MetricResult DTO ensuring status integrity and baseline enrichment.

    Args:
        metric: Incoming MetricResult object.
        db_session: Optional database session for baseline value lookup if not present on DTO.

    Returns:
        Updated MetricResult object with validated status, formatted_value, baseline_value, and percentage_change.
    """
    status, formatted_val, parsed_raw = classify_metric_status(
        raw_value=metric.raw_value,
        unit=metric.unit,
        metric_key=metric.metric_key,
        domain=metric.domain,
        error_detail=metric.error_detail,
    )
    metric.status = status
    metric.formatted_value = formatted_val
    metric.raw_value = parsed_raw

    if metric.baseline_value is None and db_session is not None:
        metric.baseline_value = lookup_baseline_value(
            db_session, metric.domain, metric.metric_key
        )

    metric.percentage_change = calculate_percentage_change(
        metric.raw_value, metric.baseline_value
    )

    return metric


def evaluate_overall_status(
    domain_results: list[DomainMetricsResult],
) -> AggregationStatus:
    """Evaluate overall multi-domain aggregation status (COMPLETE, PARTIAL, FAILED).

    Args:
        domain_results: List of DomainMetricsResult DTOs from fetched domain connectors.

    Returns:
        AggregationStatus enum (COMPLETE, PARTIAL, FAILED).
    """
    if not domain_results:
        logger.info(
            "Overall aggregation status evaluated as FAILED (empty domain results)",
            extra={
                "correlation_id": get_correlation_id(),
                "overall_status": AggregationStatus.FAILED.value,
                "total_domains": 0,
            },
        )
        return AggregationStatus.FAILED

    total_domains = len(domain_results)
    succeeded_domains = 0
    failed_domains = 0

    all_metrics: list[MetricResult] = []
    for dom_res in domain_results:
        if dom_res.status == "UNAVAILABLE" or dom_res.status == AggregationStatus.FAILED.value:
            failed_domains += 1
        else:
            succeeded_domains += 1
        all_metrics.extend(dom_res.metrics)

    if not all_metrics:
        if failed_domains == total_domains:
            eval_status = AggregationStatus.FAILED
        elif succeeded_domains == total_domains:
            eval_status = AggregationStatus.COMPLETE
        else:
            eval_status = AggregationStatus.PARTIAL

        logger.info(
            "Overall aggregation status evaluated with empty metrics list",
            extra={
                "correlation_id": get_correlation_id(),
                "overall_status": eval_status.value,
                "total_domains": total_domains,
                "failed_domains": failed_domains,
            },
        )
        return eval_status

    unavailable_metrics_count = sum(
        1 for m in all_metrics if m.status == MetricStatus.UNAVAILABLE
    )
    valid_or_zero_count = sum(
        1 for m in all_metrics if m.status in (MetricStatus.VALID, MetricStatus.ZERO)
    )

    if (
        valid_or_zero_count > 0
        and unavailable_metrics_count == 0
        and failed_domains == 0
    ):
        overall_status = AggregationStatus.COMPLETE
    elif valid_or_zero_count == 0 or failed_domains == total_domains:
        overall_status = AggregationStatus.FAILED
    else:
        overall_status = AggregationStatus.PARTIAL

    logger.info(
        "Evaluated overall multi-domain aggregation status",
        extra={
            "correlation_id": get_correlation_id(),
            "overall_status": overall_status.value,
            "total_domains": total_domains,
            "failed_domains": failed_domains,
            "valid_or_zero_metrics": valid_or_zero_count,
            "unavailable_metrics": unavailable_metrics_count,
        },
    )

    return overall_status


def collate_multi_domain_responses(
    connectors: list[DomainConnectorPort],
    timeframe: str = "30d",
    db_session: Session | None = None,
) -> list[DomainMetricsResult]:
    """Execute and collate metric responses across multiple domain connectors with fault isolation.

    Args:
        connectors: List of domain connector port adapters.
        timeframe: Comparative duration timeframe ('7d', '30d', '90d').
        db_session: Optional database session for baseline enrichment.

    Returns:
        List of DomainMetricsResult objects containing processed metrics.
    """
    logger.info(
        "Collating multi-domain connector responses",
        extra={
            "correlation_id": get_correlation_id(),
            "timeframe": timeframe,
            "connector_count": len(connectors),
        },
    )

    results: list[DomainMetricsResult] = []

    for connector in connectors:
        domain_name = getattr(connector, "domain_identifier", "unknown")
        try:
            domain_res = connector.fetch_metrics(timeframe=timeframe)
        except Exception as exc:
            sanitized_err = _sanitize_error_message(str(exc))
            logger.error(
                "Connector execution raised uncaught exception during collation",
                extra={
                    "correlation_id": get_correlation_id(),
                    "domain": domain_name,
                    "error_detail": sanitized_err,
                },
            )
            domain_res = DomainMetricsResult(
                domain=domain_name,
                status="UNAVAILABLE",
                error_detail=sanitized_err,
                metrics=[],
                breakdown_items=[],
            )

        processed_metrics: list[MetricResult] = []
        for metric in domain_res.metrics:
            processed_m = process_metric_result(metric, db_session=db_session)
            processed_metrics.append(processed_m)

        domain_res.metrics = processed_metrics
        results.append(domain_res)

    return results


def collate_summary(
    connectors: list[DomainConnectorPort],
    timeframe: str = "30d",
    db_session: Session | None = None,
) -> SummaryAggregationResult:
    """Collate operational metrics across all domain connectors into a consolidated summary payload.

    Args:
        connectors: List of domain connector port adapters.
        timeframe: Comparative duration timeframe ('7d', '30d', '90d').
        db_session: Optional database session for baseline enrichment.

    Returns:
        SummaryAggregationResult DTO containing overall status, ISO timestamp, flat metrics list, and domain results.
    """
    domain_results = collate_multi_domain_responses(
        connectors=connectors,
        timeframe=timeframe,
        db_session=db_session,
    )

    overall_status = evaluate_overall_status(domain_results)

    flat_metrics: list[MetricResult] = []
    for dom_res in domain_results:
        flat_metrics.extend(dom_res.metrics)

    generated_at = datetime.now(timezone.utc).isoformat()

    return SummaryAggregationResult(
        status=overall_status,
        generated_at=generated_at,
        metrics=flat_metrics,
        domain_results=domain_results,
    )


def collate_domain_detail(
    connectors: list[DomainConnectorPort],
    domain_id: str,
    timeframe: str = "30d",
    db_session: Session | None = None,
) -> DomainMetricsResult:
    """Collate operational metrics and distribution breakdowns for a specific domain.

    Args:
        connectors: List of domain connector port adapters.
        domain_id: Target domain identifier (e.g., 'lead_gen', 'sales_pipeline', 'support', 'financials').
        timeframe: Comparative duration timeframe.
        db_session: Optional database session for baseline enrichment.

    Returns:
        DomainMetricsResult for the requested domain identifier.
    """
    target_connector = None
    for connector in connectors:
        if getattr(connector, "domain_identifier", "") == domain_id:
            target_connector = connector
            break

    if target_connector is None:
        sanitized_err = f"No connector configured for domain '{domain_id}'"
        logger.warning(
            sanitized_err,
            extra={
                "correlation_id": get_correlation_id(),
                "domain": domain_id,
            },
        )
        return DomainMetricsResult(
            domain=domain_id,
            status="UNAVAILABLE",
            error_detail=sanitized_err,
            metrics=[],
            breakdown_items=[],
        )

    collated_list = collate_multi_domain_responses(
        connectors=[target_connector],
        timeframe=timeframe,
        db_session=db_session,
    )
    return collated_list[0]


class DashboardAggregationService:
    """Core aggregation service evaluating metric classifications and collating multi-domain KPIs."""

    def __init__(
        self,
        connectors: list[DomainConnectorPort] | None = None,
        db_session: Session | None = None,
    ) -> None:
        """Initialize DashboardAggregationService.

        Args:
            connectors: Optional list of domain connector ports.
            db_session: Optional synchronous SQLAlchemy database session.
        """
        self.connectors = connectors or []
        self.db_session = db_session

    def classify_metric(
        self,
        raw_value: float | int | str | None,
        unit: str | MetricUnit = MetricUnit.COUNT,
        metric_key: str = "",
        domain: str = "",
        error_detail: str | None = None,
    ) -> tuple[MetricStatus, str, float | None]:
        """Classify a metric's operational status and format its display value."""
        return classify_metric_status(
            raw_value=raw_value,
            unit=unit,
            metric_key=metric_key,
            domain=domain,
            error_detail=error_detail,
        )

    def lookup_baseline(self, domain: str, metric_key: str) -> float | None:
        """Lookup baseline value from configured DB session for a given domain and metric key."""
        return lookup_baseline_value(self.db_session, domain, metric_key)

    def calculate_percentage_change(
        self,
        current_value: float | None,
        baseline_value: float | None,
    ) -> float | None:
        """Calculate percentage change between current_value and baseline_value."""
        return calculate_percentage_change(current_value, baseline_value)

    def evaluate_metric_result(self, metric: MetricResult) -> MetricResult:
        """Process, classify status, attach baseline, and calculate percentage change on a MetricResult DTO."""
        return process_metric_result(metric, db_session=self.db_session)

    def evaluate_overall_status(
        self, domain_results: list[DomainMetricsResult]
    ) -> AggregationStatus:
        """Evaluate overall multi-domain aggregation status (COMPLETE, PARTIAL, FAILED)."""
        return evaluate_overall_status(domain_results)

    def collate_responses(
        self, timeframe: str = "30d"
    ) -> list[DomainMetricsResult]:
        """Execute and collate metric responses across configured domain connectors."""
        return collate_multi_domain_responses(
            connectors=self.connectors,
            timeframe=timeframe,
            db_session=self.db_session,
        )

    def aggregate_summary(
        self, timeframe: str = "30d"
    ) -> SummaryAggregationResult:
        """Collate all domain metrics into a consolidated summary payload with overall status."""
        return collate_summary(
            connectors=self.connectors,
            timeframe=timeframe,
            db_session=self.db_session,
        )

    def aggregate_domain_detail(
        self, domain_id: str, timeframe: str = "30d"
    ) -> DomainMetricsResult:
        """Collate metrics and breakdowns for a specific target domain."""
        return collate_domain_detail(
            connectors=self.connectors,
            domain_id=domain_id,
            timeframe=timeframe,
            db_session=self.db_session,
        )
