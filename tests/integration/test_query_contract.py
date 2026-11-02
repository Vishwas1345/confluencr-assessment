import re

from app.db import SessionLocal
from app.services.transactions import claim_transaction, complete_transaction

TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def payload(transaction_id: str = "txn_contract_1") -> dict[str, object]:
    return {
        "transaction_id": transaction_id,
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": 1500,
        "currency": "INR",
    }


def test_query_returns_exact_public_contract(client, monkeypatch) -> None:
    monkeypatch.setattr("app.api.webhooks.dispatch_transaction", lambda transaction_id: None)
    client.post("/v1/webhooks/transactions", json=payload())

    response = client.get("/v1/transactions/txn_contract_1")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 1
    transaction = body[0]
    assert set(transaction) == {
        "transaction_id",
        "source_account",
        "destination_account",
        "amount",
        "currency",
        "status",
        "created_at",
        "processed_at",
    }
    assert transaction["transaction_id"] == "txn_contract_1"
    assert transaction["status"] == "PROCESSING"
    assert isinstance(transaction["amount"], (int, float))
    assert not isinstance(transaction["amount"], bool)
    assert TIMESTAMP_PATTERN.match(transaction["created_at"])
    assert transaction["processed_at"] is None


def test_query_reflects_processed_status(client, monkeypatch) -> None:
    monkeypatch.setattr("app.api.webhooks.dispatch_transaction", lambda transaction_id: None)
    client.post("/v1/webhooks/transactions", json=payload("txn_contract_processed"))

    with SessionLocal() as session:
        claim = claim_transaction(session, "txn_contract_processed", lease_seconds=120)
        assert claim is not None
        assert complete_transaction(session, "txn_contract_processed", claim.token) is True

    response = client.get("/v1/transactions/txn_contract_processed")

    assert response.status_code == 200
    transaction = response.json()[0]
    assert transaction["status"] == "PROCESSED"
    assert TIMESTAMP_PATTERN.match(transaction["processed_at"])


def test_unknown_transaction_returns_404(client) -> None:
    response = client.get("/v1/transactions/txn_missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "Transaction not found"}
