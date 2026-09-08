"""Seed the database with demo transactions.

Rows are written straight to the ledger rather than through the webhook,
because the webhook has no way to backdate created_at and the example searches
need history spread over several weeks.

Safe to run repeatedly: inserts are ON CONFLICT DO NOTHING on event_id, and
balances are recomputed from the ledger rather than incremented.
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import create_pool, init_db  # noqa: E402

USER = "u_123"

# (days_ago, amount_minor_units, currency, type)
# Chosen so the three example searches each return a meaningful subset:
#   "payments over $50"    -> the payment rows above 5000
#   "anything from last week" -> the rows inside 7 days
#   "top ups in euros"     -> the EUR top_up rows
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


async def main() -> None:
    pool = await create_pool()
    await init_db(pool)

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

            # Rebuild balances from the ledger. The ledger is the source of
            # truth; wallets is a materialised view of it maintained for reads.
            # This is also what a reconciliation job would run.
            await conn.execute(
                """
                INSERT INTO wallets (user_id, currency, balance)
                SELECT user_id,
                       currency,
                       SUM(CASE WHEN type = 'top_up' THEN amount ELSE -amount END)
                FROM transactions
                GROUP BY user_id, currency
                ON CONFLICT (user_id, currency)
                DO UPDATE SET balance = EXCLUDED.balance
                """
            )

    balances = await pool.fetch(
        "SELECT currency, balance FROM wallets WHERE user_id = $1 ORDER BY currency",
        USER,
    )
    count = await pool.fetchval(
        "SELECT count(*) FROM transactions WHERE user_id = $1", USER
    )
    print(f"seeded {count} transactions for {USER}")
    for b in balances:
        print(f"  {b['currency']}: {b['balance']}")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
