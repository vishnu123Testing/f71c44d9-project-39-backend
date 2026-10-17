"""Abstract domain connector port contracts and standardized metric DTOs.

This module defines the explicit interfaces and data transfer objects (DTOs)
that decouple downstream domain data fetchers (connectors) from business logic
and UI display components.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class MetricStatus(str, Enum):
    """Operational state of a domain metric."""

    VALID = "VALID"
    ZERO = "ZERO"
    UNAVAILABLE = "UNAVAILABLE"


class AggregationStatus(str, Enum):
    """Overall operational state of multi-domain metric aggregation."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class DomainIdentifier(str, Enum):
    """Supported core domain identifiers."""

    LEAD_GEN = "lead_gen"
    SALES_PIPELINE = "sales_pipeline"
    SUPPORT = "support"
    FINANCIALS = "financials"


class MetricUnit(str, Enum):
    """Measurement unit types for formatted metrics."""

    CURRENCY = "currency"
    COUNT = "count"
    PERCENTAGE = "percentage"
    RATIO = "ratio"


@dataclass
class MetricResult:
    """Standardized DTO representing a single captured metric."""

    domain: str
    metric_key: str
    display_name: str
    raw_value: float | None = None
    baseline_value: float | None = None
    formatted_value: str = "Unavailable"
    percentage_change: float | None = None
    status: MetricStatus = MetricStatus.UNAVAILABLE
    unit: str = MetricUnit.COUNT.value
    error_detail: str | None = None


@dataclass
class BreakdownItemDTO:
    """Standardized DTO representing a distribution segment item for domain drilldown."""

    label: str
    value: float


@dataclass
class DomainMetricsResult:
    """Standardized DTO representing all captured metrics and distribution items for a domain."""

    domain: str
    status: str
    metrics: list[MetricResult] = field(default_factory=list)
    breakdown_items: list[BreakdownItemDTO] = field(default_factory=list)
    error_detail: str | None = None


@dataclass
class SummaryAggregationResult:
    """Standardized DTO representing the consolidated multi-domain summary payload."""

    status: AggregationStatus | str
    generated_at: str
    metrics: list[MetricResult] = field(default_factory=list)
    domain_results: list[DomainMetricsResult] = field(default_factory=list)


class DomainConnectorPort(ABC):
    """Abstract port contract that all downstream domain metric connector adapters must implement."""

    @property
    @abstractmethod
    def domain_identifier(self) -> str:
        """Return the unique domain identifier (e.g., 'lead_gen', 'sales_pipeline')."""
        ...

    @abstractmethod
    def fetch_metrics(self, timeframe: str = "30d") -> DomainMetricsResult:
        """Fetch operational metrics and segment breakdowns for the given timeframe.

        Args:
            timeframe: Comparative period timeframe (e.g., '7d', '30d', '90d').

        Returns:
            DomainMetricsResult containing metric items and distribution breakdowns.
        """
        ...
