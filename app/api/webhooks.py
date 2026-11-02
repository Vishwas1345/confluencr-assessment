import logging
from time import perf_counter
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import TransactionCreate, WebhookAck
from app.services.dispatch import dispatch_transaction
from app.services.transactions import TransactionInput, create_if_absent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])
DbSession = Annotated[Session, Depends(get_db)]


@router.post(
    "/transactions",
    response_model=WebhookAck,
    status_code=status.HTTP_202_ACCEPTED,
)
def ingest_transaction(
    payload: TransactionCreate,
    background_tasks: BackgroundTasks,
    db: DbSession,
) -> WebhookAck:
    started = perf_counter()
    transaction = TransactionInput(
        transaction_id=payload.transaction_id,
        source_account=payload.source_account,
        destination_account=payload.destination_account,
        amount=payload.amount,
        currency=payload.currency,
    )

    try:
        inserted = create_if_absent(db, transaction)
    except SQLAlchemyError:
        logger.exception(
            "webhook_persist_failed transaction_id=%s",
            payload.transaction_id,
            extra={
                "event": "webhook_persist_failed",
                "transaction_id": payload.transaction_id,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to accept transaction",
        ) from None

    if inserted:
        background_tasks.add_task(dispatch_transaction, payload.transaction_id)

    duration_ms = (perf_counter() - started) * 1000
    event = "webhook_accepted" if inserted else "webhook_duplicate"
    logger.info(
        "%s transaction_id=%s duration_ms=%.2f",
        event,
        payload.transaction_id,
        duration_ms,
        extra={
            "event": event,
            "transaction_id": payload.transaction_id,
            "duration_ms": round(duration_ms, 2),
        },
    )
    return WebhookAck(transaction_id=payload.transaction_id)
