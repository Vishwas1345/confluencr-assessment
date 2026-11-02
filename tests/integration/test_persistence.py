from decimal import Decimal

from app.db import SessionLocal, engine
from app.models import Transaction, TransactionStatus
from app.services.transactions import TransactionInput, create_if_absent


def test_committed_transaction_survives_engine_restart() -> None:
    with SessionLocal() as session:
        create_if_absent(
            session,
            TransactionInput(
                transaction_id="txn_persistent",
                source_account="acc_user_789",
                destination_account="acc_merchant_456",
                amount=Decimal("1500.00"),
                currency="INR",
            ),
        )

    engine.dispose()

    with SessionLocal() as session:
        transaction = session.get(Transaction, "txn_persistent")
        assert transaction is not None
        assert transaction.status == TransactionStatus.PROCESSING.value
        assert transaction.source_account == "acc_user_789"
        assert transaction.amount == Decimal("1500.00")
