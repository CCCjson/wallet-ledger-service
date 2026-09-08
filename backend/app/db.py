"""Connection pool and schema.

The schema is applied on startup with ``CREATE ... IF NOT EXISTS``. For a task
this size a migration tool would be more moving parts than it is worth.
"""

import asyncpg

from app.config import DATABASE_URL

SCHEMA_SQL = """
-- The ledger. This is the source of truth; wallet balances are materialised
-- from it and are therefore redundant by design.
CREATE TABLE IF NOT EXISTS transactions (
  id          BIGSERIAL PRIMARY KEY,
  event_id    TEXT        NOT NULL UNIQUE,             -- the entire idempotency story
  user_id     TEXT        NOT NULL,
  amount      BIGINT      NOT NULL CHECK (amount > 0), -- minor units, always positive
  currency    TEXT        NOT NULL,
  type        TEXT        NOT NULL CHECK (type IN ('top_up', 'payment')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tx_user_created
  ON transactions (user_id, created_at DESC);

-- One balance per (user, currency). Money in different currencies is never
-- added together and never converted.
CREATE TABLE IF NOT EXISTS wallets (
  user_id   TEXT   NOT NULL,
  currency  TEXT   NOT NULL,
  balance   BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, currency)
);
"""


async def create_pool() -> asyncpg.Pool:
    # min_size 10 so that concurrent webhook deliveries genuinely hit the
    # database at the same time instead of queueing on an undersized pool.
    return await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)


async def init_db(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
