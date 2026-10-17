"""Unit tests for AIForecastConnector adapter and ForecastProviderPort."""

from datetime import datetime, timedelta, timezone
import pytest

from src.connectors.crm_connector import ExternalCRMConnector
from src.connectors.forecast_connector import (
    AIForecastConnector,
    ExternalForecastConnector,
    ForecastConnector,
    ForecastConnectorError,
    ForecastProviderConnector,
    ForecastProviderError,
    _sanitize_error_message,
)
from src.services.forecasting.crm_port import (
    CRMActivityDTO,
    CRMDataResult,
    CRMStakeholderDTO,
    DealStageHistoryDTO,
)
from src.services.forecasting.forecast_port import (
    ForecastPredictionResult,
    ForecastProviderPort,
)


def test_forecast_connector_inheritance_and_initialization() -> None:
    """Test that AIForecastConnector implements ForecastProviderPort and initializes properly."""
    connector = AIForecastConnector(
        endpoint_url="https://ai.example.com/forecast",
        api_key="secret-ai-key",
        timeout=8.0,
        retries=2,
        min_historical_transitions=4,
        default_confidence_pct=80.0,
    )

    assert isinstance(connector, ForecastProviderPort)
    assert connector.endpoint_url == "https://ai.example.com/forecast"
    assert connector.api_key == "secret-ai-key"
    assert connector.timeout == 8.0
    assert connector.retries == 2
    assert connector.min_historical_transitions == 4
    assert connector.default_confidence_pct == 80.0


def test_forecast_connector_aliases() -> None:
    """Test that all forecast connector class aliases point to AIForecastConnector."""
    assert ForecastConnector is AIForecastConnector
    assert ForecastProviderConnector is AIForecastConnector
    assert ExternalForecastConnector is AIForecastConnector


def test_predict_forecast_invalid_deal_id() -> None:
    """Test that empty or whitespace deal_id raises ValueError."""
    connector = AIForecastConnector()
    with pytest.raises(ValueError, match="deal_id must be a non-empty string"):
        connector.predict_forecast("")

    with pytest.raises(ValueError, match="deal_id must be a non-empty string"):
        connector.predict_forecast("   ")


def test_predict_forecast_default_successful_prediction() -> None:
    """Test default successful forecast prediction with mock CRM data."""
    connector = AIForecastConnector()
    result = connector.predict_forecast("deal_201")

    assert isinstance(result, ForecastPredictionResult)
    assert result.deal_id == "deal_201"
    assert result.status_code == "READY"
    assert result.is_sparse is False
    assert result.error_detail is None
    assert isinstance(result.estimated_completion_date, datetime)
    assert result.estimated_completion_date > datetime.now(timezone.utc)
    assert result.confidence_score_pct is not None
    assert 0.0 <= result.confidence_score_pct <= 100.0


def test_predict_forecast_compute_forecast_alias() -> None:
    """Test compute_forecast alias method."""
    connector = AIForecastConnector()
    res1 = connector.predict_forecast("deal_202")
    res2 = connector.compute_forecast("deal_202")

    assert res1.deal_id == res2.deal_id
    assert res1.status_code == res2.status_code
    assert res1.confidence_score_pct == res2.confidence_score_pct


def test_predict_forecast_explicit_crm_data_injection() -> None:
    """Test explicit injection of CRMDataResult (NFR-005)."""
    connector = AIForecastConnector()
    now = datetime.now(timezone.utc)

    custom_crm_data = CRMDataResult(
        deal_id="deal_custom",
        stage_history=[
            DealStageHistoryDTO("deal_custom", "Qualification", now - timedelta(days=40), now - timedelta(days=30), 10.0),
            DealStageHistoryDTO("deal_custom", "Proposal", now - timedelta(days=30), now - timedelta(days=15), 15.0),
            DealStageHistoryDTO("deal_custom", "Negotiation", now - timedelta(days=15), None, 15.0),
        ],
        activities=[
            CRMActivityDTO("act_1", "deal_custom", "sh_1", "Meeting", "Demo meeting", now - timedelta(days=20)),
        ],
        stakeholders=[
            CRMStakeholderDTO("sh_1", "deal_custom", "Alice Smith", "alice@example.com", "CTO", "High", True),
        ],
        fetched_at=now,
        is_sparse=False,
    )

    result = connector.predict_forecast("deal_custom", crm_data=custom_crm_data)

    assert result.status_code == "READY"
    assert result.deal_id == "deal_custom"
    assert result.confidence_score_pct is not None
    assert result.confidence_score_pct > 75.0


def test_predict_forecast_explicit_crm_port_injection() -> None:
    """Test explicit injection of CRMDataPort instance (NFR-005)."""
    crm_port = ExternalCRMConnector()
    connector = AIForecastConnector(crm_port=crm_port)

    result = connector.predict_forecast("deal_301")

    assert result.status_code == "READY"
    assert result.deal_id == "deal_301"


def test_predict_forecast_sparse_crm_data_fallback() -> None:
    """Test sparse CRM history returning INSUFFICIENT_DATA status (FR-008, FR-009)."""
    connector = AIForecastConnector(min_historical_transitions=3)
    now = datetime.now(timezone.utc)

    sparse_crm_data = CRMDataResult(
        deal_id="deal_sparse",
        stage_history=[
            DealStageHistoryDTO("deal_sparse", "Prospecting", now - timedelta(days=2), None, 2.0),
        ],
        activities=[],
        stakeholders=[],
        fetched_at=now,
        is_sparse=True,
    )

    result = connector.predict_forecast("deal_sparse", crm_data=sparse_crm_data)

    assert result.deal_id == "deal_sparse"
    assert result.status_code == "INSUFFICIENT_DATA"
    assert result.estimated_completion_date is None
    assert result.confidence_score_pct is None
    assert result.is_sparse is True


def test_predict_forecast_simulated_sparse_data_flag() -> None:
    """Test simulate_sparse_data flag triggering INSUFFICIENT_DATA status."""
    connector = AIForecastConnector(simulate_sparse_data=True)
    result = connector.predict_forecast("deal_sparse_sim")

    assert result.status_code == "INSUFFICIENT_DATA"
    assert result.estimated_completion_date is None
    assert result.confidence_score_pct is None
    assert result.is_sparse is True


def test_predict_forecast_simulated_failure() -> None:
    """Test simulate_failure flag returning FAILED status with error details."""
    connector = AIForecastConnector(simulate_failure=True)
    result = connector.predict_forecast("deal_fail")

    assert result.status_code == "FAILED"
    assert result.estimated_completion_date is None
    assert result.confidence_score_pct is None
    assert result.is_sparse is True
    assert result.error_detail is not None
    assert "Simulated upstream AI forecast provider failure" in result.error_detail


def test_predict_forecast_simulated_timeout() -> None:
    """Test simulate_timeout flag returning FAILED status with timeout detail."""
    connector = AIForecastConnector(simulate_timeout=True, timeout=3.0)
    result = connector.predict_forecast("deal_timeout")

    assert result.status_code == "FAILED"
    assert result.error_detail is not None
    assert "Request timed out after 3.0s" in result.error_detail


def test_predict_forecast_initial_forecasts_injection() -> None:
    """Test retrieving pre-seeded initial_forecasts."""
    now = datetime.now(timezone.utc)
    seeded_est = now + timedelta(days=14)

    seeded_forecasts = {
        "deal_seeded": {
            "estimated_completion_date": seeded_est,
            "confidence_score_pct": 92.5,
            "status_code": "READY",
        }
    }

    connector = AIForecastConnector(initial_forecasts=seeded_forecasts)
    result = connector.predict_forecast("deal_seeded")

    assert result.status_code == "READY"
    assert result.confidence_score_pct == 92.5
    assert result.estimated_completion_date == seeded_est


def test_sanitize_error_message() -> None:
    """Test _sanitize_error_message helper function."""
    assert _sanitize_error_message("") == "Unknown error occurred"
    assert _sanitize_error_message("Simple error message") == "Simple error message"
    assert (
        _sanitize_error_message("https://api.example.com/query?token=secret123&user=john")
        == "https://api.example.com/query"
    )
    long_msg = "A" * 250
    sanitized = _sanitize_error_message(long_msg)
    assert len(sanitized) == 200
    assert sanitized.endswith("...")
