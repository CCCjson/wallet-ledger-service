"""Natural language search.

The architectural decision here: **the model never generates SQL.** It only
translates English into a `SearchFilter`, which Pydantic validates, and we
build a parameterised query from the validated fields. A model that emits SQL
is an injection surface, is impossible to test, and fails in ways you cannot
bound. A model that emits a constrained JSON object fails in exactly one way -
it fills in the wrong field - and that is recoverable.

The same principle applies to arithmetic: **the model does not compute, it
names.** For dates it picks a *label* - ``last_7_days``, ``last_month`` - and
``resolve_period`` turns that label into a range. For money it writes down the
figure the user said and ``to_minor_units`` multiplies it out.

Neither was theoretical. Prompted with "the 7 days ending today" the model kept
resolving "last week" to the previous calendar week, dropping today's
transactions. Asked to convert amounts to minor units it kept dropping the
factor of 100 on larger figures - "payments over $5,000" came back as a filter
for $50. Both are deterministic and belong in code, where they are a handful of
plain assertions in the test suite.
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import get_args

from app.config import OPENAI_API_KEY, OPENAI_MODEL, TRANSACTION_TYPES
from app.models import RelativePeriod, SearchFilter

log = logging.getLogger(__name__)

LLM_TIMEOUT_SECONDS = 8.0

RELATIVE_PERIODS: list[str] = list(get_args(RelativePeriod))


def build_filter_json_schema(currencies: list[str]) -> dict:
    """The response schema, built per request so the currency enum reflects
    what this user actually holds.

    Hand-written rather than generated from the Pydantic model so that
    ``additionalProperties: false`` and the required-everything rule that
    OpenAI strict mode demands are explicit and easy to read.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "min_amount_major": {
                "type": ["number", "null"],
                "description": (
                    "Minimum amount, inclusive, in the currency's main unit - "
                    "the figure the user said, not multiplied by anything."
                ),
            },
            "max_amount_major": {
                "type": ["number", "null"],
                "description": "As min_amount_major, inclusive.",
            },
            "currency": {
                "type": ["string", "null"],
                "enum": list(currencies) + [None],
            },
            "tx_type": {
                "type": ["string", "null"],
                "enum": TRANSACTION_TYPES + [None],
            },
            "period": {
                "type": ["string", "null"],
                "enum": RELATIVE_PERIODS + [None],
                "description": (
                    "A relative time window, by name. Use this for anything "
                    "relative - never compute a date yourself."
                ),
            },
            "date_from": {
                "type": ["string", "null"],
                "description": (
                    "Only for a date the user stated literally, as YYYY-MM-DD. "
                    "Leave null whenever period is set."
                ),
            },
            "date_to": {
                "type": ["string", "null"],
                "description": "As date_from, inclusive.",
            },
            "understood": {
                "type": "boolean",
                "description": "False if the query is not a transaction filter at all.",
            },
        },
        "required": [
            "min_amount_major",
            "max_amount_major",
            "currency",
            "tx_type",
            "period",
            "date_from",
            "date_to",
            "understood",
        ],
    }


def build_prompt(currencies: list[str]) -> str:
    return f"""You translate a user's plain-English request into a transaction filter.

Never compute a date. For any relative time expression, set "period" to one of:
  today, yesterday, last_7_days, last_30_days, last_calendar_week,
  this_month, last_month, this_year
"last week", "the past week" and "in the last 7 days" all mean last_7_days.
Use last_calendar_week only when the user clearly means the previous Monday-to-
Sunday week. Use date_from / date_to only for a date the user spelled out
("since 1 March", "between 2025-01-01 and 2025-01-31"), and leave them null
whenever period is set.

Never multiply an amount. Write down the figure the user said, in the currency's
main unit, and nothing else:
  "payments over $50"  -> min_amount_major 50
  "over $5,000"        -> min_amount_major 5000
  "under 10 euros"     -> max_amount_major 10
  "over $12.50"        -> min_amount_major 12.5

Currencies held by this user: {", ".join(currencies)}. Map "dollars"/"$" to USD
and "euros"/"EUR"/"€" to EUR. Leave currency null if the user did not say one,
or named a currency that is not in that list.

Transaction types: "top_up" is money coming in (top up, deposit, added funds),
"payment" is money going out (payment, spend, charge, purchase).

Set every field you cannot determine to null. Do not guess.

Set "understood" to false when the request is not a filter over transactions at
all - random characters, an unrelated question, or something this filter cannot
express. When understood is false leave every other field null."""


def resolve_period(period: str, today: date) -> tuple[date, date]:
    """Turn a relative label into an inclusive ``(date_from, date_to)`` range.

    Pure and deterministic - this is the whole point of taking the arithmetic
    away from the model.
    """
    if period == "today":
        return today, today
    if period == "yesterday":
        d = today - timedelta(days=1)
        return d, d
    if period == "last_7_days":
        # The 7 days ending today, today included.
        return today - timedelta(days=6), today
    if period == "last_30_days":
        return today - timedelta(days=29), today
    if period == "last_calendar_week":
        # Monday of the week before the one containing today, through Sunday.
        this_monday = today - timedelta(days=today.weekday())
        last_monday = this_monday - timedelta(days=7)
        return last_monday, last_monday + timedelta(days=6)
    if period == "this_month":
        return today.replace(day=1), today
    if period == "last_month":
        first_of_this = today.replace(day=1)
        last_of_prev = first_of_this - timedelta(days=1)
        return last_of_prev.replace(day=1), last_of_prev
    if period == "this_year":
        return date(today.year, 1, 1), today
    raise ValueError(f"unknown period {period!r}")  # unreachable: Literal-validated


def to_minor_units(major: float | int | None) -> int | None:
    """Main unit (what the user says, "$50") to minor units (what the ledger
    stores, 5000).

    The same principle as ``resolve_period``, applied to money: the model names
    an amount, it does not multiply one. Asked to do the multiplication it was
    getting it wrong on larger figures - "payments over $5,000" came back as
    ``5000`` often enough to matter, which is $50, a filter a hundred times too
    wide that looks perfectly plausible on screen.

    ``Decimal`` rather than float so that $0.29 lands on 29 rather than on
    28.999999999999996.
    """
    if major is None:
        return None
    return round(Decimal(str(major)) * 100)


def _empty_filter(understood: bool = True) -> SearchFilter:
    return SearchFilter(understood=understood)


async def parse_query(
    query: str, currencies: list[str], today: date | None = None
) -> SearchFilter:
    """Turn a natural-language query into a validated filter.

    Never raises. Anything that goes wrong - no API key, a timeout, a network
    error, a response that does not validate - degrades to "not understood",
    which the caller renders as "here is everything" rather than an error page.
    """
    query = (query or "").strip()
    if not query:
        return _empty_filter()

    if not OPENAI_API_KEY:
        log.warning("no OPENAI_API_KEY configured; search falls back to all rows")
        return _empty_filter(understood=False)

    today = today or datetime.now(timezone.utc).date()

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=LLM_TIMEOUT_SECONDS)
        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": build_prompt(currencies)},
                {"role": "user", "content": query},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "search_filter",
                    "strict": True,
                    "schema": build_filter_json_schema(currencies),
                },
            },
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        # The multiplication the model is not allowed to do. Inside the try, so
        # an amount too large for BIGINT fails validation below and degrades to
        # "not understood" rather than reaching the driver.
        payload["min_amount"] = to_minor_units(payload.pop("min_amount_major", None))
        payload["max_amount"] = to_minor_units(payload.pop("max_amount_major", None))
        parsed = SearchFilter.model_validate(payload)
    except Exception:  # noqa: BLE001 - search must never take down the page
        log.exception("query parsing failed for %r; falling back to all rows", query)
        return _empty_filter(understood=False)

    if not parsed.understood:
        return _empty_filter(understood=False)

    if parsed.period is not None:
        # The label wins over anything the model may also have put in the date
        # fields, and the range it produces is ours, not the model's.
        parsed.date_from, parsed.date_to = resolve_period(parsed.period, today)

    # A filter with nothing in it means the model understood the words but
    # produced no constraint. Treat it as not understood so the user gets the
    # explanatory banner instead of silently seeing everything.
    if not _has_constraint(parsed):
        return _empty_filter(understood=False)

    return parsed


def _has_constraint(f: SearchFilter) -> bool:
    return any(
        v is not None
        for v in (
            f.min_amount,
            f.max_amount,
            f.currency,
            f.tx_type,
            f.date_from,
            f.date_to,
        )
    )


def filter_to_sql(
    search: SearchFilter, params: list | None = None
) -> tuple[list[str], list]:
    """Build parameterised WHERE clauses from a validated filter.

    Values only ever reach the database as bind parameters; nothing from the
    model is ever interpolated into SQL text.
    """
    params = list(params) if params else []
    clauses: list[str] = []

    if not search.understood:
        return clauses, params

    def bind(value) -> str:
        params.append(value)
        return f"${len(params)}"

    if search.min_amount is not None:
        clauses.append(f"amount >= {bind(search.min_amount)}")
    if search.max_amount is not None:
        clauses.append(f"amount <= {bind(search.max_amount)}")
    if search.currency is not None:
        clauses.append(f"currency = {bind(search.currency.upper())}")
    if search.tx_type is not None:
        clauses.append(f"type = {bind(search.tx_type)}")
    if search.date_from is not None:
        clauses.append(f"created_at >= {bind(_start_of_day(search.date_from))}")
    if search.date_to is not None:
        # date_to is inclusive, so compare against the start of the next day.
        clauses.append(f"created_at < {bind(_start_of_day(search.date_to + timedelta(days=1)))}")

    return clauses, params


def _start_of_day(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
