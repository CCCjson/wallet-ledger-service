"""Duplicate delivery handling - the core of this task.

The provider says it will sometimes deliver the same event twice at the same
moment, which rules out check-then-insert. Correctness here comes from the
unique constraint on transactions.event_id.
"""

import asyncio

from tests.helpers import event_body, post_event, sign


async def test_same_event_twice_moves_the_balance_once(client, balance_of, pool):
    raw = event_body(event_id="evt_dup", amount=2500)
    signature = sign(raw)

    first = await post_event(client, raw, signature)
    second = await post_event(client, raw, signature)

    assert first.json()["status"] == "ok"
    # A duplicate is a 200: returning an error would make the provider retry.
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"

    assert await balance_of("u_123", "USD") == 2500
    assert await pool.fetchval("SELECT count(*) FROM transactions") == 1


async def test_ten_concurrent_deliveries_of_one_event_apply_once(
    client, balance_of, pool
):
    raw = event_body(event_id="evt_race", amount=2500)
    signature = sign(raw)

    responses = await asyncio.gather(
        *(post_event(client, raw, signature) for _ in range(10))
    )

    statuses = [r.json()["status"] for r in responses]
    assert all(r.status_code == 200 for r in responses)
    assert statuses.count("ok") == 1
    assert statuses.count("duplicate") == 9

    assert await balance_of("u_123", "USD") == 2500
    assert await pool.fetchval("SELECT count(*) FROM transactions") == 1


async def test_concurrent_distinct_events_all_apply(client, balance_of, pool):
    """No lost updates: the balance is summed in SQL, not read-modify-written."""
    bodies = [event_body(event_id=f"evt_{i}", amount=100) for i in range(10)]

    await asyncio.gather(*(post_event(client, b, sign(b)) for b in bodies))

    assert await balance_of("u_123", "USD") == 1000
    assert await pool.fetchval("SELECT count(*) FROM transactions") == 10


async def test_payment_decreases_and_top_up_increases(client, balance_of):
    top_up = event_body(event_id="evt_in", amount=10000, type="top_up")
    payment = event_body(event_id="evt_out", amount=2500, type="payment")

    await post_event(client, top_up, sign(top_up))
    await post_event(client, payment, sign(payment))

    assert await balance_of("u_123", "USD") == 7500


async def test_ledger_stores_the_raw_positive_amount(client, pool):
    """Direction lives in `type`; the ledger never stores a signed amount."""
    payment = event_body(event_id="evt_out", amount=2500, type="payment")
    await post_event(client, payment, sign(payment))

    row = await pool.fetchrow("SELECT amount, type FROM transactions")
    assert row["amount"] == 2500
    assert row["type"] == "payment"


async def test_currencies_are_kept_separate(client, balance_of):
    usd = event_body(event_id="evt_usd", amount=2500, currency="USD")
    eur = event_body(event_id="evt_eur", amount=3000, currency="EUR")

    await post_event(client, usd, sign(usd))
    await post_event(client, eur, sign(eur))

    assert await balance_of("u_123", "USD") == 2500
    assert await balance_of("u_123", "EUR") == 3000


async def test_balance_is_allowed_to_go_negative(client, balance_of):
    """A signed event is a fact that already happened. We record it rather than
    reject it; a rejection would only make the provider retry forever."""
    payment = event_body(event_id="evt_neg", amount=5000, type="payment")
    await post_event(client, payment, sign(payment))

    assert await balance_of("u_123", "USD") == -5000


async def test_same_event_id_different_payload_first_write_wins(client, balance_of, pool):
    first = event_body(event_id="evt_same", amount=2500)
    second = event_body(event_id="evt_same", amount=9999)

    await post_event(client, first, sign(first))
    r = await post_event(client, second, sign(second))

    assert r.json()["status"] == "duplicate"
    assert await pool.fetchval("SELECT amount FROM transactions") == 2500
    assert await balance_of("u_123", "USD") == 2500
