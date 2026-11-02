from datetime import timedelta
from decimal import Decimal

from app.config import get_settings
from app.db import SessionLocal
from app.models import Transaction, TransactionStatus
from app.services import dispatch as dispatch_module
from app.services import reconcile as reconcile_module
from app.services.reconcile import ReconcileResult, reconcile_undispatched
from app.services.transactions import (
    TransactionInput,
    claim_transaction,
    complete_transaction,
    create_if_absent,
    mark_dispatched,
)
from app.tasks import process_transaction_impl
from app.timestamps import utc_now


class FakeCelery:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    def send_task(self, name: str, args=None, retry: bool = True) -> None:
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


def _load(transaction_id: str) -> Transaction:
    with SessionLocal() as session:
        transaction = session.get(Transaction, transaction_id)
        assert transaction is not None
        session.expunge(transaction)
        return transaction


def test_dispatch_failure_returns_202_and_reconcile_recovers(client, monkeypatch) -> None:
    failing = FakeCelery(error=RuntimeError("redis unavailable"))
    monkeypatch.setattr(dispatch_module, "celery_app", failing)

    response = client.post(
        "/v1/webhooks/transactions",
        json={
            "transaction_id": "txn_reconcile_1",
            "source_account": "acc_user_789",
            "destination_account": "acc_merchant_456",
            "amount": 1500,
            "currency": "INR",
        },
    )

    assert response.status_code == 202
    assert _load("txn_reconcile_1").dispatched_at is None

    succeeding = FakeCelery()
    monkeypatch.setattr(dispatch_module, "celery_app", succeeding)
    result = reconcile_undispatched()

    assert result == ReconcileResult(candidates=1, dispatched=1, skipped=0, failed=0)
    assert succeeding.calls == [
        {"name": "transactions.process", "args": ["txn_reconcile_1"], "retry": False}
    ]
    assert _load("txn_reconcile_1").dispatched_at is not None


def test_reconcile_skips_active_lease(monkeypatch) -> None:
    _create("txn_reconcile_2")
    with SessionLocal() as session:
        assert claim_transaction(session, "txn_reconcile_2", lease_seconds=120) is not None
    fake = FakeCelery()
    monkeypatch.setattr(dispatch_module, "celery_app", fake)

    result = reconcile_undispatched()

    assert result == ReconcileResult(candidates=0, dispatched=0, skipped=0, failed=0)
    assert fake.calls == []


def test_reconcile_redispatches_expired_lease(monkeypatch) -> None:
    _create("txn_reconcile_3")
    with SessionLocal() as session:
        assert claim_transaction(session, "txn_reconcile_3", lease_seconds=120) is not None
        transaction = session.get(Transaction, "txn_reconcile_3")
        transaction.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.commit()
    fake = FakeCelery()
    monkeypatch.setattr(dispatch_module, "celery_app", fake)

    result = reconcile_undispatched()

    assert result == ReconcileResult(candidates=1, dispatched=1, skipped=0, failed=0)
    assert fake.calls[0]["args"] == ["txn_reconcile_3"]
    assert process_transaction_impl("txn_reconcile_3")["result"] == "processed"
    assert _load("txn_reconcile_3").status == TransactionStatus.PROCESSED.value


def test_reconcile_never_dispatches_processed_rows(monkeypatch) -> None:
    _create("txn_reconcile_4")
    with SessionLocal() as session:
        claim = claim_transaction(session, "txn_reconcile_4", lease_seconds=120)
        assert claim is not None
        assert complete_transaction(session, "txn_reconcile_4", claim.token) is True
    fake = FakeCelery()
    monkeypatch.setattr(dispatch_module, "celery_app", fake)

    result = reconcile_undispatched()

    assert result == ReconcileResult(candidates=0, dispatched=0, skipped=0, failed=0)
    assert fake.calls == []


def test_reconcile_skips_recently_dispatched_rows(monkeypatch) -> None:
    _create("txn_reconcile_5")
    with SessionLocal() as session:
        assert mark_dispatched(session, "txn_reconcile_5") is True
    fake = FakeCelery()
    monkeypatch.setattr(dispatch_module, "celery_app", fake)

    result = reconcile_undispatched()

    assert result == ReconcileResult(candidates=0, dispatched=0, skipped=0, failed=0)
    assert fake.calls == []


def test_reconcile_redispatches_stale_dispatched_rows(monkeypatch) -> None:
    settings = get_settings()
    _create("txn_reconcile_6")
    with SessionLocal() as session:
        assert mark_dispatched(session, "txn_reconcile_6") is True
        transaction = session.get(Transaction, "txn_reconcile_6")
        transaction.dispatched_at = utc_now() - timedelta(
            seconds=settings.dispatch_stale_seconds + 1
        )
        session.commit()
    fake = FakeCelery()
    monkeypatch.setattr(dispatch_module, "celery_app", fake)

    result = reconcile_undispatched()

    assert result == ReconcileResult(candidates=1, dispatched=1, skipped=0, failed=0)
    assert fake.calls[0]["args"] == ["txn_reconcile_6"]


def test_enqueue_reconcile(monkeypatch) -> None:
    fake = FakeCelery()
    monkeypatch.setattr(reconcile_module, "celery_app", fake)

    assert reconcile_module.enqueue_reconcile() is True
    assert fake.calls == [{"name": "transactions.reconcile", "args": None, "retry": False}]


def test_enqueue_reconcile_failure_is_swallowed(monkeypatch) -> None:
    fake = FakeCelery(error=RuntimeError("redis unavailable"))
    monkeypatch.setattr(reconcile_module, "celery_app", fake)

    assert reconcile_module.enqueue_reconcile() is False
