"""Background processing worker for async dataset exports (CSV/PDF) [FR-012, NFR-004].

This module provides Celery background tasks and core report generation functions
for exporting analytics, funnel, campaign comparison, and territory heat map report
datasets into standard CSV and PDF document formats.
"""

import csv
from datetime import datetime, timezone
import logging
import os
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from src.lib.config import settings
from src.lib.logging import get_correlation_id
from src.models.database import SessionLocal
from src.services.analytics.funnel import analyze_conversion_funnel
from src.services.analytics.reporting import (
    get_campaign_trend_comparison,
    get_channel_acquisition_report,
)
from src.services.marketing.territory import get_territory_heatmap
from src.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


class ExportWorkerError(Exception):
    """Base exception class for export worker operations."""

    pass


class UnsupportedExportFormatError(ExportWorkerError):
    """Raised when an unsupported export format is requested."""

    pass


class InvalidExportRequestError(ExportWorkerError):
    """Raised when export request criteria or parameters are invalid."""

    pass


def _ensure_utc(dt: datetime | str | None) -> datetime | None:
    """Ensure a datetime instance is timezone-aware in UTC."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def generate_csv_report(
    headers: list[str],
    rows: list[list[Any]],
    output_path: str,
    title: str | None = None,
) -> str:
    """Generate a standard CSV report file [FR-012].

    Writes UTF-8 encoded CSV file with optional title header row, column headers,
    and data rows.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        if title:
            writer.writerow([f"# {title}"])
        writer.writerow(headers)
        for row in rows:
            writer.writerow([str(cell) if cell is not None else "" for cell in row])
    return output_path


def generate_pdf_report(
    headers: list[str],
    rows: list[list[Any]],
    output_path: str,
    title: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Generate a standard PDF report file using pure Python PDF stream syntax [FR-012].

    Creates a structured, valid PDF 1.4 document containing title, metadata headers,
    and formatted tabular data without requiring external dependencies.
    """
    metadata = metadata or {}

    def clean_pdf_str(text: Any) -> str:
        s = str(text if text is not None else "")
        s = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        return "".join(c if 32 <= ord(c) <= 126 else " " for c in s)

    clean_title = clean_pdf_str(title)

    num_cols = max(len(headers), 1)
    col_width = 512.0 / num_cols

    pages_streams: list[str] = []

    current_stream: list[str] = []
    current_stream.append("BT")
    current_stream.append("/F2 18 Tf")
    current_stream.append(f"50 740 Td ({clean_title}) Tj")

    y_pos = 715
    if metadata:
        current_stream.append("/F1 9 Tf")
        meta_str = " | ".join(
            f"{clean_pdf_str(k)}: {clean_pdf_str(v)}"
            for k, v in metadata.items()
            if v is not None
        )
        if meta_str:
            current_stream.append(f"0 -15 Td ({meta_str}) Tj")
            y_pos -= 15

    y_pos -= 25
    current_stream.append("/F2 10 Tf")
    current_stream.append(f"50 {y_pos} Td")

    for idx, h in enumerate(headers):
        h_clean = clean_pdf_str(h)
        if idx == 0:
            current_stream.append(f"({h_clean}) Tj")
        else:
            current_stream.append(f"{col_width:.1f} 0 Td ({h_clean}) Tj")

    y_pos -= 18
    current_stream.append("/F1 9 Tf")

    first_row_on_page = True
    row_y_tracker = y_pos

    row_index = 0
    if not rows:
        current_stream.append("50 630 Td (No records available for export) Tj")
    else:
        while row_index < len(rows):
            if row_y_tracker < 60:
                current_stream.append("ET")
                pages_streams.append("\n".join(current_stream))

                current_stream = []
                current_stream.append("BT")
                current_stream.append("/F2 10 Tf")
                current_stream.append("50 740 Td (Continued...) Tj")
                current_stream.append("/F1 9 Tf")
                row_y_tracker = 710
                first_row_on_page = True

            row = rows[row_index]
            if first_row_on_page:
                current_stream.append(f"50 {row_y_tracker} Td")
                first_row_on_page = False
            else:
                current_stream.append("0 -15 Td")

            for c_idx, cell in enumerate(row):
                c_clean = clean_pdf_str(cell)
                if c_idx == 0:
                    current_stream.append(f"({c_clean}) Tj")
                else:
                    current_stream.append(f"{col_width:.1f} 0 Td ({c_clean}) Tj")

            if len(row) > 1:
                back_x = -1.0 * col_width * (len(row) - 1)
                current_stream.append(f"{back_x:.1f} 0 Td")

            row_y_tracker -= 15
            row_index += 1

    current_stream.append("ET")
    pages_streams.append("\n".join(current_stream))

    num_pages = len(pages_streams)

    font1_obj_id = 3 + 2 * num_pages
    font2_obj_id = 4 + 2 * num_pages

    pdf_bytes = bytearray()
    offsets: list[int] = []

    def write_obj(obj_id: int, content: bytes) -> None:
        offsets.append(len(pdf_bytes))
        pdf_bytes.extend(f"{obj_id} 0 obj\n".encode("ascii"))
        pdf_bytes.extend(content)
        pdf_bytes.extend(b"\nendobj\n")

    pdf_bytes.extend(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

    page_obj_ids = [3 + i for i in range(num_pages)]
    content_obj_ids = [3 + num_pages + i for i in range(num_pages)]

    write_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")

    kids_str = " ".join(f"{p} 0 R" for p in page_obj_ids)
    write_obj(
        2, f"<< /Type /Pages /Kids [{kids_str}] /Count {num_pages} >>".encode("ascii")
    )

    for i in range(num_pages):
        page_id = page_obj_ids[i]
        content_id = content_obj_ids[i]
        page_dict = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font1_obj_id} 0 R /F2 {font2_obj_id} 0 R >> >> >>"
        )
        write_obj(page_id, page_dict.encode("ascii"))

    for i in range(num_pages):
        content_id = content_obj_ids[i]
        stream_data = pages_streams[i].encode("latin1", errors="replace")
        stream_obj = (
            f"<< /Length {len(stream_data)} >>\nstream\n".encode("ascii")
            + stream_data
            + b"\nendstream"
        )
        write_obj(content_id, stream_obj)

    write_obj(
        font1_obj_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    )
    write_obj(
        font2_obj_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"
    )

    start_xref = len(pdf_bytes)
    total_objs = font2_obj_id + 1

    xref_lines = [f"xref\n0 {total_objs}\n0000000000 65535 f \n"]
    for offset in offsets:
        xref_lines.append(f"{offset:010d} 00000 n \n")

    pdf_bytes.extend("".join(xref_lines).encode("ascii"))

    trailer = (
        f"trailer\n<< /Size {total_objs} /Root 1 0 R >>\n"
        f"startxref\n{start_xref}\n%%EOF\n"
    )
    pdf_bytes.extend(trailer.encode("ascii"))

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(pdf_bytes)

    return output_path


def run_export_report_generation(
    db: Session,
    report_type: str,
    export_format: str,
    start_date_val: datetime | str | None = None,
    end_date_val: datetime | str | None = None,
    filters: dict[str, Any] | None = None,
    output_dir: str | None = None,
    user_id: str | None = None,
    task_id: str | None = None,
    custom_filename: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Execute synchronous background report dataset query and file generation [FR-012, NFR-004].

    Args:
        db: Synchronous SQLAlchemy database session.
        report_type: Report domain type ('acquisition', 'funnel', 'campaigns', 'territory').
        export_format: Output file format ('csv' or 'pdf').
        start_date_val: Optional filter start timestamp.
        end_date_val: Optional filter end timestamp.
        filters: Optional additional filter key-value dictionary.
        output_dir: Optional custom export output directory.
        user_id: Optional requesting user UUID.
        task_id: Optional tracking task ID.
        custom_filename: Optional custom output filename override.
        correlation_id: Optional correlation identifier.

    Returns:
        Summary dictionary containing export file details, row counts, and status.
    """
    corr_id = correlation_id or get_correlation_id()
    task_id_val = task_id or str(uuid4())
    filters_dict = filters or {}
    fmt = export_format.lower().strip() if export_format else ""

    allowed_formats = [f.lower().strip() for f in getattr(settings, "ALLOWED_EXPORT_FORMATS", ["csv", "pdf"])]
    if fmt not in allowed_formats:
        err_msg = f"Unsupported export format '{export_format}'. Allowed formats: {allowed_formats}"
        logger.warning(
            "Export task failed: %s [correlation_id=%s]",
            err_msg,
            corr_id,
            extra={
                "event_type": "export_task_failed",
                "report_type": report_type,
                "export_format": export_format,
                "correlation_id": corr_id,
                "error": err_msg,
            },
        )
        return {
            "task_id": str(task_id_val),
            "report_type": report_type,
            "export_format": export_format,
            "file_path": None,
            "file_name": None,
            "file_size_bytes": 0,
            "row_count": 0,
            "status": "FAILED",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "user_id": str(user_id) if user_id else None,
            "correlation_id": corr_id,
            "error_detail": err_msg,
        }

    start_dt = _ensure_utc(start_date_val)
    end_dt = _ensure_utc(end_date_val)

    clean_type = report_type.lower().strip() if report_type else "generic"

    headers: list[str] = []
    rows: list[list[Any]] = []
    report_title = f"{clean_type.replace('_', ' ').title()} Report"

    try:
        if clean_type in ("acquisition", "channel_acquisition", "acquisition_channels"):
            report_title = "Channel Acquisition & ROI Report"
            headers = [
                "Channel ID",
                "Channel Name",
                "Lead Count",
                "Quality Score",
                "Spend Amount ($)",
                "Revenue Amount ($)",
                "ROI",
            ]
            ch_id = filters_dict.get("channel_id")
            channel_dtos = get_channel_acquisition_report(
                db=db,
                start_date_val=start_dt,
                end_date_val=end_dt,
                channel_id=ch_id,
            )
            for item in channel_dtos:
                rows.append([
                    str(item.channel_id),
                    item.channel_name,
                    item.lead_count,
                    f"{item.quality_score:.2f}",
                    f"{item.spend_amount:.2f}",
                    f"{item.revenue_amount:.2f}",
                    item.roi,
                ])

        elif clean_type in ("funnel", "conversion_funnel"):
            report_title = "Conversion Funnel Report"
            headers = [
                "Stage Name",
                "Lead Count",
                "Conversion Rate (%)",
                "Dropoff Rate (%)",
                "Is Bottleneck",
                "Threshold (%)",
                "Overall Conversion Rate (%)",
            ]
            ch_id = filters_dict.get("channel_id")
            camp_id = filters_dict.get("campaign_id")
            funnel_dtos = analyze_conversion_funnel(
                db=db,
                start_date_val=start_dt,
                end_date_val=end_dt,
                channel_id=ch_id,
                campaign_id=camp_id,
            )
            for item in funnel_dtos:
                rows.append([
                    item.stage_name,
                    item.lead_count,
                    f"{item.conversion_rate:.2f}%",
                    f"{item.dropoff_rate:.2f}%",
                    "Yes" if item.is_bottleneck else "No",
                    f"{item.threshold_percent:.2f}%",
                    f"{item.overall_conversion_rate:.2f}%",
                ])

        elif clean_type in ("campaigns", "campaign_comparison"):
            report_title = "Campaign Comparison Report"
            headers = [
                "Campaign ID",
                "Campaign Name",
                "Total Leads",
                "Cost Per Lead ($)",
                "Engagement Rate (%)",
            ]
            camp_ids = filters_dict.get("campaign_ids")
            if camp_ids:
                cmp_dto = get_campaign_trend_comparison(
                    db=db,
                    campaign_ids=camp_ids,
                    start_date_val=start_dt,
                    end_date_val=end_dt,
                )
                for item in cmp_dto.campaigns:
                    rows.append([
                        str(item.campaign_id),
                        item.campaign_name,
                        item.total_leads,
                        f"{item.cost_per_lead:.2f}",
                        f"{item.engagement_rate:.2f}%",
                    ])

        elif clean_type in ("territory", "territory_heatmap"):
            report_title = "Geographic Territory Density Report"
            headers = [
                "Territory Code",
                "Territory Name",
                "Lead Count",
                "Total Deal Value ($)",
                "Density Score",
            ]
            lead_status = filters_dict.get("lead_status")
            min_val = filters_dict.get("min_deal_value")
            max_val = filters_dict.get("max_deal_value")
            terr_id = filters_dict.get("territory_id")
            auth_terrs = filters_dict.get("user_authorized_territories")
            heatmap = get_territory_heatmap(
                db=db,
                lead_status=lead_status,
                min_deal_value=min_val,
                max_deal_value=max_val,
                territory_id=terr_id,
                start_date_val=start_dt,
                end_date_val=end_dt,
                user_authorized_territories=auth_terrs,
            )
            for region in heatmap.regions:
                rows.append([
                    region.territory_code,
                    region.territory_name,
                    region.lead_count,
                    f"{region.total_deal_value:.2f}",
                    f"{region.density_score:.2f}",
                ])

        elif "headers" in filters_dict and "rows" in filters_dict:
            headers = filters_dict["headers"]
            rows = filters_dict["rows"]
            report_title = filters_dict.get("title", report_title)

        else:
            headers = ["Field", "Value"]
            rows = [
                ["Report Type", clean_type],
                ["Generated At", datetime.now(timezone.utc).isoformat()],
            ]

    except Exception as exc:
        logger.error(
            "Error querying dataset for report '%s': %s [correlation_id=%s]",
            clean_type,
            exc,
            corr_id,
            extra={
                "event_type": "export_dataset_query_failed",
                "report_type": report_type,
                "correlation_id": corr_id,
                "error": str(exc),
            },
        )
        return {
            "task_id": str(task_id_val),
            "report_type": report_type,
            "export_format": fmt,
            "file_path": None,
            "file_name": None,
            "file_size_bytes": 0,
            "row_count": 0,
            "status": "FAILED",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "user_id": str(user_id) if user_id else None,
            "correlation_id": corr_id,
            "error_detail": str(exc),
        }

    target_dir = (
        output_dir
        or getattr(settings, "ANALYTICS_EXPORT_DIR", None)
        or getattr(settings, "EXPORT_OUTPUT_DIR", None)
        or "./exports"
    )
    os.makedirs(os.path.abspath(target_dir), exist_ok=True)

    if custom_filename:
        file_name = custom_filename
    else:
        task_suffix = str(task_id_val)[:8]
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        file_name = f"{clean_type}_report_{ts_str}_{task_suffix}.{fmt}"

    file_path = os.path.join(target_dir, file_name)

    metadata = {
        "Generated At": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "Report Type": clean_type,
        "Record Count": len(rows),
    }

    try:
        if fmt == "csv":
            generate_csv_report(headers=headers, rows=rows, output_path=file_path, title=report_title)
        elif fmt == "pdf":
            generate_pdf_report(headers=headers, rows=rows, output_path=file_path, title=report_title, metadata=metadata)
    except Exception as exc:
        logger.error(
            "Error writing export file '%s': %s [correlation_id=%s]",
            file_path,
            exc,
            corr_id,
            extra={
                "event_type": "export_file_write_failed",
                "file_path": file_path,
                "correlation_id": corr_id,
                "error": str(exc),
            },
        )
        return {
            "task_id": str(task_id_val),
            "report_type": report_type,
            "export_format": fmt,
            "file_path": None,
            "file_name": None,
            "file_size_bytes": 0,
            "row_count": len(rows),
            "status": "FAILED",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "user_id": str(user_id) if user_id else None,
            "correlation_id": corr_id,
            "error_detail": str(exc),
        }

    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0

    summary = {
        "task_id": str(task_id_val),
        "report_type": report_type,
        "export_format": fmt,
        "file_path": file_path,
        "file_name": file_name,
        "file_size_bytes": file_size,
        "row_count": len(rows),
        "status": "COMPLETED",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "user_id": str(user_id) if user_id else None,
        "correlation_id": corr_id,
        "error_detail": None,
    }

    logger.info(
        "Report export successfully generated: %s (%d bytes, %d rows) [correlation_id=%s]",
        file_name,
        file_size,
        len(rows),
        corr_id,
        extra={
            "event_type": "export_report_generated",
            "report_type": report_type,
            "export_format": fmt,
            "file_name": file_name,
            "file_size_bytes": file_size,
            "row_count": len(rows),
            "correlation_id": corr_id,
        },
    )

    return summary


generate_report_export = run_export_report_generation


@celery_app.task(name="src.workers.export_tasks.generate_report_export_task", bind=True)
def generate_report_export_task(
    self: Any,
    report_type: str,
    export_format: str,
    start_date_val: str | None = None,
    end_date_val: str | None = None,
    filters: dict[str, Any] | None = None,
    output_dir: str | None = None,
    user_id: str | None = None,
    custom_filename: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Celery background worker task for asynchronous CSV/PDF report generation [FR-012, NFR-004].

    Args:
        self: Celery Task instance.
        report_type: Domain report type identifier.
        export_format: File format ('csv' or 'pdf').
        start_date_val: Optional ISO string start timestamp.
        end_date_val: Optional ISO string end timestamp.
        filters: Optional additional filtering parameters.
        output_dir: Optional custom export file output directory.
        user_id: Optional requesting user UUID.
        custom_filename: Optional custom output filename.
        correlation_id: Optional log correlation identifier.

    Returns:
        Summary dictionary containing export status, file path, size, and metadata.
    """
    task_id = getattr(self, "request", None) and getattr(self.request, "id", None) or str(uuid4())
    db = SessionLocal()
    try:
        return run_export_report_generation(
            db=db,
            report_type=report_type,
            export_format=export_format,
            start_date_val=start_date_val,
            end_date_val=end_date_val,
            filters=filters,
            output_dir=output_dir,
            user_id=user_id,
            task_id=task_id,
            custom_filename=custom_filename,
            correlation_id=correlation_id,
        )
    finally:
        db.close()


export_report_task = generate_report_export_task
