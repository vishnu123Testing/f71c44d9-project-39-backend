"""Abstract CRM data port definition for retrieving deal history and activities.

This module defines the abstract port interface (CRMDataPort) and associated DTOs
that decouple external CRM system integration from core domain services (NFR-005).
Supports retrieving deal stage history (FR-008), stakeholder interaction logs, and
contact mapping details.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class DealStageHistoryDTO:
    """DTO representing a historical deal milestone stage transition."""

    deal_id: str
    milestone_stage: str
    entered_at: datetime
    exited_at: datetime | None = None
    duration_days: float | None = None


@dataclass
class CRMActivityDTO:
    """DTO representing an activity or interaction log entry in the CRM."""

    activity_id: str
    deal_id: str
    stakeholder_id: str | None
    activity_type: str  # Email, Meeting, Call, Note
    summary: str
    occurred_on: datetime


@dataclass
class CRMStakeholderDTO:
    """DTO representing a contact mapped from the external CRM."""

    stakeholder_id: str
    deal_id: str
    full_name: str
    email: str
    org_role: str
    influence_level: str  # High, Medium, Low
    is_active: bool = True


@dataclass
class CRMDataResult:
    """DTO encapsulating complete CRM deal state including history and activities."""

    deal_id: str
    stage_history: list[DealStageHistoryDTO] = field(default_factory=list)
    activities: list[CRMActivityDTO] = field(default_factory=list)
    stakeholders: list[CRMStakeholderDTO] = field(default_factory=list)
    fetched_at: datetime | None = None
    is_sparse: bool = False
    error_detail: str | None = None


class CRMDataPort(ABC):
    """Abstract port interface for retrieving deal history, activities, and contact data."""

    @abstractmethod
    def get_deal_stage_history(self, deal_id: str) -> list[DealStageHistoryDTO]:
        """Retrieve chronological milestone stage transition history for a deal.

        Args:
            deal_id: Unique string identifier of the deal.

        Returns:
            List of DealStageHistoryDTO items.
        """
        ...

    @abstractmethod
    def get_deal_activities(self, deal_id: str) -> list[CRMActivityDTO]:
        """Retrieve chronological interaction activity logs associated with a deal.

        Args:
            deal_id: Unique string identifier of the deal.

        Returns:
            List of CRMActivityDTO items sorted chronologically.
        """
        ...

    @abstractmethod
    def get_stakeholders(self, deal_id: str) -> list[CRMStakeholderDTO]:
        """Retrieve mapped contacts and stakeholders for a deal.

        Args:
            deal_id: Unique string identifier of the deal.

        Returns:
            List of CRMStakeholderDTO items.
        """
        ...

    @abstractmethod
    def fetch_crm_deal_data(self, deal_id: str) -> CRMDataResult:
        """Fetch consolidated deal history, activities, and stakeholder mappings.

        Args:
            deal_id: Unique string identifier of the deal.

        Returns:
            CRMDataResult containing consolidated CRM data.
        """
        ...
