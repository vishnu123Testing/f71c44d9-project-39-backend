"""Lead Generation domain connector adapter.

This module implements the LeadGenConnector adapter for fetching lead generation
operational metrics (volume, conversion rates) and segment distribution breakdowns
from downstream Lead Gen domain sources with fault isolation, timeout handling, and
retry mechanisms.
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
    # Basic sanitization removing raw URLs with query params or credentials
    cleaned = str(msg).split("?")[0]
    cleaned = cleaned.replace("\n", " ").strip()
    if len(cleaned) > 200:
        cleaned = cleaned[:197] + "..."
    return cleaned


class LeadGenConnector(DomainConnectorPort):
    """Adapter fetching Lead Generation operational metrics with fault isolation."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        http_client: Any | None = None,
        simulate_failure: bool = False,
        simulate_timeout: bool = False,
    ) -> None:
        """Initialize LeadGenConnector adapter.

        Args:
            endpoint_url: Optional API endpoint URL for downstream Lead Gen service.
            timeout: Execution timeout in seconds. Defaults to CONNECTOR_TIMEOUT setting.
            retries: Retry attempts for transient failures. Defaults to CONNECTOR_RETRIES setting.
            http_client: Optional HTTP client instance or callable for dependency injection.
            simulate_failure: Flag to simulate an upstream fault for isolation testing.
            simulate_timeout: Flag to simulate a request timeout for isolation testing.
        """
        self.endpoint_url = endpoint_url or os.getenv("LEAD_GEN_API_URL")
        self.timeout = timeout if timeout is not None else settings.CONNECTOR_TIMEOUT
        self.retries = retries if retries is not None else settings.CONNECTOR_RETRIES
        self.http_client = http_client
        self.simulate_failure = simulate_failure
        self.simulate_timeout = simulate_timeout

    @property
    def domain_identifier(self) -> str:
        """Return the unique domain identifier for Lead Generation."""
        return DomainIdentifier.LEAD_GEN.value

    def fetch_metrics(self, timeframe: str = "30d") -> DomainMetricsResult:
        """Fetch Lead Generation operational metrics and breakdown items for a timeframe.

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
                "Lead Gen connector rejected invalid timeframe input",
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
                        f"Lead Gen connector timed out after {self.timeout}s"
                    )

                if self.simulate_failure:
                    raise RuntimeError("Simulated upstream Lead Gen service failure")

                raw_data = self._retrieve_data(timeframe_clean)
                return self._parse_and_build_result(raw_data, timeframe_clean)

            except (TimeoutError, urllib.error.URLError, OSError) as exc:
                last_error = f"Lead Gen connector execution timed out or network error (attempt {attempt}/{max_attempts}): {_sanitize_error_message(str(exc))}"
                logger.warning(
                    last_error,
                    extra={
                        "correlation_id": get_correlation_id(),
                        "domain": self.domain_identifier,
                        "attempt": attempt,
                    },
                )
            except Exception as exc:
                last_error = f"Lead Gen connector encountered unexpected error (attempt {attempt}/{max_attempts}): {_sanitize_error_message(str(exc))}"
                logger.error(
                    last_error,
                    extra={
                        "correlation_id": get_correlation_id(),
                        "domain": self.domain_identifier,
                        "attempt": attempt,
                    },
                )

        # Fault isolation fallback: return UNAVAILABLE result without raising exception
        return self._build_unavailable_result(last_error or "Lead Gen connector failed")

    def _retrieve_data(self, timeframe: str) -> dict[str, Any]:
        """Execute HTTP request or retrieve raw metric payload from downstream provider."""
        if self.http_client is not None:
            if callable(self.http_client):
                res = self.http_client(self.endpoint_url or "http://lead-gen.local/api", timeframe, self.timeout)
            elif hasattr(self.http_client, "get"):
                res = self.http_client.get(self.endpoint_url or "http://lead-gen.local/api", params={"timeframe": timeframe}, timeout=self.timeout)
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
        """Generate standard default metrics for lead generation when no external endpoint is connected."""
        timeframe_factors = {
            "7d": {"volume": 310.0, "baseline_vol": 280.0, "conv": 4.1, "baseline_conv": 3.7},
            "30d": {"volume": 1250.0, "baseline_vol": 1000.0, "conv": 4.2, "baseline_conv": 3.8},
            "90d": {"volume": 3800.0, "baseline_vol": 3200.0, "conv": 4.0, "baseline_conv": 3.6},
        }
        factors = timeframe_factors.get(timeframe, timeframe_factors["30d"])

        vol = factors["volume"]
        return {
            "metrics": {
                "lead_volume": {
                    "raw_value": vol,
                    "baseline_value": factors["baseline_vol"],
                },
                "conversion_rate": {
                    "raw_value": factors["conv"],
                    "baseline_value": factors["baseline_conv"],
                },
            },
            "breakdowns": [
                {"label": "Organic Search", "value": round(vol * 0.36, 1)},
                {"label": "Paid Search", "value": round(vol * 0.28, 1)},
                {"label": "Referrals", "value": round(vol * 0.20, 1)},
                {"label": "Direct", "value": round(vol * 0.16, 1)},
            ],
        }

    def _parse_and_build_result(
        self, data: dict[str, Any], timeframe: str
    ) -> DomainMetricsResult:
        """Parse raw metrics payload into standardized DomainMetricsResult DTOs."""
        metrics_payload = data.get("metrics", {})
        breakdowns_payload = data.get("breakdowns", [])

        # Process Lead Volume Metric
        vol_data = metrics_payload.get("lead_volume", {})
        vol_raw = vol_data.get("raw_value")
        vol_baseline = vol_data.get("baseline_value")
        vol_metric = self._format_volume_metric(vol_raw, vol_baseline)

        # Process Conversion Rate Metric
        conv_data = metrics_payload.get("conversion_rate", {})
        conv_raw = conv_data.get("raw_value")
        conv_baseline = conv_data.get("baseline_value")
        conv_metric = self._format_conversion_metric(conv_raw, conv_baseline)

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
            domain=DomainIdentifier.LEAD_GEN.value,
            status="COMPLETE",
            metrics=[vol_metric, conv_metric],
            breakdown_items=breakdown_items,
        )

    def _format_volume_metric(
        self, raw_value: Any, baseline_value: Any
    ) -> MetricResult:
        """Format Lead Volume metric and compute percentage change."""
        if raw_value is None:
            return MetricResult(
                domain=DomainIdentifier.LEAD_GEN.value,
                metric_key="lead_volume",
                display_name="Lead Volume",
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
            domain=DomainIdentifier.LEAD_GEN.value,
            metric_key="lead_volume",
            display_name="Lead Volume",
            raw_value=val_float,
            baseline_value=base_float,
            formatted_value=formatted_value,
            percentage_change=pct_change,
            status=status,
            unit=MetricUnit.COUNT.value,
        )

    def _format_conversion_metric(
        self, raw_value: Any, baseline_value: Any
    ) -> MetricResult:
        """Format Conversion Rate metric and compute percentage change."""
        if raw_value is None:
            return MetricResult(
                domain=DomainIdentifier.LEAD_GEN.value,
                metric_key="conversion_rate",
                display_name="Conversion Rate",
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
            domain=DomainIdentifier.LEAD_GEN.value,
            metric_key="conversion_rate",
            display_name="Conversion Rate",
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
            domain=DomainIdentifier.LEAD_GEN.value,
            status="UNAVAILABLE",
            error_detail=sanitized_err,
            metrics=[
                MetricResult(
                    domain=DomainIdentifier.LEAD_GEN.value,
                    metric_key="lead_volume",
                    display_name="Lead Volume",
                    raw_value=None,
                    baseline_value=None,
                    formatted_value="Unavailable",
                    percentage_change=None,
                    status=MetricStatus.UNAVAILABLE,
                    unit=MetricUnit.COUNT.value,
                    error_detail=sanitized_err,
                ),
                MetricResult(
                    domain=DomainIdentifier.LEAD_GEN.value,
                    metric_key="conversion_rate",
                    display_name="Conversion Rate",
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
