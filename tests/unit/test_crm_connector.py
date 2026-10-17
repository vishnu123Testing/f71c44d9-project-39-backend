"""Unit tests for ExternalCRMConnector adapter and CRMDataPort."""

from datetime import datetime, timezone
import pytest

from src.connectors.crm_connector import (
    CRMConnector,
    CRMConnectorError,
    ExternalCRMConnector,
    _sanitize_error_message,
)
from src.services.forecasting.crm_port import (
    CRMActivityDTO,
    CRMDataPort,
    CRMDataResult,
    CRMStakeholderDTO,
    DealStageHistoryDTO,
)


def test_crm_connector_inheritance_and_initialization() -> None:
    """Test that ExternalCRMConnector implements CRMDataPort and initializes settings."""
    connector = ExternalCRMConnector(
        endpoint_url="https://crm.example.com/api",
        api_key="secret-key-123",
        timeout=10.0,
        retries=2,
    )
    assert isinstance(connector, CRMDataPort)
    assert connector.endpoint_url == "https://crm.example.com/api"
    assert connector.api_key == "secret-key-123"
    assert connector.timeout == 10.0
    assert connector.retries == 2


def test_crm_connector_alias() -> None:
    """Test that CRMConnector alias points to ExternalCRMConnector."""
    assert CRMConnector is ExternalCRMConnector


def test_get_deal_stage_history_default_mock() -> None:
    """Test retrieving default mock stage history for a valid deal ID."""
    connector = ExternalCRMConnector()
    history = connector.get_deal_stage_history("deal_101")

    assert len(history) == 3
    assert all(isinstance(h, DealStageHistoryDTO) for h in history)

    q_stage = history[0]
    assert q_stage.deal_id == "deal_101"
    assert q_stage.milestone_stage == "Qualification"
    assert q_stage.duration_days == 10.0
    assert q_stage.entered_at < q_stage.exited_at

    neg_stage = history[2]
    assert neg_stage.milestone_stage == "Negotiation"
    assert neg_stage.exited_at is None


def test_get_deal_activities_default_mock() -> None:
    """Test retrieving default mock activities for a deal."""
    connector = ExternalCRMConnector()
    activities = connector.get_deal_activities("deal_101")

    assert len(activities) == 3
    assert all(isinstance(a, CRMActivityDTO) for a in activities)
    assert activities[0].deal_id == "deal_101"
    assert activities[0].activity_type == "Meeting"
    assert "discovery call" in activities[0].summary.lower()


def test_get_stakeholders_default_mock() -> None:
    """Test retrieving default mapped stakeholders for a deal."""
    connector = ExternalCRMConnector()
    stakeholders = connector.get_stakeholders("deal_101")

    assert len(stakeholders) == 2
    assert all(isinstance(s, CRMStakeholderDTO) for s in stakeholders)

    sarah = next(s for s in stakeholders if s.full_name == "Sarah Jenkins")
    assert sarah.email == "s.jenkins@acmecorp.com"
    assert sarah.org_role == "VP of Engineering"
    assert sarah.influence_level == "High"
    assert sarah.is_active is True


def test_fetch_crm_deal_data_success() -> None:
    """Test fetching consolidated CRM deal data."""
    connector = ExternalCRMConnector()
    result = connector.fetch_crm_deal_data("deal_101")

    assert isinstance(result, CRMDataResult)
    assert result.deal_id == "deal_101"
    assert len(result.stage_history) == 3
    assert len(result.activities) == 3
    assert len(result.stakeholders) == 2
    assert result.is_sparse is False
    assert result.error_detail is None
    assert result.fetched_at is not None


def test_boundary_validation_invalid_deal_id() -> None:
    """Test that boundary validation rejects empty or invalid deal IDs."""
    connector = ExternalCRMConnector()

    with pytest.raises(ValueError, match="deal_id must be a non-empty string"):
        connector.get_deal_stage_history("")

    with pytest.raises(ValueError, match="deal_id must be a non-empty string"):
        connector.get_deal_activities("   ")


def test_simulation_failure_flag() -> None:
    """Test fault isolation when simulate_failure flag is set."""
    connector = ExternalCRMConnector(simulate_failure=True)

    with pytest.raises(CRMConnectorError, match="Simulated upstream CRM failure"):
        connector.get_deal_stage_history("deal_101")

    result = connector.fetch_crm_deal_data("deal_101")
    assert result.is_sparse is True
    assert result.error_detail is not None
    assert "Simulated upstream CRM failure" in result.error_detail
    assert len(result.stage_history) == 0


def test_simulation_timeout_flag() -> None:
    """Test timeout handling when simulate_timeout flag is set."""
    connector = ExternalCRMConnector(simulate_timeout=True, timeout=5.0)

    with pytest.raises(CRMConnectorError, match="timed out after 5.0s"):
        connector.get_deal_activities("deal_101")

    result = connector.fetch_crm_deal_data("deal_101")
    assert result.is_sparse is True
    assert "timed out" in result.error_detail.lower()


def test_simulation_sparse_data_flag() -> None:
    """Test sparse CRM history handling for AI forecasting fallback."""
    connector = ExternalCRMConnector(simulate_sparse_data=True)

    history = connector.get_deal_stage_history("deal_101")
    assert len(history) == 0

    result = connector.fetch_crm_deal_data("deal_101")
    assert result.is_sparse is True
    assert len(result.stage_history) == 0


def test_custom_initial_data_injection() -> None:
    """Test dependency injection of custom initial CRM data records."""
    now = datetime.now(timezone.utc)
    custom_data = {
        "deal_custom": {
            "stage_history": [
                {
                    "milestone_stage": "Discovery",
                    "entered_at": now,
                    "exited_at": None,
                    "duration_days": 1.0,
                }
            ],
            "activities": [
                {
                    "activity_id": "act_custom_1",
                    "activity_type": "Call",
                    "summary": "Custom Call Summary",
                    "occurred_on": now,
                }
            ],
            "stakeholders": [
                {
                    "stakeholder_id": "sh_custom_1",
                    "full_name": "Alice Smith",
                    "email": "alice@example.com",
                    "org_role": "CTO",
                    "influence_level": "High",
                    "is_active": False,
                }
            ],
        }
    }

    connector = ExternalCRMConnector(initial_data=custom_data)
    result = connector.fetch_crm_deal_data("deal_custom")

    assert len(result.stage_history) == 1
    assert result.stage_history[0].milestone_stage == "Discovery"
    assert result.activities[0].summary == "Custom Call Summary"
    assert result.stakeholders[0].full_name == "Alice Smith"
    assert result.stakeholders[0].is_active is False
    # Only 1 stage transition < FORECAST_MIN_HISTORICAL_TRANSITIONS (3), so is_sparse should be True
    assert result.is_sparse is True


def test_custom_http_client_callable() -> None:
    """Test dependency injection using a custom http_client callable."""
    now = datetime.now(timezone.utc)

    def mock_client(data_type: str, deal_id: str) -> list:
        if data_type == "stage_history":
            return [
                DealStageHistoryDTO(
                    deal_id=deal_id,
                    milestone_stage="Closed Won",
                    entered_at=now,
                    exited_at=now,
                    duration_days=5.0,
                )
            ]
        return []

    connector = ExternalCRMConnector(http_client=mock_client)
    history = connector.get_deal_stage_history("deal_999")
    assert len(history) == 1
    assert history[0].milestone_stage == "Closed Won"


def test_sanitize_error_message() -> None:
    """Test error message sanitization removing query string secrets and truncating."""
    raw_msg = "Failed to connect to https://crm.com/api?token=secret123456\nUnreachable"
    sanitized = _sanitize_error_message(raw_msg)
    assert "secret123456" not in sanitized
    assert "https://crm.com/api" in sanitized
    assert "\n" not in sanitized

    long_msg = "Error " * 50
    assert len(_sanitize_error_message(long_msg)) <= 200
