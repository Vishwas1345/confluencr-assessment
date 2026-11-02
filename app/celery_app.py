import logging
import tempfile
from pathlib import Path

from celery import Celery
from celery.signals import worker_ready

from app.config import get_settings
from app.logging_config import configure_logging
from app.task_names import RECONCILE_TRANSACTIONS_TASK

logger = logging.getLogger(__name__)

settings = get_settings()
configure_logging(settings.log_level)

celery_app = Celery(
    "confluencr",
    broker=settings.redis_url,
    include=["app.tasks"],
)

celery_app.conf.update(
    accept_content=["json"],
    beat_schedule={
        "reconcile-transactions": {
            "task": RECONCILE_TRANSACTIONS_TASK,
            "schedule": float(settings.reconcile_interval_seconds),
        },
    },
    beat_schedule_filename=str(Path(tempfile.gettempdir()) / "celerybeat-schedule"),
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=2,
    broker_transport_options={
        "visibility_timeout": max(settings.processing_lease_seconds + 60, 180),
        "socket_connect_timeout": 2,
        "socket_timeout": 2,
    },
    enable_utc=True,
    result_backend=None,
    task_acks_late=True,
    task_ignore_result=True,
    task_reject_on_worker_lost=True,
    task_serializer="json",
    task_soft_time_limit=max(90, int(settings.processing_delay_seconds) + 60),
    task_time_limit=max(120, int(settings.processing_delay_seconds) + 90),
    task_track_started=False,
    timezone="UTC",
    worker_hijack_root_logger=False,
    worker_prefetch_multiplier=1,
)


@worker_ready.connect
def trigger_reconcile_on_start(**_: object) -> None:
    try:
        celery_app.send_task(RECONCILE_TRANSACTIONS_TASK, retry=False)
    except Exception:
        logger.exception(
            "worker_start_reconcile_failed",
            extra={"event": "worker_start_reconcile_failed"},
        )
