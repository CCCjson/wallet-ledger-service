"""Request and response schemas."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# transactions.amount is BIGINT. Anything above this is not a large payment,
# it is a malformed event: the driver would raise OverflowError at bind time
# and the endpoint would answer 500, which tells the provider to retry an
# event that can never succeed. Bounding it here turns that into a 422 the
# provider will stop resending.
MAX_MINOR_UNITS = 9_223_372_036_854_775_807  # 2**63 - 1

# Relative windows the search model may ask for by name. It never computes a
# date itself; Python resolves these against "today". See search.resolve_period.
RelativePeriod = Literal[
    "today",
    "yesterday",
    "last_7_days",
    "last_30_days",
    "last_calendar_week",
    "this_month",
    "last_month",
    "this_year",
]


class PaymentEvent(BaseModel):
    """A webhook event from the payment provider.

    The provider sends an explicit ``type``; direction is never inferred from
    the sign of the amount, and ``amount`` is always positive.
    """

    event_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    amount: int = Field(gt=0, le=MAX_MINOR_UNITS)  # minor units: 2500 == $25.00
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

    It also never does arithmetic. Relative windows arrive as a ``period``
    label and are turned into dates by Python; ``date_from`` / ``date_to`` are
    reserved for dates the user stated literally. Amounts arrive in the
    currency's main unit and are multiplied out by ``search.to_minor_units``,
    so by the time they land here they are already minor units.

    The amount bounds are the same ``BIGINT`` ceiling ``PaymentEvent`` uses.
    Without them a query naming an absurd figure produced an ``int`` too large
    for the column, asyncpg raised at bind time, and an unauthenticated GET
    answered 500. Bounding here routes it into the existing "not understood"
    fallback instead, which returns the full list.
    """

    min_amount: int | None = Field(default=None, ge=0, le=MAX_MINOR_UNITS)
    max_amount: int | None = Field(default=None, ge=0, le=MAX_MINOR_UNITS)
    currency: str | None = None
    tx_type: Literal["top_up", "payment"] | None = None
    period: RelativePeriod | None = None
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
