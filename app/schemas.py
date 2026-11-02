from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.models import TransactionStatus
from app.timestamps import to_iso_z


class TransactionCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    transaction_id: str = Field(min_length=1, max_length=255)
    source_account: str = Field(min_length=1, max_length=255)
    destination_account: str = Field(min_length=1, max_length=255)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    currency: str = Field(min_length=1, max_length=8)


class WebhookAck(BaseModel):
    status: Literal["ACCEPTED"] = "ACCEPTED"
    transaction_id: str


class HealthResponse(BaseModel):
    status: Literal["HEALTHY"] = "HEALTHY"
    current_time: str


class TransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: str
    source_account: str
    destination_account: str
    amount: Decimal
    currency: str
    status: TransactionStatus
    created_at: datetime
    processed_at: datetime | None

    @field_serializer("amount", when_used="json")
    def serialize_amount(self, value: Decimal) -> float:
        return float(value)

    @field_serializer("created_at", "processed_at", when_used="json")
    def serialize_timestamp(self, value: datetime | None) -> str | None:
        return to_iso_z(value)
