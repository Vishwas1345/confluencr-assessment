import logging
from enum import StrEnum

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models import TransactionStatus
from app.services.transactions import get_transaction, mark_dispatched
from app.task_names import PROCESS_TRANSACTION_TASK

logger = logging.getLogger(__name__)


class DispatchOutcome(StrEnum):
    DISPATCHED = "dispatched"
    SKIPPED = "skipped"
    FAILED = "failed"


def dispatch_transaction(transaction_id: str) -> DispatchOutcome:
    try:
        with SessionLocal() as session:
            transaction = get_transaction(session, transaction_id)
            if transaction is None or transaction.status != TransactionStatus.PROCESSING.value:
                logger.info(
                    "transaction_dispatch_skipped transaction_id=%s reason=not_processing",
                    transaction_id,
                    extra={
                        "event": "transaction_dispatch_skipped",
                        "transaction_id": transaction_id,
                        "reason": "not_processing",
                    },
                )
                return DispatchOutcome.SKIPPED
        celery_app.send_task(
            PROCESS_TRANSACTION_TASK,
            args=[transaction_id],
            retry=False,
        )
    except Exception:
        logger.exception(
            "transaction_dispatch_failed transaction_id=%s",
            transaction_id,
            extra={
                "event": "transaction_dispatch_failed",
                "transaction_id": transaction_id,
            },
        )
        return DispatchOutcome.FAILED

    try:
        with SessionLocal() as session:
            mark_dispatched(session, transaction_id)
    except Exception:
        logger.exception(
            "transaction_dispatch_mark_failed transaction_id=%s",
            transaction_id,
            extra={
                "event": "transaction_dispatch_mark_failed",
                "transaction_id": transaction_id,
            },
        )
        return DispatchOutcome.DISPATCHED

    logger.info(
        "transaction_dispatched transaction_id=%s",
        transaction_id,
        extra={
            "event": "transaction_dispatched",
            "transaction_id": transaction_id,
        },
    )
    return DispatchOutcome.DISPATCHED
