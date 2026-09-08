"""Request and response schemas."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class PaymentEvent(BaseModel):
    """A webhook event from the payment provider.

    The provider sends an explicit ``type``; direction is never inferred from
    the sign of the amount, and ``amount`` is always positive.
    """

    event_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    amount: int = Field(gt=0)  # minor units: 2500 == $25.00
    currency: str = Field(min_length=3, max_length=3)
    type: Literal["top_up", "payment"]

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @property
    def delta(self) -> int:
        """Signed amount to apply to the balance. The ledger keeps the raw
        positive amount; only the wallet sees a sign."""
        return self.amount if self.type == "top_up" else -self.amount


class SearchFilter(BaseModel):
    """Structured filter the LLM is allowed to produce.

    The model never writes SQL. It fills in this object, Pydantic validates it,
    and we build a parameterised query from the validated fields.
    """

    min_amount: int | None = None  # minor units
    max_amount: int | None = None
    currency: str | None = None
    tx_type: Literal["top_up", "payment"] | None = None
    date_from: date | None = None
    date_to: date | None = None  # inclusive
    understood: bool = True


class Balance(BaseModel):
    currency: str
    balance: int


class Transaction(BaseModel):
    id: int
    event_id: str
    amount: int
    currency: str
    type: str
    created_at: datetime


class WalletResponse(BaseModel):
    user_id: str
    balances: list[Balance]
    transactions: list[Transaction]
    query_understood: bool = True
