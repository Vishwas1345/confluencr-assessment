from time import perf_counter
from uuid import uuid4

import pytest

pytestmark = pytest.mark.slow


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    index = round((len(ordered) - 1) * 0.95)
    return ordered[index]


def test_webhook_p95_latency_is_under_500ms(client, monkeypatch) -> None:
    monkeypatch.setattr("app.api.webhooks.dispatch_transaction", lambda transaction_id: None)
    latencies: list[float] = []

    for _ in range(50):
        transaction_id = f"latency_{uuid4().hex}"
        started = perf_counter()
        response = client.post(
            "/v1/webhooks/transactions",
            json={
                "transaction_id": transaction_id,
                "source_account": "acc_user_789",
                "destination_account": "acc_merchant_456",
                "amount": 1500,
                "currency": "INR",
            },
        )
        latencies.append((perf_counter() - started) * 1000)
        assert response.status_code == 202

    assert _p95(latencies) < 500
