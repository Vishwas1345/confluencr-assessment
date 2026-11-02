from decimal import Decimal

from app.db import SessionLocal
from app.models import Transaction
from app.services import dispatch as dispatch_module
from app.services.dispatch import DispatchOutcome
from app.services.transactions import (
    TransactionInput,
    claim_transaction,
    complete_transaction,
    create_if_absent,
)


class FakeCelery:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    def send_task(self, name: str, args: list[str], retry: bool) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append({"name": name, "args": args, "retry": retry})


def _create(transaction_id: str) -> None:
    with SessionLocal() as session:
        create_if_absent(
            session,
            TransactionInput(
                transaction_id=transaction_id,
                source_account="acc_user_789",
                destination_account="acc_merchant_456",
                amount=Decimal("1500.00"),
                currency="INR",
            ),
        )


def test_dispatch_enqueues_task_and_marks_row(monkeypatch) -> None:
    _create("txn_dispatch_1")
    fake = FakeCelery()
    monkeypatch.setattr(dispatch_module, "celery_app", fake)

    outcome = dispatch_module.dispatch_transaction("txn_dispatch_1")

    assert outcome == DispatchOutcome.DISPATCHED
    assert fake.calls == [
        {"name": "transactions.process", "args": ["txn_dispatch_1"], "retry": False}
    ]
    with SessionLocal() as session:
        transaction = session.get(Transaction, "txn_dispatch_1")
        assert transaction is not None
        assert transaction.dispatched_at is not None


def test_dispatch_failure_leaves_row_undispatched(monkeypatch) -> None:
    _create("txn_dispatch_2")
    fake = FakeCelery(error=RuntimeError("redis unavailable"))
    monkeypatch.setattr(dispatch_module, "celery_app", fake)

    outcome = dispatch_module.dispatch_transaction("txn_dispatch_2")

    assert outcome == DispatchOutcome.FAILED
    with SessionLocal() as session:
        transaction = session.get(Transaction, "txn_dispatch_2")
        assert transaction is not None
        assert transaction.dispatched_at is None


def test_dispatch_skips_processed_transaction(monkeypatch) -> None:
    _create("txn_dispatch_3")
    with SessionLocal() as session:
        claim = claim_transaction(session, "txn_dispatch_3", lease_seconds=120)
        assert claim is not None
        assert complete_transaction(session, "txn_dispatch_3", claim.token) is True
    fake = FakeCelery()
    monkeypatch.setattr(dispatch_module, "celery_app", fake)

    outcome = dispatch_module.dispatch_transaction("txn_dispatch_3")

    assert outcome == DispatchOutcome.SKIPPED
    assert fake.calls == []


def test_dispatch_skips_missing_transaction(monkeypatch) -> None:
    fake = FakeCelery()
    monkeypatch.setattr(dispatch_module, "celery_app", fake)

    outcome = dispatch_module.dispatch_transaction("txn_dispatch_missing")

    assert outcome == DispatchOutcome.SKIPPED
    assert fake.calls == []
