"""Filter-to-SQL translation and the wallet endpoint.

These tests never call the LLM. The model's only job is to produce a
SearchFilter; everything downstream of that is deterministic and is what
actually needs testing.
"""

from datetime import date

from app.models import SearchFilter
from app.search import (
    RELATIVE_PERIODS,
    build_filter_json_schema,
    build_prompt,
    filter_to_sql,
    resolve_period,
)
from tests.helpers import event_body, post_event, sign


def test_empty_filter_produces_no_clauses():
    clauses, params = filter_to_sql(SearchFilter())
    assert clauses == []
    assert params == []


def test_not_understood_filter_produces_no_clauses():
    """An unparseable query returns everything rather than nothing."""
    clauses, params = filter_to_sql(
        SearchFilter(understood=False, min_amount=5000, currency="USD")
    )
    assert clauses == []
    assert params == []


def test_amount_and_currency_bind_in_order():
    clauses, params = filter_to_sql(
        SearchFilter(min_amount=5000, currency="usd", tx_type="payment")
    )
    assert clauses == ["amount >= $1", "currency = $2", "type = $3"]
    assert params == [5000, "USD", "payment"]


def test_placeholders_continue_from_existing_params():
    """The wallet query already binds user_id as $1."""
    clauses, params = filter_to_sql(SearchFilter(max_amount=1000), params=["u_123"])
    assert clauses == ["amount <= $2"]
    assert params == ["u_123", 1000]


def test_date_to_is_inclusive():
    clauses, params = filter_to_sql(
        SearchFilter(date_from=date(2026, 9, 1), date_to=date(2026, 9, 8))
    )
    assert clauses == ["created_at >= $1", "created_at < $2"]
    # date_to is inclusive, so the upper bound is the start of the next day.
    assert params[0].isoformat() == "2026-09-01T00:00:00+00:00"
    assert params[1].isoformat() == "2026-09-09T00:00:00+00:00"


def test_values_are_never_interpolated_into_sql_text():
    clauses, params = filter_to_sql(SearchFilter(currency="USD"))
    assert "USD" not in " ".join(clauses)
    assert params == ["USD"]


async def test_wallet_endpoint_returns_balances_and_transactions(client):
    for i, (amount, currency) in enumerate([(2500, "USD"), (3000, "EUR")]):
        raw = event_body(event_id=f"evt_{i}", amount=amount, currency=currency)
        await post_event(client, raw, sign(raw))

    r = await client.get("/wallets/u_123")
    body = r.json()

    assert r.status_code == 200
    assert body["balances"] == [
        {"currency": "EUR", "balance": 3000},
        {"currency": "USD", "balance": 2500},
    ]
    assert len(body["transactions"]) == 2
    assert body["query_understood"] is True


async def test_unknown_user_returns_empty_not_404(client):
    r = await client.get("/wallets/u_does_not_exist")
    assert r.status_code == 200
    assert r.json()["balances"] == []
    assert r.json()["transactions"] == []


async def test_transactions_are_newest_first(client, pool):
    for i in range(3):
        raw = event_body(event_id=f"evt_{i}")
        await post_event(client, raw, sign(raw))
    await pool.execute(
        "UPDATE transactions SET created_at = now() - (id || ' days')::interval"
    )

    r = await client.get("/wallets/u_123")
    ids = [t["id"] for t in r.json()["transactions"]]
    assert ids == sorted(ids)  # id 1 is the most recent after the shuffle above


# --- Date resolution ---------------------------------------------------------
# The model no longer computes dates; it names a window and Python resolves it.
# That arithmetic is deterministic, so it can simply be tested.


def test_last_7_days_includes_today():
    """The bug this replaced: the model resolved "last week" to the previous
    calendar week and silently dropped today's transactions."""
    assert resolve_period("last_7_days", date(2026, 9, 8)) == (
        date(2026, 9, 2),
        date(2026, 9, 8),
    )


def test_today_and_yesterday_are_single_days():
    assert resolve_period("today", date(2026, 9, 8)) == (date(2026, 9, 8),) * 2
    assert resolve_period("yesterday", date(2026, 9, 8)) == (date(2026, 9, 7),) * 2


def test_last_calendar_week_is_monday_to_sunday():
    # 2026-09-08 is a Tuesday, so its week began Mon the 7th and the week
    # before that runs Mon 31 Aug to Sun 6 Sep.
    assert resolve_period("last_calendar_week", date(2026, 9, 8)) == (
        date(2026, 8, 31),
        date(2026, 9, 6),
    )


def test_last_calendar_week_from_a_monday_looks_back_a_full_week():
    assert resolve_period("last_calendar_week", date(2026, 9, 7)) == (
        date(2026, 8, 31),
        date(2026, 9, 6),
    )


def test_this_month_runs_from_the_first_to_today():
    assert resolve_period("this_month", date(2026, 9, 8)) == (
        date(2026, 9, 1),
        date(2026, 9, 8),
    )


def test_last_month_crosses_a_year_boundary():
    assert resolve_period("last_month", date(2026, 1, 15)) == (
        date(2025, 12, 1),
        date(2025, 12, 31),
    )


def test_last_month_from_a_short_month():
    assert resolve_period("last_month", date(2026, 3, 1)) == (
        date(2026, 2, 1),
        date(2026, 2, 28),
    )


def test_last_30_days_and_this_year():
    assert resolve_period("last_30_days", date(2026, 9, 8)) == (
        date(2026, 8, 10),
        date(2026, 9, 8),
    )
    assert resolve_period("this_year", date(2026, 9, 8)) == (
        date(2026, 1, 1),
        date(2026, 9, 8),
    )


def test_every_declared_period_resolves():
    """No label can be offered to the model that Python cannot resolve."""
    for period in RELATIVE_PERIODS:
        start, end = resolve_period(period, date(2026, 9, 8))
        assert start <= end


# --- Prompt construction -----------------------------------------------------


def test_currency_enum_comes_from_what_the_user_holds():
    schema = build_filter_json_schema(["GBP", "USD"])
    assert schema["properties"]["currency"]["enum"] == ["GBP", "USD", None]
    assert "GBP" in build_prompt(["GBP", "USD"])


def test_schema_requires_every_property_for_strict_mode():
    schema = build_filter_json_schema(["USD"])
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


# --- Query length ------------------------------------------------------------


async def test_overlong_query_is_rejected_before_it_reaches_the_model(client):
    r = await client.get("/wallets/u_123", params={"q": "a" * 201})
    assert r.status_code == 422


async def test_query_at_the_limit_is_accepted(client):
    """No API key is configured in tests, so this falls through to "everything",
    which is exactly the degradation the endpoint promises."""
    r = await client.get("/wallets/u_123", params={"q": "a" * 200})
    assert r.status_code == 200
    assert r.json()["query_understood"] is False
