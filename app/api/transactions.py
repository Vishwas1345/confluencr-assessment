from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import TransactionResponse
from app.services.transactions import get_transaction

router = APIRouter(prefix="/v1/transactions", tags=["transactions"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/{transaction_id}", response_model=list[TransactionResponse])
def read_transaction(transaction_id: str, db: DbSession) -> list[TransactionResponse]:
    transaction = get_transaction(db, transaction_id)
    if transaction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found",
        )
    return [TransactionResponse.model_validate(transaction)]
