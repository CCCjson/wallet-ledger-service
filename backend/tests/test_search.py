"""Filter-to-SQL translation and the wallet endpoint.

These tests never call the LLM. The model's only job is to produce a
SearchFilter; everything downstream of that is deterministic and is what
actually needs testing.
"""

from datetime import date

from app.models import SearchFilter
from app.search import filter_to_sql
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
