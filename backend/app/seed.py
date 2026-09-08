"""Demo data.

Rows are written straight to the ledger rather than through the webhook,
because the webhook has no way to backdate created_at and the example searches
need history spread over several weeks.

Seeding is idempotent: inserts conflict on event_id and are skipped, and
balances are recomputed from the ledger rather than incremented. Running it
twice changes nothing, which is what makes it safe to call on startup.
"""

import logging
from datetime import datetime, timedelta, timezone

import asyncpg

log = logging.getLogger(__name__)

USER = "u_123"

# (days_ago, amount_minor_units, currency, type)
# Chosen so each example search returns a meaningful subset:
#   "payments over $50"       -> the payment rows above 5000
#   "anything from last week" -> the rows inside 7 days
#   "top ups in euros"        -> the EUR top_up rows
SEED_ROWS = [
    (20, 50000, "USD", "top_up"),    # older than a week, large
    (18, 12000, "EUR", "top_up"),    # older than a week, EUR top up
    (15, 7500, "USD", "payment"),    # older than a week, over $50
    (12, 2200, "USD", "payment"),    # older than a week, under $50
    (9, 30000, "EUR", "top_up"),     # just outside a week, EUR top up
    (6, 25000, "USD", "top_up"),     # within a week
    (5, 8900, "USD", "payment"),     # within a week, over $50
    (4, 1500, "EUR", "payment"),     # within a week, small, EUR
    (2, 6250, "USD", "payment"),     # within a week, over $50
    (1, 4000, "EUR", "top_up"),      # within a week, EUR top up
    (1, 990, "USD", "payment"),      # within a week, under $50
    (0, 15000, "USD", "top_up"),     # today
]

# Balances are a materialised view of the ledger, so they can always be rebuilt
# from it. This is also what a reconciliation job would run.
RECOMPUTE_BALANCES_SQL = """
INSERT INTO wallets (user_id, currency, balance)
SELECT user_id,
       currency,
       SUM(CASE WHEN type = 'top_up' THEN amount ELSE -amount END)
FROM transactions
GROUP BY user_id, currency
ON CONFLICT (user_id, currency)
DO UPDATE SET balance = EXCLUDED.balance
"""


async def seed(pool: asyncpg.Pool) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        (
            f"evt_seed_{i:02d}",
            USER,
            amount,
            currency,
            tx_type,
            now - timedelta(days=days_ago, hours=3),
        )
        for i, (days_ago, amount, currency, tx_type) in enumerate(SEED_ROWS)
    ]

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO transactions
                    (event_id, user_id, amount, currency, type, created_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (event_id) DO NOTHING
                """,
                rows,
            )
            await conn.execute(RECOMPUTE_BALANCES_SQL)


async def summary(pool: asyncpg.Pool) -> str:
    count = await pool.fetchval(
        "SELECT count(*) FROM transactions WHERE user_id = $1", USER
    )
    balances = await pool.fetch(
        "SELECT currency, balance FROM wallets WHERE user_id = $1 ORDER BY currency",
        USER,
    )
    detail = ", ".join(f"{b['currency']} {b['balance']}" for b in balances)
    return f"{count} transactions for {USER} ({detail})"
