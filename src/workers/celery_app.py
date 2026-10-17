"""Celery worker application initialization for background job execution."""

from celery import Celery

from src.lib.config import settings

celery_app = Celery(
    "fde_workers",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "src.workers.renewal_worker",
        "src.workers.escalation_worker",
        "src.workers.forecast_worker",
        "src.workers.export_tasks",
        "src.workers.sla_worker",
        "src.workers.notification_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
)
