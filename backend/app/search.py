"""Natural language search.

The architectural decision here: **the model never generates SQL.** It only
translates English into a `SearchFilter`, which Pydantic validates, and we
build a parameterised query from the validated fields. A model that emits SQL
is an injection surface, is impossible to test, and fails in ways you cannot
bound. A model that emits a constrained JSON object fails in exactly one way -
it fills in the wrong field - and that is recoverable.
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone

from app.config import (
    OPENAI_API_KEY,
    OPENAI_MODEL,
    SUPPORTED_CURRENCIES,
    TRANSACTION_TYPES,
)
from app.models import SearchFilter

log = logging.getLogger(__name__)

LLM_TIMEOUT_SECONDS = 8.0

# Hand-written rather than generated from the model so that "additionalProperties
# false" and the required-everything rule that OpenAI strict mode demands are
# explicit and easy to read.
FILTER_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "min_amount": {
            "type": ["integer", "null"],
            "description": "Minimum amount in minor units, inclusive.",
        },
        "max_amount": {
            "type": ["integer", "null"],
            "description": "Maximum amount in minor units, inclusive.",
        },
        "currency": {
            "type": ["string", "null"],
            "enum": SUPPORTED_CURRENCIES + [None],
        },
        "tx_type": {
            "type": ["string", "null"],
            "enum": TRANSACTION_TYPES + [None],
        },
        "date_from": {
            "type": ["string", "null"],
            "description": "Earliest date, inclusive, as YYYY-MM-DD.",
        },
        "date_to": {
            "type": ["string", "null"],
            "description": "Latest date, inclusive, as YYYY-MM-DD.",
        },
        "understood": {
            "type": "boolean",
            "description": "False if the query is not a transaction filter at all.",
        },
    },
    "required": [
        "min_amount",
        "max_amount",
        "currency",
        "tx_type",
        "date_from",
        "date_to",
        "understood",
    ],
}


def build_prompt(today: date) -> str:
    return f"""You translate a user's plain-English request into a transaction filter.

Today's date is {today.isoformat()} (UTC). Resolve every relative date against it.
- "last week" / "the past week" means the 7 days ending today.
- "this month" means the 1st of the current month through today.
- "yesterday" is a single day: date_from and date_to are both that date.

Amounts are integers in MINOR UNITS: 2500 means $25.00, 100 means $1.00.
So "payments over $50" is min_amount 5000, and "under 10 euros" is max_amount 1000.

Available currencies: {", ".join(SUPPORTED_CURRENCIES)}. Map "dollars"/"$" to USD
and "euros"/"EUR"/"€" to EUR. Leave currency null if the user did not say one.

Transaction types: "top_up" is money coming in (top up, deposit, added funds),
"payment" is money going out (payment, spend, charge, purchase).

Set every field you cannot determine to null. Do not guess.

Set "understood" to false when the request is not a filter over transactions at
all - random characters, an unrelated question, or something this filter cannot
express. When understood is false leave every other field null."""


def _empty_filter(understood: bool = True) -> SearchFilter:
    return SearchFilter(understood=understood)


async def parse_query(query: str, today: date | None = None) -> SearchFilter:
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
                {"role": "system", "content": build_prompt(today)},
                {"role": "user", "content": query},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "search_filter",
                    "strict": True,
                    "schema": FILTER_JSON_SCHEMA,
                },
            },
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        parsed = SearchFilter.model_validate(payload)
    except Exception:  # noqa: BLE001 - search must never take down the page
        log.exception("query parsing failed for %r; falling back to all rows", query)
        return _empty_filter(understood=False)

    if not parsed.understood:
        return _empty_filter(understood=False)

    # A filter with nothing in it means the model understood the words but
    # produced no constraint. Treat it as not understood so the user gets the
    # explanatory banner instead of silently seeing everything.
    if not _has_constraint(parsed):
        return _empty_filter(understood=False)

    return parsed


def _has_constraint(f: SearchFilter) -> bool:
    return any(
        v is not None
        for v in (f.min_amount, f.max_amount, f.currency, f.tx_type, f.date_from, f.date_to)
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
