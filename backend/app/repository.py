"""All database access.

Two invariants hold everywhere in this file:

* The ledger stores the raw positive amount exactly as the provider sent it.
  Direction lives in ``type``. Only ``wallets.balance`` carries a sign.
* Balance arithmetic happens inside SQL, never in Python. Reading a balance,
  adding to it in application code and writing it back is a lost-update race:
  two concurrent deliveries read the same old value and one of them is
  silently dropped.
"""

import asyncpg

from app.models import Balance, PaymentEvent, SearchFilter, Transaction
from app.search import filter_to_sql


async def record_event(pool: asyncpg.Pool, event: PaymentEvent) -> str:
    """Append an event to the ledger and move the balance. Returns "ok" for a
    newly recorded event, "duplicate" if this ``event_id`` was already seen.

    The unique constraint on ``event_id`` is what makes this safe against the
    provider delivering the same event twice at the same moment. Two concurrent
    INSERTs serialise on the unique index: the first one wins, the second hits
    the conflict and comes back with no row. The database adjudicates, not us.
    """
    async with pool.acquire() as conn:
        # Ledger insert and balance update share one transaction, so a crash
        # can never leave a recorded event whose money was not applied.
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO transactions (event_id, user_id, amount, currency, type)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING id
                """,
                event.event_id,
                event.user_id,
                event.amount,
                event.currency,
                event.type,
            )

            if row is None:
                # Already applied. Return success so the provider stops retrying.
                return "duplicate"

            await conn.execute(
                """
                INSERT INTO wallets (user_id, currency, balance)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id, currency)
                DO UPDATE SET balance = wallets.balance + EXCLUDED.balance
                """,
                event.user_id,
                event.currency,
                event.delta,
            )

    return "ok"


async def get_balances(pool: asyncpg.Pool, user_id: str) -> list[Balance]:
    """Every currency this user holds. Never affected by a search query."""
    rows = await pool.fetch(
        """
        SELECT currency, balance
        FROM wallets
        WHERE user_id = $1
        ORDER BY currency
        """,
        user_id,
    )
    return [Balance(currency=r["currency"], balance=r["balance"]) for r in rows]


async def list_transactions(
    pool: asyncpg.Pool,
    user_id: str,
    search: SearchFilter | None = None,
    limit: int = 200,
) -> list[Transaction]:
    """Transactions for a user, newest first, optionally narrowed by a filter."""
    clauses = ["user_id = $1"]
    params: list = [user_id]

    if search is not None:
        extra_clauses, params = filter_to_sql(search, params)
        clauses.extend(extra_clauses)

    params.append(limit)
    sql = f"""
        SELECT id, event_id, amount, currency, type, created_at
        FROM transactions
        WHERE {" AND ".join(clauses)}
        ORDER BY created_at DESC, id DESC
        LIMIT ${len(params)}
    """

    rows = await pool.fetch(sql, *params)
    return [Transaction(**dict(r)) for r in rows]
