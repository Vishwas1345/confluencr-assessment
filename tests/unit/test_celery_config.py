from pathlib import Path

from app.celery_app import celery_app, settings
from app.task_names import RECONCILE_TRANSACTIONS_TASK
from app.tasks import process_transaction, reconcile_transactions


def test_celery_uses_reliable_delivery_settings() -> None:
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert (
        celery_app.conf.broker_transport_options["visibility_timeout"]
        > settings.processing_lease_seconds
    )


def test_processing_task_is_registered() -> None:
    assert process_transaction.name == "transactions.process"
    assert "transactions.process" in celery_app.tasks


def test_reconcile_task_and_beat_are_configured() -> None:
    schedule = celery_app.conf.beat_schedule["reconcile-transactions"]
    assert reconcile_transactions.name == RECONCILE_TRANSACTIONS_TASK
    assert schedule["task"] == RECONCILE_TRANSACTIONS_TASK
    assert schedule["schedule"] == float(settings.reconcile_interval_seconds)
    assert Path(celery_app.conf.beat_schedule_filename).name == "celerybeat-schedule"
