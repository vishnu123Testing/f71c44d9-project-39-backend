"""Support Ticketing domain connector adapter.

This module implements the SupportTicketsConnector adapter for fetching Support Ticketing
operational metrics (open tickets, resolution times, SLA compliance) and ticket priority
distribution breakdowns from downstream Support domain sources with fault isolation,
timeout handling, and retry mechanisms.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from src.lib.config import settings
from src.lib.logging import get_correlation_id
from src.services.dashboard.domain_ports import (
    BreakdownItemDTO,
    DomainConnectorPort,
    DomainIdentifier,
    DomainMetricsResult,
    MetricResult,
    MetricStatus,
    MetricUnit,
)

logger = logging.getLogger(__name__)

SUPPORTED_TIMEFRAMES = {"7d", "30d", "90d"}


def _sanitize_error_message(msg: str) -> str:
    """Sanitize error messages to ensure sensitive tokens or credentials are not exposed."""
    if not msg:
        return "Unknown error occurred"
    cleaned = str(msg).split("?")[0]
    cleaned = cleaned.replace("\n", " ").strip()
    if len(cleaned) > 200:
        cleaned = cleaned[:197] + "..."
    return cleaned


class SupportTicketsConnector(DomainConnectorPort):
    """Adapter fetching Support Ticketing operational metrics with fault isolation."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        http_client: Any | None = None,
        simulate_failure: bool = False,
        simulate_timeout: bool = False,
    ) -> None:
        """Initialize SupportTicketsConnector adapter.

        Args:
            endpoint_url: Optional API endpoint URL for downstream Support Ticketing service.
            timeout: Execution timeout in seconds. Defaults to CONNECTOR_TIMEOUT setting.
            retries: Retry attempts for transient failures. Defaults to CONNECTOR_RETRIES setting.
            http_client: Optional HTTP client instance or callable for dependency injection.
            simulate_failure: Flag to simulate an upstream fault for isolation testing.
            simulate_timeout: Flag to simulate a request timeout for isolation testing.
        """
        self.endpoint_url = endpoint_url or os.getenv("SUPPORT_TICKETS_API_URL") or os.getenv("SUPPORT_API_URL")
        self.timeout = timeout if timeout is not None else settings.CONNECTOR_TIMEOUT
        self.retries = retries if retries is not None else settings.CONNECTOR_RETRIES
        self.http_client = http_client
        self.simulate_failure = simulate_failure
        self.simulate_timeout = simulate_timeout

    @property
    def domain_identifier(self) -> str:
        """Return the unique domain identifier for Support Ticketing."""
        return DomainIdentifier.SUPPORT.value

    def fetch_metrics(self, timeframe: str = "30d") -> DomainMetricsResult:
        """Fetch Support Ticketing operational metrics and priority breakdown items for a timeframe.

        Args:
            timeframe: Comparative duration ('7d', '30d', '90d').

        Returns:
            DomainMetricsResult containing metric items and distribution breakdowns.
        """
        # Strict boundary validation & input sanitization
        timeframe_clean = str(timeframe).strip().lower() if timeframe else "30d"
        if timeframe_clean not in SUPPORTED_TIMEFRAMES:
            err_msg = f"Unsupported timeframe parameter '{timeframe_clean}'"
            logger.warning(
                "Support Ticketing connector rejected invalid timeframe input",
                extra={
                    "correlation_id": get_correlation_id(),
                    "timeframe": timeframe_clean,
                },
            )
            return self._build_unavailable_result(err_msg)

        max_attempts = max(1, self.retries)
        last_error = ""

        for attempt in range(1, max_attempts + 1):
            try:
                if self.simulate_timeout:
                    raise TimeoutError(
                        f"Support Ticketing connector timed out after {self.timeout}s"
                    )

                if self.simulate_failure:
                    raise RuntimeError("Simulated upstream Support Ticketing service failure")

                raw_data = self._retrieve_data(timeframe_clean)
                return self._parse_and_build_result(raw_data, timeframe_clean)

            except (TimeoutError, urllib.error.URLError, OSError) as exc:
                last_error = f"Support Ticketing connector execution timed out or network error (attempt {attempt}/{max_attempts}): {_sanitize_error_message(str(exc))}"
                logger.warning(
                    last_error,
                    extra={
                        "correlation_id": get_correlation_id(),
                        "domain": self.domain_identifier,
                        "attempt": attempt,
                    },
                )
            except Exception as exc:
                last_error = f"Support Ticketing connector encountered unexpected error (attempt {attempt}/{max_attempts}): {_sanitize_error_message(str(exc))}"
                logger.error(
                    last_error,
                    extra={
                        "correlation_id": get_correlation_id(),
                        "domain": self.domain_identifier,
                        "attempt": attempt,
                    },
                )

        # Fault isolation fallback: return UNAVAILABLE result without raising exception
        return self._build_unavailable_result(last_error or "Support Ticketing connector failed")

    def _retrieve_data(self, timeframe: str) -> dict[str, Any]:
        """Execute HTTP request or retrieve raw metric payload from downstream provider."""
        if self.http_client is not None:
            if callable(self.http_client):
                res = self.http_client(
                    self.endpoint_url or "http://support-tickets.local/api",
                    timeframe,
                    self.timeout,
                )
            elif hasattr(self.http_client, "get"):
                res = self.http_client.get(
                    self.endpoint_url or "http://support-tickets.local/api",
                    params={"timeframe": timeframe},
                    timeout=self.timeout,
                )
                if hasattr(res, "json"):
                    res = res.json()
            else:
                res = self.http_client
            if isinstance(res, dict):
                return res

        if self.endpoint_url:
            req_url = f"{self.endpoint_url}?timeframe={timeframe}"
            req = urllib.request.Request(req_url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = resp.read().decode("utf-8")
                return json.loads(data)

        # Default simulated dataset if no external API endpoint is configured
        return self._generate_default_dataset(timeframe)

    def _generate_default_dataset(self, timeframe: str) -> dict[str, Any]:
        """Generate standard default metrics for Support Ticketing when no external endpoint is connected."""
        timeframe_factors = {
            "7d": {
                "open_tickets": 18.0,
                "baseline_open": 22.0,
                "res_time": 2.1,
                "baseline_res": 2.5,
                "sla": 98.2,
                "baseline_sla": 96.5,
                "breakdown": [
                    {"label": "P1 - Critical", "value": 2.0},
                    {"label": "P2 - High", "value": 5.0},
                    {"label": "P3 - Medium", "value": 7.0},
                    {"label": "P4 - Low", "value": 4.0},
                ],
            },
            "30d": {
                "open_tickets": 42.0,
                "baseline_open": 50.0,
                "res_time": 2.4,
                "baseline_res": 2.8,
                "sla": 97.5,
                "baseline_sla": 95.0,
                "breakdown": [
                    {"label": "P1 - Critical", "value": 4.0},
                    {"label": "P2 - High", "value": 12.0},
                    {"label": "P3 - Medium", "value": 18.0},
                    {"label": "P4 - Low", "value": 8.0},
                ],
            },
            "90d": {
                "open_tickets": 135.0,
                "baseline_open": 160.0,
                "res_time": 2.6,
                "baseline_res": 3.0,
                "sla": 96.8,
                "baseline_sla": 94.2,
                "breakdown": [
                    {"label": "P1 - Critical", "value": 15.0},
                    {"label": "P2 - High", "value": 38.0},
                    {"label": "P3 - Medium", "value": 52.0},
                    {"label": "P4 - Low", "value": 30.0},
                ],
            },
        }
        factors = timeframe_factors.get(timeframe, timeframe_factors["30d"])

        return {
            "metrics": {
                "open_tickets": {
                    "raw_value": factors["open_tickets"],
                    "baseline_value": factors["baseline_open"],
                },
                "resolution_time": {
                    "raw_value": factors["res_time"],
                    "baseline_value": factors["baseline_res"],
                },
                "sla_compliance": {
                    "raw_value": factors["sla"],
                    "baseline_value": factors["baseline_sla"],
                },
            },
            "breakdowns": factors["breakdown"],
        }

    def _parse_and_build_result(
        self, data: dict[str, Any], timeframe: str
    ) -> DomainMetricsResult:
        """Parse raw metrics payload into standardized DomainMetricsResult DTOs."""
        metrics_payload = data.get("metrics", {})
        breakdowns_payload = data.get("breakdowns", [])

        # Process Open Tickets Metric
        open_data = metrics_payload.get("open_tickets", {})
        open_raw = open_data.get("raw_value")
        open_baseline = open_data.get("baseline_value")
        open_metric = self._format_open_tickets_metric(open_raw, open_baseline)

        # Process Resolution Time Metric
        res_data = metrics_payload.get("resolution_time") or metrics_payload.get("avg_resolution_time") or {}
        res_raw = res_data.get("raw_value")
        res_baseline = res_data.get("baseline_value")
        res_metric = self._format_resolution_time_metric(res_raw, res_baseline)

        # Process SLA Compliance Metric
        sla_data = metrics_payload.get("sla_compliance") or metrics_payload.get("sla_compliance_rate") or {}
        sla_raw = sla_data.get("raw_value")
        sla_baseline = sla_data.get("baseline_value")
        sla_metric = self._format_sla_compliance_metric(sla_raw, sla_baseline)

        # Process Breakdown Items
        breakdown_items = [
            BreakdownItemDTO(
                label=str(item.get("label", "Unknown")),
                value=float(item.get("value", 0.0)),
            )
            for item in breakdowns_payload
            if isinstance(item, dict)
        ]

        return DomainMetricsResult(
            domain=DomainIdentifier.SUPPORT.value,
            status="COMPLETE",
            metrics=[open_metric, res_metric, sla_metric],
            breakdown_items=breakdown_items,
        )

    def _format_open_tickets_metric(
        self, raw_value: Any, baseline_value: Any
    ) -> MetricResult:
        """Format Open Tickets metric and compute percentage change."""
        if raw_value is None:
            return MetricResult(
                domain=DomainIdentifier.SUPPORT.value,
                metric_key="open_tickets",
                display_name="Open Tickets",
                raw_value=None,
                baseline_value=None,
                formatted_value="Unavailable",
                percentage_change=None,
                status=MetricStatus.UNAVAILABLE,
                unit=MetricUnit.COUNT.value,
            )

        val_float = float(raw_value)
        base_float = float(baseline_value) if baseline_value is not None else None

        if val_float == 0.0:
            status = MetricStatus.ZERO
            formatted_value = "0"
        else:
            status = MetricStatus.VALID
            formatted_value = f"{int(val_float):,}"

        pct_change = self._calc_pct_change(val_float, base_float)

        return MetricResult(
            domain=DomainIdentifier.SUPPORT.value,
            metric_key="open_tickets",
            display_name="Open Tickets",
            raw_value=val_float,
            baseline_value=base_float,
            formatted_value=formatted_value,
            percentage_change=pct_change,
            status=status,
            unit=MetricUnit.COUNT.value,
        )

    def _format_resolution_time_metric(
        self, raw_value: Any, baseline_value: Any
    ) -> MetricResult:
        """Format Resolution Time metric and compute percentage change."""
        if raw_value is None:
            return MetricResult(
                domain=DomainIdentifier.SUPPORT.value,
                metric_key="resolution_time",
                display_name="Resolution Time",
                raw_value=None,
                baseline_value=None,
                formatted_value="Unavailable",
                percentage_change=None,
                status=MetricStatus.UNAVAILABLE,
                unit=MetricUnit.RATIO.value,
            )

        val_float = float(raw_value)
        base_float = float(baseline_value) if baseline_value is not None else None

        if val_float == 0.0:
            status = MetricStatus.ZERO
            formatted_value = "0.0 hrs"
        else:
            status = MetricStatus.VALID
            formatted_value = f"{val_float:.1f} hrs"

        pct_change = self._calc_pct_change(val_float, base_float)

        return MetricResult(
            domain=DomainIdentifier.SUPPORT.value,
            metric_key="resolution_time",
            display_name="Resolution Time",
            raw_value=val_float,
            baseline_value=base_float,
            formatted_value=formatted_value,
            percentage_change=pct_change,
            status=status,
            unit=MetricUnit.RATIO.value,
        )

    def _format_sla_compliance_metric(
        self, raw_value: Any, baseline_value: Any
    ) -> MetricResult:
        """Format SLA Compliance metric and compute percentage change."""
        if raw_value is None:
            return MetricResult(
                domain=DomainIdentifier.SUPPORT.value,
                metric_key="sla_compliance",
                display_name="SLA Compliance",
                raw_value=None,
                baseline_value=None,
                formatted_value="Unavailable",
                percentage_change=None,
                status=MetricStatus.UNAVAILABLE,
                unit=MetricUnit.PERCENTAGE.value,
            )

        val_float = float(raw_value)
        base_float = float(baseline_value) if baseline_value is not None else None

        if val_float == 0.0:
            status = MetricStatus.ZERO
            formatted_value = "0.0%"
        else:
            status = MetricStatus.VALID
            formatted_value = f"{val_float:.1f}%"

        pct_change = self._calc_pct_change(val_float, base_float)

        return MetricResult(
            domain=DomainIdentifier.SUPPORT.value,
            metric_key="sla_compliance",
            display_name="SLA Compliance",
            raw_value=val_float,
            baseline_value=base_float,
            formatted_value=formatted_value,
            percentage_change=pct_change,
            status=status,
            unit=MetricUnit.PERCENTAGE.value,
        )

    def _calc_pct_change(
        self, current: float, baseline: float | None
    ) -> float | None:
        """Calculate percentage change between current and baseline values."""
        if baseline is None or baseline == 0.0:
            return None
        return round(((current - baseline) / baseline) * 100, 2)

    def _build_unavailable_result(self, error_detail: str) -> DomainMetricsResult:
        """Build standard UNAVAILABLE response payload when connector encounters fault or timeout."""
        sanitized_err = _sanitize_error_message(error_detail)
        return DomainMetricsResult(
            domain=DomainIdentifier.SUPPORT.value,
            status="UNAVAILABLE",
            error_detail=sanitized_err,
            metrics=[
                MetricResult(
                    domain=DomainIdentifier.SUPPORT.value,
                    metric_key="open_tickets",
                    display_name="Open Tickets",
                    raw_value=None,
                    baseline_value=None,
                    formatted_value="Unavailable",
                    percentage_change=None,
                    status=MetricStatus.UNAVAILABLE,
                    unit=MetricUnit.COUNT.value,
                    error_detail=sanitized_err,
                ),
                MetricResult(
                    domain=DomainIdentifier.SUPPORT.value,
                    metric_key="resolution_time",
                    display_name="Resolution Time",
                    raw_value=None,
                    baseline_value=None,
                    formatted_value="Unavailable",
                    percentage_change=None,
                    status=MetricStatus.UNAVAILABLE,
                    unit=MetricUnit.RATIO.value,
                    error_detail=sanitized_err,
                ),
                MetricResult(
                    domain=DomainIdentifier.SUPPORT.value,
                    metric_key="sla_compliance",
                    display_name="SLA Compliance",
                    raw_value=None,
                    baseline_value=None,
                    formatted_value="Unavailable",
                    percentage_change=None,
                    status=MetricStatus.UNAVAILABLE,
                    unit=MetricUnit.PERCENTAGE.value,
                    error_detail=sanitized_err,
                ),
            ],
            breakdown_items=[],
        )
