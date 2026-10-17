"""Unit tests for background report export tasks and CSV/PDF generation [FR-012, NFR-004]."""

from datetime import datetime, timezone
import os
import tempfile
from typing import Any
from unittest.mock import patch
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.analytics import AcquisitionChannel, Campaign, LeadEvent
from src.models.database import Base
from src.models.marketing import Territory
from src.workers.export_tasks import (
    generate_csv_report,
    generate_pdf_report,
    generate_report_export_task,
    run_export_report_generation,
)


@pytest.fixture
def test_db_session() -> Session:
    """Fixture providing an isolated in-memory SQLite database session."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    session = testing_session_local()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def sample_analytics_data(test_db_session: Session) -> dict[str, Any]:
    """Fixture populating sample channel, campaign, lead, and territory records."""
    channel = AcquisitionChannel(
        id=uuid.uuid4(),
        channel_name="Search Ads",
        spend_amount=5000.00,
        created_on=datetime.now(timezone.utc),
    )
    test_db_session.add(channel)

    campaign1 = Campaign(
        id=uuid.uuid4(),
        campaign_name="Summer Promo",
        budget=10000.00,
        start_date_val=datetime.now(timezone.utc),
        end_date_val=datetime.now(timezone.utc),
    )
    campaign2 = Campaign(
        id=uuid.uuid4(),
        campaign_name="Fall Launch",
        budget=12000.00,
        start_date_val=datetime.now(timezone.utc),
        end_date_val=datetime.now(timezone.utc),
    )
    test_db_session.add_all([campaign1, campaign2])

    territory = Territory(
        id=uuid.uuid4(),
        territory_code="US-EAST",
        territory_name="US Eastern Region",
        postal_code_patterns="100*,101*",
    )
    test_db_session.add(territory)

    lead_event = LeadEvent(
        id=uuid.uuid4(),
        lead_id=uuid.uuid4(),
        channel_id=channel.id,
        campaign_id=campaign1.id,
        territory_id=territory.id,
        stage_name="SQL",
        quality_score=85,
        deal_value=25000.00,
        revenue_amount=25000.00,
        postal_code="10001",
        event_timestamp=datetime.now(timezone.utc),
    )
    test_db_session.add(lead_event)
    test_db_session.commit()

    return {
        "channel": channel,
        "campaign1": campaign1,
        "campaign2": campaign2,
        "territory": territory,
        "lead_event": lead_event,
    }


def test_generate_csv_report_file_creation() -> None:
    """Test generate_csv_report creates a valid UTF-8 CSV file on disk."""
    headers = ["Channel Name", "Lead Count", "ROI"]
    rows = [["Search Ads", 42, "150.00%"], ["Email Marketing", 18, "210.50%"]]
    title = "Test Channel Report"

    with tempfile.TemporaryDirectory() as temp_dir:
        file_path = os.path.join(temp_dir, "test_report.csv")
        result_path = generate_csv_report(
            headers=headers, rows=rows, output_path=file_path, title=title
        )

        assert result_path == file_path
        assert os.path.exists(file_path)
        assert os.path.getsize(file_path) > 0

        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert "# Test Channel Report" in content
        assert "Channel Name,Lead Count,ROI" in content
        assert "Search Ads,42,150.00%" in content


def test_generate_pdf_report_file_creation() -> None:
    """Test generate_pdf_report creates a valid PDF document file on disk."""
    headers = ["Stage", "Count", "Conversion Rate"]
    rows = [["Lead", 100, "100.00%"], ["MQL", 60, "60.00%"], ["SQL", 30, "50.00%"]]
    title = "Test Conversion Funnel Report"
    metadata = {"Filter": "All Channels", "Period": "Last 30 Days"}

    with tempfile.TemporaryDirectory() as temp_dir:
        file_path = os.path.join(temp_dir, "test_report.pdf")
        result_path = generate_pdf_report(
            headers=headers,
            rows=rows,
            output_path=file_path,
            title=title,
            metadata=metadata,
        )

        assert result_path == file_path
        assert os.path.exists(file_path)
        assert os.path.getsize(file_path) > 0

        with open(file_path, "rb") as f:
            header_bytes = f.read(10)

        assert header_bytes.startswith(b"%PDF-1.4")


def test_run_export_report_generation_unsupported_format(
    test_db_session: Session,
) -> None:
    """Test requesting an unsupported export format returns a FAILED status summary."""
    summary = run_export_report_generation(
        db=test_db_session,
        report_type="acquisition",
        export_format="docx",
    )

    assert summary["status"] == "FAILED"
    assert "Unsupported export format" in summary["error_detail"]
    assert summary["file_path"] is None


def test_run_export_report_generation_channel_acquisition_csv(
    test_db_session: Session, sample_analytics_data: dict[str, Any]
) -> None:
    """Test generating channel acquisition report dataset as CSV."""
    with tempfile.TemporaryDirectory() as temp_dir:
        summary = run_export_report_generation(
            db=test_db_session,
            report_type="acquisition",
            export_format="csv",
            output_dir=temp_dir,
        )

        assert summary["status"] == "COMPLETED"
        assert summary["report_type"] == "acquisition"
        assert summary["export_format"] == "csv"
        assert summary["row_count"] == 1
        assert summary["file_path"] is not None
        assert os.path.exists(summary["file_path"])
        assert summary["file_size_bytes"] > 0


def test_run_export_report_generation_channel_acquisition_pdf(
    test_db_session: Session, sample_analytics_data: dict[str, Any]
) -> None:
    """Test generating channel acquisition report dataset as PDF."""
    with tempfile.TemporaryDirectory() as temp_dir:
        summary = run_export_report_generation(
            db=test_db_session,
            report_type="acquisition",
            export_format="pdf",
            output_dir=temp_dir,
        )

        assert summary["status"] == "COMPLETED"
        assert summary["report_type"] == "acquisition"
        assert summary["export_format"] == "pdf"
        assert summary["row_count"] == 1
        assert summary["file_path"] is not None
        assert os.path.exists(summary["file_path"])


def test_run_export_report_generation_conversion_funnel(
    test_db_session: Session, sample_analytics_data: dict[str, Any]
) -> None:
    """Test generating conversion funnel report dataset as CSV and PDF."""
    with tempfile.TemporaryDirectory() as temp_dir:
        summary_csv = run_export_report_generation(
            db=test_db_session,
            report_type="funnel",
            export_format="csv",
            output_dir=temp_dir,
        )
        assert summary_csv["status"] == "COMPLETED"
        assert summary_csv["row_count"] > 0

        summary_pdf = run_export_report_generation(
            db=test_db_session,
            report_type="funnel",
            export_format="pdf",
            output_dir=temp_dir,
        )
        assert summary_pdf["status"] == "COMPLETED"
        assert summary_pdf["row_count"] > 0


def test_run_export_report_generation_campaign_comparison(
    test_db_session: Session, sample_analytics_data: dict[str, Any]
) -> None:
    """Test generating campaign comparison report dataset as CSV."""
    camp1_id = sample_analytics_data["campaign1"].id
    camp2_id = sample_analytics_data["campaign2"].id

    with tempfile.TemporaryDirectory() as temp_dir:
        summary = run_export_report_generation(
            db=test_db_session,
            report_type="campaigns",
            export_format="csv",
            filters={"campaign_ids": [camp1_id, camp2_id]},
            output_dir=temp_dir,
        )

        assert summary["status"] == "COMPLETED"
        assert summary["row_count"] == 2


def test_run_export_report_generation_territory_heatmap(
    test_db_session: Session, sample_analytics_data: dict[str, Any]
) -> None:
    """Test generating territory heat map report dataset as PDF."""
    with tempfile.TemporaryDirectory() as temp_dir:
        summary = run_export_report_generation(
            db=test_db_session,
            report_type="territory",
            export_format="pdf",
            output_dir=temp_dir,
        )

        assert summary["status"] == "COMPLETED"
        assert summary["row_count"] == 1


def test_run_export_report_generation_custom_dataset(
    test_db_session: Session,
) -> None:
    """Test generating report export from custom headers and rows in filters."""
    custom_headers = ["Metric", "Value"]
    custom_rows = [["Conversion Rate", "15.4%"], ["Avg Order Value", "$120.00"]]

    with tempfile.TemporaryDirectory() as temp_dir:
        summary = run_export_report_generation(
            db=test_db_session,
            report_type="custom_kpis",
            export_format="csv",
            filters={"headers": custom_headers, "rows": custom_rows, "title": "Custom KPI Export"},
            output_dir=temp_dir,
        )

        assert summary["status"] == "COMPLETED"
        assert summary["row_count"] == 2


def test_generate_report_export_task_celery_execution(
    test_db_session: Session, sample_analytics_data: dict[str, Any]
) -> None:
    """Test Celery task wrapper generate_report_export_task."""
    with tempfile.TemporaryDirectory() as temp_dir, patch(
        "src.workers.export_tasks.SessionLocal", return_value=test_db_session
    ):
        result = generate_report_export_task(
            report_type="acquisition",
            export_format="csv",
            output_dir=temp_dir,
        )

        assert result["status"] == "COMPLETED"
        assert result["report_type"] == "acquisition"
        assert result["export_format"] == "csv"
