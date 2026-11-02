from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionLocal
from app.models import Transaction


def payload(transaction_id: str = "txn_http_1") -> dict[str, object]:
    return {
        "transaction_id": transaction_id,
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": 1500,
        "currency": "INR",
    }


def test_webhook_accepts_and_persists(client, monkeypatch) -> None:
    dispatched: list[str] = []
    monkeypatch.setattr("app.api.webhooks.dispatch_transaction", dispatched.append)

    response = client.post("/v1/webhooks/transactions", json=payload())

    assert response.status_code == 202
    assert response.json() == {
        "status": "ACCEPTED",
        "transaction_id": "txn_http_1",
    }
    assert dispatched == ["txn_http_1"]

    with SessionLocal() as session:
        transaction = session.get(Transaction, "txn_http_1")
        assert transaction is not None
        assert transaction.status == "PROCESSING"
        assert transaction.amount == Decimal("1500.00")
        assert transaction.processed_at is None

    status_response = client.get("/v1/transactions/txn_http_1")
    assert status_response.status_code == 200
    body = status_response.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["status"] == "PROCESSING"
    assert body[0]["processed_at"] is None
    assert body[0]["created_at"].endswith("Z")
    assert body[0]["amount"] == 1500.0


def test_duplicate_webhook_returns_202_without_duplicate_processing(client, monkeypatch) -> None:
    dispatched: list[str] = []
    monkeypatch.setattr("app.api.webhooks.dispatch_transaction", dispatched.append)

    first = client.post("/v1/webhooks/transactions", json=payload("txn_http_dup"))
    first_status = client.get("/v1/transactions/txn_http_dup").json()[0]
    second = client.post("/v1/webhooks/transactions", json=payload("txn_http_dup"))
    third = client.post("/v1/webhooks/transactions", json=payload("txn_http_dup"))
    final_status = client.get("/v1/transactions/txn_http_dup").json()[0]

    assert first.status_code == 202
    assert second.status_code == 202
    assert third.status_code == 202
    assert dispatched == ["txn_http_dup"]
    assert final_status["created_at"] == first_status["created_at"]
    assert final_status["processed_at"] is None

    with SessionLocal() as session:
        count = session.scalar(select(func.count()).select_from(Transaction))
        assert count == 1


def test_concurrent_duplicate_webhooks_are_idempotent(client, monkeypatch) -> None:
    dispatched: list[str] = []
    monkeypatch.setattr("app.api.webhooks.dispatch_transaction", dispatched.append)

    with ThreadPoolExecutor(max_workers=5) as executor:
        responses = list(
            executor.map(
                lambda _: client.post(
                    "/v1/webhooks/transactions",
                    json=payload("txn_http_concurrent"),
                ),
                range(5),
            )
        )

    assert [response.status_code for response in responses] == [202] * 5
    assert dispatched == ["txn_http_concurrent"]

    with SessionLocal() as session:
        count = session.scalar(select(func.count()).select_from(Transaction))
        assert count == 1
        transaction = session.get(Transaction, "txn_http_concurrent")
        assert transaction is not None
        assert transaction.status == "PROCESSING"


@pytest.mark.parametrize(
    "invalid_payload",
    [
        {
            "source_account": "acc_user_789",
            "destination_account": "acc_merchant_456",
            "amount": 1500,
            "currency": "INR",
        },
        payload() | {"amount": 0},
        payload() | {"amount": -5},
        payload() | {"transaction_id": "   "},
        payload() | {"currency": ""},
    ],
)
def test_invalid_webhook_returns_422_and_creates_no_row(client, invalid_payload) -> None:
    response = client.post("/v1/webhooks/transactions", json=invalid_payload)

    assert response.status_code == 422

    with SessionLocal() as session:
        count = session.scalar(select(func.count()).select_from(Transaction))
        assert count == 0


def test_database_failure_returns_503(client, monkeypatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr("app.api.webhooks.create_if_absent", fail)

    response = client.post("/v1/webhooks/transactions", json=payload("txn_db_fail"))

    assert response.status_code == 503
    assert response.json() == {"detail": "Unable to accept transaction"}
