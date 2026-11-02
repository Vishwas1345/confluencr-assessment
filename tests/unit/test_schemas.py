from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas import TransactionCreate


def valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "transaction_id": "txn_schema_1",
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": 1500,
        "currency": "INR",
    }
    payload.update(overrides)
    return payload


def test_valid_payload_parses_decimal_amount() -> None:
    transaction = TransactionCreate.model_validate(valid_payload(amount=Decimal("150.50")))

    assert transaction.amount == Decimal("150.50")
    assert transaction.currency == "INR"


@pytest.mark.parametrize(
    "payload",
    [
        valid_payload(transaction_id=""),
        valid_payload(transaction_id="   "),
        valid_payload(amount=0),
        valid_payload(amount=-1),
        valid_payload(currency=""),
        valid_payload(amount="not-a-number"),
    ],
)
def test_invalid_payloads_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TransactionCreate.model_validate(payload)


def test_missing_required_field_is_rejected() -> None:
    payload = valid_payload()
    del payload["transaction_id"]

    with pytest.raises(ValidationError):
        TransactionCreate.model_validate(payload)
